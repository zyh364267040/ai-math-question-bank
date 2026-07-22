import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.web.app import create_app


class PageRenderWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_root = self.root / "private"
        self.database_path = self.root / "question-bank.db"
        initialize_database(self.database_path).close()
        document = pymupdf.open()
        document.new_page(width=72, height=72)
        content = document.tobytes()
        document.close()
        relative = Path("raw_papers/TJ/2026/web.pdf")
        source = self.private_root / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        with sqlite3.connect(self.database_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256, file_size, original_filename, stored_path,
                    region_code, exam_year, exam_type_code, paper_name)
                   VALUES (?, ?, 'web.pdf', ?, 'TJ', 2026, 'YK', 'Web合成试卷')""",
                (hashlib.sha256(content).hexdigest(), len(content), relative.as_posix()),
            ).lastrowid
            self.job_id = connection.execute(
                """INSERT INTO import_jobs
                   (source_paper_id, page_start, page_end, status)
                   VALUES (?, 1, 1, 'pending')""",
                (source_id,),
            ).lastrowid
        self.client = TestClient(create_app(self.database_path, self.private_root))
        self.client.get("/papers")
        self.csrf = self.client.cookies.get("basket_csrf")

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_pending_paper_requires_explicit_csrf_post_and_schedules_one_worker(self):
        papers = self.client.get("/papers")
        self.assertIn("恢复页面处理", papers.text)
        self.assertIn(f'action="/imports/{self.job_id}/render"', papers.text)

        with patch("src.web.app.run_claimed_render") as worker:
            response = self.client.post(
                f"/imports/{self.job_id}/render",
                data={"csrf_token": self.csrf},
                follow_redirects=False,
            )
            repeated = self.client.post(
                f"/imports/{self.job_id}/render",
                data={"csrf_token": self.csrf},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertEqual(
            f"/imports/{self.job_id}/processing", response.headers["location"]
        )
        self.assertEqual(303, repeated.status_code)
        worker.assert_called_once()
        worker.call_args.args[0].close()
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                ("processing", 300),
                connection.execute(
                    """SELECT status, dpi FROM import_page_render_runs
                       WHERE import_job_id=?""",
                    (self.job_id,),
                ).fetchone(),
            )

    def test_render_post_rejects_csrf_unknown_extra_parameters_and_history(self):
        bad_csrf = self.client.post(
            f"/imports/{self.job_id}/render", data={"csrf_token": "wrong"}
        )
        extra = self.client.post(
            f"/imports/{self.job_id}/render",
            data={"csrf_token": self.csrf, "dpi": "72", "path": "/tmp/a.pdf"},
        )
        unknown = self.client.post(
            "/imports/999999/render", data={"csrf_token": self.csrf}
        )
        with sqlite3.connect(self.database_path) as connection:
            source_id = connection.execute(
                "SELECT source_paper_id FROM import_jobs WHERE id=?", (self.job_id,)
            ).fetchone()[0]
            history_id = connection.execute(
                "INSERT INTO import_jobs (source_paper_id,status) VALUES (?,'completed')",
                (source_id,),
            ).lastrowid
        history = self.client.post(
            f"/imports/{history_id}/render", data={"csrf_token": self.csrf}
        )

        self.assertEqual(403, bad_csrf.status_code)
        self.assertEqual(400, extra.status_code)
        self.assertEqual(404, unknown.status_code)
        self.assertEqual(409, history.status_code)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM import_page_render_runs").fetchone()[0],
            )
        papers = self.client.get("/papers")
        self.assertNotIn(f'action="/imports/{history_id}/render"', papers.text)

    def test_processing_status_page_covers_pending_processing_failed_and_completed(self):
        pending = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertEqual(200, pending.status_code)
        self.assertIn("Web合成试卷", pending.text)
        self.assertIn("web.pdf", pending.text)
        self.assertIn("1–1", pending.text)
        self.assertIn("300 DPI", pending.text)
        self.assertIn("0 / 1 页", pending.text)
        self.assertIn("恢复页面处理", pending.text)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'processing',300,1,0)""",
                (self.job_id,),
            )
        processing = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertIn("页面处理中", processing.text)
        self.assertIn('http-equiv="refresh"', processing.text)
        self.assertIn("继续或检查页面处理", processing.text)
        self.assertIn(f'action="/imports/{self.job_id}/render"', processing.text)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET status='failed', error_message=? WHERE import_job_id=?""",
                (f"sqlite at {self.root}", self.job_id),
            )
        failed = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertIn("页面处理失败，请重试", failed.text)
        self.assertNotIn(str(self.root), failed.text)
        self.assertIn("重试页面处理", failed.text)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET status='completed', rendered_pages=1, error_message=NULL
                   WHERE import_job_id=?""",
                (self.job_id,),
            )
        completed = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertIn("页面处理完成", completed.text)
        self.assertIn('href="/papers"', completed.text)
        self.assertNotIn('http-equiv="refresh"', completed.text)
        papers = self.client.get("/papers")
        self.assertIn("查看处理进度", papers.text)
        self.assertNotIn(
            f'action="/imports/{self.job_id}/render"', papers.text
        )

    def test_stale_processing_post_starts_one_worker_and_completes(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'processing',300,1,0)""",
                (self.job_id,),
            )

        response = self.client.post(
            f"/imports/{self.job_id}/render",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual(
            f"/imports/{self.job_id}/processing", response.headers["location"]
        )
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                ("completed", 1),
                connection.execute(
                    """SELECT status, rendered_pages FROM import_page_render_runs
                       WHERE import_job_id=?""",
                    (self.job_id,),
                ).fetchone(),
            )

    def test_processing_post_does_not_start_worker_while_claim_lock_is_active(self):
        from src.processing.pdf_page_renderer import claim_render_job

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'processing',300,1,0)""",
                (self.job_id,),
            )
        active = claim_render_job(
            self.database_path, self.private_root, self.job_id
        )
        self.assertIsNotNone(active)
        try:
            with patch("src.web.app.run_claimed_render") as worker:
                response = self.client.post(
                    f"/imports/{self.job_id}/render",
                    data={"csrf_token": self.csrf},
                    follow_redirects=False,
                )
            self.assertEqual(303, response.status_code)
            worker.assert_not_called()
        finally:
            active.close()

    def test_processing_recovery_post_still_requires_csrf(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'processing',300,1,0)""",
                (self.job_id,),
            )

        with patch("src.web.app.run_claimed_render") as worker:
            response = self.client.post(
                f"/imports/{self.job_id}/render",
                data={"csrf_token": "invalid"},
                follow_redirects=False,
            )

        self.assertEqual(403, response.status_code)
        worker.assert_not_called()

    def test_unknown_processing_page_is_404(self):
        response = self.client.get("/imports/999999/processing")
        self.assertEqual(404, response.status_code)

    def test_historical_job_without_render_run_does_not_claim_background_processing(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE import_jobs SET status='needs_review' WHERE id=?",
                (self.job_id,),
            )

        response = self.client.get(f"/imports/{self.job_id}/processing")

        self.assertEqual(409, response.status_code)
        self.assertIn("该历史任务没有页面处理记录", response.text)
        self.assertNotIn("页面处理在后台进行", response.text)

    def test_real_background_worker_completes_only_after_explicit_post(self):
        job_dir = self.private_root / "processing" / f"import_job_{self.job_id}"
        self.assertFalse(job_dir.exists())

        response = self.client.post(
            f"/imports/{self.job_id}/render",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertTrue((job_dir / "pages/page_001.png").is_file())
        self.assertTrue((job_dir / "render_manifest.json").is_file())
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM import_page_render_runs WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )

    def test_create_app_migrates_legacy_database_without_changing_existing_data(self):
        with sqlite3.connect(self.database_path) as connection:
            knowledge_id = connection.execute(
                "SELECT id FROM knowledge_points ORDER BY id LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO questions
                   (question_code, stem_markdown, answer_markdown,
                    region_code, exam_type_code, question_type_code,
                    primary_knowledge_point_id, content_hash)
                   VALUES ('SYNTHETIC-LEGACY-001', '合成旧题', '合成答案',
                           'TJ', 'YK', 'solution', ?, 'synthetic-legacy-hash')""",
                (knowledge_id,),
            )
            before = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY id"
                ).fetchall()
                for table in ("source_papers", "import_jobs", "questions")
            }
            connection.execute("DROP TABLE import_page_render_runs")
        self.client.close()

        self.client = TestClient(create_app(self.database_path, self.private_root))
        response = self.client.get("/papers")

        self.assertEqual(200, response.status_code)
        self.assertIn("Web合成试卷", response.text)
        with sqlite3.connect(self.database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            after = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY id"
                ).fetchall()
                for table in ("source_papers", "import_jobs", "questions")
            }
        self.assertIn("import_page_render_runs", tables)
        self.assertEqual(before, after)

    def test_completed_web_result_can_be_validated_and_explicitly_rebuilt(self):
        self.client.post(
            f"/imports/{self.job_id}/render", data={"csrf_token": self.csrf}
        )
        job_dir = self.private_root / "processing" / f"import_job_{self.job_id}"
        original_mtimes = {
            path.relative_to(job_dir).as_posix(): path.stat().st_mtime_ns
            for path in job_dir.rglob("*")
            if path.is_file()
        }
        completed = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertIn("校验或重新处理", completed.text)

        valid_check = self.client.post(
            f"/imports/{self.job_id}/render",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )

        self.assertEqual(303, valid_check.status_code)
        self.assertEqual(
            original_mtimes,
            {
                path.relative_to(job_dir).as_posix(): path.stat().st_mtime_ns
                for path in job_dir.rglob("*")
                if path.is_file()
            },
        )
        (job_dir / "pages/page_001.png").write_bytes(b"corrupt")

        damaged_check = self.client.post(
            f"/imports/{self.job_id}/render",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )

        self.assertEqual(303, damaged_check.status_code)
        failed = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertIn("页面处理失败", failed.text)
        self.assertIn("重试页面处理", failed.text)

        retry = self.client.post(
            f"/imports/{self.job_id}/render",
            data={"csrf_token": self.csrf},
            follow_redirects=False,
        )

        self.assertEqual(303, retry.status_code)
        rebuilt = self.client.get(f"/imports/{self.job_id}/processing")
        self.assertIn("页面处理完成", rebuilt.text)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM import_page_render_runs WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )

    def test_processing_refresh_is_in_head_and_layout_has_mobile_styles(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'processing',300,1,0)""",
                (self.job_id,),
            )

        response = self.client.get(f"/imports/{self.job_id}/processing")

        refresh = response.text.index('http-equiv="refresh"')
        self.assertLess(response.text.index("<head>"), refresh)
        self.assertLess(refresh, response.text.index("</head>"))
        css = (
            Path(__file__).resolve().parents[1] / "src/web/static/imports.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".processing-card", css)
        self.assertIn("@media (max-width:", css)


if __name__ == "__main__":
    unittest.main()
