import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import src.learning.correction_dataset as correction_dataset_module
from src.database.initialize import initialize_database
from src.learning.correction_dataset import (
    CorrectionDatasetError,
    MAX_DATASET_BYTES,
    harvest_completed_job,
    load_few_shot_examples,
)


class CorrectionDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.job_dir = self.private / "processing" / "import_job_1"
        self.job_dir.mkdir(parents=True)
        self.db = self.root / "question-bank.db"
        initialize_database(self.db).close()
        candidate = {
            "import_job_id": 1,
            "source_paper_id": 1,
            "question_count": 1,
            "questions": [{
                "source_question_no": "1",
                "stem_markdown": "模型最初转写（有误）",
                "question_type_code": "solution",
                "options": [],
                "subquestions": [],
                "answer_markdown": "",
                "source_pages": [1],
                "primary_knowledge_point_code": "01.01.06",
                "related_knowledge_point_codes": [],
            }],
        }
        audit = {
            "import_job_id": 1,
            "question_count": 1,
            "questions": [{
                "source_question_no": "1",
                "audit_status": "human_required",
                "issues": ["题干漏字"],
                "suggested_corrections": ["补全题干"],
                "audit_confidence": "high",
            }],
        }
        self._write("candidate_questions.json", candidate)
        self._write("ai_audit.json", audit)
        self._write("frozen_crop_reviews/old.json", {
            "import_job_id": 1,
            "reviewer_run_id": "crop-old",
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "questions": [{"question_no": 1, "status": "needs_recrop", "warnings": ["下边界缺失"]}],
        })
        self._write("crop_ai_review.json", {
            "import_job_id": 1,
            "reviewer_run_id": "crop-final",
            "reviewed_at": "2026-01-02T00:00:00+00:00",
            "questions": [{"question_no": 1, "status": "ai_review_passed", "warnings": []}],
        })
        with sqlite3.connect(self.db) as connection:
            point_id = connection.execute(
                "SELECT id FROM knowledge_points WHERE code='01.01.06'"
            ).fetchone()[0]
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES(?,1,'sample.pdf','raw_papers/TJ/2026/sample.pdf',
                          'TJ',2026,'YK','纠错样本测试卷')""",
                ("a" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES(1,?,'completed')",
                (source_id,),
            )
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,region_code,exam_type_code,
                    question_type_code,primary_knowledge_point_id,content_hash,
                    answer_status,ocr_review_status,formula_review_status,
                    figure_review_status,answer_review_status,tag_review_status)
                   VALUES('Q-AUTH-001','最终权威题干','TJ','YK','solution',?,?,
                          'missing','passed','passed','not_applicable','passed','passed')""",
                (point_id, "b" * 64),
            ).lastrowid
            connection.execute(
                """INSERT INTO question_sources
                   (question_id,source_paper_id,import_job_id,source_question_no,
                    source_pages_json)
                   VALUES(?,?,1,'1','[1]')""",
                (question_id, source_id),
            )
            source_snapshot = json.dumps(candidate["questions"][0], ensure_ascii=False)
            final_draft = {**candidate["questions"][0], "stem_markdown": "最终权威题干"}
            edited = json.dumps(final_draft, ensure_ascii=False)
            connection.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,status,version,reviewed_at,
                    approval_source,approval_evidence_json)
                   VALUES(1,'1',?,?,?,'approved',2,?,'human',?)""",
                (
                    "c" * 64, source_snapshot, edited,
                    "2026-01-03T00:00:00+00:00",
                    '{"method":"workbench","reviewed_at":"2026-01-03T00:00:00+00:00"}',
                ),
            )
            connection.execute(
                """INSERT INTO candidate_knowledge_classifications
                   (import_job_id,source_question_no,approved_draft_version,
                    edited_sha256,primary_knowledge_point_code,
                    related_knowledge_point_codes_json,classifier,reviewer,
                    approval_source,classifier_run_id,evidence_sha256,reason,created_at)
                   VALUES(1,'1',2,?,'01.01.06','[]','muse','teacher','human',
                          'classification-run-1',?,'人工最终确认',?)""",
                ("d" * 64, "e" * 64, "2026-01-03T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO candidate_knowledge_classification_drafts
                   (import_job_id,source_question_no,approved_draft_version,
                    edited_sha256,proposal_primary_code,proposal_related_codes_json,
                    proposal_confidence,proposal_reason,verifier_primary_code,
                    verifier_related_codes_json,verifier_confidence,verifier_reason,
                    final_primary_code,final_related_codes_json,final_reason,status,
                    approval_source,reviewed_at,created_at,updated_at)
                   VALUES(1,'1',2,?,'01.01.07','[]','medium','模型初判',
                          '01.01.06','[]','high','复核意见','01.01.06','[]',
                          '人工最终确认','approved','human',?,?,?)""",
                ("d" * 64, "2026-01-03T00:00:00+00:00",
                 "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:00+00:00"),
            )
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, relative, value):
        path = self.job_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _rows(self):
        path = self.private / "learning" / "correction_samples.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_harvests_three_authoritative_types_and_is_idempotent(self):
        database_before = self.db.read_bytes()
        first = harvest_completed_job(self.db, self.private, 1)
        second = harvest_completed_job(self.db, self.private, 1)

        self.assertEqual(3, first["added"])
        self.assertEqual(0, second["added"])
        rows = self._rows()
        self.assertEqual(
            ["crop_review", "knowledge_classification", "transcription_review"],
            sorted(row["task_type"] for row in rows),
        )
        required = {
            "schema_version", "sample_id", "task_type", "job_id", "question_no",
            "source_refs", "model_input", "correction", "final_target",
            "provenance", "quality_gate", "created_at",
        }
        self.assertTrue(all(required <= set(row) for row in rows))
        self.assertTrue(all(row["quality_gate"]["passed"] for row in rows))
        self.assertNotIn(str(self.root), json.dumps(rows, ensure_ascii=False))
        self.assertEqual({
            "94608e89f90e0cf022029d198123423a2e5fef32aebf971a2cc68b3751e80572",
            "36f7c65129a4738fc65cf30c344f9b2d00bc76ce6a4c1571a4690101ca330a3a",
            "91cb6d8c9930d42aaf6be608621386f006b297caf444e30be9bca7ca16787bbe",
        }, {row["sample_id"] for row in rows})
        self.assertEqual(database_before, self.db.read_bytes())

    def test_changed_input_evidence_creates_new_sample_id(self):
        harvest_completed_job(self.db, self.private, 1)
        audit = json.loads((self.job_dir / "ai_audit.json").read_text(encoding="utf-8"))
        audit["questions"][0]["suggested_corrections"] = ["补全题干和标点"]
        self._write("ai_audit.json", audit)

        result = harvest_completed_job(self.db, self.private, 1)

        self.assertEqual(1, result["added"])
        transcription_ids = {
            row["sample_id"] for row in self._rows()
            if row["task_type"] == "transcription_review"
        }
        self.assertEqual(2, len(transcription_ids))

    def test_few_shot_filters_quality_task_and_limit_stably(self):
        harvest_completed_job(self.db, self.private, 1)

        first = load_few_shot_examples(
            self.private, task_type="knowledge_classification", limit=1,
        )
        second = load_few_shot_examples(
            self.private, task_type="knowledge_classification", limit=1,
        )

        self.assertEqual(first, second)
        self.assertEqual(1, len(first))
        self.assertEqual("knowledge_classification", first[0]["task_type"])
        self.assertFalse(any("image" in key for key in first[0]))
        self.assertEqual([], load_few_shot_examples(
            self.private, task_type="unknown", limit=3,
        ))
        with self.assertRaises(CorrectionDatasetError):
            load_few_shot_examples(self.private, task_type="crop_review", limit=0)

        rows = self._rows()
        crop = next(row for row in rows if row["task_type"] == "crop_review")
        crop["quality_gate"]["passed"] = False
        dataset = self.private / "learning" / "correction_samples.jsonl"
        dataset.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        self.assertEqual([], load_few_shot_examples(
            self.private, task_type="crop_review", limit=3,
        ))

    def test_concurrent_harvest_has_one_append_and_no_duplicates(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: harvest_completed_job(self.db, self.private, 1), range(2)
            ))

        self.assertEqual([0, 3], sorted(result["added"] for result in results))
        self.assertEqual(3, len(self._rows()))
        self.assertEqual(3, len({row["sample_id"] for row in self._rows()}))

    def test_corrupt_jsonl_fails_closed(self):
        dataset = self.private / "learning" / "correction_samples.jsonl"
        dataset.parent.mkdir(parents=True)
        dataset.write_text('{"schema_version":1}\nnot-json\n', encoding="utf-8")

        with self.assertRaisesRegex(CorrectionDatasetError, "损坏"):
            load_few_shot_examples(self.private, task_type="crop_review", limit=1)
        with self.assertRaisesRegex(CorrectionDatasetError, "损坏"):
            harvest_completed_job(self.db, self.private, 1)

    def test_total_file_size_limit_fails_closed(self):
        dataset = self.private / "learning" / "correction_samples.jsonl"
        dataset.parent.mkdir(parents=True)
        dataset.write_bytes(b"x" * (MAX_DATASET_BYTES + 1))

        with self.assertRaisesRegex(CorrectionDatasetError, "大小限制"):
            load_few_shot_examples(self.private, task_type="crop_review", limit=1)

    def test_rejects_path_traversal_and_symlink_targets(self):
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private / ".." / "escape", 1)

        learning = self.private / "learning"
        learning.mkdir(parents=True)
        target = self.root / "outside.jsonl"
        target.write_text("", encoding="utf-8")
        (learning / "correction_samples.jsonl").symlink_to(target)
        with self.assertRaisesRegex(CorrectionDatasetError, "符号链接"):
            harvest_completed_job(self.db, self.private, 1)

    def test_non_completed_job_is_rejected_without_writing(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE import_jobs SET status='failed' WHERE id=1")
            connection.commit()

        with self.assertRaisesRegex(CorrectionDatasetError, "completed"):
            harvest_completed_job(self.db, self.private, 1)
        self.assertFalse((self.private / "learning").exists())

    def test_sensitive_keys_and_absolute_paths_are_never_persisted(self):
        candidate_path = self.job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["questions"][0]["api_key"] = "sk-test-secret"
        candidate["questions"][0]["review_notes"] = [str(self.root / "secret.png")]
        self._write("candidate_questions.json", candidate)

        harvest_completed_job(self.db, self.private, 1)

        raw = (self.private / "learning" / "correction_samples.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("api_key", raw)
        self.assertNotIn("sk-test-secret", raw)
        self.assertNotIn(str(self.root), raw)

    def test_math_text_beginning_with_slash_is_preserved(self):
        candidate_path = self.job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["questions"][0]["review_notes"] = ["/x + 1", "/frac{1}{2}"]
        self._write("candidate_questions.json", candidate)

        harvest_completed_job(self.db, self.private, 1)

        transcription = next(
            row for row in self._rows() if row["task_type"] == "transcription_review"
        )
        self.assertEqual(
            ["/x + 1", "/frac{1}{2}"],
            transcription["model_input"]["candidate"]["review_notes"],
        )

    def test_embedded_real_local_path_is_redacted_without_dropping_text(self):
        candidate_path = self.job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        local_path = str(self.root / "private" / "scan.png")
        candidate["questions"][0]["review_notes"] = f"证据位于 {local_path} 请复核 /x"
        self._write("candidate_questions.json", candidate)

        harvest_completed_job(self.db, self.private, 1)

        raw = (self.private / "learning" / "correction_samples.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(local_path, raw)
        self.assertIn("证据位于 [redacted_absolute_path] 请复核 /x", raw)

    def test_rejects_symlinked_private_root_and_learning_directory(self):
        linked_private = self.root / "private-link"
        linked_private.symlink_to(self.private, target_is_directory=True)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, linked_private, 1)

        learning_target = self.root / "outside-learning"
        learning_target.mkdir()
        (self.private / "learning").symlink_to(learning_target, target_is_directory=True)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

    def test_rejects_symlinked_processing_job_and_frozen_directories(self):
        processing = self.private / "processing"
        real_processing = self.private / "processing-real"
        processing.rename(real_processing)
        processing.symlink_to(real_processing, target_is_directory=True)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

        processing.unlink()
        real_processing.rename(processing)
        real_job = processing / "import_job_1-real"
        self.job_dir.rename(real_job)
        self.job_dir.symlink_to(real_job, target_is_directory=True)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

        self.job_dir.unlink()
        real_job.rename(self.job_dir)
        frozen = self.job_dir / "frozen_crop_reviews"
        real_frozen = self.job_dir / "frozen-real"
        frozen.rename(real_frozen)
        frozen.symlink_to(real_frozen, target_is_directory=True)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

    def test_rejects_hardlinked_job_artifact(self):
        artifact = self.job_dir / "candidate_questions.json"
        outside = self.root / "outside-candidate.json"
        artifact.rename(outside)
        os.link(outside, artifact)

        with self.assertRaisesRegex(CorrectionDatasetError, "工件"):
            harvest_completed_job(self.db, self.private, 1)

    def test_artifact_has_no_check_then_path_read_window(self):
        original = Path.read_bytes
        artifact = self.job_dir / "candidate_questions.json"
        replacement = artifact.with_name("replacement.json")
        replacement.write_text('{"questions":[]}', encoding="utf-8")

        def replace_before_path_read(path):
            if path == artifact:
                artifact.unlink()
                replacement.rename(artifact)
            return original(path)

        with mock.patch.object(Path, "read_bytes", replace_before_path_read):
            result = harvest_completed_job(self.db, self.private, 1)

        self.assertEqual(3, result["added"])

    def test_rejects_database_parent_and_target_symlinks(self):
        linked_parent = self.root / "db-parent-link"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(CorrectionDatasetError, "数据库"):
            harvest_completed_job(linked_parent / self.db.name, self.private, 1)

        real_db = self.root / "real-question-bank.db"
        self.db.rename(real_db)
        self.db.symlink_to(real_db)
        with self.assertRaisesRegex(CorrectionDatasetError, "数据库"):
            harvest_completed_job(self.db, self.private, 1)

    def test_database_open_is_pinned_before_sqlite_connect(self):
        real_connect = correction_dataset_module.sqlite3.connect
        replacement = self.root / "replacement.db"
        replacement.write_bytes(b"not a sqlite database")
        seen = []

        def replace_target(database, *args, **kwargs):
            seen.append(str(database))
            self.db.unlink()
            replacement.rename(self.db)
            return real_connect(database, *args, **kwargs)

        with mock.patch.object(
            correction_dataset_module.sqlite3, "connect", side_effect=replace_target,
        ):
            with self.assertRaisesRegex(CorrectionDatasetError, "数据库"):
                harvest_completed_job(self.db, self.private, 1)

        self.assertTrue(seen[0].startswith("file:/dev/fd/"))

    def test_rejects_dataset_and_lock_symlinks_and_hardlinks(self):
        learning = self.private / "learning"
        learning.mkdir()
        outside = self.root / "outside.jsonl"
        outside.write_bytes(b"")
        dataset = learning / "correction_samples.jsonl"
        os.link(outside, dataset)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

        dataset.unlink()
        (learning / ".correction_samples.lock").unlink()
        lock_outside = self.root / "outside.lock"
        lock_outside.write_bytes(b"")
        lock_path = learning / ".correction_samples.lock"
        os.link(lock_outside, lock_path)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

        lock_path.unlink()
        lock_path.symlink_to(lock_outside)
        with self.assertRaises(CorrectionDatasetError):
            harvest_completed_job(self.db, self.private, 1)

    def test_write_verification_failure_does_not_publish_dataset(self):
        with mock.patch.object(
            correction_dataset_module, "_verify_written_payload",
            side_effect=CorrectionDatasetError("注入的写后验证失败"), create=True,
        ):
            with self.assertRaisesRegex(CorrectionDatasetError, "写后验证失败"):
                harvest_completed_job(self.db, self.private, 1)

        self.assertFalse(
            (self.private / "learning" / "correction_samples.jsonl").exists()
        )

    def test_dataset_replacement_race_is_rejected_before_publish(self):
        harvest_completed_job(self.db, self.private, 1)
        audit = json.loads((self.job_dir / "ai_audit.json").read_text(encoding="utf-8"))
        audit["questions"][0]["suggested_corrections"] = ["竞态新修正"]
        self._write("ai_audit.json", audit)
        dataset = self.private / "learning" / "correction_samples.jsonl"
        attacker = dataset.with_name("attacker.jsonl")
        attacker.write_bytes(b"attacker-owned\n")
        original_verify = correction_dataset_module._verify_dataset_name
        swapped = False

        def replace_then_verify(directory_fd, expected):
            nonlocal swapped
            if not swapped:
                dataset.unlink()
                attacker.rename(dataset)
                swapped = True
            return original_verify(directory_fd, expected)

        with mock.patch.object(
            correction_dataset_module, "_verify_dataset_name",
            side_effect=replace_then_verify,
        ):
            with self.assertRaisesRegex(CorrectionDatasetError, "竞态替换"):
                harvest_completed_job(self.db, self.private, 1)

        self.assertEqual(b"attacker-owned\n", dataset.read_bytes())

    def test_lock_replacement_while_waiting_is_rejected(self):
        learning = self.private / "learning"
        learning.mkdir()
        lock_path = learning / ".correction_samples.lock"
        real_flock = correction_dataset_module.fcntl.flock

        def replace_after_lock(descriptor, operation):
            result = real_flock(descriptor, operation)
            lock_path.unlink()
            lock_path.write_bytes(b"replacement")
            return result

        with mock.patch.object(
            correction_dataset_module.fcntl, "flock", side_effect=replace_after_lock,
        ):
            with self.assertRaisesRegex(CorrectionDatasetError, "锁.*替换"):
                harvest_completed_job(self.db, self.private, 1)


if __name__ == "__main__":
    unittest.main()
