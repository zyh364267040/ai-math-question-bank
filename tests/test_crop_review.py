import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.database.initialize import initialize_database
from src.processing.candidate_extractor import CandidateExtractionError, claim_candidate_extraction
from src.processing.crop_review import CropReviewError, record_crop_ai_review
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest
from tests.fixture_factory import create_import_job_fixture


class NeverRunner:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("runner must not be called by claim")


class CropReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.database = self.root / "question-bank.db"
        initialize_database(self.database).close()
        with sqlite3.connect(self.database) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name)
                   VALUES (?,1,'review.pdf','raw_papers/TJ/unknown/review.pdf',
                           'TJ','QT','审核合成卷')""", ("a" * 64,),
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (source_id,),
            ).lastrowid
        self.job_dir = create_import_job_fixture(
            self.private, job_id=self.job_id, source_paper_id=source_id
        )
        (self.job_dir / "candidate_questions.json").unlink()
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        for question in manifest["questions"]:
            question["review_status"] = "pending_ai_review"
            question["warnings"] = []
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        content = manifest_path.read_bytes()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,question_count,processed_pages,codex_run_id,
                    result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                    crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                    completed_at,updated_at)
                   VALUES (?,'completed',23,4,'split-review',?,?,?,?,?,?,
                           CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (self.job_id, "b" * 64, "c" * 64, "a" * 64,
                 hashlib.sha256(content).hexdigest(), manifest["generation_id"],
                 manifest["signature"]),
            )
        self.input_digest = hashlib.sha256(content).hexdigest()
        self.generation_id = manifest["generation_id"]

    def tearDown(self):
        self.temporary.cleanup()

    def payload(self, *, recrop=()):
        return {
            "version": 1,
            "import_job_id": self.job_id,
            "input_generation_id": self.generation_id,
            "input_manifest_sha256": self.input_digest,
            "reviewer_run_id": "vision-review.synthetic-001",
            "questions": [
                {
                    "question_no": number,
                    "status": "needs_recrop" if number in recrop else "ai_review_passed",
                    "warnings": (["下边界可能截断"] if number in recrop else []),
                }
                for number in range(1, 24)
            ],
        }

    def anchors(self):
        with sqlite3.connect(self.database) as connection:
            return connection.execute(
                """SELECT crop_manifest_sha256,crop_generation_id,crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()

    def image_state(self):
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted((self.job_dir / "question_crops").glob("Q*.png"))
        }

    def test_all_pass_publishes_signed_evidence_and_allows_candidate_claim(self):
        images = self.image_state()
        result = record_crop_ai_review(self.database, self.private, self.payload())
        self.assertTrue(result.can_extract_candidates)
        self.assertEqual((23, 0), (result.passed_count, result.needs_recrop_count))
        manifest_bytes = (self.job_dir / "question_crops.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        evidence = json.loads((self.job_dir / "crop_ai_review.json").read_text())
        self.assertTrue(all(q["review_status"] == "ai_review_passed"
                            for q in manifest["questions"]))
        self.assertEqual(self.generation_id, manifest["generation_id"])
        self.assertEqual(self.input_digest, evidence["input_manifest_sha256"])
        self.assertEqual(manifest["signature"], evidence["output_manifest_signature"])
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(),
                         evidence["output_manifest_sha256"])
        self.assertEqual(self.anchors(), (
            hashlib.sha256(manifest_bytes).hexdigest(), self.generation_id,
            manifest["signature"],
        ))
        self.assertEqual(images, self.image_state())
        claim = claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=NeverRunner(),
        )
        self.assertIsNotNone(claim)
        claim.close()

    def test_existing_crop_warnings_are_preserved_and_idempotent(self):
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["questions"][0]["warnings"] = ["原卷像素中缺少幂指数"]
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        content = manifest_path.read_bytes()
        self.input_digest = hashlib.sha256(content).hexdigest()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE import_question_split_runs SET crop_manifest_sha256=?,
                          crop_manifest_signature=? WHERE import_job_id=?""",
                (self.input_digest, manifest["signature"], self.job_id),
            )

        payload = self.payload()
        first = record_crop_ai_review(self.database, self.private, payload)
        reviewed_manifest = json.loads(manifest_path.read_text())
        evidence = json.loads((self.job_dir / "crop_ai_review.json").read_text())
        self.assertEqual(
            ["原卷像素中缺少幂指数"], reviewed_manifest["questions"][0]["warnings"]
        )
        self.assertEqual(
            ["原卷像素中缺少幂指数"], evidence["questions"][0]["warnings"]
        )
        state = [
            (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (manifest_path, self.job_dir / "crop_ai_review.json")
        ]
        second = record_crop_ai_review(self.database, self.private, payload)
        self.assertEqual(first, second)
        self.assertEqual(
            state,
            [
                (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (manifest_path, self.job_dir / "crop_ai_review.json")
            ],
        )

    def test_partial_recrop_is_recorded_but_candidate_gate_stays_closed(self):
        result = record_crop_ai_review(
            self.database, self.private, self.payload(recrop={2, 7})
        )
        self.assertFalse(result.can_extract_candidates)
        self.assertEqual((21, 2), (result.passed_count, result.needs_recrop_count))
        manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual("needs_recrop", manifest["questions"][1]["review_status"])
        self.assertEqual(["下边界可能截断"], manifest["questions"][1]["warnings"])
        with self.assertRaises(CandidateExtractionError):
            claim_candidate_extraction(
                self.database, self.private, self.job_id, runner=NeverRunner(),
            )

    def test_identical_payload_is_idempotent_zero_write(self):
        payload = self.payload()
        first = record_crop_ai_review(self.database, self.private, payload)
        paths = [self.job_dir / "question_crops.json", self.job_dir / "crop_ai_review.json"]
        state = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        second = record_crop_ai_review(self.database, self.private, copy.deepcopy(payload))
        self.assertEqual(first, second)
        self.assertEqual(state, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])

    def test_requires_exact_ordered_full_coverage_and_bounded_schema(self):
        cases = []
        missing = self.payload(); missing["questions"].pop(); cases.append(missing)
        duplicate = self.payload(); duplicate["questions"][1]["question_no"] = 1; cases.append(duplicate)
        unordered = self.payload(); unordered["questions"].reverse(); cases.append(unordered)
        illegal = self.payload(); illegal["questions"][0]["status"] = "pending_ai_review"; cases.append(illegal)
        warning = self.payload(); warning["questions"][0]["warnings"] = ["x" * 501]; cases.append(warning)
        reviewer = self.payload(); reviewer["reviewer_run_id"] = "../unsafe"; cases.append(reviewer)
        extra = self.payload(); extra["extra"] = True; cases.append(extra)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(CropReviewError):
                record_crop_ai_review(self.database, self.private, payload)
        self.assertFalse((self.job_dir / "crop_ai_review.json").exists())

    def test_stale_generation_digest_and_tampered_manifest_are_rejected(self):
        for field, value in (
            ("input_generation_id", "f" * 32),
            ("input_manifest_sha256", "e" * 64),
        ):
            payload = self.payload(); payload[field] = value
            with self.subTest(field=field), self.assertRaises(CropReviewError):
                record_crop_ai_review(self.database, self.private, payload)
        path = self.job_dir / "question_crops.json"
        tampered = json.loads(path.read_text())
        tampered["questions"][0]["warnings"] = ["tampered"]
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, self.payload())

    def test_tampered_review_evidence_blocks_candidate_gate_and_resubmission(self):
        payload = self.payload()
        record_crop_ai_review(self.database, self.private, payload)
        path = self.job_dir / "crop_ai_review.json"
        evidence = json.loads(path.read_text())
        evidence["questions"][0]["status"] = "needs_recrop"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, payload)
        with self.assertRaises(CandidateExtractionError):
            claim_candidate_extraction(
                self.database, self.private, self.job_id, runner=NeverRunner(),
            )

    def test_database_failure_restores_old_manifest_and_old_evidence(self):
        first_payload = self.payload(recrop={2})
        record_crop_ai_review(self.database, self.private, first_payload)
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        old_evidence = (self.job_dir / "crop_ai_review.json").read_bytes()
        old_anchors = self.anchors()
        next_payload = self.payload(recrop={3})
        next_payload["input_manifest_sha256"] = old_anchors[0]
        with mock.patch(
            "src.processing.crop_review._commit_database_anchors",
            side_effect=sqlite3.OperationalError("synthetic failure"),
        ), self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, next_payload)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual(old_evidence, (self.job_dir / "crop_ai_review.json").read_bytes())
        self.assertEqual(old_anchors, self.anchors())
        self.assertEqual([], list(self.job_dir.glob(".crop_review.*")))

    def test_crash_before_database_commit_is_recovered_to_old_then_retried(self):
        payload = self.payload()
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        old_anchors = self.anchors()
        with mock.patch(
            "src.processing.crop_review._commit_database_anchors",
            side_effect=SystemExit("synthetic crash"),
        ), self.assertRaises(SystemExit):
            record_crop_ai_review(self.database, self.private, payload)
        self.assertTrue((self.job_dir / ".crop_review.journal").exists())
        self.assertEqual(old_anchors, self.anchors())
        result = record_crop_ai_review(self.database, self.private, payload)
        self.assertTrue(result.can_extract_candidates)
        self.assertNotEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual([], list(self.job_dir.glob(".crop_review.*")))

    def test_crash_after_database_commit_finishes_new_publication_on_retry(self):
        payload = self.payload()
        with mock.patch(
            "src.processing.crop_review._finish_new_files",
            side_effect=SystemExit("synthetic post-commit crash"),
        ), self.assertRaises(SystemExit):
            record_crop_ai_review(self.database, self.private, payload)
        committed_anchors = self.anchors()
        self.assertEqual(
            committed_anchors[0],
            hashlib.sha256((self.job_dir / "question_crops.json").read_bytes()).hexdigest(),
        )
        before = (self.job_dir / "question_crops.json").read_bytes()
        result = record_crop_ai_review(self.database, self.private, payload)
        self.assertTrue(result.can_extract_candidates)
        self.assertEqual(before, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual([], list(self.job_dir.glob(".crop_review.*")))

    def test_symlink_hardlink_and_wrong_split_state_are_rejected(self):
        manifest_path = self.job_dir / "question_crops.json"
        backup = self.job_dir / "manifest-copy"
        os.link(manifest_path, backup)
        with self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, self.payload())
        backup.unlink()
        crop = self.job_dir / "question_crops/Q001.png"
        crop_link = self.job_dir / "crop-copy"
        os.link(crop, crop_link)
        with self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, self.payload())
        crop_link.unlink()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE import_question_split_runs SET status='failed' WHERE import_job_id=?",
                (self.job_id,),
            )
        with self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, self.payload())

    def test_unsafe_existing_review_evidence_is_not_overwritten(self):
        evidence = self.job_dir / "crop_ai_review.json"
        evidence.symlink_to(self.job_dir / "question_crops.json")
        with self.assertRaises(CropReviewError):
            record_crop_ai_review(self.database, self.private, self.payload())
        self.assertTrue(evidence.is_symlink())


if __name__ == "__main__":
    unittest.main()
