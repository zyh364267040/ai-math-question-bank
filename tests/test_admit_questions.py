import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from PIL import Image

from src.database.initialize import initialize_database
from src.importing.admit_questions import (
    AdmissionError,
    _assess,
    _effective_questions,
    _insert_one,
    _job_artifact_lock,
    _load_context,
    _read_stable_artifact,
    _verify_artifact_snapshots,
    admit_questions,
    assess_job,
)
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest
from tests.fixture_factory import create_import_job_fixture


ROOT = Path(__file__).resolve().parents[1]
class AdmitQuestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.job = create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as con:
            source = con.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'paper.pdf','raw_papers/TJ/2025/paper.pdf','TJ',2025,'YK',?)""",
                ("a" * 64, "测试卷"),
            ).lastrowid
            con.execute("INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) VALUES(1,?,1,4,'needs_review')", (source,))
        self._anchor_batch_audit()

    def tearDown(self):
        self.temp.cleanup()

    def _json(self, name):
        path = self.job / name
        return path, json.loads(path.read_text(encoding="utf-8"))

    def _write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if path.name in {"candidate_questions.json", "ai_audit.json", "question_crops.json"}:
            self._anchor_batch_audit()

    def _anchor_batch_audit(self):
        candidate = self.job / "candidate_questions.json"
        audit = self.job / "ai_audit.json"
        crops = self.job / "question_crops.json"
        if not all(path.is_file() for path in (candidate, audit, crops)):
            return
        crop_payload = json.loads(crops.read_text(encoding="utf-8"))
        if "generation_id" not in crop_payload or "signature" not in crop_payload:
            return
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        question_count = candidate_payload["question_count"]
        candidate_raw, audit_raw, crop_raw = (
            candidate.read_bytes(), audit.read_bytes(), crops.read_bytes()
        )
        try:
            with sqlite3.connect(self.db, timeout=0.05) as con:
                con.execute(
                """INSERT INTO import_candidate_audit_runs
                   (import_job_id,status,question_count,processed_questions,codex_run_id,
                    input_candidate_sha256,input_candidate_byte_size,
                    input_crop_generation_id,input_manifest_sha256,input_manifest_signature,
                    output_sha256,output_byte_size,completed_at,updated_at)
                   VALUES(1,'completed',?,?,'admission-fixture-run',?,?,?,?,?,?,?,
                          '2026-07-16T00:00:00+00:00','2026-07-16T00:00:00+00:00')
                   ON CONFLICT(import_job_id) DO UPDATE SET
                     status=excluded.status,question_count=excluded.question_count,
                     processed_questions=excluded.processed_questions,
                     input_candidate_sha256=excluded.input_candidate_sha256,
                     input_candidate_byte_size=excluded.input_candidate_byte_size,
                     input_crop_generation_id=excluded.input_crop_generation_id,
                     input_manifest_sha256=excluded.input_manifest_sha256,
                     input_manifest_signature=excluded.input_manifest_signature,
                     output_sha256=excluded.output_sha256,
                     output_byte_size=excluded.output_byte_size,
                     completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
                (question_count, question_count,
                 hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw),
                 crop_payload["generation_id"], hashlib.sha256(crop_raw).hexdigest(),
                 crop_payload["signature"], hashlib.sha256(audit_raw).hexdigest(),
                    len(audit_raw)),
                )
        except sqlite3.OperationalError:
            pass

    def _mutate_audits(self, mutate):
        path, audit = self._json("ai_audit.json")
        mutate({item["source_question_no"]: item for item in audit["questions"]})
        audit["counts"] = {
            status: sum(item["audit_status"] == status for item in audit["questions"])
            for status in ("auto_pass", "disputed", "human_required")
        }
        self._write(path, audit)

    def _strict_auto_pass(self, item):
        item.update(
            audit_status="auto_pass",
            audit_confidence="high",
            issues=[],
            suggested_corrections=[],
        )

    def _answer_analysis_sha256(self, question):
        payload = {
            "source_question_no": question["source_question_no"],
            "answer_markdown": question.get("answer_markdown", ""),
            "analysis_markdown": question.get("analysis_markdown", ""),
            "subquestions": [
                {
                    "label": subquestion.get("label", ""),
                    "stem_markdown": subquestion.get("stem_markdown", ""),
                    "answer_markdown": subquestion.get("answer_markdown", ""),
                    "analysis_markdown": subquestion.get("analysis_markdown", ""),
                }
                for subquestion in question.get("subquestions", [])
            ],
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _approve_human_draft(self, number="12", mutate=None):
        candidate_path, candidate = self._json("candidate_questions.json")
        original = next(
            question for question in candidate["questions"]
            if question["source_question_no"] == number
        )
        edited = copy.deepcopy(original)
        if mutate is not None:
            mutate(edited)
        snapshot = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
        with sqlite3.connect(self.db) as con:
            con.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,status,reviewed_at,
                    approval_source,approval_evidence_json)
                   VALUES (1,?,?,?,?, 'approved','2026-07-16T10:00:00+08:00',
                           'human',?)""",
                (
                    number,
                    hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                    snapshot,
                    json.dumps(edited, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        {"method": "workbench", "reviewed_at": "2026-07-16T10:00:00+08:00"},
                        separators=(",", ":"),
                    ),
                ),
            )
        return original, edited

    def _update_draft(self, **values):
        assignments = ",".join(f"{name}=?" for name in values)
        with sqlite3.connect(self.db) as con:
            con.execute(
                f"UPDATE candidate_review_drafts SET {assignments} "
                "WHERE import_job_id=1 AND source_question_no='12'",
                tuple(values.values()),
            )

    def _make_existing_approval(self, number):
        self._approve_human_draft(number)
        reviewed_at = "2026-07-16T10:00:00+08:00"
        evidence = json.dumps(
            {"method": "existing_approval", "reviewed_at": reviewed_at},
            separators=(",", ":"),
        )
        with sqlite3.connect(self.db) as con:
            con.execute(
                """UPDATE candidate_review_drafts
                   SET reviewed_at=?,approval_evidence_json=?
                   WHERE import_job_id=1 AND source_question_no=?""",
                (reviewed_at, evidence, number),
            )

    def _write_signed_crop_manifest(self):
        path, signed = self._json("question_crops.json")
        payload = {key: value for key, value in signed.items() if key != "signature"}
        signed = sign_manifest(load_hmac_key(self.job), payload)
        self._write(path, signed)
        return path, signed

    def _resign_crop_manifest(self, manifest):
        payload = {key: value for key, value in manifest.items() if key != "signature"}
        return sign_manifest(load_hmac_key(self.job), payload)

    def _mark_answer_analysis_reviewed(self, number, question, *, human_required=True):
        def mutate(by_no):
            changes = {
                "answer_analysis_sha256": self._answer_analysis_sha256(question),
            }
            if self._question_has_markdown(question, "answer_markdown"):
                changes["answer_status"] = "passed"
            if self._question_has_markdown(question, "analysis_markdown"):
                changes["analysis_status"] = "passed"
            if human_required:
                changes.update(
                    audit_status="human_required",
                    audit_confidence="medium",
                    issues=["需要人工确认"],
                    suggested_corrections=["核对人工审核内容"],
                )
            by_no[number].update(changes)

        self._mutate_audits(mutate)

    def _prepare_q22_with_reviewed_subquestion_answer(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(
            item for item in candidate["questions"]
            if item["source_question_no"] == "22"
        )
        question["subquestions"][0]["answer_markdown"] = "$a=2$"
        question["subquestions"][0]["analysis_markdown"] = "先化简再求值。"
        self._write(candidate_path, candidate)
        self._mark_answer_analysis_reviewed("22", question)
        return question

    @staticmethod
    def _question_has_markdown(question, field):
        return bool(str(question.get(field, "")).strip()) or any(
            bool(str(subquestion.get(field, "")).strip())
            for subquestion in question.get("subquestions", [])
        )

    def test_assessment_finds_22_eligible_and_excludes_q12(self):
        report = assess_job(self.db, self.private, 1)
        self.assertEqual(22, len(report.eligible))
        self.assertEqual(["12"], [item.question_no for item in report.ineligible])
        self.assertIn("human_required", report.ineligible[0].reasons)

    def test_human_approved_candidate_is_eligible_and_first_admission_uses_edited_json(self):
        _, approved = self._approve_human_draft(
            mutate=lambda question: question.update(
                stem_markdown="人工审核确认后的第12题题干",
                options=[
                    {"code": "A", "content": "人工选项甲"},
                    {"code": "B", "content": "人工选项乙"},
                ],
                primary_knowledge_point_code="01.01.07",
                related_knowledge_point_codes=["01.01.06"],
            )
        )

        report = assess_job(self.db, self.private, 1)
        self.assertIn("12", [item.question_no for item in report.eligible])

        result = admit_questions(self.db, self.private, 1)
        self.assertEqual((23, 0, 23, 0), (
            result.inserted, result.already_present, result.eligible, result.ineligible,
        ))
        with sqlite3.connect(self.db) as con:
            question = con.execute(
                """SELECT q.stem_markdown,q.question_type_code,k.code
                   FROM questions q JOIN knowledge_points k ON k.id=q.primary_knowledge_point_id
                   WHERE q.source_question_no='12'"""
            ).fetchone()
            options = con.execute(
                """SELECT o.option_code,o.content_markdown
                   FROM question_options o JOIN questions q ON q.id=o.question_id
                   WHERE q.source_question_no='12' ORDER BY o.display_order"""
            ).fetchall()
            related = con.execute(
                """SELECT k.code FROM question_related_knowledge_points r
                   JOIN questions q ON q.id=r.question_id
                   JOIN knowledge_points k ON k.id=r.knowledge_point_id
                   WHERE q.source_question_no='12' ORDER BY r.rowid"""
            ).fetchall()
        self.assertEqual(
            (approved["stem_markdown"], approved["question_type_code"],
             approved["primary_knowledge_point_code"]),
            question,
        )
        self.assertEqual([("A", "人工选项甲"), ("B", "人工选项乙")], options)
        self.assertEqual([("01.01.06",)], related)

    def test_valid_human_approval_overrides_auto_pass_candidate_and_review_source(self):
        _, approved = self._approve_human_draft(
            "1", mutate=lambda question: question.update(
                stem_markdown="人工审核覆盖AI自动准入后的第1题题干"
            )
        )

        result = admit_questions(self.db, self.private, 1)

        self.assertEqual(22, result.inserted)
        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT q.stem_markdown,r.reviewer,r.notes
                   FROM questions q JOIN question_reviews r ON r.question_id=q.id
                   WHERE q.source_question_no='1' AND r.review_item='usability'"""
            ).fetchone()
        self.assertEqual(approved["stem_markdown"], row[0])
        self.assertEqual("human", row[1])
        self.assertIn("人工审核通过", row[2])

    def test_nonapproved_drafts_do_not_obscure_auto_pass_candidate(self):
        for status in ("pending", "draft", "needs_fix"):
            with self.subTest(status=status):
                with sqlite3.connect(self.db) as con:
                    con.execute("PRAGMA foreign_keys=ON")
                    con.execute("DELETE FROM questions")
                    con.execute("DELETE FROM candidate_review_drafts")
                self._approve_human_draft(
                    "1", mutate=lambda question: question.update(
                        stem_markdown="尚未批准、不得覆盖AI的题干"
                    )
                )
                with sqlite3.connect(self.db) as con:
                    con.execute(
                        """UPDATE candidate_review_drafts SET status=?
                           WHERE import_job_id=1 AND source_question_no='1'""",
                        (status,),
                    )

                report = assess_job(self.db, self.private, 1)
                q1 = next(item for item in report.ineligible if item.question_no == "1")
                self.assertIn("human_approval_status_invalid", q1.reasons)

    def test_human_draft_status_must_be_approved(self):
        self._approve_human_draft()
        self._update_draft(status="draft")

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("human_approval_status_invalid", q12.reasons)

    def test_human_draft_approval_source_must_be_human(self):
        self._approve_human_draft()
        self._update_draft(approval_source="ai_second_pass")

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("ai_approval_provenance_invalid", q12.reasons)

    def test_human_draft_requires_reviewed_at_and_valid_approval_evidence(self):
        self._approve_human_draft()
        invalid_updates = (
            {"reviewed_at": None},
            {"approval_evidence_json": None},
            {"approval_evidence_json": "{broken"},
        )
        for values in invalid_updates:
            with self.subTest(values=values):
                with sqlite3.connect(self.db) as con:
                    con.execute("PRAGMA ignore_check_constraints=ON")
                    assignments = ",".join(f"{name}=?" for name in values)
                    con.execute(
                        f"UPDATE candidate_review_drafts SET {assignments} "
                        "WHERE import_job_id=1 AND source_question_no='12'",
                        tuple(values.values()),
                    )

                report = assess_job(self.db, self.private, 1)

                q12 = next(item for item in report.ineligible if item.question_no == "12")
                self.assertIn("human_approval_evidence_invalid", q12.reasons)
                self._update_draft(
                    reviewed_at="2026-07-16T10:00:00+08:00",
                    approval_evidence_json=(
                        '{"method":"workbench",'
                        '"reviewed_at":"2026-07-16T10:00:00+08:00"}'
                    ),
                )

    def test_human_approval_evidence_requires_exact_workbench_schema(self):
        self._approve_human_draft()
        invalid_evidence = (
            {"method": "unknown", "reviewed_at": "2026-07-16T10:00:00+08:00"},
            {"reviewed_at": "2026-07-16T10:00:00+08:00"},
            {"method": "workbench", "reviewed_at": "2026-07-16T10:00:01+08:00"},
            {
                "method": "workbench_quick",
                "reviewed_at": "2026-07-16T10:00:00+08:00",
                "actor": "forged-extra-field",
            },
        )
        for evidence in invalid_evidence:
            with self.subTest(evidence=evidence):
                self._update_draft(approval_evidence_json=json.dumps(
                    evidence, ensure_ascii=False, separators=(",", ":")
                ))

                report = assess_job(self.db, self.private, 1)

                q12 = next(item for item in report.ineligible if item.question_no == "12")
                self.assertIn("human_approval_evidence_invalid", q12.reasons)

    def test_human_approval_reviewed_at_requires_timezone_aware_iso8601(self):
        invalid_times = ("not-a-time", "2026-07-16T10:00:00")
        for reviewed_at in invalid_times:
            with self.subTest(reviewed_at=reviewed_at):
                if self._draft_exists("12"):
                    with sqlite3.connect(self.db) as con:
                        con.execute("DELETE FROM candidate_review_drafts")
                self._approve_human_draft()
                self._update_draft(
                    reviewed_at=reviewed_at,
                    approval_evidence_json=json.dumps(
                        {"method": "workbench", "reviewed_at": reviewed_at},
                        separators=(",", ":"),
                    ),
                )

                report = assess_job(self.db, self.private, 1)

                q12 = next(item for item in report.ineligible if item.question_no == "12")
                self.assertIn("human_approval_evidence_invalid", q12.reasons)

    def test_workbench_approval_time_far_in_the_future_is_rejected(self):
        reviewed_at = "2099-01-01T00:00:00+08:00"
        self._approve_human_draft()
        self._update_draft(
            reviewed_at=reviewed_at,
            approval_evidence_json=json.dumps(
                {"method": "workbench", "reviewed_at": reviewed_at},
                separators=(",", ":"),
            ),
        )

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("human_approval_evidence_invalid", q12.reasons)

    def test_legacy_existing_approval_accepts_old_timezone_aware_time(self):
        admit_questions(self.db, self.private, 1)
        self._make_existing_approval("1")
        reviewed_at = "2018-01-01T00:00:00+08:00"
        evidence = json.dumps(
            {"method": "existing_approval", "reviewed_at": reviewed_at},
            separators=(",", ":"),
        )
        with sqlite3.connect(self.db) as con:
            con.execute(
                """UPDATE candidate_review_drafts
                   SET reviewed_at=?,approval_evidence_json=?
                   WHERE import_job_id=1 AND source_question_no='1'""",
                (reviewed_at, evidence),
            )

        repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((0, 22, 22, 1), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))

    def test_forged_human_approval_on_auto_pass_candidate_fails_closed(self):
        self._approve_human_draft("1")
        with sqlite3.connect(self.db) as con:
            con.execute(
                """UPDATE candidate_review_drafts
                   SET approval_evidence_json='{"method":"forged","reviewed_at":"2026-07-16T10:00:00+08:00"}'
                   WHERE import_job_id=1 AND source_question_no='1'"""
            )

        report = assess_job(self.db, self.private, 1)

        q1 = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("human_approval_evidence_invalid", q1.reasons)

    def test_human_draft_must_be_bound_to_current_candidate_hash_and_snapshot(self):
        original, _ = self._approve_human_draft()
        invalid_updates = (
            {"source_candidate_sha256": "0" * 64},
            {"source_snapshot_json": json.dumps(
                {**original, "stem_markdown": "伪造的旧快照"},
                ensure_ascii=False,
                separators=(",", ":"),
            )},
        )
        for values in invalid_updates:
            with self.subTest(values=values):
                self._update_draft(**values)

                report = assess_job(self.db, self.private, 1)

                q12 = next(item for item in report.ineligible if item.question_no == "12")
                self.assertIn("human_approval_source_binding_invalid", q12.reasons)
                candidate_path, _ = self._json("candidate_questions.json")
                self._update_draft(
                    source_candidate_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                    source_snapshot_json=json.dumps(
                        original, ensure_ascii=False, separators=(",", ":")
                    ),
                )

    def test_human_approved_edited_json_cannot_change_question_number(self):
        self._approve_human_draft(
            mutate=lambda question: question.update(source_question_no="99")
        )

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("human_approval_question_identity_invalid", q12.reasons)

    def test_human_approved_damaged_edited_json_is_rejected(self):
        self._approve_human_draft()
        self._update_draft(edited_json="{broken")

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("human_approval_edited_json_invalid", q12.reasons)

    def test_human_approved_structurally_damaged_edited_json_is_rejected_safely(self):
        mutations = (
            lambda question: question.update(related_knowledge_point_codes=None),
            lambda question: question.update(options="broken"),
            lambda question: question.update(subquestions=None),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with sqlite3.connect(self.db) as con:
                    con.execute("DELETE FROM candidate_review_drafts")
                self._approve_human_draft(mutate=mutate)

                report = assess_job(self.db, self.private, 1)

                q12 = next(item for item in report.ineligible if item.question_no == "12")
                self.assertIn("human_approval_edited_json_invalid", q12.reasons)
                result = admit_questions(self.db, self.private, 1)
                self.assertEqual((22, 1), (result.eligible, result.ineligible))

    def test_human_approved_soft_deleted_candidate_is_rejected(self):
        self._approve_human_draft()
        self._update_draft(
            deleted_at="2026-07-16T11:00:00+08:00",
            deletion_reason="unneeded",
        )

        report = assess_job(self.db, self.private, 1)
        result = admit_questions(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("candidate_deleted", q12.reasons)
        self.assertEqual((22, 1), (result.eligible, result.ineligible))
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM question_sources WHERE source_question_no='12'"
            ).fetchone())

    def test_human_approved_content_still_requires_valid_type_and_knowledge(self):
        invalid_edits = (
            ({"question_type_code": "missing_type"}, "invalid_question_type"),
            ({"primary_knowledge_point_code": "missing.point"}, "missing_knowledge_point"),
            ({"related_knowledge_point_codes": ["missing.point"]}, "missing_knowledge_point"),
        )
        for changes, expected_reason in invalid_edits:
            with self.subTest(changes=changes):
                if self._draft_exists("12"):
                    with sqlite3.connect(self.db) as con:
                        con.execute("DELETE FROM candidate_review_drafts")
                self._approve_human_draft(
                    mutate=lambda question, changes=changes: question.update(changes)
                )

                report = assess_job(self.db, self.private, 1)

                q12 = next(item for item in report.ineligible if item.question_no == "12")
                self.assertIn(expected_reason, q12.reasons)
                with self.assertRaisesRegex(AdmissionError, expected_reason):
                    admit_questions(self.db, self.private, 1)

    def test_human_approved_choice_type_requires_safe_option_structure(self):
        self._approve_human_draft(mutate=lambda question: question.update(
            options=[{"code": "A", "content": "唯一选项"}]
        ))

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("human_approval_edited_json_invalid", q12.reasons)

    def _draft_exists(self, number):
        with sqlite3.connect(self.db) as con:
            return con.execute(
                """SELECT 1 FROM candidate_review_drafts
                   WHERE import_job_id=1 AND source_question_no=?""",
                (number,),
            ).fetchone() is not None

    def test_human_approval_cannot_clear_figure_required_to_bypass_figure_gate(self):
        self._mutate_audits(lambda by_no: by_no["3"].update(
            audit_status="human_required",
            audit_confidence="medium",
            issues=["需要人工确认"],
            suggested_corrections=["核对配图"],
        ))
        self._approve_human_draft(
            "3", mutate=lambda question: question.update(figure_required=False)
        )
        figure_path, figures = self._json("figure_assets.json")
        figures["assets"] = [item for item in figures["assets"] if item["question_no"] != "3"]
        self._write(figure_path, figures)

        report = assess_job(self.db, self.private, 1)
        with self.assertRaisesRegex(AdmissionError, "missing_approved_figure"):
            admit_questions(self.db, self.private, 1)

        q3 = next(item for item in report.ineligible if item.question_no == "3")
        self.assertIn("human_approval_immutable_fields_invalid", q3.reasons)
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM question_sources WHERE source_question_no='3'"
            ).fetchone())

    def test_human_approved_candidate_still_requires_crop_and_required_figure(self):
        self._mutate_audits(lambda by_no: by_no["3"].update(
            audit_status="human_required",
            audit_confidence="medium",
            issues=["需要人工确认"],
            suggested_corrections=["核对配图"],
        ))
        self._approve_human_draft("3")
        figure_path, figures = self._json("figure_assets.json")
        figures["assets"] = [item for item in figures["assets"] if item["question_no"] != "3"]
        self._write(figure_path, figures)

        with self.assertRaisesRegex(AdmissionError, "missing_approved_figure"):
            admit_questions(self.db, self.private, 1)

        crop_path, crops = self._json("question_crops.json")
        crops["questions"][2]["review_status"] = "pending_ai_review"
        self._write(crop_path, crops)
        with self.assertRaises(AdmissionError):
            admit_questions(self.db, self.private, 1)

    def test_human_approval_does_not_bypass_answer_or_analysis_audit(self):
        self._approve_human_draft(mutate=lambda question: question.update(
            answer_markdown="$x=12$",
            analysis_markdown="未经答案解析审核的人工草稿内容。",
        ))

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("answer_status_not_passed", q12.reasons)
        self.assertIn("analysis_status_not_passed", q12.reasons)
        self.assertIn("answer_analysis_sha256_mismatch", q12.reasons)
        with self.assertRaises(AdmissionError):
            admit_questions(self.db, self.private, 1)

    def test_human_approval_cannot_clear_reviewed_parent_answer(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        original = next(
            question for question in candidate["questions"]
            if question["source_question_no"] == "12"
        )
        original["answer_markdown"] = "$x=12$"
        self._write(candidate_path, candidate)
        self._mark_answer_analysis_reviewed("12", original)
        self._approve_human_draft(mutate=lambda question: question.update(
            answer_markdown=""
        ))

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("answer_analysis_sha256_mismatch", q12.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_human_approval_cannot_clear_reviewed_parent_analysis(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        original = next(
            question for question in candidate["questions"]
            if question["source_question_no"] == "12"
        )
        original["analysis_markdown"] = "原卷中经过审核的解析。"
        self._write(candidate_path, candidate)
        self._mark_answer_analysis_reviewed("12", original)
        self._approve_human_draft(mutate=lambda question: question.update(
            analysis_markdown=""
        ))

        report = assess_job(self.db, self.private, 1)

        q12 = next(item for item in report.ineligible if item.question_no == "12")
        self.assertIn("answer_analysis_sha256_mismatch", q12.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_human_approval_cannot_delete_subquestion_with_reviewed_answer(self):
        self._prepare_q22_with_reviewed_subquestion_answer()
        self._approve_human_draft("22", mutate=lambda question: question.update(
            subquestions=question["subquestions"][1:]
        ))

        report = assess_job(self.db, self.private, 1)

        q22 = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", q22.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_human_approval_cannot_reorder_reviewed_answer_subquestions(self):
        self._prepare_q22_with_reviewed_subquestion_answer()

        def reorder(question):
            question["subquestions"][0], question["subquestions"][1] = (
                question["subquestions"][1], question["subquestions"][0]
            )

        self._approve_human_draft("22", mutate=reorder)

        report = assess_job(self.db, self.private, 1)

        q22 = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", q22.reasons)

    def test_human_approval_cannot_change_reviewed_answer_subquestion_stem(self):
        self._prepare_q22_with_reviewed_subquestion_answer()
        self._approve_human_draft("22", mutate=lambda question: question[
            "subquestions"
        ][0].update(stem_markdown="人工改动了已绑定答案的小问题干"))

        report = assess_job(self.db, self.private, 1)

        q22 = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", q22.reasons)

    def test_question_review_distinguishes_human_approval_and_keeps_evidence(self):
        self._approve_human_draft()

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT r.reviewer,r.reviewed_at,r.notes
                   FROM question_reviews r JOIN questions q ON q.id=r.question_id
                   WHERE q.source_question_no='12' AND r.review_item='usability'"""
            ).fetchone()
        self.assertEqual("human", row[0])
        self.assertEqual("2026-07-16T10:00:00+08:00", row[1])
        self.assertIn("人工审核通过", row[2])
        self.assertIn("草稿版本=1", row[2])
        self.assertIn("候选源SHA256=", row[2])
        self.assertIn('批准证据={"method":"workbench"', row[2])

    def test_human_question_review_binds_version_and_canonical_edited_sha256(self):
        _, approved = self._approve_human_draft(mutate=lambda question: question.update(
            stem_markdown="需要被稳定散列绑定的人工获批题干",
            options=[
                {"code": "A", "content": "获批选项甲"},
                {"code": "B", "content": "获批选项乙"},
            ],
        ))
        self._update_draft(version=7)
        canonical = json.dumps(
            approved, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        expected_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT q.stem_markdown,r.notes
                   FROM questions q JOIN question_reviews r ON r.question_id=q.id
                   WHERE q.source_question_no='12' AND r.review_item='usability'"""
            ).fetchone()
        self.assertEqual(approved["stem_markdown"], row[0])
        self.assertIn("草稿版本=7", row[1])
        self.assertIn(f"获批内容SHA256={expected_sha256}", row[1])

    def test_existing_22_plus_one_human_approval_inserts_only_one_and_is_idempotent(self):
        initial = admit_questions(self.db, self.private, 1)
        self.assertEqual((22, 0), (initial.inserted, initial.already_present))
        self._approve_human_draft(
            mutate=lambda question: question.update(
                stem_markdown="人工审核后补入的第12题"
            )
        )

        supplemented = admit_questions(self.db, self.private, 1)
        repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((1, 22, 23, 0), (
            supplemented.inserted,
            supplemented.already_present,
            supplemented.eligible,
            supplemented.ineligible,
        ))
        self.assertEqual((0, 23, 23, 0), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(23, con.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(23, con.execute(
                "SELECT count(DISTINCT question_code) FROM questions"
            ).fetchone()[0])
            self.assertEqual(1, con.execute(
                "SELECT count(*) FROM question_sources WHERE source_question_no='12'"
            ).fetchone()[0])

    def test_legacy_existing_approvals_count_only_exact_formal_questions_as_present(self):
        initial = admit_questions(self.db, self.private, 1)
        self.assertEqual((22, 0), (initial.inserted, initial.already_present))
        for number in ("1", "5", "9"):
            self._make_existing_approval(number)

        with mock.patch(
            "src.importing.admit_questions._insert_one", wraps=_insert_one
        ) as insert_mock:
            repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((0, 22, 22, 1), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))
        insert_mock.assert_not_called()

    def test_legacy_existing_approval_never_rebuilds_missing_formal_question(self):
        admit_questions(self.db, self.private, 1)
        self._make_existing_approval("1")
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("""DELETE FROM questions WHERE question_code=?""", (
                "Q-aaaaaaaaaaaaaaaa-001",
            ))

        with mock.patch(
            "src.importing.admit_questions._insert_one", wraps=_insert_one
        ) as insert_mock:
            repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((0, 21, 21, 2), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))
        insert_mock.assert_not_called()
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM questions WHERE question_code='Q-aaaaaaaaaaaaaaaa-001'"
            ).fetchone())

    def test_legacy_existing_approval_rejects_wrong_formal_question_code(self):
        admit_questions(self.db, self.private, 1)
        self._make_existing_approval("1")
        with sqlite3.connect(self.db) as con:
            con.execute(
                """UPDATE questions SET question_code='Q-wrong-legacy-001'
                   WHERE question_code='Q-aaaaaaaaaaaaaaaa-001'"""
            )

        repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((0, 21, 21, 2), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM questions WHERE question_code='Q-aaaaaaaaaaaaaaaa-001'"
            ).fetchone())

    def test_legacy_existing_approval_rejects_duplicate_source_mapping(self):
        admit_questions(self.db, self.private, 1)
        self._make_existing_approval("1")
        with sqlite3.connect(self.db) as con:
            con.execute("ALTER TABLE question_sources RENAME TO question_sources_unique")
            con.execute(
                """CREATE TABLE question_sources (
                       question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
                       source_paper_id INTEGER NOT NULL REFERENCES source_papers(id),
                       import_job_id INTEGER NOT NULL REFERENCES import_jobs(id),
                       source_question_no TEXT NOT NULL,
                       source_pages_json TEXT NOT NULL
                   )"""
            )
            con.execute(
                """INSERT INTO question_sources
                   SELECT * FROM question_sources_unique"""
            )
            con.execute(
                """UPDATE question_sources SET source_question_no='1'
                   WHERE source_question_no='2'"""
            )

        repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((0, 21, 21, 2), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))

    def test_legacy_existing_approval_rejects_soft_deleted_formal_question(self):
        admit_questions(self.db, self.private, 1)
        self._make_existing_approval("1")
        with sqlite3.connect(self.db) as con:
            con.execute(
                """UPDATE questions
                   SET deleted_at='2026-07-16T11:00:00+08:00',deletion_reason='unneeded'
                   WHERE question_code='Q-aaaaaaaaaaaaaaaa-001'"""
            )

        repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((0, 21, 21, 2), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))

    def test_legacy_existing_approvals_plus_new_q12_reach_23_present_idempotently(self):
        admit_questions(self.db, self.private, 1)
        for number in ("1", "5", "9"):
            self._make_existing_approval(number)
        self._approve_human_draft("12")

        supplemented = admit_questions(self.db, self.private, 1)
        repeated = admit_questions(self.db, self.private, 1)

        self.assertEqual((1, 22, 23, 0), (
            supplemented.inserted,
            supplemented.already_present,
            supplemented.eligible,
            supplemented.ineligible,
        ))
        self.assertEqual((0, 23, 23, 0), (
            repeated.inserted,
            repeated.already_present,
            repeated.eligible,
            repeated.ineligible,
        ))

    def test_admission_uses_one_effective_snapshot_when_draft_changes_after_assessment(self):
        _, approved = self._approve_human_draft(mutate=lambda question: question.update(
            stem_markdown="评估时获批的版本A"
        ))
        changed = copy.deepcopy(approved)
        changed["stem_markdown"] = "评估后偷换的版本B"
        calls = 0

        def changing_effective(connection, context):
            nonlocal calls
            calls += 1
            if calls == 2:
                connection.execute(
                    """UPDATE candidate_review_drafts
                       SET edited_json=?,version=version+1
                       WHERE import_job_id=1 AND source_question_no='12'""",
                    (json.dumps(changed, ensure_ascii=False, separators=(",", ":")),),
                )
            return _effective_questions(connection, context)

        with mock.patch(
            "src.importing.admit_questions._effective_questions",
            side_effect=changing_effective,
        ) as effective_mock:
            admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            stem = con.execute(
                "SELECT stem_markdown FROM questions WHERE source_question_no='12'"
            ).fetchone()[0]
        self.assertEqual(1, effective_mock.call_count)
        self.assertEqual("评估时获批的版本A", stem)

    def test_admission_uses_one_effective_snapshot_when_draft_is_deleted_after_assessment(self):
        self._approve_human_draft(mutate=lambda question: question.update(
            stem_markdown="评估时仍有效的人工版本"
        ))
        calls = 0

        def deleting_effective(connection, context):
            nonlocal calls
            calls += 1
            if calls == 2:
                connection.execute(
                    """UPDATE candidate_review_drafts
                       SET deleted_at='2026-07-16T12:00:00+08:00',
                           deletion_reason='unneeded',version=version+1
                       WHERE import_job_id=1 AND source_question_no='12'"""
                )
            return _effective_questions(connection, context)

        with mock.patch(
            "src.importing.admit_questions._effective_questions",
            side_effect=deleting_effective,
        ) as effective_mock:
            admit_questions(self.db, self.private, 1)

        self.assertEqual(1, effective_mock.call_count)
        with sqlite3.connect(self.db) as con:
            self.assertEqual("评估时仍有效的人工版本", con.execute(
                "SELECT stem_markdown FROM questions WHERE source_question_no='12'"
            ).fetchone()[0])

    def test_candidate_replaced_after_initial_read_rolls_back_entire_admission(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        replacement = copy.deepcopy(candidate)
        replacement["questions"][0]["stem_markdown"] = "路径替换后的另一份合法候选"
        replacement_path = self.job / ".candidate_questions.replacement.json"
        self._write(replacement_path, replacement)

        def replace_then_assess(connection, context, effective=None):
            os.replace(replacement_path, candidate_path)
            return _assess(connection, context, effective)

        with mock.patch(
            "src.importing.admit_questions._assess",
            side_effect=replace_then_assess,
        ):
            with self.assertRaisesRegex(AdmissionError, "输入文件.*变化"):
                admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_json_artifact_changes_after_loading_roll_back_entire_admission(self):
        mutations = (
            ("ai_audit.json", "replace"),
            ("question_crops.json", "rewrite"),
            ("figure_assets.json", "replace"),
        )
        for filename, mode in mutations:
            with self.subTest(filename=filename, mode=mode):
                path, payload = self._json(filename)
                original = path.read_bytes()

                def change_then_assess(connection, context, effective=None):
                    changed = copy.deepcopy(payload)
                    changed["runtime_snapshot_marker"] = filename
                    if mode == "replace":
                        replacement = self.job / f".{filename}.replacement"
                        self._write(replacement, changed)
                        os.replace(replacement, path)
                    else:
                        self._write(path, changed)
                    return _assess(connection, context, effective)

                try:
                    with mock.patch(
                        "src.importing.admit_questions._assess",
                        side_effect=change_then_assess,
                    ):
                        with self.assertRaisesRegex(AdmissionError, "输入文件.*变化"):
                            admit_questions(self.db, self.private, 1)
                    with sqlite3.connect(self.db) as con:
                        self.assertEqual(
                            0, con.execute("SELECT count(*) FROM questions").fetchone()[0]
                        )
                finally:
                    path.write_bytes(original)

    def test_png_changes_after_validation_roll_back_entire_admission(self):
        mutations = (
            ("question_crops/Q001.png", "replace"),
            ("assets/question_003_figure_01.png", "rewrite"),
        )
        for relative, mode in mutations:
            with self.subTest(relative=relative, mode=mode):
                path = self.job / relative
                original = path.read_bytes()

                def change_then_assess(connection, context, effective=None):
                    replacement = self.job / ".runtime-replacement.png"
                    Image.new("RGB", (48, 32) if "Q001" in relative else (40, 24), (
                        250, 20, 20,
                    )).save(replacement, format="PNG")
                    if mode == "replace":
                        os.replace(replacement, path)
                    else:
                        path.write_bytes(replacement.read_bytes())
                        replacement.unlink()
                    return _assess(connection, context, effective)

                try:
                    with mock.patch(
                        "src.importing.admit_questions._assess",
                        side_effect=change_then_assess,
                    ):
                        with self.assertRaisesRegex(AdmissionError, "输入文件.*变化"):
                            admit_questions(self.db, self.private, 1)
                    with sqlite3.connect(self.db) as con:
                        self.assertEqual(
                            0, con.execute("SELECT count(*) FROM questions").fetchone()[0]
                        )
                finally:
                    path.write_bytes(original)

    def test_signed_v2_crop_manifest_uses_shared_complete_validation(self):
        self._write_signed_crop_manifest()

        result = admit_questions(self.db, self.private, 1)

        self.assertEqual((22, 0, 22, 1), (
            result.inserted,
            result.already_present,
            result.eligible,
            result.ineligible,
        ))

    def test_tampered_signed_v2_crop_manifest_is_rejected(self):
        path, signed = self._write_signed_crop_manifest()
        signed["signature"] = "0" * 64
        self._write(path, signed)

        with self.assertRaisesRegex(AdmissionError, "签名或结构无效"):
            admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_signed_v2_crop_manifest_requires_signature_and_unchanged_generation(self):
        for mutation in ("delete_signature", "change_generation"):
            with self.subTest(mutation=mutation):
                path, manifest = self._json("question_crops.json")
                original = path.read_bytes()
                if mutation == "delete_signature":
                    manifest.pop("signature")
                else:
                    manifest["generation_id"] = "f" * 32
                self._write(path, manifest)
                try:
                    with self.assertRaisesRegex(AdmissionError, "重新裁图迁移"):
                        admit_questions(self.db, self.private, 1)
                    with sqlite3.connect(self.db) as con:
                        self.assertEqual(
                            0,
                            con.execute("SELECT count(*) FROM questions").fetchone()[0],
                        )
                finally:
                    path.write_bytes(original)

    def test_signed_v2_manifest_rejects_coordinated_png_and_metadata_tampering(self):
        path, manifest = self._json("question_crops.json")
        crop_path = self.job / "question_crops/Q001.png"
        Image.new("RGB", (48, 32), (199, 31, 73)).save(crop_path, format="PNG")
        content = crop_path.read_bytes()
        manifest["questions"][0].update(
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            crop_status="generated",
            review_status="ai_review_passed",
        )
        self._write(path, manifest)

        with self.assertRaisesRegex(AdmissionError, "重新裁图迁移"):
            admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_signed_crop_manifest_cannot_be_downgraded_to_v1_with_coordinated_png(self):
        path, manifest = self._json("question_crops.json")
        crop_path = self.job / "question_crops/Q001.png"
        Image.new("RGB", (48, 32), (251, 17, 29)).save(crop_path, format="PNG")
        content = crop_path.read_bytes()
        entry = manifest["questions"][0]
        entry.update(
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            crop_status="generated",
            review_status="ai_review_passed",
        )
        manifest["version"] = 1
        manifest.pop("generation_id")
        manifest.pop("signature")
        self._write(path, manifest)

        with self.assertRaisesRegex(AdmissionError, "重新裁图迁移"):
            admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_symlink_and_hardlink_artifacts_are_rejected(self):
        candidate = self.job / "candidate_questions.json"
        saved_candidate = candidate.read_bytes()
        target = self.job / ".candidate-target.json"
        target.write_bytes(saved_candidate)
        candidate.unlink()
        candidate.symlink_to(target.name)
        with self.assertRaises(AdmissionError):
            admit_questions(self.db, self.private, 1)

        candidate.unlink()
        candidate.write_bytes(saved_candidate)
        crop = self.job / "question_crops/Q001.png"
        os.link(crop, self.job / ".hardlinked-crop.png")
        with self.assertRaises(AdmissionError):
            admit_questions(self.db, self.private, 1)

    def test_symlink_and_hardlink_job_locks_are_rejected(self):
        lock = self.job / ".crop_artifacts.lock"
        target = self.job / ".lock-target"
        target.write_bytes(b"")
        lock.symlink_to(target.name)
        with self.assertRaisesRegex(AdmissionError, "文件锁"):
            admit_questions(self.db, self.private, 1)

        lock.unlink()
        os.link(target, lock)
        with self.assertRaisesRegex(AdmissionError, "文件锁"):
            admit_questions(self.db, self.private, 1)

    def test_candidate_initial_parse_and_sha_share_one_read_snapshot(self):
        candidate_path = self.job / "candidate_questions.json"
        reads = []

        def tracking_read(job_fd, relative_path, label, max_bytes):
            if relative_path == candidate_path.name:
                reads.append(label)
            return _read_stable_artifact(job_fd, relative_path, label, max_bytes)

        with mock.patch(
            "src.importing.admit_questions._read_stable_artifact",
            side_effect=tracking_read,
        ):
            admit_questions(self.db, self.private, 1)

        self.assertEqual(["候选题", "候选题"], reads)

        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            with _job_artifact_lock(self.job) as artifact_lock:
                context = _load_context(
                    connection, self.private, 1, artifact_lock=artifact_lock
                )
                with mock.patch(
                    "src.importing.admit_questions._read_stable_artifact"
                ) as read_mock:
                    _effective_questions(connection, context)
                read_mock.assert_not_called()

    def test_concurrent_admissions_serialize_into_insert_then_already_present(self):
        start = threading.Barrier(2)
        first_verifying = threading.Event()
        release_first = threading.Event()
        second_loaded = threading.Event()
        observation_lock = threading.Lock()
        load_count = 0
        verify_count = 0

        def run_admission():
            start.wait(timeout=5)
            return admit_questions(self.db, self.private, 1)

        def observed_load(connection, private_root, job_id, artifact_lock=None):
            nonlocal load_count
            with observation_lock:
                load_count += 1
                if load_count == 2:
                    second_loaded.set()
            return _load_context(
                connection, private_root, job_id, artifact_lock=artifact_lock
            )

        def observed_verify(job_fd, snapshots):
            nonlocal verify_count
            with observation_lock:
                verify_count += 1
                first = verify_count == 1
            if first:
                first_verifying.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("未释放首个准入调用")
            return _verify_artifact_snapshots(job_fd, snapshots)

        with mock.patch(
            "src.importing.admit_questions._load_context", side_effect=observed_load
        ), mock.patch(
            "src.importing.admit_questions._verify_artifact_snapshots",
            side_effect=observed_verify,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_admission) for _ in range(2)]
                self.assertTrue(first_verifying.wait(timeout=5))
                self.assertFalse(second_loaded.wait(timeout=0.2))
                release_first.set()
                results = [future.result(timeout=15) for future in futures]

        self.assertTrue(second_loaded.is_set())
        counts = sorted((result.inserted, result.already_present) for result in results)
        self.assertEqual([(0, 22), (22, 0)], counts)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(22, con.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual("ok", con.execute("PRAGMA integrity_check").fetchone()[0])

    def test_abnormal_question_number_is_dynamic_and_q12_can_become_eligible(self):
        def mutate(by_no):
            self._strict_auto_pass(by_no["12"])
            by_no["7"].update(
                audit_status="human_required",
                audit_confidence="medium",
                issues=["需要人工确认"],
                suggested_corrections=["核对原图"],
            )

        self._mutate_audits(mutate)
        report = assess_job(self.db, self.private, 1)
        self.assertIn("12", [item.question_no for item in report.eligible])
        self.assertEqual(["7"], [item.question_no for item in report.ineligible])
        admit_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as con:
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='12'"
            ).fetchone())
            self.assertIsNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='7'"
            ).fetchone())

    def test_candidate_question_numbers_must_be_canonical_ascii_1_to_999(self):
        candidate_path, original = self._json("candidate_questions.json")
        for invalid in ("0", "001", "１", "1" * 100):
            with self.subTest(invalid=invalid):
                candidate = copy.deepcopy(original)
                candidate["questions"][0]["source_question_no"] = invalid
                self._write(candidate_path, candidate)

                with self.assertRaisesRegex(AdmissionError, "候选题号非法或重复"):
                    assess_job(self.db, self.private, 1)

        self._write(candidate_path, original)

    def test_strict_ai_gate_requires_each_of_all_four_signals(self):
        failures = (
            ("audit_status", "disputed"),
            ("audit_confidence", "medium"),
            ("issues", ["发现问题"]),
            ("suggested_corrections", ["需要修正"]),
        )
        for field, value in failures:
            with self.subTest(field=field):
                path, original = self._json("ai_audit.json")

                def mutate(by_no):
                    self._strict_auto_pass(by_no["12"])
                    by_no["5"][field] = value

                self._mutate_audits(mutate)
                report = assess_job(self.db, self.private, 1)
                self.assertEqual(["5"], [item.question_no for item in report.ineligible])
                self._write(path, original)

    def test_dynamic_question_count_is_admitted_without_batch_size_assumptions(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"] = candidate["questions"][:5]
        candidate["question_count"] = 5
        self._write(candidate_path, candidate)

        audit_path, audit = self._json("ai_audit.json")
        audit["questions"] = audit["questions"][:5]
        audit["question_count"] = 5
        audit["counts"] = {"auto_pass": 5, "disputed": 0, "human_required": 0}
        audit["random_sample_recommendation"]["question_nos"] = ["3"]
        self._write(audit_path, audit)

        crops_path, crops = self._json("question_crops.json")
        crops["questions"] = crops["questions"][:5]
        crops["question_count"] = 5
        self._write(crops_path, self._resign_crop_manifest(crops))

        figures_path, figures = self._json("figure_assets.json")
        figures["assets"] = [item for item in figures["assets"] if str(item["question_no"]) in {"1", "2", "3", "4", "5"}]
        self._write(figures_path, figures)

        result = admit_questions(self.db, self.private, 1)
        self.assertEqual((5, 0, 5, 0), (result.inserted, result.already_present, result.eligible, result.ineligible))

    def test_soft_deleted_candidate_is_ineligible_but_missing_draft_is_allowed(self):
        with sqlite3.connect(self.db) as con:
            con.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,deleted_at)
                   VALUES (1,'2',?,'{}','{}','2026-07-14T00:00:00+08:00')""",
                ("b" * 64,),
            )
        result = admit_questions(self.db, self.private, 1)
        self.assertEqual((21, 2), (result.eligible, result.ineligible))
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='2'"
            ).fetchone())
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='1'"
            ).fetchone())

    def test_no_eligible_questions_is_a_safe_idempotent_noop(self):
        def mutate(by_no):
            for item in by_no.values():
                item.update(
                    audit_status="human_required",
                    audit_confidence="medium",
                    issues=["需要人工确认"],
                    suggested_corrections=["核对原图"],
                )

        self._mutate_audits(mutate)
        first = admit_questions(self.db, self.private, 1)
        second = admit_questions(self.db, self.private, 1)
        self.assertEqual((0, 0, 0, 23), (first.inserted, first.already_present, first.eligible, first.ineligible))
        self.assertEqual(first, second)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_fixture_is_generated_inside_temp_root_without_repository_private_data(self):
        repository_private_processing = ROOT / "data" / "private" / "processing"
        self.assertTrue(self.job.resolve().is_relative_to(self.root.resolve()))
        self.assertFalse(self.job.resolve().is_relative_to(repository_private_processing.resolve()))
        self.assertEqual(23, len(list((self.job / "question_crops").glob("Q*.png"))))
        factory_source = Path(create_import_job_fixture.__code__.co_filename).read_text(encoding="utf-8")
        self.assertNotIn("data/private", factory_source)

    def test_fixture_metadata_and_required_semantics_are_self_consistent(self):
        candidate = json.loads((self.job / "candidate_questions.json").read_text(encoding="utf-8"))
        by_no = {int(item["source_question_no"]): item for item in candidate["questions"]}
        self.assertEqual(23, candidate["question_count"])
        self.assertTrue(all(option["content"] == "见原页选项图" for option in by_no[3]["options"]))
        self.assertEqual({3, 16}, {number for number, item in by_no.items() if item["figure_required"]})
        self.assertEqual(["（1）", "（2）", "（2）（i）", "（2）（ii）"], [x["label"] for x in by_no[22]["subquestions"]])
        self.assertEqual(["（1）", "（2）", "（2）①", "（2）②"], [x["label"] for x in by_no[23]["subquestions"]])

        render = json.loads((self.job / "render_manifest.json").read_text(encoding="utf-8"))
        crops = json.loads((self.job / "question_crops.json").read_text(encoding="utf-8"))
        self.assertEqual(2, crops["version"])
        self.assertRegex(crops["generation_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(crops["signature"], r"^[0-9a-f]{64}$")
        figures = json.loads((self.job / "figure_assets.json").read_text(encoding="utf-8"))
        entries = [
            *[(entry, "pixel_width", "pixel_height") for entry in render["pages"]],
            *[(entry, "width", "height") for entry in crops["questions"]],
            *[(entry, "width", "height") for entry in figures["assets"]],
        ]
        for entry, width_key, height_key in entries:
            with self.subTest(path=entry["relative_path"] if "relative_path" in entry else entry["output_relative_path"]):
                relative = entry.get("relative_path", entry.get("output_relative_path"))
                path = self.job / relative
                self.assertEqual(path.stat().st_size, entry["byte_size"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                with Image.open(path) as image:
                    self.assertEqual("PNG", image.format)
                    self.assertEqual((entry[width_key], entry[height_key]), image.size)

    def test_admits_all_relations_without_answers_and_is_idempotent(self):
        first = admit_questions(self.db, self.private, 1)
        second = admit_questions(self.db, self.private, 1)
        self.assertEqual((22, 0), (first.inserted, first.already_present))
        self.assertEqual((0, 22), (second.inserted, second.already_present))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(22, con.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(22, con.execute("SELECT count(DISTINCT question_code) FROM questions").fetchone()[0])
            self.assertIsNone(con.execute("SELECT 1 FROM question_sources WHERE source_question_no='12'").fetchone())
            self.assertEqual(22, con.execute("SELECT count(*) FROM question_assets WHERE asset_kind='complete_question'").fetchone()[0])
            self.assertEqual(2, con.execute("SELECT count(*) FROM question_assets WHERE asset_kind='question_figure'").fetchone()[0])
            self.assertEqual(22, con.execute("SELECT count(*) FROM question_sources").fetchone()[0])
            self.assertEqual(22, con.execute("SELECT count(*) FROM question_reviews WHERE review_item='usability'").fetchone()[0])
            self.assertEqual((0, 22), con.execute("SELECT count(nullif(answer_markdown,'')), count(*) FROM questions WHERE answer_status='missing'").fetchone())
            self.assertEqual((0, 22), con.execute(
                "SELECT count(analysis_markdown),count(*) FROM questions WHERE analysis_review_status='not_applicable'"
            ).fetchone())
            self.assertEqual((0, 0), con.execute(
                "SELECT count(nullif(answer_markdown,'')),count(analysis_markdown) FROM subquestions WHERE answer_status='missing'"
            ).fetchone())

    def test_answer_without_dedicated_audit_status_is_unsafe(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"][0]["answer_markdown"] = "$x=1$"
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        question = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("answer_status_not_passed", question.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_status_not_passed"):
            admit_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_analysis_without_dedicated_audit_status_is_unsafe(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = candidate["questions"][0]
        question["analysis_markdown"] = "原卷解析。"
        self._write(candidate_path, candidate)
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_analysis_sha256=self._answer_analysis_sha256(question)
            )
        )

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("analysis_status_not_passed", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "analysis_status_not_passed"):
            admit_questions(self.db, self.private, 1)

    def test_answer_changed_after_audit_is_unsafe_due_to_hash_mismatch(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = candidate["questions"][0]
        question["answer_markdown"] = "$x=1$"
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_status="passed", answer_analysis_sha256=reviewed_hash
            )
        )
        question["answer_markdown"] = "$x=2$"
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("answer_analysis_sha256_mismatch", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_subquestion_changed_after_audit_is_unsafe_due_to_hash_mismatch(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0]["answer_markdown"] = "$2$"
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["22"].update(
                answer_status="passed", answer_analysis_sha256=reviewed_hash
            )
        )
        question["subquestions"][0]["stem_markdown"] = "审核后被篡改的小问题干"
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_subquestion_reorder_after_audit_is_unsafe_due_to_hash_mismatch(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0]["answer_markdown"] = "$2$"
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["22"].update(
                answer_status="passed", answer_analysis_sha256=reviewed_hash
            )
        )
        question["subquestions"][0], question["subquestions"][1] = (
            question["subquestions"][1], question["subquestions"][0]
        )
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_admits_parent_answer_and_analysis_as_reviewed_content(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"][0].update(
            answer_markdown="$x=1$",
            analysis_markdown="由题意直接计算。",
        )
        self._write(candidate_path, candidate)
        reviewed_hash = self._answer_analysis_sha256(candidate["questions"][0])
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_status="passed",
                analysis_status="passed",
                answer_analysis_sha256=reviewed_hash,
            )
        )

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT answer_markdown,answer_status,answer_review_status,
                          analysis_markdown,analysis_review_status
                   FROM questions WHERE source_question_no='1'"""
            ).fetchone()
        self.assertEqual(
            ("$x=1$", "provided", "passed", "由题意直接计算。", "passed"),
            row,
        )
        with sqlite3.connect(self.db) as con:
            review_note = con.execute(
                """SELECT r.notes FROM question_reviews r JOIN questions q ON q.id=r.question_id
                   WHERE q.source_question_no='1' AND r.review_item='usability'"""
            ).fetchone()[0]
        self.assertIn("原卷答案已通过审核", review_note)

    def test_admits_subquestion_answer_and_analysis(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0].update(
            answer_markdown="$2$",
            analysis_markdown="先化简，再求值。",
        )
        self._write(candidate_path, candidate)
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["22"].update(
                answer_status="passed",
                analysis_status="passed",
                answer_analysis_sha256=reviewed_hash,
            )
        )

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT s.answer_markdown,s.answer_status,s.analysis_markdown
                   FROM subquestions s JOIN questions q ON q.id=s.question_id
                   WHERE q.source_question_no='22' AND s.display_order=1"""
            ).fetchone()
            review_note = con.execute(
                """SELECT r.notes FROM question_reviews r JOIN questions q ON q.id=r.question_id
                   WHERE q.source_question_no='22' AND r.review_item='usability'"""
            ).fetchone()[0]
            parent_statuses = con.execute(
                """SELECT q.answer_status,q.answer_review_status,q.analysis_review_status
                   FROM questions q WHERE q.source_question_no='22'"""
            ).fetchone()
        self.assertEqual(("$2$", "provided", "先化简，再求值。"), row)
        self.assertEqual(("missing", "not_applicable", "not_applicable"), parent_statuses)
        self.assertNotIn("未提供答案", review_note)
        self.assertIn("答案已通过审核", review_note)

    def test_non_string_answer_or_analysis_rejects_and_rolls_back_batch(self):
        candidate_path, original = self._json("candidate_questions.json")
        mutations = (
            lambda data: data["questions"][0].__setitem__("answer_markdown", 123),
            lambda data: data["questions"][0].__setitem__("analysis_markdown", None),
            lambda data: next(q for q in data["questions"] if q["source_question_no"] == "22")["subquestions"][0].__setitem__("answer_markdown", ["2"]),
            lambda data: next(q for q in data["questions"] if q["source_question_no"] == "22")["subquestions"][0].__setitem__("analysis_markdown", {"text": "解析"}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(original)
                mutate(changed)
                self._write(candidate_path, changed)
                try:
                    with self.assertRaises(AdmissionError):
                        admit_questions(self.db, self.private, 1)

                    with sqlite3.connect(self.db) as con:
                        self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])
                finally:
                    with sqlite3.connect(self.db) as con:
                        con.execute("DELETE FROM questions")
        self._write(candidate_path, original)

    def test_explicit_non_list_subquestions_raise_controlled_admission_error(self):
        candidate_path, original = self._json("candidate_questions.json")
        for invalid in (None, {}, "not-a-list"):
            with self.subTest(invalid=invalid):
                changed = copy.deepcopy(original)
                changed["questions"][0]["subquestions"] = invalid
                self._write(candidate_path, changed)

                with self.assertRaisesRegex(AdmissionError, "subquestions必须为列表"):
                    admit_questions(self.db, self.private, 1)

        self._write(candidate_path, original)

    def test_empty_answer_and_analysis_keep_missing_semantics(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"][0].update(answer_markdown="", analysis_markdown="   ")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0].update(answer_markdown="   ", analysis_markdown="")
        self._write(candidate_path, candidate)

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            parent = con.execute(
                """SELECT answer_markdown,answer_status,answer_review_status,
                          analysis_markdown,analysis_review_status
                   FROM questions WHERE source_question_no='1'"""
            ).fetchone()
            child = con.execute(
                """SELECT s.answer_markdown,s.answer_status,s.analysis_markdown
                   FROM subquestions s JOIN questions q ON q.id=s.question_id
                   WHERE q.source_question_no='22' AND s.display_order=1"""
            ).fetchone()
        self.assertEqual(("", "missing", "not_applicable", None, "not_applicable"), parent)
        self.assertEqual(("", "missing", None), child)

    def test_manifest_failures_abort_batch_without_partial_rows(self):
        mutations = [
            ("ai_audit.json", lambda d: d["questions"].pop()),
            ("question_crops.json", lambda d: d["questions"].pop()),
            ("question_crops.json", lambda d: d["questions"][0].__setitem__("sha256", "0" * 64)),
            ("figure_assets.json", lambda d: d["assets"][0].__setitem__("review_status", "pending_ai_review")),
            ("candidate_questions.json", lambda d: d["questions"][0].__setitem__("primary_knowledge_point_code", "missing.code")),
        ]
        for filename, mutate in mutations:
            with self.subTest(filename=filename, mutation=mutate):
                path, original = self._json(filename)
                changed = copy.deepcopy(original); mutate(changed); self._write(path, changed)
                with self.assertRaises(AdmissionError):
                    admit_questions(self.db, self.private, 1)
                with sqlite3.connect(self.db) as con:
                    self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])
                self._write(path, original)

    def test_transaction_rolls_back_on_insert_error(self):
        with mock.patch("src.importing.admit_questions._insert_one", side_effect=sqlite3.IntegrityError("boom")):
            with self.assertRaises(sqlite3.IntegrityError):
                admit_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_later_sqlite_error_rolls_back_previously_inserted_answer(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        first = candidate["questions"][0]
        first["answer_markdown"] = "$x=1$"
        candidate["questions"][1]["options"][1]["code"] = "A"
        self._write(candidate_path, candidate)
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_status="passed",
                answer_analysis_sha256=self._answer_analysis_sha256(first),
            )
        )

        with self.assertRaises(sqlite3.IntegrityError):
            admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(0, con.execute("SELECT count(*) FROM question_options").fetchone()[0])

    def test_question_code_is_stable_and_paths_are_safe(self):
        report = admit_questions(self.db, self.private, 1)
        codes = report.question_codes
        self.assertEqual(codes, admit_questions(self.db, self.private, 1).question_codes)
        with sqlite3.connect(self.db) as con:
            paths = [row[0] for row in con.execute("SELECT relative_path FROM question_assets")]
        self.assertTrue(all(not p.startswith("/") and ".." not in Path(p).parts for p in paths))


if __name__ == "__main__":
    unittest.main()
