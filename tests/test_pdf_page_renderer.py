import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image

from src.database.initialize import initialize_database
from src.processing.pdf_page_renderer import (
    PageRenderError,
    claim_render_job,
    render_import_job,
)
from src.processing.question_crop import generate_question_crops


class PdfPageRendererTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_root = self.root / "private"
        self.database_path = self.root / "question-bank.db"
        initialize_database(self.database_path).close()

    def tearDown(self):
        self.temporary.cleanup()

    def create_job(
        self, page_count=3, page_start=2, page_end=3, *, name="synthetic",
        size=(72, 144), private_root=None,
    ):
        private_root = Path(private_root) if private_root is not None else self.private_root
        document = pymupdf.open()
        for number in range(1, page_count + 1):
            page = document.new_page(width=size[0], height=size[1])
            page.insert_text((8, 24), f"{name} page {number}")
        content = document.tobytes()
        document.close()
        relative_path = Path(f"raw_papers/TJ/2026/{name}.pdf")
        source = private_root / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        with sqlite3.connect(self.database_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256, file_size, original_filename, stored_path,
                    region_code, exam_year, exam_type_code, paper_name)
                   VALUES (?, ?, ?, ?, 'TJ', 2026, 'YK', '合成试卷')""",
                (digest, len(content), f"{name}.pdf", relative_path.as_posix()),
            ).lastrowid
            job_id = connection.execute(
                """INSERT INTO import_jobs
                   (source_paper_id, page_start, page_end, status)
                   VALUES (?, ?, ?, 'pending')""",
                (source_id, page_start, page_end),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id, status, dpi) VALUES (?, 'processing', 300)""",
                (job_id,),
            )
        return job_id, digest

    @staticmethod
    def tree_snapshot(root):
        root = Path(root)
        if root.is_file():
            return {".": root.read_bytes()}
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes() if path.is_file() else b"<directory>"
            )
            for path in root.rglob("*")
        }

    def test_renders_confirmed_range_at_300_dpi_with_verified_manifest(self):
        job_id, source_digest = self.create_job()

        manifest = render_import_job(
            self.database_path, self.private_root, job_id
        )

        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        saved = json.loads(
            (job_dir / "render_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest, saved)
        self.assertEqual(1, manifest["version"])
        self.assertEqual(job_id, manifest["import_job_id"])
        self.assertEqual(300, manifest["dpi"])
        self.assertEqual(source_digest, manifest["source_pdf_sha256"])
        self.assertEqual(3, manifest["source_page_count"])
        self.assertEqual((2, 3), (manifest["page_start"], manifest["page_end"]))
        self.assertEqual(2, manifest["page_count"])
        self.assertEqual([2, 3], [page["page_number"] for page in manifest["pages"]])
        for entry in manifest["pages"]:
            self.assertEqual(
                f"pages/page_{entry['page_number']:03d}.png",
                entry["relative_path"],
            )
            path = job_dir / entry["relative_path"]
            with Image.open(path) as image:
                image.load()
                self.assertEqual((300, 600), image.size)
                self.assertEqual("PNG", image.format)
            self.assertEqual((300, 600), (entry["pixel_width"], entry["pixel_height"]))
            self.assertEqual(path.stat().st_size, entry["byte_size"])
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"]
            )
        with sqlite3.connect(self.database_path) as connection:
            run = connection.execute(
                """SELECT status, dpi, total_pages, rendered_pages,
                          error_message, completed_at
                   FROM import_page_render_runs WHERE import_job_id = ?""",
                (job_id,),
            ).fetchone()
        self.assertEqual(("completed", 300, 2, 2), run[:4])
        self.assertIsNone(run[4])
        self.assertIsNotNone(run[5])

    def test_rejects_legacy_stored_path_containing_dot_dot_without_output(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        original = self.private_root / "raw_papers/TJ/2026/synthetic.pdf"
        unsafe = original.with_name("synthetic..pdf")
        original.rename(unsafe)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                """UPDATE source_papers SET stored_path=?
                   WHERE id=(SELECT source_paper_id FROM import_jobs WHERE id=?)""",
                ("raw_papers/TJ/2026/synthetic..pdf", job_id),
            )

        with self.assertRaisesRegex(ValueError, "归档 PDF 校验失败"):
            render_import_job(self.database_path, self.private_root, job_id)

        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        self.assertFalse((job_dir / "pages").exists())
        self.assertFalse((job_dir / "render_manifest.json").exists())
        with sqlite3.connect(self.database_path) as connection:
            status, message = connection.execute(
                """SELECT status, error_message FROM import_page_render_runs
                   WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        self.assertEqual("failed", status)
        self.assertEqual("归档 PDF 校验失败，请重新导入后重试", message)
        self.assertNotIn(str(self.root), message)

    def test_rejects_out_of_bounds_range_missing_and_tampered_archives(self):
        scenarios = ("range", "missing", "tampered")
        for index, scenario in enumerate(scenarios, start=1):
            with self.subTest(scenario=scenario):
                job_id, _ = self.create_job(
                    page_count=1, page_start=1, page_end=2 if scenario == "range" else 1,
                    name=f"invalid-{index}",
                )
                source = self.private_root / f"raw_papers/TJ/2026/invalid-{index}.pdf"
                if scenario == "missing":
                    source.unlink()
                elif scenario == "tampered":
                    source.write_bytes(source.read_bytes() + b"tampered")
                expected = "确认页码范围无效" if scenario == "range" else "归档 PDF 校验失败"
                with self.assertRaisesRegex(PageRenderError, expected):
                    render_import_job(self.database_path, self.private_root, job_id)
                job_dir = self.private_root / "processing" / f"import_job_{job_id}"
                self.assertFalse((job_dir / "pages").exists())
                self.assertFalse((job_dir / "render_manifest.json").exists())

    def test_rejects_absolute_backslash_traversal_directory_and_symlink_paths(self):
        cases = ("absolute", "backslash", "traversal", "directory", "symlink")
        for index, case in enumerate(cases, start=1):
            with self.subTest(case=case):
                name = f"path-{index}"
                job_id, _ = self.create_job(
                    page_count=1, page_start=1, page_end=1, name=name
                )
                source = self.private_root / f"raw_papers/TJ/2026/{name}.pdf"
                stored_path = source.relative_to(self.private_root).as_posix()
                if case == "absolute":
                    stored_path = str(source)
                elif case == "backslash":
                    stored_path = stored_path.replace("/", "\\")
                elif case == "traversal":
                    stored_path = "raw_papers/TJ/../2026/path.pdf"
                elif case == "directory":
                    source.unlink()
                    source.mkdir()
                elif case == "symlink":
                    external = self.root / f"external-{index}.pdf"
                    external.write_bytes(source.read_bytes())
                    source.unlink()
                    source.symlink_to(external)
                if case in {"absolute", "backslash", "traversal"}:
                    with sqlite3.connect(self.database_path) as connection:
                        connection.execute("PRAGMA ignore_check_constraints = ON")
                        connection.execute(
                            """UPDATE source_papers SET stored_path=?
                               WHERE id=(SELECT source_paper_id FROM import_jobs WHERE id=?)""",
                            (stored_path, job_id),
                        )
                with self.assertRaisesRegex(PageRenderError, "归档 PDF 校验失败"):
                    render_import_job(self.database_path, self.private_root, job_id)
                job_dir = self.private_root / "processing" / f"import_job_{job_id}"
                self.assertFalse((job_dir / "pages").exists())

    def test_rejects_more_than_200_pages_and_oversized_pixel_page(self):
        page_job, _ = self.create_job(
            page_count=201, page_start=None, page_end=None, name="many", size=(10, 10)
        )
        pixel_job, _ = self.create_job(
            page_count=1, page_start=1, page_end=1, name="huge", size=(2000, 2000)
        )
        for job_id in (page_job, pixel_job):
            with self.subTest(job_id=job_id):
                with self.assertRaisesRegex(PageRenderError, "超过安全处理限制"):
                    render_import_job(self.database_path, self.private_root, job_id)
                job_dir = self.private_root / "processing" / f"import_job_{job_id}"
                self.assertFalse((job_dir / "pages").exists())

    def test_rejects_total_pixel_and_output_byte_budgets(self):
        total_pixel_job, _ = self.create_job(
            page_count=12,
            page_start=1,
            page_end=12,
            name="total-pixels",
            size=(1440, 1440),
        )
        from src.processing import pdf_page_renderer as renderer

        with self.assertRaisesRegex(PageRenderError, "超过安全处理限制"):
            render_import_job(self.database_path, self.private_root, total_pixel_job)

        byte_job, _ = self.create_job(
            page_count=1, page_start=1, page_end=1, name="output-bytes"
        )
        with patch.object(renderer, "MAX_RENDER_OUTPUT_BYTES", 100):
            with self.assertRaisesRegex(PageRenderError, "超过安全处理限制"):
                render_import_job(self.database_path, self.private_root, byte_job)

        for job_id in (total_pixel_job, byte_job):
            job_dir = self.private_root / "processing" / f"import_job_{job_id}"
            self.assertFalse((job_dir / "pages").exists())
            self.assertFalse((job_dir / "render_manifest.json").exists())

    def test_mid_render_unexpected_failure_is_safe_and_preserves_old_result(self):
        job_id, _ = self.create_job(page_count=2, page_start=1, page_end=2)
        render_import_job(self.database_path, self.private_root, job_id)
        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        old_files = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*")
            if path.is_file()
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET status='processing', rendered_pages=0, completed_at=NULL
                   WHERE import_job_id=?""",
                (job_id,),
            )

        from src.processing import pdf_page_renderer as renderer

        real_verify = renderer._verify_png
        calls = 0

        def fail_second(path, width, height):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError(f"sensitive path: {self.root}")
            return real_verify(path, width, height)

        with patch.object(renderer, "_verify_png", side_effect=fail_second):
            with self.assertRaisesRegex(PageRenderError, "页面处理失败，请重试"):
                render_import_job(self.database_path, self.private_root, job_id)

        current_files = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(old_files, current_files)
        self.assertEqual([], list(job_dir.glob(".render_batch.*")))
        with sqlite3.connect(self.database_path) as connection:
            status, message = connection.execute(
                """SELECT status, error_message FROM import_page_render_runs
                   WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        self.assertEqual(("failed", "页面处理失败，请重试"), (status, message))
        self.assertNotIn(str(self.root), message)

    def test_completed_valid_result_is_idempotent_without_mtime_changes(self):
        job_id, _ = self.create_job(page_count=2, page_start=1, page_end=2)
        first = render_import_job(self.database_path, self.private_root, job_id)
        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        mtimes = {
            path.relative_to(job_dir).as_posix(): path.stat().st_mtime_ns
            for path in job_dir.rglob("*")
            if path.is_file()
        }

        second = render_import_job(self.database_path, self.private_root, job_id)

        self.assertEqual(first, second)
        self.assertEqual(
            mtimes,
            {
                path.relative_to(job_dir).as_posix(): path.stat().st_mtime_ns
                for path in job_dir.rglob("*")
                if path.is_file()
            },
        )
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    """SELECT status FROM import_page_render_runs
                       WHERE import_job_id=?""",
                    (job_id,),
                ).fetchone()[0],
            )

    def test_corrupt_completed_png_or_manifest_requires_explicit_retry_and_rebuilds(self):
        for index, target in enumerate(("png", "manifest"), start=1):
            with self.subTest(target=target):
                job_id, _ = self.create_job(
                    page_count=1, page_start=1, page_end=1, name=f"corrupt-{index}"
                )
                render_import_job(self.database_path, self.private_root, job_id)
                job_dir = self.private_root / "processing" / f"import_job_{job_id}"
                if target == "png":
                    corrupt_path = job_dir / "pages/page_001.png"
                    corrupt_path.write_bytes(b"not a png")
                else:
                    corrupt_path = job_dir / "render_manifest.json"
                    corrupt_path.write_text("{}", encoding="utf-8")

                with self.assertRaisesRegex(PageRenderError, "现有页面结果校验失败"):
                    render_import_job(self.database_path, self.private_root, job_id)
                with sqlite3.connect(self.database_path) as connection:
                    self.assertEqual(
                        "failed",
                        connection.execute(
                            "SELECT status FROM import_page_render_runs WHERE import_job_id=?",
                            (job_id,),
                        ).fetchone()[0],
                    )
                    connection.execute(
                        """UPDATE import_page_render_runs
                           SET status='processing', rendered_pages=0, error_message=NULL
                           WHERE import_job_id=? AND status='failed'""",
                        (job_id,),
                    )

                rebuilt = render_import_job(self.database_path, self.private_root, job_id)
                self.assertEqual(1, rebuilt["page_count"])
                with Image.open(job_dir / "pages/page_001.png") as image:
                    image.verify()

    def test_progress_database_failure_cleans_temporary_output(self):
        job_id, _ = self.create_job(page_count=2, page_start=1, page_end=2)
        from src.processing import pdf_page_renderer as renderer

        with patch.object(
            renderer, "_set_progress", side_effect=sqlite3.OperationalError("private db")
        ):
            with self.assertRaisesRegex(PageRenderError, "页面处理失败，请重试"):
                render_import_job(self.database_path, self.private_root, job_id)

        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        self.assertFalse((job_dir / "pages").exists())
        self.assertFalse((job_dir / "render_manifest.json").exists())
        self.assertEqual([], list(job_dir.glob(".render_batch.*")))

    def test_publish_replace_failure_rolls_back_old_complete_result(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        render_import_job(self.database_path, self.private_root, job_id)
        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        old_files = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*")
            if path.is_file()
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET status='processing', rendered_pages=0, completed_at=NULL
                   WHERE import_job_id=?""",
                (job_id,),
            )
        from src.processing import pdf_page_renderer as renderer

        real_replace = renderer.os.replace

        def fail_new_pages(source, destination):
            source_path = Path(source)
            if source_path.name == "pages" and source_path.parent.name.startswith(
                ".render_batch."
            ):
                raise OSError("publish failure")
            return real_replace(source, destination)

        with patch.object(renderer.os, "replace", side_effect=fail_new_pages):
            with self.assertRaisesRegex(PageRenderError, "页面处理失败，请重试"):
                render_import_job(self.database_path, self.private_root, job_id)

        self.assertEqual(
            old_files,
            {
                path.relative_to(job_dir).as_posix(): path.read_bytes()
                for path in job_dir.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual([], list(job_dir.glob(".*backup*")))

    def test_concurrent_claim_has_one_worker_and_stale_processing_is_recoverable(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "DELETE FROM import_page_render_runs WHERE import_job_id=?", (job_id,)
            )
        barrier = threading.Barrier(2)
        claims = []

        def claim():
            barrier.wait(timeout=5)
            claims.append(
                claim_render_job(self.database_path, self.private_root, job_id)
            )

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        active = [claim for claim in claims if claim is not None]
        self.assertEqual(2, len(claims))
        self.assertEqual(1, len(active))
        self.assertIsNone(
            claim_render_job(self.database_path, self.private_root, job_id)
        )
        active[0].close()

        recovered = claim_render_job(self.database_path, self.private_root, job_id)
        self.assertIsNotNone(recovered)
        recovered.close()
        with sqlite3.connect(self.database_path) as connection:
            run = connection.execute(
                """SELECT status, dpi, rendered_pages
                   FROM import_page_render_runs WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        self.assertEqual(("processing", 300, 0), run)

    def test_claim_rejects_symlinked_output_roots_and_lock_files(self):
        for index, case in enumerate(
            ("private_root", "processing", "lock_dir", "lock_file"), start=1
        ):
            with self.subTest(case=case):
                actual_root = self.root / f"safe-root-{index}"
                configured_root = actual_root
                job_id, _ = self.create_job(
                    page_count=1, page_start=1, page_end=1,
                    name=f"lock-link-{index}", private_root=actual_root,
                )
                external = self.root / f"external-lock-{index}"
                external.mkdir()
                (external / "sentinel").write_bytes(b"unchanged")
                if case == "private_root":
                    configured_root = self.root / f"configured-root-{index}"
                    configured_root.symlink_to(actual_root, target_is_directory=True)
                else:
                    processing = actual_root / "processing"
                    if case == "processing":
                        processing.symlink_to(external, target_is_directory=True)
                    else:
                        processing.mkdir()
                        lock_dir = processing / ".render_locks"
                        if case == "lock_dir":
                            lock_dir.symlink_to(external, target_is_directory=True)
                        else:
                            lock_dir.mkdir()
                            (lock_dir / f"import_job_{job_id}.lock").symlink_to(
                                external / "sentinel"
                            )
                before = self.tree_snapshot(external)

                with self.assertRaisesRegex(PageRenderError, "暂时无法启动"):
                    claim_render_job(self.database_path, configured_root, job_id)

                self.assertEqual(before, self.tree_snapshot(external))
                with sqlite3.connect(self.database_path) as connection:
                    status, message = connection.execute(
                        """SELECT status, error_message FROM import_page_render_runs
                           WHERE import_job_id=?""",
                        (job_id,),
                    ).fetchone()
                self.assertEqual(("failed", "页面处理失败，请重试"), (status, message))

    def test_renderer_rejects_symlinked_job_pages_and_manifest(self):
        for index, case in enumerate(("job_dir", "pages", "manifest"), start=1):
            with self.subTest(case=case):
                private_root = self.root / f"render-root-{index}"
                job_id, _ = self.create_job(
                    page_count=1, page_start=1, page_end=1,
                    name=f"output-link-{index}", private_root=private_root,
                )
                processing = private_root / "processing"
                processing.mkdir()
                job_dir = processing / f"import_job_{job_id}"
                external = self.root / f"external-render-{index}"
                external.mkdir()
                (external / "sentinel").write_bytes(b"unchanged")
                if case == "job_dir":
                    job_dir.symlink_to(external, target_is_directory=True)
                else:
                    job_dir.mkdir()
                    if case == "pages":
                        (job_dir / "pages").symlink_to(
                            external, target_is_directory=True
                        )
                    else:
                        (job_dir / "render_manifest.json").symlink_to(
                            external / "sentinel"
                        )
                before = self.tree_snapshot(external)

                with self.assertRaisesRegex(PageRenderError, "页面处理失败"):
                    render_import_job(self.database_path, private_root, job_id)

                self.assertEqual(before, self.tree_snapshot(external))
                with sqlite3.connect(self.database_path) as connection:
                    status, message = connection.execute(
                        """SELECT status, error_message FROM import_page_render_runs
                           WHERE import_job_id=?""",
                        (job_id,),
                    ).fetchone()
                self.assertEqual(("failed", "页面处理失败，请重试"), (status, message))

    def test_database_completion_failure_rolls_back_old_complete_result(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        render_import_job(self.database_path, self.private_root, job_id)
        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        old_png = job_dir / "pages/page_001.png"
        Image.new("RGB", (300, 600), (12, 34, 56)).save(old_png, "PNG")
        old_manifest_path = job_dir / "render_manifest.json"
        old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
        old_content = old_png.read_bytes()
        old_manifest["pages"][0]["byte_size"] = len(old_content)
        old_manifest["pages"][0]["sha256"] = hashlib.sha256(old_content).hexdigest()
        old_manifest_path.write_text(
            json.dumps(old_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*") if path.is_file()
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET status='processing', rendered_pages=0, completed_at=NULL
                   WHERE import_job_id=?""",
                (job_id,),
            )
        from src.processing import pdf_page_renderer as renderer

        with patch.object(
            renderer, "_mark_completed", side_effect=sqlite3.OperationalError("db")
        ):
            with self.assertRaisesRegex(PageRenderError, "页面处理失败，请重试"):
                render_import_job(self.database_path, self.private_root, job_id)

        self.assertEqual(
            before,
            {
                path.relative_to(job_dir).as_posix(): path.read_bytes()
                for path in job_dir.rglob("*") if path.is_file()
            },
        )
        self.assertEqual([], list(job_dir.glob(".*backup*")))

    def test_database_completion_failure_without_old_result_removes_formal_output(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        from src.processing import pdf_page_renderer as renderer

        with patch.object(
            renderer, "_mark_completed", side_effect=sqlite3.OperationalError("db")
        ):
            with self.assertRaisesRegex(PageRenderError, "页面处理失败，请重试"):
                render_import_job(self.database_path, self.private_root, job_id)

        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        self.assertFalse((job_dir / "pages").exists())
        self.assertFalse((job_dir / "render_manifest.json").exists())
        self.assertEqual([], list(job_dir.glob(".*backup*")))

    def test_finalize_cleanup_failure_keeps_completed_new_result(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        from src.processing import pdf_page_renderer as renderer

        with patch.object(
            renderer, "_finalize_publish", side_effect=OSError(f"private {self.root}")
        ):
            manifest = render_import_job(self.database_path, self.private_root, job_id)

        self.assertEqual(1, manifest["page_count"])
        job_dir = self.private_root / "processing" / f"import_job_{job_id}"
        self.assertTrue((job_dir / "pages/page_001.png").is_file())
        with sqlite3.connect(self.database_path) as connection:
            status, message = connection.execute(
                """SELECT status, error_message FROM import_page_render_runs
                   WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        self.assertEqual(("completed", None), (status, message))

    def test_renderer_manifest_drives_existing_question_crop_pipeline(self):
        job_id, _ = self.create_job(page_count=2, page_start=1, page_end=2)
        render_import_job(self.database_path, self.private_root, job_id)
        job_dir = self.private_root / "processing" / f"import_job_{job_id}"

        crops = generate_question_crops(
            job_dir=job_dir,
            questions=[
                {
                    "question_no": 1,
                    "regions": [{"page_number": 1, "bbox": [0, 0, 120, 100]}],
                },
                {
                    "question_no": 2,
                    "regions": [{"page_number": 2, "bbox": [20, 40, 180, 180]}],
                },
            ],
            expected_question_nos=[1, 2],
            min_width=20,
            min_height=20,
        )

        self.assertEqual(job_id, crops["import_job_id"])
        self.assertEqual(2, crops["question_count"])
        for entry in crops["questions"]:
            output = job_dir / entry["output_relative_path"]
            with Image.open(output) as image:
                image.verify()
            self.assertEqual(output.stat().st_size, entry["byte_size"])
            self.assertEqual(
                hashlib.sha256(output.read_bytes()).hexdigest(), entry["sha256"]
            )

    def test_completed_validation_rejects_extra_directory_and_nonregular_page_item(self):
        for index, extra_kind in enumerate(("directory", "fifo"), start=1):
            with self.subTest(extra_kind=extra_kind):
                job_id, _ = self.create_job(
                    page_count=1, page_start=1, page_end=1,
                    name=f"extra-item-{index}",
                )
                render_import_job(self.database_path, self.private_root, job_id)
                pages = (
                    self.private_root / "processing" / f"import_job_{job_id}" / "pages"
                )
                extra = pages / f"unexpected-{index}"
                if extra_kind == "directory":
                    extra.mkdir()
                else:
                    os.mkfifo(extra)

                with self.assertRaisesRegex(PageRenderError, "现有页面结果校验失败"):
                    render_import_job(self.database_path, self.private_root, job_id)

    def test_completed_validation_reads_manifest_and_png_from_nofollow_descriptors(self):
        job_id, _ = self.create_job(page_count=1, page_start=1, page_end=1)
        expected = render_import_job(self.database_path, self.private_root, job_id)
        from src.processing import pdf_page_renderer as renderer

        real_image_open = renderer.Image.open

        def open_bytes_only(value, *args, **kwargs):
            if not isinstance(value, io.BytesIO):
                raise AssertionError("Pillow must receive verified in-memory bytes")
            return real_image_open(value, *args, **kwargs)

        with (
            patch.object(Path, "read_text", side_effect=AssertionError("unsafe read_text")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("unsafe read_bytes")),
            patch.object(renderer.Image, "open", side_effect=open_bytes_only),
        ):
            actual = render_import_job(self.database_path, self.private_root, job_id)

        self.assertEqual(expected, actual)

    def test_renderer_rejects_symlink_private_root_before_reading_source(self):
        actual_root = self.root / "actual-private"
        job_id, _ = self.create_job(
            page_count=1, page_start=1, page_end=1,
            name="linked-private", private_root=actual_root,
        )
        configured_root = self.root / "linked-private-root"
        configured_root.symlink_to(actual_root, target_is_directory=True)

        with self.assertRaisesRegex(PageRenderError, "归档 PDF 校验失败"):
            render_import_job(self.database_path, configured_root, job_id)

        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                ("failed", "归档 PDF 校验失败，请重新导入后重试"),
                connection.execute(
                    """SELECT status, error_message FROM import_page_render_runs
                       WHERE import_job_id=?""",
                    (job_id,),
                ).fetchone(),
            )


if __name__ == "__main__":
    unittest.main()
