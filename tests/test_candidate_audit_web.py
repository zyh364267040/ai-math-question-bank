import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.processing.candidate_extractor import (
    CandidateExtractionRunResult,
    claim_candidate_extraction,
    run_claimed_candidate_extraction,
)
from src.reviewing.candidate_auditor import CandidateAuditRunResult
from src.web.app import create_app
from tests.fixture_factory import (
    create_import_job_fixture,
    write_synthetic_crop_review_evidence,
)


class ExtractionRunner:
    def __init__(self, payload):
        self.payload = payload

    def run(self, *, image_paths, prompt):
        return CandidateExtractionRunResult(json.dumps(self.payload), "candidate-web")


class AuditRunner:
    def __init__(self, payload, run_id="audit-web"):
        self.payload = payload
        self.run_id = run_id
        self.calls = []

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        return CandidateAuditRunResult(json.dumps(self.payload), self.run_id)


class CandidateAuditWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.database = self.root / "bank.db"
        initialize_database(self.database).close()
        with sqlite3.connect(self.database) as connection:
            source = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name)
                   VALUES (?,1,'web-audit.pdf','raw_papers/TJ/unknown/web-audit.pdf',
                           'TJ','QT','Web视觉二审合成卷')""", ("a" * 64,),
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (source,),
            ).lastrowid
        self.job_dir = create_import_job_fixture(
            self.private, job_id=self.job_id, source_paper_id=source
        )
        (self.job_dir / "candidate_questions.json").unlink()
        (self.job_dir / "ai_audit.json").unlink()
        manifest_raw = (self.job_dir / "question_crops.json").read_bytes()
        manifest = json.loads(manifest_raw)
        write_synthetic_crop_review_evidence(self.job_dir)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,question_count,processed_pages,codex_run_id,
                    result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                    crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                    completed_at,updated_at)
                   VALUES (?,'completed',23,4,'split',?,?,?,?,?,?,CURRENT_TIMESTAMP,
                           CURRENT_TIMESTAMP)""",
                (
                    self.job_id, "b" * 64, "c" * 64, "a" * 64,
                    hashlib.sha256(manifest_raw).hexdigest(),
                    manifest["generation_id"], manifest["signature"],
                ),
            )
        candidates = []
        for number in range(1, 24):
            page = 1 if number <= 6 else 2 if number <= 12 else 3 if number <= 18 else 4
            candidates.append({
                "source_question_no": str(number), "stem_markdown": f"Web题干{number}",
                "question_type_code": "solution", "primary_knowledge_point_code": "",
                "related_knowledge_point_codes": [], "options": [], "subquestions": [],
                "answer_markdown": "", "analysis_markdown": "",
                "figure_required": number in {3, 16}, "source_pages": [page],
                "extraction_confidence": "high", "warnings": [],
            })
        candidate_payload = {
            "version": 1, "import_job_id": self.job_id, "source_paper_id": source,
            "question_count": 23, "questions": candidates,
        }
        run_claimed_candidate_extraction(claim_candidate_extraction(
            self.database, self.private, self.job_id,
            runner=ExtractionRunner(candidate_payload), weekly_checker=lambda: 100.0,
        ))
        questions = []
        for candidate in candidates:
            special = candidate["source_question_no"] == "2"
            questions.append({
                "source_question_no": candidate["source_question_no"],
                "audit_status": "human_required" if special else "auto_pass",
                "text_match": not special, "structure_match": True,
                "formula_match": True,
                "figure_check": "passed" if candidate["figure_required"] else "not_applicable",
                "knowledge_check": "not_reviewed",
                "issues": ["题干局部与图片不一致"] if special else [],
                "suggested_corrections": ["按图片逐字核对"] if special else [],
                "evidence_page": candidate["source_pages"][0],
                "audit_confidence": "medium" if special else "high",
            })
        payload = {
            "import_job_id": self.job_id,
            "auditor": "independent_codex_visual_second_pass",
            "audit_scope": {
                "kind": "candidate_text_vs_verified_single_question_crops",
                "source_pages": [1, 2, 3, 4],
            },
            "question_count": 23,
            "counts": {"auto_pass": 22, "disputed": 0, "human_required": 1},
            "questions": questions,
            "random_sample_recommendation": {"question_nos": ["2"], "reason": "复核异常题"},
            "global_findings": ["一题需要人工确认"],
        }
        self.runner = AuditRunner(payload)
        self.client = TestClient(create_app(
            self.database, self.private, audit_runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        self.client.get("/imports/new")
        self.csrf = self.client.cookies.get("basket_csrf")

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_get_is_read_only_and_post_requires_explicit_csrf(self):
        before_files = {
            path.relative_to(self.private).as_posix() for path in self.private.rglob("*")
        }
        with sqlite3.connect(self.database) as connection:
            before_rows = connection.execute(
                "SELECT count(*) FROM import_candidate_audit_runs"
            ).fetchone()[0]
        response = self.client.get(f"/imports/{self.job_id}/audit")
        self.assertEqual(200, response.status_code)
        self.assertIn("启动独立视觉二审", response.text)
        self.assertEqual(before_files, {
            path.relative_to(self.private).as_posix() for path in self.private.rglob("*")
        })
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(before_rows, connection.execute(
                "SELECT count(*) FROM import_candidate_audit_runs"
            ).fetchone()[0])
        self.assertEqual(403, self.client.post(
            f"/imports/{self.job_id}/audit", data={"csrf_token": "wrong"}
        ).status_code)
        self.assertEqual(400, self.client.post(
            f"/imports/{self.job_id}/audit",
            data={"csrf_token": self.csrf, "unexpected": "x"},
        ).status_code)

    def test_post_runs_once_and_result_shows_counts_issues_without_writes(self):
        first = self.client.post(
            f"/imports/{self.job_id}/audit", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        second = self.client.post(
            f"/imports/{self.job_id}/audit", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        self.assertEqual((303, 303), (first.status_code, second.status_code))
        self.assertEqual(1, len(self.runner.calls))
        result = self.client.get(f"/imports/{self.job_id}/audit")
        for text in (
            "独立视觉二审完成", "22", "自动通过", "0", "有争议", "1",
            "需人工确认", "题干局部与图片不一致", "按图片逐字核对",
            "不分类知识点", "不会创建草稿或正式入库",
        ):
            self.assertIn(text, result.text)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM candidate_review_drafts"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM questions"
            ).fetchone()[0])

    def test_apply_auto_pass_is_post_only_csrf_protected_counted_and_idempotent(self):
        self.client.post(
            f"/imports/{self.job_id}/audit", data={"csrf_token": self.csrf},
            follow_redirects=False,
        )
        page = self.client.get(f"/imports/{self.job_id}/audit")
        self.assertIn("应用 22 条自动通过结果", page.text)
        endpoint = f"/imports/{self.job_id}/audit/apply-auto-pass"
        self.assertEqual(405, self.client.get(endpoint).status_code)
        self.assertEqual(403, self.client.post(
            endpoint, data={"csrf_token": "wrong"}
        ).status_code)
        first = self.client.post(
            endpoint, data={"csrf_token": self.csrf}, follow_redirects=False
        )
        second = self.client.post(
            endpoint, data={"csrf_token": self.csrf}, follow_redirects=False
        )
        self.assertEqual(f"/imports/{self.job_id}/audit?applied=22", first.headers["location"])
        self.assertEqual(f"/imports/{self.job_id}/audit?applied=0", second.headers["location"])
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(23, connection.execute(
                "SELECT count(*) FROM candidate_review_drafts"
            ).fetchone()[0])
            self.assertEqual(22, connection.execute(
                """SELECT count(*) FROM candidate_review_drafts
                   WHERE status='approved' AND approval_source='ai_second_pass'"""
            ).fetchone()[0])
            self.assertEqual("pending", connection.execute(
                "SELECT status FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone()[0])
            connection.execute(
                "UPDATE import_jobs SET status='completed' WHERE id=?", (self.job_id,)
            )
            connection.commit()
        completed_page = self.client.get(f"/imports/{self.job_id}/audit")
        self.assertEqual(200, completed_page.status_code)
        self.assertIn("独立视觉二审完成", completed_page.text)
        self.assertEqual(409, self.client.post(
            f"/imports/{self.job_id}/audit",
            data={"csrf_token": self.csrf}, follow_redirects=False,
        ).status_code)

    def test_corrected_reaudit_web_is_explicit_get_read_only_and_records_ai_source(self):
        self.client.post(
            f"/imports/{self.job_id}/audit", data={"csrf_token": self.csrf}
        )
        self.client.post(
            f"/imports/{self.job_id}/audit/apply-auto-pass",
            data={"csrf_token": self.csrf},
        )
        candidate = json.loads(
            (self.job_dir / "candidate_questions.json").read_text(encoding="utf-8")
        )["questions"][1]
        edited = dict(candidate)
        edited["stem_markdown"] += " 已按原图修正"
        edited["primary_knowledge_point_code"] = "01.01.01"
        encoded = json.dumps(edited, ensure_ascii=False, separators=(",", ":"))
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE candidate_review_drafts SET edited_json=?,status='draft',version=2
                   WHERE source_question_no='2'""", (encoded,),
            )
            connection.commit()
        single = {
            "import_job_id": self.job_id,
            "auditor": "independent_codex_visual_second_pass",
            "audit_scope": {
                "kind": "candidate_text_vs_verified_single_question_crops",
                "source_pages": edited["source_pages"],
            },
            "question_count": 1,
            "counts": {"auto_pass": 1, "disputed": 0, "human_required": 0},
            "questions": [{
                "source_question_no": "2", "audit_status": "auto_pass",
                "text_match": True, "structure_match": True, "formula_match": True,
                "figure_check": "not_applicable", "knowledge_check": "not_reviewed",
                "issues": [], "suggested_corrections": [], "evidence_page": 1,
                "audit_confidence": "high",
            }],
            "random_sample_recommendation": {"question_nos": [], "reason": "单题复审"},
            "global_findings": [],
        }
        corrected = AuditRunner(single, "corrected-web-fresh")
        self.client.app.state.corrected_audit_runner = corrected
        page = self.client.get(f"/reviews/{self.job_id}/questions/2")
        self.assertIn("修正后重新 AI 复审", page.text)
        self.assertEqual([], corrected.calls)
        endpoint = f"/reviews/{self.job_id}/questions/2/ai-reaudit"
        self.assertEqual(405, self.client.get(endpoint).status_code)
        self.assertEqual(403, self.client.post(endpoint, data={
            "csrf_token": "wrong", "version": "2", "edited_sha256": "0" * 64,
        }).status_code)
        edited_sha = hashlib.sha256(json.dumps(
            edited, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        response = self.client.post(endpoint, data={
            "csrf_token": self.csrf, "version": "2", "edited_sha256": edited_sha,
        }, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual(1, len(corrected.calls))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(("approved", 3, "ai_second_pass"), connection.execute(
                """SELECT status,version,approval_source FROM candidate_review_drafts
                   WHERE source_question_no='2'"""
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
