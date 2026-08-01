import io
import hashlib
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
from src.processing.crop_review import record_crop_ai_review
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
        self.client = TestClient(create_app(
            self.db, self.private, split_runner=self.runner,
            auto_submit=lambda callback: None,
        ))
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

    def test_manual_split_recovery_after_render_still_works(self):
        job_id = self.upload_confirm_render()
        before = self.client.get(f"/imports/{job_id}/split")
        papers = self.client.get("/papers")
        self.assertEqual(200, before.status_code)
        self.assertIn("Codex 切题任务即将开始", before.text)
        self.assertIn("查看自动切题", papers.text)
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

        job_dir = self.private / "processing" / f"import_job_{job_id}"
        manifest_bytes = (job_dir / "question_crops.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        record_crop_ai_review(self.db, self.private, {
            "version": 1,
            "import_job_id": job_id,
            "input_generation_id": manifest["generation_id"],
            "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "reviewer_run_id": "web-vision-review-1",
            "questions": [
                {"question_no": 1, "status": "ai_review_passed", "warnings": []},
                {"question_no": 2, "status": "needs_recrop", "warnings": ["下边界需重切"]},
            ],
        })
        reviewed = self.client.get(f"/imports/{job_id}/split")
        self.assertIn("通过 1 题，需重切 1 题，待审核 0 题", reviewed.text)
        self.assertIn("审核状态：已通过", reviewed.text)
        self.assertIn("审核状态：需重切", reviewed.text)

        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_question_split_runs SET status='failed' WHERE import_job_id=?",
                (job_id,),
            )
        failed = self.client.get(f"/imports/{job_id}/split")
        self.assertIn("Q001", failed.text)
        self.assertIn("当前可信审核意见", failed.text)
        self.assertIn("根据审核意见重新切题", failed.text)
        self.assertNotIn("重试调用 Codex 自动切题", failed.text)
        self.assertEqual(200, self.client.get(
            f"/imports/{job_id}/split-images/1.png"
        ).status_code)

        manifest = json.loads((job_dir / "question_crops.json").read_text())
        self.assertEqual(2, manifest["question_count"])
        self.assertEqual("ai_review_passed", manifest["questions"][0]["review_status"])
        self.assertEqual(64, len(manifest["questions"][0]["sha256"]))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_review_drafts"
            ).fetchone()[0])

    def test_recrop_button_is_mutually_exclusive_with_all_passed_review(self):
        job_id = self.upload_confirm_render()
        self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        job_dir = self.private / "processing" / f"import_job_{job_id}"

        def review(second_status, warnings):
            manifest_bytes = (job_dir / "question_crops.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            record_crop_ai_review(self.db, self.private, {
                "version": 1,
                "import_job_id": job_id,
                "input_generation_id": manifest["generation_id"],
                "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "reviewer_run_id": f"web-button-{second_status}",
                "questions": [
                    {"question_no": 1, "status": "ai_review_passed", "warnings": []},
                    {"question_no": 2, "status": second_status, "warnings": warnings},
                ],
            })

        review("ai_review_passed", [])
        all_passed = self.client.get(f"/imports/{job_id}/split")
        self.assertNotIn("根据审核意见重新切题", all_passed.text)

        review("needs_recrop", ["第二题下边界错误"])
        needs_recrop = self.client.get(f"/imports/{job_id}/split")
        self.assertIn("根据审核意见重新切题", needs_recrop.text)
        self.assertIn(
            f'action="/imports/{job_id}/split"', needs_recrop.text
        )
        self.assertIn('name="csrf_token"', needs_recrop.text)

        restarted = self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, restarted.status_code)
        self.assertEqual(2, len(self.runner.calls))
        self.assertIn('"question_no":2', self.runner.calls[-1][1])
        pending = self.client.get(f"/imports/{job_id}/split")
        self.assertNotIn("根据审核意见重新切题", pending.text)
        self.assertIn("通过 1 题，需重切 0 题，待审核 1 题", pending.text)

    def test_invalid_review_evidence_hides_recrop_action_and_post_is_not_success(self):
        job_id = self.upload_confirm_render()
        self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        job_dir = self.private / "processing" / f"import_job_{job_id}"
        manifest_bytes = (job_dir / "question_crops.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        record_crop_ai_review(self.db, self.private, {
            "version": 1,
            "import_job_id": job_id,
            "input_generation_id": manifest["generation_id"],
            "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "reviewer_run_id": "web-invalid-evidence",
            "questions": [
                {"question_no": 1, "status": "ai_review_passed", "warnings": []},
                {"question_no": 2, "status": "needs_recrop", "warnings": ["边界错误"]},
            ],
        })
        (job_dir / "crop_ai_review.json").write_bytes(b"{broken")

        page = self.client.get(f"/imports/{job_id}/split")
        self.assertNotIn("当前可信审核意见", page.text)
        self.assertNotIn("根据审核意见重新切题", page.text)
        self.assertIn("审核证据校验失败", page.text)

        post = self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(409, post.status_code)
        self.assertEqual(1, len(self.runner.calls))

    def test_mask_review_get_is_read_only_and_posts_require_strict_csrf_and_fields(self):
        job_id = self.upload_confirm_render()
        self.client.post(
            f"/imports/{job_id}/split", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        before = {
            path.relative_to(self.private).as_posix(): path.read_bytes()
            for path in self.private.rglob("*") if path.is_file()
        }

        response = self.client.get(f"/imports/{job_id}/mask-review")

        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json()["proposals"])
        after = {
            path.relative_to(self.private).as_posix(): path.read_bytes()
            for path in self.private.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(403, self.client.post(
            f"/imports/{job_id}/mask-review", data={"csrf_token": "wrong"},
        ).status_code)
        self.assertEqual(400, self.client.post(
            f"/imports/{job_id}/mask-review",
            data={"csrf_token": self.csrf, "review_status": "ai_review_passed"},
        ).status_code)

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
        self.assertEqual(409, second.status_code)
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
