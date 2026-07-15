import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from src.database.initialize import initialize_database
from src.processing.pdf_page_renderer import render_import_job


class PageLayoutAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "question-bank.db"

    def tearDown(self):
        self.temporary.cleanup()

    def create_rendered_job(self, columns=2):
        private_root = self.root / "private"
        document = pymupdf.open()
        page = document.new_page(width=360, height=480)
        margin, gutter = 24, 24
        column_width = (360 - 2 * margin - (columns - 1) * gutter) / columns
        for column in range(columns):
            left = margin + column * (column_width + gutter)
            for top in range(42, 440, 22):
                page.draw_rect(
                    pymupdf.Rect(left, top, left + column_width, top + 5),
                    color=(0, 0, 0), fill=(0, 0, 0),
                )
            page.insert_text((left + 2, 34), f"{column + 1}. main question")
        content = document.tobytes()
        document.close()
        relative = Path("raw_papers/TJ/2026/layout.pdf")
        source = private_root / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        initialize_database(self.database_path).close()
        with sqlite3.connect(self.database_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,?, 'layout.pdf',?,'TJ',2026,'YK','合成版面')""",
                (digest, len(content), relative.as_posix()),
            ).lastrowid
            job_id = connection.execute(
                """INSERT INTO import_jobs
                   (source_paper_id,page_start,page_end,status)
                   VALUES (?,1,1,'pending')""",
                (source_id,),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,rendered_pages)
                   VALUES (?,'processing',300,0)""",
                (job_id,),
            )
        render_import_job(self.database_path, private_root, job_id)
        return private_root, job_id

    def create_custom_rendered_job(self, page_specs, name="custom"):
        private_root = self.root / "private"
        document = pymupdf.open()
        for spec in page_specs:
            columns = spec["columns"]
            page = document.new_page(width=360, height=480)
            margin, gutter = 24, 24
            column_width = (360 - 2 * margin - (columns - 1) * gutter) / columns
            for column in range(columns):
                left = margin + column * (column_width + gutter)
                for top in range(42, 440, 22):
                    page.draw_rect(
                        pymupdf.Rect(left, top, left + column_width, top + 5),
                        color=(0, 0, 0), fill=(0, 0, 0),
                    )
            for column, y, text in spec.get("texts", []):
                left = margin + column * (column_width + gutter)
                page.insert_text((left + 2, y), text)
        content = document.tobytes()
        document.close()
        relative = Path(f"raw_papers/TJ/2026/{name}.pdf")
        source = private_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        initialize_database(self.database_path).close()
        with sqlite3.connect(self.database_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,?,?,?,'TJ',2026,'YK','自定义合成版面')""",
                (hashlib.sha256(content).hexdigest(), len(content), f"{name}.pdf", relative.as_posix()),
            ).lastrowid
            job_id = connection.execute(
                """INSERT INTO import_jobs
                   (source_paper_id,page_start,page_end,status)
                   VALUES (?,1,?,'pending')""", (source_id, len(page_specs))
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,rendered_pages)
                   VALUES (?,'processing',300,0)""", (job_id,)
            )
        render_import_job(self.database_path, private_root, job_id)
        return private_root, job_id

    def test_schema_creates_constrained_layout_analysis_run(self):
        initialize_database(self.database_path).close()

        with sqlite3.connect(self.database_path) as connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='import_layout_analysis_runs'"
            ).fetchone()
            self.assertIsNotNone(sql)
            self.assertIn("detected_questions", sql[0])
            for column in (
                "manifest_sha256",
                "manifest_byte_size",
                "published_batch_id",
                "source_pdf_sha256",
                "render_manifest_sha256",
            ):
                self.assertIn(column, sql[0])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO import_layout_analysis_runs "
                    "(import_job_id,status,analyzed_pages,detected_questions) "
                    "VALUES (999,'invented',0,0)"
                )

    def test_real_render_detects_two_columns_and_embedded_main_question_anchors(self):
        private_root, job_id = self.create_rendered_job(columns=2)
        from src.processing.page_layout_analyzer import analyze_page_layout

        manifest = analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(2, manifest["pages"][0]["column_count"])
        self.assertEqual(["1", "2"], [q["question_no"] for q in manifest["questions"]])
        self.assertEqual(2, manifest["question_count"])
        for question in manifest["questions"]:
            self.assertTrue(question["regions"])
            for region in question["regions"]:
                x0, y0, x1, y1 = region["bbox"]
                self.assertLess(x0, x1)
                self.assertLess(y0, y1)
        saved = json.loads(
            (private_root / "processing" / f"import_job_{job_id}" /
             "layout_result" / "layout_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest, saved)

    def test_completed_loader_rejects_structurally_valid_manifest_rewrite(self):
        private_root, job_id = self.create_rendered_job()
        from src.processing.page_layout_analyzer import (
            PageLayoutError,
            analyze_page_layout,
            load_completed_layout,
        )

        analyze_page_layout(self.database_path, private_root, job_id)
        path = (
            private_root / "processing" / f"import_job_{job_id}" /
            "layout_result" / "layout_manifest.json"
        )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["questions"][0]["question_no"] = "999"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(PageLayoutError):
            load_completed_layout(self.database_path, private_root, job_id)

    def test_completed_loader_rejects_replaced_overlay_with_updated_internal_hash(self):
        private_root, job_id = self.create_rendered_job()
        from src.processing.page_layout_analyzer import (
            PageLayoutError,
            analyze_page_layout,
            load_completed_layout,
        )

        analyze_page_layout(self.database_path, private_root, job_id)
        result = (
            private_root / "processing" / f"import_job_{job_id}" / "layout_result"
        )
        manifest_path = result / "layout_manifest.json"
        overlay_path = result / "overlays/page_001.png"
        replacement = overlay_path.read_bytes() + b"tampered"
        overlay_path.write_bytes(replacement)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pages"][0]["overlay"]["byte_size"] = len(replacement)
        manifest["pages"][0]["overlay"]["sha256"] = hashlib.sha256(
            replacement
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(PageLayoutError):
            load_completed_layout(self.database_path, private_root, job_id)

    def test_completed_db_counts_and_upstream_digests_cross_check_manifest(self):
        private_root, job_id = self.create_rendered_job()
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        cases = {
            "total_pages": 2,
            "analyzed_pages": 0,
            "detected_questions": 99,
            "source_pdf_sha256": "0" * 64,
            "render_manifest_sha256": "0" * 64,
        }
        with sqlite3.connect(self.database_path) as connection:
            original = connection.execute(
                """SELECT total_pages,analyzed_pages,detected_questions,
                          source_pdf_sha256,render_manifest_sha256
                   FROM import_layout_analysis_runs WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        for index, (column, bad_value) in enumerate(cases.items()):
            with self.subTest(column=column):
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f"UPDATE import_layout_analysis_runs SET {column}=? "
                        "WHERE import_job_id=?",
                        (bad_value, job_id),
                    )
                with self.assertRaises(analyzer.PageLayoutError):
                    analyzer.load_completed_layout(
                        self.database_path, private_root, job_id
                    )
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute(
                        f"UPDATE import_layout_analysis_runs SET {column}=? "
                        "WHERE import_job_id=?",
                        (original[index], job_id),
                    )

    def test_overlay_direct_read_verifies_only_target_upstream_and_overlay_once(self):
        private_root, job_id = self.create_custom_rendered_job(
            [
                {"columns": 1, "texts": [(0, 34, "1. first")]},
                {"columns": 1, "texts": [(0, 34, "2. second")]},
            ],
            name="overlay-o1",
        )
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        reads = []
        verifies = []
        real_read = analyzer._read_regular_at
        real_verify = analyzer._verify_png
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        page_dir_inode = (job_dir / "pages").stat().st_ino
        overlay_dir_inode = (job_dir / "layout_result" / "overlays").stat().st_ino

        def recording_read(parent_fd, name, **kwargs):
            reads.append((os.fstat(parent_fd).st_ino, name))
            return real_read(parent_fd, name, **kwargs)

        def recording_verify(content, width, height):
            verifies.append((width, height))
            return real_verify(content, width, height)

        with (
            patch.object(analyzer, "_read_regular_at", side_effect=recording_read),
            patch.object(analyzer, "_verify_png", side_effect=recording_verify),
        ):
            content = analyzer.read_layout_overlay(
                self.database_path, private_root, job_id, 2
            )

        self.assertTrue(content.startswith(b"\x89PNG"))
        self.assertEqual(1, reads.count((page_dir_inode, "page_002.png")))
        self.assertEqual(1, reads.count((overlay_dir_inode, "page_002.png")))
        self.assertFalse(any(name == "page_001.png" for _, name in reads))
        self.assertEqual(2, len(verifies))

    def test_layout_output_budget_includes_manifest_and_remaining_disk_budget(self):
        from src.processing import page_layout_analyzer as analyzer

        with patch.object(analyzer, "MAX_LAYOUT_OUTPUT_BYTES", 10):
            analyzer._check_output_budget(7, 3)
            with self.assertRaisesRegex(analyzer.PageLayoutError, "超过安全处理限制"):
                analyzer._check_output_budget(7, 4)
            with patch.object(
                analyzer, "_available_bytes", return_value=analyzer.MIN_FREE_BYTES + 10
            ):
                analyzer._check_remaining_disk(123, 0)
            with patch.object(
                analyzer, "_available_bytes", return_value=analyzer.MIN_FREE_BYTES + 9
            ):
                with self.assertRaisesRegex(
                    analyzer.PageLayoutError, "超过安全处理限制"
                ):
                    analyzer._check_remaining_disk(123, 0)

    def test_generation_disk_reservation_tracks_bytes_already_written(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        with patch.object(
            analyzer, "_check_remaining_disk", wraps=analyzer._check_remaining_disk
        ) as check_disk:
            manifest = analyzer.analyze_page_layout(
                self.database_path, private_root, job_id
            )

        overlay_bytes = manifest["pages"][0]["overlay"]["byte_size"]
        committed = [call.args[1] for call in check_disk.call_args_list]
        self.assertEqual([0, 0, overlay_bytes], committed)

    def test_claim_recovers_all_three_atomic_publication_crash_boundaries(self):
        from src.processing import page_layout_analyzer as analyzer

        for boundary in ("old_moved", "new_installed", "db_completed"):
            with self.subTest(boundary=boundary):
                private_root, job_id = self.create_custom_rendered_job(
                    [{"columns": 1, "texts": [(0, 34, "1. old")]}],
                    name=f"recover-{boundary}",
                )
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )
                job_dir = private_root / "processing" / f"import_job_{job_id}"
                formal = job_dir / "layout_result"
                trusted = {
                    path.relative_to(formal).as_posix(): path.read_bytes()
                    for path in formal.rglob("*")
                    if path.is_file()
                }
                backup = job_dir / f".layout_backup.{'a' * 32}"
                batch = job_dir / f".layout_batch.{'b' * 32}"
                if boundary == "db_completed":
                    shutil.copytree(formal, backup)
                    batch.mkdir()
                else:
                    formal.rename(backup)
                    if boundary == "new_installed":
                        shutil.copytree(backup, formal)
                        manifest_path = formal / "layout_manifest.json"
                        manifest_path.write_bytes(
                            manifest_path.read_bytes() + b" "
                        )
                    batch.mkdir()
                    with sqlite3.connect(self.database_path) as connection:
                        connection.execute(
                            "UPDATE import_layout_analysis_runs SET status='failed' "
                            "WHERE import_job_id=?",
                            (job_id,),
                        )

                claim = analyzer.claim_layout_job(
                    self.database_path, private_root, job_id
                )
                self.assertIsNotNone(claim)
                claim.close()
                self.assertEqual(
                    trusted,
                    {
                        path.relative_to(formal).as_posix(): path.read_bytes()
                        for path in formal.rglob("*")
                        if path.is_file()
                    },
                )
                self.assertEqual([], list(job_dir.glob(".layout_batch.*")))
                self.assertEqual([], list(job_dir.glob(".layout_backup.*")))

    def test_failed_install_and_two_failed_rollbacks_recover_on_next_claim(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        formal = job_dir / "layout_result"
        trusted = {
            path.relative_to(formal).as_posix(): path.read_bytes()
            for path in formal.rglob("*")
            if path.is_file()
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?",
                (job_id,),
            )
        real_replace = analyzer.os.replace
        calls = 0

        def fail_install_and_rollbacks(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls in {2, 3, 4}:
                raise OSError("synthetic transient rename failure")
            return real_replace(*args, **kwargs)

        with patch.object(
            analyzer.os, "replace", side_effect=fail_install_and_rollbacks
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )

        self.assertFalse(formal.exists())
        self.assertEqual(1, len(list(job_dir.glob(".layout_backup.*"))))
        claim = analyzer.claim_layout_job(
            self.database_path, private_root, job_id
        )
        self.assertIsNotNone(claim)
        claim.close()
        self.assertEqual(
            trusted,
            {
                path.relative_to(formal).as_posix(): path.read_bytes()
                for path in formal.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual([], list(job_dir.glob(".layout_backup.*")))

    def test_precommit_completion_failure_is_reconciled_on_next_claim(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        formal = job_dir / "layout_result"
        trusted = {
            path.relative_to(formal).as_posix(): path.read_bytes()
            for path in formal.rglob("*")
            if path.is_file()
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?",
                (job_id,),
            )
        with patch.object(
            analyzer,
            "_complete_run",
            side_effect=analyzer.PageLayoutError(analyzer.SAFE_ANALYSIS_ERROR),
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )

        self.assertNotEqual(
            trusted,
            {
                path.relative_to(formal).as_posix(): path.read_bytes()
                for path in formal.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual(1, len(list(job_dir.glob(".layout_backup.*"))))
        claim = analyzer.claim_layout_job(
            self.database_path, private_root, job_id
        )
        self.assertIsNotNone(claim)
        claim.close()
        self.assertEqual(
            trusted,
            {
                path.relative_to(formal).as_posix(): path.read_bytes()
                for path in formal.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual([], list(job_dir.glob(".layout_backup.*")))

    def test_post_completion_attachment_failure_never_rolls_back_new_formal(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        formal = job_dir / "layout_result"
        old_manifest = (formal / "layout_manifest.json").read_bytes()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?",
                (job_id,),
            )
        real_complete = analyzer._complete_run
        committed = False
        real_assert = analyzer._LayoutWorkspace.assert_attached

        def complete_then_arm(*args, **kwargs):
            nonlocal committed
            result = real_complete(*args, **kwargs)
            committed = True
            return result

        def fail_after_commit(workspace):
            if committed:
                raise OSError("synthetic post-completion attachment failure")
            return real_assert(workspace)

        with (
            patch.object(analyzer, "_complete_run", side_effect=complete_then_arm),
            patch.object(
                analyzer._LayoutWorkspace,
                "assert_attached",
                autospec=True,
                side_effect=fail_after_commit,
            ),
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )

        new_manifest = (formal / "layout_manifest.json").read_bytes()
        self.assertNotEqual(old_manifest, new_manifest)
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT manifest_sha256,manifest_byte_size
                   FROM import_layout_analysis_runs WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        self.assertEqual(hashlib.sha256(new_manifest).hexdigest(), row[0])
        self.assertEqual(len(new_manifest), row[1])
        self.assertEqual(1, len(list(job_dir.glob(".layout_backup.*"))))

    def test_complete_commits_then_raises_preserves_new_formal_for_recovery(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        formal = job_dir / "layout_result"
        old_manifest = (formal / "layout_manifest.json").read_bytes()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?",
                (job_id,),
            )
        real_complete = analyzer._complete_run

        def commit_then_raise(*args, **kwargs):
            real_complete(*args, **kwargs)
            raise OSError("synthetic exception after DB commit")

        with patch.object(
            analyzer, "_complete_run", side_effect=commit_then_raise
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )

        new_manifest = (formal / "layout_manifest.json").read_bytes()
        self.assertNotEqual(old_manifest, new_manifest)
        self.assertEqual(1, len(list(job_dir.glob(".layout_backup.*"))))
        with sqlite3.connect(self.database_path) as connection:
            digest = connection.execute(
                "SELECT manifest_sha256 FROM import_layout_analysis_runs "
                "WHERE import_job_id=?",
                (job_id,),
            ).fetchone()[0]
        self.assertEqual(hashlib.sha256(new_manifest).hexdigest(), digest)

        claim = analyzer.claim_layout_job(
            self.database_path, private_root, job_id
        )
        self.assertIsNotNone(claim)
        claim.close()
        self.assertEqual([], list(job_dir.glob(".layout_backup.*")))
        self.assertEqual(
            digest,
            hashlib.sha256(
                (formal / "layout_manifest.json").read_bytes()
            ).hexdigest(),
        )

    def test_recovery_preserves_old_backup_when_anchored_formal_overlay_is_damaged(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?",
                (job_id,),
            )
        with patch.object(
            analyzer, "_finalize", side_effect=OSError("synthetic finalize failure")
        ):
            analyzer.analyze_page_layout(
                self.database_path, private_root, job_id
            )
        formal = job_dir / "layout_result"
        backup = next(job_dir.glob(".layout_backup.*"))
        overlay = formal / "overlays/page_001.png"
        overlay.write_bytes(b"damaged")
        formal_snapshot = {
            path.relative_to(formal).as_posix(): path.read_bytes()
            for path in formal.rglob("*")
            if path.is_file()
        }
        backup_snapshot = {
            path.relative_to(backup).as_posix(): path.read_bytes()
            for path in backup.rglob("*")
            if path.is_file()
        }

        with self.assertRaisesRegex(
            analyzer.PageLayoutError, "版面分析任务暂时无法启动"
        ):
            analyzer.claim_layout_job(
                self.database_path, private_root, job_id
            )

        self.assertEqual(
            formal_snapshot,
            {
                path.relative_to(formal).as_posix(): path.read_bytes()
                for path in formal.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual(
            backup_snapshot,
            {
                path.relative_to(backup).as_posix(): path.read_bytes()
                for path in backup.rglob("*")
                if path.is_file()
            },
        )

    def test_real_render_distinguishes_single_and_three_column_pages(self):
        from src.processing.page_layout_analyzer import analyze_page_layout

        for columns in (1, 3):
            with self.subTest(columns=columns):
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory()
                self.root = Path(self.temporary.name)
                self.database_path = self.root / "question-bank.db"
                private_root, job_id = self.create_rendered_job(columns=columns)

                manifest = analyze_page_layout(
                    self.database_path, private_root, job_id
                )

                self.assertEqual(columns, manifest["pages"][0]["column_count"])

    def test_completed_valid_layout_is_idempotent_without_mtime_changes(self):
        private_root, job_id = self.create_rendered_job(columns=2)
        from src.processing.page_layout_analyzer import analyze_page_layout

        first = analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        mtimes = {
            path.relative_to(job_dir).as_posix(): path.stat().st_mtime_ns
            for path in (job_dir / "layout_result" / "overlays").iterdir()
        }
        mtimes["layout_manifest.json"] = (
            job_dir / "layout_result" / "layout_manifest.json"
        ).stat().st_mtime_ns

        second = analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(first, second)
        self.assertEqual(
            mtimes,
            {
                **{
                    path.relative_to(job_dir).as_posix(): path.stat().st_mtime_ns
                    for path in (job_dir / "layout_result" / "overlays").iterdir()
                },
                "layout_manifest.json": (job_dir / "layout_result" / "layout_manifest.json").stat().st_mtime_ns,
            },
        )

    def test_claim_is_exclusive_and_stale_processing_can_be_recovered(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing.page_layout_analyzer import claim_layout_job

        first = claim_layout_job(self.database_path, private_root, job_id)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(
                claim_layout_job(self.database_path, private_root, job_id)
            )
            with sqlite3.connect(self.database_path) as connection:
                self.assertEqual(
                    "processing",
                    connection.execute(
                        "SELECT status FROM import_layout_analysis_runs "
                        "WHERE import_job_id=?", (job_id,)
                    ).fetchone()[0],
                )
        finally:
            first.close()

        recovered = claim_layout_job(self.database_path, private_root, job_id)
        self.assertIsNotNone(recovered)
        recovered.close()

    def test_cross_column_and_page_regions_preserve_numbers_and_warn_on_sequence(self):
        private_root, job_id = self.create_custom_rendered_job(
            [
                {"columns": 2, "texts": [(0, 200, "1. first"), (0, 260, "(1) subpart")]},
                {"columns": 2, "texts": [(1, 240, "3. third")]},
            ],
            name="cross-page",
        )
        from src.processing.page_layout_analyzer import (
            analyze_page_layout,
            load_completed_layout,
        )

        manifest = analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(["1", "3"], [q["question_no"] for q in manifest["questions"]])
        self.assertGreaterEqual(len(manifest["questions"][0]["regions"]), 4)
        self.assertEqual("medium", manifest["questions"][1]["confidence"])
        self.assertTrue(any("跳号" in warning for warning in manifest["questions"][1]["warnings"]))
        self.assertEqual(
            manifest,
            load_completed_layout(self.database_path, private_root, job_id),
        )

    def test_embedded_number_prefix_without_space_is_detected_but_decimal_is_not(self):
        private_root, job_id = self.create_custom_rendered_job(
            [{"columns": 1, "texts": [
                (0, 70, "1.first"),
                (0, 120, "2.5 decimal"),
                (0, 170, "2.second"),
            ]}],
            name="joined-number-prefix",
        )
        from src.processing.page_layout_analyzer import analyze_page_layout

        manifest = analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(
            ["1", "2"],
            [question["question_no"] for question in manifest["questions"]],
        )

    def test_no_text_layer_detects_columns_but_never_guesses_questions(self):
        private_root, job_id = self.create_custom_rendered_job(
            [{"columns": 3}], name="no-text"
        )
        from src.processing.page_layout_analyzer import analyze_page_layout

        manifest = analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(3, manifest["pages"][0]["column_count"])
        self.assertFalse(manifest["pages"][0]["text_layer_available"])
        self.assertEqual([], manifest["questions"])
        self.assertTrue(any("无可用文本层" in warning for warning in manifest["warnings"]))

    def test_rejects_symlink_layout_output_entry_without_external_write(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing.page_layout_analyzer import (
            PageLayoutError,
            analyze_page_layout,
        )
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        external = self.root / "external-overlays"
        external.mkdir()
        (job_dir / "layout_result").symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(PageLayoutError, "版面分析失败"):
            analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual([], list(external.iterdir()))

    def test_low_disk_space_fails_safely_and_preserves_old_complete_result(self):
        private_root, job_id = self.create_rendered_job(columns=2)
        from src.processing import page_layout_analyzer as analyzer

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        old = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*") if path.is_file()
            and ("layout_" in path.name or "layout_result" in path.parts)
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?", (job_id,)
            )

        with patch.object(analyzer, "_available_bytes", return_value=analyzer.MIN_FREE_BYTES):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "超过安全处理限制"):
                analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(
            old,
            {
                path.relative_to(job_dir).as_posix(): path.read_bytes()
                for path in job_dir.rglob("*") if path.is_file()
                and ("layout_" in path.name or "layout_result" in path.parts)
            },
        )
        self.assertEqual([], list(job_dir.glob(".layout_batch.*")))

    def test_more_than_max_questions_fails_instead_of_silent_truncation(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        anchors = [
            {
                "question_no": str(index + 1),
                "page_number": 1,
                "column_index": 0,
                "x": 100,
                "y": 180 + index * 5,
            }
            for index in range(analyzer.MAX_QUESTIONS + 1)
        ]

        with patch.object(analyzer, "_anchors_for_page", return_value=(True, anchors)):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "超过安全处理限制"):
                analyzer.analyze_page_layout(self.database_path, private_root, job_id)

    def test_total_analysis_pixels_are_rejected_before_source_page_decode(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer

        with (
            patch.object(analyzer, "MAX_TOTAL_ANALYSIS_PIXELS", 1),
            patch.object(analyzer, "_verify_png", wraps=analyzer._verify_png) as decode,
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "超过安全处理限制"):
                analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        decode.assert_not_called()

    def test_completed_loader_does_not_convert_all_source_pages_to_rgb(self):
        private_root, job_id = self.create_custom_rendered_job(
            [{"columns": 1, "texts": [(0, 34, "1. one")]},
             {"columns": 1, "texts": [(0, 34, "2. two")]}],
            name="bounded-loader",
        )
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        with patch.object(
            analyzer, "_verify_png", wraps=analyzer._verify_png
        ) as rgb_decode:
            analyzer.load_completed_layout(
                self.database_path, private_root, job_id
            )

        self.assertEqual(2, rgb_decode.call_count, "只应解码两张overlay，不应转RGB原页面")

    def test_overlay_read_does_not_reload_all_source_pages(self):
        private_root, job_id = self.create_custom_rendered_job(
            [
                {"columns": 1, "texts": [(0, 34, "1. one")]},
                {"columns": 1, "texts": [(0, 34, "2. two")]},
            ],
            name="targeted-overlay-read",
        )
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        with patch.object(
            analyzer,
            "_load_inputs",
            side_effect=AssertionError("overlay route must not reload source pages"),
        ):
            content = analyzer.read_layout_overlay(
                self.database_path, private_root, job_id, 2
            )

        self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_analysis_reads_and_releases_source_pages_one_at_a_time(self):
        private_root, job_id = self.create_custom_rendered_job(
            [{"columns": 1}, {"columns": 1}], name="sequential-pages"
        )
        from src.processing import page_layout_analyzer as analyzer

        with (
            patch.object(analyzer, "_load_inputs", wraps=analyzer._load_inputs) as load,
            patch.object(analyzer, "_read_render_page", wraps=analyzer._read_render_page) as read,
        ):
            analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        self.assertFalse(load.call_args.kwargs["decode_images"])
        self.assertEqual(4, read.call_count)

    def test_rejects_render_source_page_count_not_matching_actual_pdf(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        manifest_path = (
            private_root / "processing" / f"import_job_{job_id}" /
            "render_manifest.json"
        )
        render = json.loads(manifest_path.read_text(encoding="utf-8"))
        render["source_page_count"] += 1
        manifest_path.write_text(json.dumps(render), encoding="utf-8")

        with self.assertRaisesRegex(analyzer.PageLayoutError, "输入校验失败"):
            analyzer.analyze_page_layout(self.database_path, private_root, job_id)

    def test_completed_loader_rejects_hardlinked_layout_manifest(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        manifest_path = job_dir / "layout_result" / "layout_manifest.json"
        second_link = self.root / "layout-manifest-hardlink.json"
        os.link(manifest_path, second_link)

        with self.assertRaisesRegex(analyzer.PageLayoutError, "现有版面分析结果校验失败"):
            analyzer.load_completed_layout(
                self.database_path, private_root, job_id
            )

    def test_publish_manifest_rename_failure_restores_old_complete_pair(self):
        private_root, job_id = self.create_rendered_job(columns=2)
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        old = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*") if path.is_file()
            and (path.name == "layout_manifest.json" or "layout_result" in path.parts)
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?", (job_id,)
            )
        real_replace = analyzer.os.replace
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic rename failure")
            return real_replace(*args, **kwargs)

        with patch.object(analyzer.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        current = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*") if path.is_file()
            and (path.name == "layout_manifest.json" or "layout_result" in path.parts)
        }
        self.assertEqual(old, current)
        self.assertEqual([], list(job_dir.glob(".layout_batch.*")))
        self.assertEqual([], list(job_dir.glob(".layout_*backup*")))

    def test_duplicate_reverse_and_jump_numbers_are_preserved_and_warned(self):
        private_root, job_id = self.create_custom_rendered_job(
            [{"columns": 1, "texts": [
                (0, 70, "1. first"), (0, 120, "（1） subpart"),
                (0, 170, "1. duplicate"), (0, 240, "3. jump"),
                (0, 310, "2. reverse"),
            ]}], name="number-warnings",
        )
        from src.processing import page_layout_analyzer as analyzer

        manifest = analyzer.analyze_page_layout(
            self.database_path, private_root, job_id
        )

        self.assertEqual(
            ["1", "1", "3", "2"],
            [question["question_no"] for question in manifest["questions"]],
        )
        self.assertEqual("low", manifest["questions"][1]["confidence"])
        self.assertTrue(any("重复" in item for item in manifest["questions"][1]["warnings"]))
        self.assertEqual("medium", manifest["questions"][2]["confidence"])
        self.assertTrue(any("跳号" in item for item in manifest["questions"][2]["warnings"]))
        self.assertEqual("low", manifest["questions"][3]["confidence"])
        self.assertTrue(any("倒序" in item for item in manifest["questions"][3]["warnings"]))
        self.assertTrue(all(question["regions"] for question in manifest["questions"]))

    def test_rejects_hardlinked_source_pdf(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        source = private_root / "raw_papers/TJ/2026/layout.pdf"
        os.link(source, self.root / "source-hardlink.pdf")

        with self.assertRaisesRegex(analyzer.PageLayoutError, "输入校验失败"):
            analyzer.analyze_page_layout(self.database_path, private_root, job_id)

    def test_publish_checks_job_attachment_before_moving_old_result(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?", (job_id,)
            )
        real_assert = analyzer._LayoutWorkspace.assert_attached
        checks = 0

        def fail_second(workspace):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise OSError("synthetic job entry swap")
            return real_assert(workspace)

        with (
            patch.object(analyzer._LayoutWorkspace, "assert_attached", fail_second),
            patch.object(analyzer.os, "replace", wraps=analyzer.os.replace) as replace,
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        replace.assert_not_called()

    def test_database_completion_failure_is_recovered_from_old_db_anchor(self):
        private_root, job_id = self.create_rendered_job(columns=2)
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        old = {
            path.relative_to(job_dir).as_posix(): path.read_bytes()
            for path in job_dir.rglob("*") if path.is_file()
            and (path.name == "layout_manifest.json" or "layout_result" in path.parts)
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?", (job_id,)
            )

        with patch.object(
            analyzer, "_complete_run", side_effect=sqlite3.OperationalError("private sql")
        ):
            with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(1, len(list(job_dir.glob(".layout_backup.*"))))
        claim = analyzer.claim_layout_job(
            self.database_path, private_root, job_id
        )
        self.assertIsNotNone(claim)
        claim.close()
        self.assertEqual(
            old,
            {
                path.relative_to(job_dir).as_posix(): path.read_bytes()
                for path in job_dir.rglob("*") if path.is_file()
                and (path.name == "layout_manifest.json" or "layout_result" in path.parts)
            },
        )
        self.assertEqual([], list(job_dir.glob(".layout_*backup*")))

    def test_real_job_directory_swap_is_safe_at_three_publish_boundaries(self):
        from src.processing import page_layout_analyzer as analyzer

        for index, boundary in enumerate(("before_publish", "between_renames", "after_db"), start=1):
            with self.subTest(boundary=boundary):
                private_root, job_id = self.create_custom_rendered_job(
                    [{"columns": 1, "texts": [(0, 34, "1. safe")]}],
                    name=f"swap-{index}",
                )
                job_dir = private_root / "processing" / f"import_job_{job_id}"
                moved = job_dir.with_name(f"{job_dir.name}.moved-{index}")
                external = self.root / f"external-swap-{index}"
                external.mkdir()
                swapped = False

                def swap_entry():
                    nonlocal swapped
                    if swapped:
                        return
                    job_dir.rename(moved)
                    job_dir.symlink_to(external, target_is_directory=True)
                    swapped = True

                if boundary == "before_publish":
                    real_publish = analyzer._publish

                    def publish_after_swap(workspace):
                        swap_entry()
                        return real_publish(workspace)

                    context = patch.object(analyzer, "_publish", publish_after_swap)
                elif boundary == "between_renames":
                    real_replace = analyzer.os.replace

                    def replace_then_swap(*args, **kwargs):
                        result = real_replace(*args, **kwargs)
                        if len(args) >= 2 and args[1] == "layout_result":
                            swap_entry()
                        return result

                    context = patch.object(analyzer.os, "replace", replace_then_swap)
                else:
                    real_complete = analyzer._complete_run

                    def complete_then_swap(*args, **kwargs):
                        result = real_complete(*args, **kwargs)
                        swap_entry()
                        return result

                    context = patch.object(analyzer, "_complete_run", complete_then_swap)

                with context:
                    with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                        analyzer.analyze_page_layout(
                            self.database_path, private_root, job_id
                        )

                self.assertTrue(swapped)
                self.assertEqual([], list(external.iterdir()))
                if boundary in {"between_renames", "after_db"}:
                    manifest_bytes = (
                        moved / "layout_result" / "layout_manifest.json"
                    ).read_bytes()
                    with sqlite3.connect(self.database_path) as connection:
                        digest = connection.execute(
                            "SELECT manifest_sha256 FROM import_layout_analysis_runs "
                            "WHERE import_job_id=?",
                            (job_id,),
                        ).fetchone()[0]
                    if boundary == "after_db":
                        self.assertEqual(
                            hashlib.sha256(manifest_bytes).hexdigest(), digest
                        )
                    else:
                        self.assertIsNone(digest)
                    self.assertTrue((moved / "layout_result" / "overlays").is_dir())
                else:
                    self.assertFalse(
                        (moved / "layout_result" / "layout_manifest.json").exists()
                    )
                    self.assertFalse((moved / "layout_result" / "overlays").exists())
                self.assertEqual([], list(moved.glob(".layout_batch.*")))
                self.assertEqual([], list(moved.glob(".layout_*backup*")))
                with sqlite3.connect(self.database_path) as connection:
                    self.assertEqual(
                        "failed",
                        connection.execute(
                            "SELECT status FROM import_layout_analysis_runs "
                            "WHERE import_job_id=?", (job_id,)
                        ).fetchone()[0],
                    )

    def test_completed_result_rejects_link_extra_and_manifest_tampering(self):
        from src.processing import page_layout_analyzer as analyzer

        scenarios = (
            "manifest_symlink", "overlay_symlink", "overlay_hardlink", "extra",
            "hash", "size", "dimensions", "columns", "questions",
            "page_metadata", "manifest_warnings", "question_number",
        )
        for index, scenario in enumerate(scenarios, start=1):
            with self.subTest(scenario=scenario):
                private_root, job_id = self.create_custom_rendered_job(
                    [{"columns": 1, "texts": [(0, 34, "1. candidate")]}],
                    name=f"completed-tamper-{index}",
                )
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )
                job_dir = private_root / "processing" / f"import_job_{job_id}"
                manifest_path = job_dir / "layout_result" / "layout_manifest.json"
                overlay_path = job_dir / "layout_result" / "overlays/page_001.png"
                if scenario == "manifest_symlink":
                    external = self.root / f"external-manifest-{index}.json"
                    manifest_path.rename(external)
                    manifest_path.symlink_to(external)
                elif scenario == "overlay_symlink":
                    external = self.root / f"external-overlay-{index}.png"
                    overlay_path.rename(external)
                    overlay_path.symlink_to(external)
                elif scenario == "overlay_hardlink":
                    os.link(overlay_path, self.root / f"overlay-link-{index}.png")
                elif scenario == "extra":
                    (overlay_path.parent / "extra.png").write_bytes(overlay_path.read_bytes())
                else:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if scenario == "hash":
                        manifest["pages"][0]["overlay"]["sha256"] = "0" * 64
                    elif scenario == "size":
                        manifest["pages"][0]["overlay"]["byte_size"] += 1
                    elif scenario == "dimensions":
                        manifest["pages"][0]["overlay"]["pixel_width"] += 1
                    elif scenario == "columns":
                        manifest["pages"][0]["columns"] = [[-1, 0, 10, 10]]
                    elif scenario == "questions":
                        manifest["questions"][0]["regions"] = []
                    elif scenario == "page_metadata":
                        manifest["pages"][0]["text_layer_available"] = "yes"
                    elif scenario == "manifest_warnings":
                        manifest["warnings"] = {"unexpected": True}
                    else:
                        manifest["questions"][0]["question_no"] = ""
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(
                    analyzer.PageLayoutError, "现有版面分析结果校验失败"
                ):
                    analyzer.load_completed_layout(
                        self.database_path, private_root, job_id
                    )

    def test_completed_reads_reject_non_object_render_manifest_safely(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        analyzer.analyze_page_layout(self.database_path, private_root, job_id)
        render_manifest = (
            private_root / "processing" / f"import_job_{job_id}"
            / "render_manifest.json"
        )
        render_manifest.write_text("[]", encoding="utf-8")

        for operation in (
            lambda: analyzer.load_completed_layout(
                self.database_path, private_root, job_id
            ),
            lambda: analyzer.read_layout_overlay(
                self.database_path, private_root, job_id, 1
            ),
        ):
            with self.assertRaises(analyzer.PageLayoutError):
                operation()

    def test_global_claim_blocks_different_job_without_creating_second_run(self):
        private_root, first_id = self.create_custom_rendered_job(
            [{"columns": 1}], name="global-first"
        )
        _, second_id = self.create_custom_rendered_job(
            [{"columns": 1}], name="global-second"
        )
        from src.processing import page_layout_analyzer as analyzer

        first = analyzer.claim_layout_job(
            self.database_path, private_root, first_id
        )
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(
                analyzer.claim_layout_job(
                    self.database_path, private_root, second_id
                )
            )
            with sqlite3.connect(self.database_path) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT status FROM import_layout_analysis_runs "
                        "WHERE import_job_id=?", (second_id,)
                    ).fetchone()
                )
        finally:
            first.close()

        second = analyzer.claim_layout_job(
            self.database_path, private_root, second_id
        )
        self.assertIsNotNone(second)
        second.close()

    def test_all_four_publish_replace_failures_restore_old_complete_pair(self):
        from src.processing import page_layout_analyzer as analyzer

        for failure_call in range(1, 3):
            with self.subTest(failure_call=failure_call):
                private_root, job_id = self.create_custom_rendered_job(
                    [{"columns": 1, "texts": [(0, 34, "1. old")]}],
                    name=f"replace-boundary-{failure_call}",
                )
                analyzer.analyze_page_layout(
                    self.database_path, private_root, job_id
                )
                job_dir = private_root / "processing" / f"import_job_{job_id}"
                old = {
                    path.relative_to(job_dir).as_posix(): path.read_bytes()
                    for path in job_dir.rglob("*") if path.is_file()
                    and (path.name == "layout_manifest.json" or "layout_result" in path.parts)
                }
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute(
                        "UPDATE import_layout_analysis_runs SET status='failed' "
                        "WHERE import_job_id=?", (job_id,)
                    )
                real_replace = analyzer.os.replace
                calls = 0

                def fail_selected(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == failure_call:
                        raise OSError("synthetic publish boundary")
                    return real_replace(*args, **kwargs)

                with patch.object(analyzer.os, "replace", fail_selected):
                    with self.assertRaisesRegex(analyzer.PageLayoutError, "版面分析失败"):
                        analyzer.analyze_page_layout(
                            self.database_path, private_root, job_id
                        )

                self.assertEqual(
                    old,
                    {
                        path.relative_to(job_dir).as_posix(): path.read_bytes()
                        for path in job_dir.rglob("*") if path.is_file()
                        and (path.name == "layout_manifest.json" or "layout_result" in path.parts)
                    },
                )
                self.assertEqual([], list(job_dir.glob(".layout_batch.*")))
                self.assertEqual([], list(job_dir.glob(".layout_*backup*")))

    def test_analysis_never_modifies_render_pages_or_generates_question_data(self):
        private_root, job_id = self.create_custom_rendered_job(
            [{"columns": 2, "texts": [(0, 34, "1. one"), (1, 34, "2. two")]}],
            name="no-question-side-effects",
        )
        from src.processing import page_layout_analyzer as analyzer
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        page = job_dir / "pages/page_001.png"
        before = (hashlib.sha256(page.read_bytes()).hexdigest(), page.stat().st_mtime_ns)

        analyzer.analyze_page_layout(self.database_path, private_root, job_id)

        self.assertEqual(
            before,
            (hashlib.sha256(page.read_bytes()).hexdigest(), page.stat().st_mtime_ns),
        )
        self.assertFalse((job_dir / "question_crops").exists())
        self.assertFalse((job_dir / "question_crops.json").exists())
        self.assertFalse((job_dir / "candidate_questions.json").exists())

    def test_claim_sqlite_failure_uses_safe_summary_and_releases_both_locks(self):
        private_root, job_id = self.create_rendered_job(columns=1)
        from src.processing import page_layout_analyzer as analyzer
        real_connect = analyzer.sqlite3.connect
        calls = 0

        def fail_claim_transaction(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError(f"private database {self.root}")
            return real_connect(*args, **kwargs)

        with patch.object(analyzer.sqlite3, "connect", fail_claim_transaction):
            with self.assertRaisesRegex(
                analyzer.PageLayoutError, "版面分析任务暂时无法启动"
            ) as raised:
                analyzer.claim_layout_job(
                    self.database_path, private_root, job_id
                )
        self.assertNotIn(str(self.root), str(raised.exception))

        recovered = analyzer.claim_layout_job(
            self.database_path, private_root, job_id
        )
        self.assertIsNotNone(recovered)
        recovered.close()


if __name__ == "__main__":
    unittest.main()
