import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient
from PIL import Image

from src.database.initialize import initialize_database
from src.processing.question_splitter import CodexRunResult
from src.web.app import create_app


class FakeWebRunner:
    def __init__(self):
        self.calls = []

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        job_id = int(re.search(r"import_job_id=(\d+)", prompt).group(1))
        return CodexRunResult(json.dumps({
            "version": 1, "import_job_id": job_id, "question_count": 2,
            "questions": [
                {"question_no": 1, "regions": [{
                    "page_number": 1, "bbox_normalized": [0.05, 0.05, 0.95, 0.45]
                }], "warnings": ["检查第一题下边界"], "confidence": 0.9},
                {"question_no": 2, "regions": [{
                    "page_number": 1, "bbox_normalized": [0.05, 0.45, 0.95, 0.9]
                }], "warnings": [], "confidence": 0.85},
            ],
        }), "web-fake-1")


class QuestionSplitWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.db = self.root / "question-bank.db"
        initialize_database(self.db).close()
        self.runner = FakeWebRunner()
        self.client = TestClient(create_app(self.db, self.private, split_runner=self.runner))
        self.client.get("/imports/new")
        self.csrf = self.client.cookies.get("basket_csrf")

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    @staticmethod
    def pdf_bytes():
        document = pymupdf.open()
        page = document.new_page(width=200, height=300)
        page.insert_text((15, 35), "1. synthetic first question")
        page.insert_text((15, 165), "2. synthetic second question")
        content = document.tobytes()
        document.close()
        return content

    def upload_confirm_render(self):
        preview = self.client.post(
            "/imports/preview", data={"csrf_token": self.csrf},
            files={"pdf_file": ("split.pdf", self.pdf_bytes(), "application/pdf")},
        )
        self.assertEqual(200, preview.status_code)
        token = re.search(r'action="/imports/([^/]+)/confirm"', preview.text).group(1)
        confirmed = self.client.post(
            f"/imports/{token}/confirm",
            data={
                "csrf_token": self.csrf, "paper_name": "切题闭环合成卷",
                "region_code": "TJ", "exam_year": "2026",
                "exam_type_code": "QT", "page_range": "1-1",
            }, follow_redirects=False,
        )
        self.assertEqual(303, confirmed.status_code)
        with sqlite3.connect(self.db) as connection:
            job_id = connection.execute("SELECT max(id) FROM import_jobs").fetchone()[0]
        rendered = self.client.post(
            f"/imports/{job_id}/render", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, rendered.status_code)
        return job_id

    def test_real_upload_confirm_render_then_explicit_split_mvp(self):
        job_id = self.upload_confirm_render()
        before = self.client.get(f"/imports/{job_id}/split")
        papers = self.client.get("/papers")
        self.assertEqual(200, before.status_code)
        self.assertIn("原卷页面图片会发送给 OpenAI Codex", before.text)
        self.assertIn("调用 Codex 自动切题", papers.text)
        self.assertEqual([], self.runner.calls)

        started = self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, started.status_code)
        self.assertEqual(1, len(self.runner.calls))
        self.assertEqual(1, len(self.runner.calls[0][0]))

        completed = self.client.get(f"/imports/{job_id}/split")
        self.assertIn("共切分 2 题", completed.text)
        self.assertIn("Q001", completed.text)
        self.assertIn("Q002", completed.text)
        self.assertIn("检查第一题下边界", completed.text)
        for number in (1, 2):
            image = self.client.get(f"/imports/{job_id}/split-images/{number}.png")
            self.assertEqual(200, image.status_code)
            self.assertEqual("image/png", image.headers["content-type"])
            with Image.open(io.BytesIO(image.content)) as opened:
                opened.load()
                self.assertEqual("PNG", opened.format)

        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_question_split_runs SET status='failed' WHERE import_job_id=?",
                (job_id,),
            )
        failed = self.client.get(f"/imports/{job_id}/split")
        self.assertIn("Q001", failed.text)
        self.assertEqual(200, self.client.get(
            f"/imports/{job_id}/split-images/1.png"
        ).status_code)

        job_dir = self.private / "processing" / f"import_job_{job_id}"
        manifest = json.loads((job_dir / "question_crops.json").read_text())
        self.assertEqual(2, manifest["question_count"])
        self.assertEqual("pending_ai_review", manifest["questions"][0]["review_status"])
        self.assertEqual(64, len(manifest["questions"][0]["sha256"]))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_review_drafts"
            ).fetchone()[0])

    def test_get_csrf_body_limit_duplicate_post_and_safe_image_route(self):
        job_id = self.upload_confirm_render()
        before = {
            path.relative_to(self.private).as_posix(): (
                path.stat().st_size, path.stat().st_mtime_ns
            )
            for path in self.private.rglob("*") if path.is_file()
        }
        self.client.get(f"/imports/{job_id}/split")
        after = {
            path.relative_to(self.private).as_posix(): (
                path.stat().st_size, path.stat().st_mtime_ns
            )
            for path in self.private.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(0, len(self.runner.calls))
        self.assertEqual(403, self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": "wrong"}
        ).status_code)
        self.assertEqual(400, self.client.post(
            f"/imports/{job_id}/split",
            data={"csrf_token": self.csrf, "path": "/tmp/no"},
        ).status_code)
        self.assertEqual(413, self.client.post(
            f"/imports/{job_id}/split", content=b"csrf_token=" + b"a" * 65536,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code)
        first = self.client.post(f"/imports/{job_id}/split", data={"csrf_token": self.csrf})
        second = self.client.post(f"/imports/{job_id}/split", data={"csrf_token": self.csrf})
        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, len(self.runner.calls))
        self.assertEqual(404, self.client.get(
            f"/imports/{job_id}/split-images/999.png"
        ).status_code)
        self.assertEqual(404, self.client.get(
            f"/imports/{job_id}/split-images/%2e%2e%2fquestion_crops%2fQ001.png"
        ).status_code)

        job_dir = self.private / "processing" / f"import_job_{job_id}"
        second_image = job_dir / "question_crops/Q002.png"
        second_original = second_image.read_bytes()
        second_image.write_bytes(b"tampered-other-crop")
        self.assertEqual(200, self.client.get(
            f"/imports/{job_id}/split-images/1.png"
        ).status_code)
        self.assertEqual(404, self.client.get(
            f"/imports/{job_id}/split-images/2.png"
        ).status_code)
        second_image.write_bytes(second_original)

        image = job_dir / "question_crops/Q001.png"
        image.write_bytes(b"tampered")
        self.assertEqual(404, self.client.get(
            f"/imports/{job_id}/split-images/1.png"
        ).status_code)
        retry = self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, retry.status_code)
        self.assertEqual(2, len(self.runner.calls))
        self.assertEqual(200, self.client.get(
            f"/imports/{job_id}/split-images/1.png"
        ).status_code)


if __name__ == "__main__":
    unittest.main()
