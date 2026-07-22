import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.processing.candidate_extractor import CandidateExtractionRunResult
from src.web.app import create_app
from tests.fixture_factory import create_import_job_fixture, write_synthetic_crop_review_evidence


class WebCandidateRunner:
    def __init__(self, job_id, source_id):
        self.calls = []
        self.payload = {
            "version": 1, "import_job_id": job_id, "source_paper_id": source_id,
            "question_count": 23, "questions": [self.question(number) for number in range(1, 24)],
        }

    @staticmethod
    def question(number):
        return {
            "source_question_no": str(number), "stem_markdown": f"Web 合成题干 {number}",
            "question_type_code": "single_choice" if number == 1 else "solution",
            "primary_knowledge_point_code": "", "related_knowledge_point_codes": [],
            "options": ([{"code": "A", "content": "甲"}, {"code": "B", "content": "乙"}]
                        if number == 1 else []),
            "subquestions": ([{"label": "（1）", "stem_markdown": "证明结论。"}]
                             if number == 23 else []),
            "answer_markdown": "", "analysis_markdown": "", "figure_required": False,
            "source_pages": [1 if number <= 6 else 2 if number <= 12 else 3 if number <= 18 else 4],
            "extraction_confidence": "low" if number == 2 else "high",
            "warnings": ["局部字迹不清"] if number == 2 else [],
        }

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        return CandidateExtractionRunResult(json.dumps(self.payload), "web-candidate-fake")


class CandidateExtractionWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.database = self.root / "question-bank.db"
        initialize_database(self.database).close()
        with sqlite3.connect(self.database) as connection:
            self.source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name)
                   VALUES (?,1,'web.pdf','raw_papers/TJ/unknown/web-candidate.pdf',
                           'TJ','QT','Web候选合成卷')""", ("a" * 64,),
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (self.source_id,),
            ).lastrowid
        self.job_dir = create_import_job_fixture(
            self.private, job_id=self.job_id, source_paper_id=self.source_id
        )
        (self.job_dir / "candidate_questions.json").unlink()
        content = (self.job_dir / "question_crops.json").read_bytes()
        manifest = json.loads(content)
        write_synthetic_crop_review_evidence(self.job_dir)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,question_count,processed_pages,codex_run_id,
                    result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                    crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                    completed_at,updated_at)
                   VALUES (?,'completed',23,4,'split',?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (self.job_id, "b" * 64, "c" * 64, "a" * 64,
                 hashlib.sha256(content).hexdigest(), manifest["generation_id"], manifest["signature"]),
            )
        self.runner = WebCandidateRunner(self.job_id, self.source_id)
        self.client = TestClient(create_app(
            self.database, self.private, candidate_runner=self.runner,
        ))
        self.client.get("/imports/new")
        self.csrf = self.client.cookies.get("basket_csrf")

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_get_is_read_only_post_is_explicit_csrf_and_completed_is_idempotent(self):
        before = {path.relative_to(self.private).as_posix() for path in self.private.rglob("*")}
        response = self.client.get(f"/imports/{self.job_id}/candidates")
        self.assertEqual(200, response.status_code)
        self.assertIn("调用 Codex 识别题目内容", response.text)
        self.assertEqual(before, {path.relative_to(self.private).as_posix() for path in self.private.rglob("*")})
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM import_candidate_extraction_runs"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM candidate_review_drafts"
            ).fetchone()[0])
        self.assertEqual(403, self.client.post(
            f"/imports/{self.job_id}/candidates", data={"csrf_token": "wrong"}
        ).status_code)
        self.assertEqual(400, self.client.post(
            f"/imports/{self.job_id}/candidates",
            data={"csrf_token": self.csrf, "path": "/tmp/no"},
        ).status_code)
        first = self.client.post(
            f"/imports/{self.job_id}/candidates", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        second = self.client.post(
            f"/imports/{self.job_id}/candidates", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, first.status_code)
        self.assertEqual(303, second.status_code)
        self.assertEqual(1, len(self.runner.calls))

    def test_result_renders_images_content_confidence_warnings_and_boundaries(self):
        self.client.post(
            f"/imports/{self.job_id}/candidates", data={"csrf_token": self.csrf}
        )
        result = self.client.get(f"/imports/{self.job_id}/candidates")
        self.assertEqual(200, result.status_code)
        for text in (
            "Q001", "Web 合成题干 1", "甲", "Q023", "证明结论",
            "low", "局部字迹不清", "未生成答案/解析", "尚未AI二审", "尚未正式入库",
        ):
            self.assertIn(text, result.text)
        self.assertIn('loading="lazy"', result.text)
        self.assertIn(f'/imports/{self.job_id}/split-images/1.png', result.text)
        self.assertEqual(200, self.client.get(
            f"/imports/{self.job_id}/split-images/1.png"
        ).status_code)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM candidate_review_drafts"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
