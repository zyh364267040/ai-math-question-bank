import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.processing.pdf_page_renderer import render_import_job
from src.web.app import create_app


class PageLayoutWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_root = self.root / "private"
        self.database_path = self.root / "question-bank.db"
        initialize_database(self.database_path).close()
        document = pymupdf.open()
        page = document.new_page(width=120, height=160)
        page.insert_text((10, 24), "1. synthetic question")
        content = document.tobytes()
        document.close()
        relative = Path("raw_papers/TJ/2026/layout-web.pdf")
        source = self.private_root / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        with sqlite3.connect(self.database_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,?,'layout-web.pdf',?,'TJ',2026,'YK','版面 Web 合成')""",
                (hashlib.sha256(content).hexdigest(), len(content), relative.as_posix()),
            ).lastrowid
            self.job_id = connection.execute(
                """INSERT INTO import_jobs
                   (source_paper_id,page_start,page_end,status)
                   VALUES (?,1,1,'pending')""", (source_id,)
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,rendered_pages)
                   VALUES (?,'processing',300,0)""", (self.job_id,)
            )
        render_import_job(self.database_path, self.private_root, self.job_id)
        self.client = TestClient(create_app(self.database_path, self.private_root))
        self.client.get("/papers")
        self.csrf = self.client.cookies.get("basket_csrf")

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_get_never_analyzes_and_explicit_csrf_post_schedules_worker(self):
        with patch("src.web.app.run_claimed_layout") as worker:
            papers = self.client.get("/papers")
            status_before = self.client.get(f"/imports/{self.job_id}/layout")
            self.assertEqual(200, status_before.status_code)
            worker.assert_not_called()
            self.assertIn("开始版面分析", papers.text)
            self.assertIn(f'action="/imports/{self.job_id}/layout"', papers.text)

            response = self.client.post(
                f"/imports/{self.job_id}/layout",
                data={"csrf_token": self.csrf},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertEqual(f"/imports/{self.job_id}/layout", response.headers["location"])
        worker.assert_called_once()
        worker.call_args.args[0].close()

    def test_completed_page_previews_verified_overlay_and_candidates(self):
        response = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)

        completed = self.client.get(f"/imports/{self.job_id}/layout")
        overlay = self.client.get(
            f"/imports/{self.job_id}/layout-overlays/1.png"
        )

        self.assertIn("版面分析完成", completed.text)
        self.assertIn("第 1 页", completed.text)
        self.assertIn("题号 1", completed.text)
        self.assertIn(
            f'/imports/{self.job_id}/layout-overlays/1.png', completed.text
        )
        self.assertEqual(200, overlay.status_code)
        self.assertEqual("image/png", overlay.headers["content-type"])
        self.assertEqual("no-store", overlay.headers["cache-control"])
        self.assertEqual(
            404,
            self.client.get(
                f"/imports/{self.job_id}/layout-overlays/2.png"
            ).status_code,
        )

    def test_layout_status_uses_render_total_when_confirmed_end_page_is_null(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_jobs SET page_start=NULL,page_end=NULL WHERE id=?",
                (self.job_id,),
            )

        response = self.client.get(f"/imports/{self.job_id}/layout")

        self.assertEqual(200, response.status_code)
        self.assertIn("0 / 1 页", response.text)

    def test_layout_post_rejects_csrf_extra_duplicate_and_oversized_forms(self):
        bad_csrf = self.client.post(
            f"/imports/{self.job_id}/layout", data={"csrf_token": "wrong"}
        )
        extra = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf, "path": "/tmp/private.pdf"},
        )
        duplicate = self.client.post(
            f"/imports/{self.job_id}/layout",
            content=f"csrf_token={self.csrf}&csrf_token={self.csrf}",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        oversized = self.client.post(
            f"/imports/{self.job_id}/layout",
            content=b"csrf_token=" + b"a" * (64 * 1024),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(403, bad_csrf.status_code)
        self.assertEqual(400, extra.status_code)
        self.assertEqual(400, duplicate.status_code)
        self.assertEqual(413, oversized.status_code)

    def test_layout_ui_explains_non_ocr_policy_and_versions_mobile_css(self):
        status = self.client.get(f"/imports/{self.job_id}/layout")
        render_status = self.client.get(f"/imports/{self.job_id}/processing")

        self.assertIn("PDF 嵌入文本层", status.text)
        self.assertIn("不是 OCR", status.text)
        self.assertIn("无文本层不猜题号", status.text)
        self.assertRegex(status.text, r'imports\.css\?v=[^" ]+')
        self.assertIn("开始版面分析", render_status.text)
        self.assertIn(
            f'action="/imports/{self.job_id}/layout"', render_status.text
        )

    def test_render_not_completed_is_409_without_layout_run_or_lock_artifacts(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_page_render_runs SET status='processing' "
                "WHERE import_job_id=?", (self.job_id,)
            )

        response = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf},
        )

        self.assertEqual(409, response.status_code)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM import_layout_analysis_runs"
                ).fetchone()[0],
            )
        self.assertFalse(
            (self.private_root / "processing" / ".layout_locks").exists()
        )

    def test_stale_failed_and_active_layout_posts_schedule_at_most_one_worker(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO import_layout_analysis_runs
                   (import_job_id,status,total_pages,analyzed_pages,detected_questions)
                   VALUES (?,'processing',1,0,0)""", (self.job_id,)
            )
        stale = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(303, stale.status_code)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM import_layout_analysis_runs WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )

        job_dir = self.private_root / "processing" / f"import_job_{self.job_id}"
        (job_dir / "layout_result" / "overlays/page_001.png").write_bytes(b"damaged")
        damaged = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(303, damaged.status_code)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM import_layout_analysis_runs WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='failed' "
                "WHERE import_job_id=?",
                (self.job_id,),
            )
        retry = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(303, retry.status_code)
        self.assertIn(
            "版面分析完成",
            self.client.get(f"/imports/{self.job_id}/layout").text,
        )

        from src.processing.page_layout_analyzer import claim_layout_job
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_layout_analysis_runs SET status='processing' "
                "WHERE import_job_id=?", (self.job_id,)
            )
        active = claim_layout_job(
            self.database_path, self.private_root, self.job_id
        )
        self.assertIsNotNone(active)
        try:
            with patch("src.web.app.run_claimed_layout") as worker:
                response = self.client.post(
                    f"/imports/{self.job_id}/layout",
                    data={"csrf_token": self.csrf}, follow_redirects=False,
                )
            self.assertEqual(303, response.status_code)
            worker.assert_not_called()
        finally:
            active.close()

    def test_damaged_and_extra_overlay_routes_fail_without_path_disclosure(self):
        self.client.post(
            f"/imports/{self.job_id}/layout", data={"csrf_token": self.csrf}
        )
        job_dir = self.private_root / "processing" / f"import_job_{self.job_id}"
        overlay = job_dir / "layout_result" / "overlays/page_001.png"
        overlay.write_bytes(b"damaged")

        damaged = self.client.get(
            f"/imports/{self.job_id}/layout-overlays/1.png"
        )
        self.assertEqual(409, damaged.status_code)
        self.assertNotIn(str(self.root), damaged.text)
        self.assertEqual("nosniff", damaged.headers["x-content-type-options"])

        self.client.post(
            f"/imports/{self.job_id}/layout", data={"csrf_token": self.csrf}
        )
        (job_dir / "layout_result/overlays/extra.png").write_bytes(b"extra")
        direct = self.client.get(
            f"/imports/{self.job_id}/layout-overlays/1.png"
        )
        status = self.client.get(f"/imports/{self.job_id}/layout")
        self.assertEqual(200, direct.status_code)
        self.assertIn("现有版面分析结果校验失败", status.text)
        self.assertEqual("no-store", status.headers["cache-control"])

    def test_corrupted_completed_result_can_be_rebuilt_with_one_explicit_post(self):
        self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf},
        )
        job_dir = self.private_root / "processing" / f"import_job_{self.job_id}"
        (job_dir / "layout_result" / "overlays/page_001.png").write_bytes(b"damaged")

        status = self.client.get(f"/imports/{self.job_id}/layout")

        self.assertEqual(200, status.status_code)
        self.assertIn("现有版面分析结果校验失败", status.text)
        self.assertIn(f'action="/imports/{self.job_id}/layout"', status.text)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM import_layout_analysis_runs WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )

        retry = self.client.post(
            f"/imports/{self.job_id}/layout",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )

        self.assertEqual(303, retry.status_code)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM import_layout_analysis_runs WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )
        completed = self.client.get(f"/imports/{self.job_id}/layout")
        self.assertEqual(200, completed.status_code)
        self.assertIn("版面分析完成", completed.text)


if __name__ == "__main__":
    unittest.main()
