import copy
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import src.importing.admit_questions as admit_module
import src.reviewing.historical_residual as residual_module
from PIL import Image

from src.database.initialize import initialize_database
from src.importing.admit_questions import admit_questions
from src.processing.historical_v1_crop_recovery import _formal_snapshot
from src.processing.historical_v1_crop_recovery import (
    recover_historical_v1_crops,
    resume_historical_v1_fresh_pipeline,
)
from src.processing.crop_review import record_crop_ai_review
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest
from src.reviewing.historical_residual import (
    HistoricalResidualError,
    admit_historical_residual_questions,
    apply_historical_residual_classification,
    assess_historical_residual_admission,
    assess_historical_residual_no_answer_source,
    claim_historical_residual_classification,
    _effective_evidence_row,
    _remove_unreferenced_new_backup,
    register_historical_residual_no_answer_source,
    run_claimed_historical_residual_classification,
)
from tests.fixture_factory import (
    SYNTHETIC_PAPER_NAME,
    anchor_synthetic_candidate_audit,
    anchor_synthetic_figure_reviews,
    create_import_job_fixture,
)


class _StrictResidualRunner:
    def __init__(self):
        self.stages = []

    def run(self, stage, _prompt):
        self.stages.append(stage)
        if stage == "level2":
            question = {
                "source_question_no": "12", "level2_code": "01.01",
                "confidence": "high", "reason": "合成一级分类",
            }
        else:
            question = {
                "source_question_no": "12", "primary_code": "01.01.06",
                "related_codes": ["01.01.07"], "confidence": "high",
                "reason": "合成双路一致分类",
            }
        return json.dumps({"questions": [question]}, ensure_ascii=False)


class _AdjudicatingResidualRunner:
    def run(self, stage, _prompt):
        if stage == "level2":
            question = {
                "source_question_no": "12", "level2_code": "01.01",
                "confidence": "high", "reason": "合成一级分类",
            }
        else:
            code = {
                "proposal": "01.01.06",
                "verifier": "01.01.07",
                "adjudicator": "01.01.08",
            }[stage]
            question = {
                "source_question_no": "12", "primary_code": code,
                "related_codes": [], "confidence": "high",
                "reason": "合成独立裁决分类",
            }
        return json.dumps({"questions": [question]}, ensure_ascii=False)


class HistoricalResidualTests(unittest.TestCase):
    def _old_formal_graph(self):
        tables = (
            "questions", "question_sources", "question_options", "subquestions",
            "question_formulas", "question_figures", "question_assets",
            "question_related_knowledge_points",
        )
        with sqlite3.connect(self.db) as connection:
            ids = tuple(row[0] for row in connection.execute(
                """SELECT question_id FROM question_sources
                   WHERE import_job_id=1 AND source_question_no!='12' ORDER BY question_id"""
            ))
            placeholders = ",".join("?" for _ in ids)
            return {
                table: connection.execute(
                    f"SELECT * FROM {table} WHERE "
                    + ("id" if table == "questions" else "question_id")
                    + f" IN ({placeholders}) ORDER BY 1", ids,
                ).fetchall()
                for table in tables
            }

    def _replace_synthetic_crop_with_legacy_v1(self):
        crop_path = self.job_dir / "question_crops.json"
        crop = json.loads(crop_path.read_text())
        render_path = self.job_dir / "render_manifest.json"
        render = json.loads(render_path.read_text())
        for page in render["pages"]:
            page_path = self.job_dir / page["relative_path"]
            with Image.open(page_path) as image:
                image.resize((96, 80)).save(page_path, "PNG")
            page_bytes = page_path.read_bytes()
            page.update({
                "pixel_width": 96, "pixel_height": 80,
                "byte_size": len(page_bytes),
                "sha256": hashlib.sha256(page_bytes).hexdigest(),
            })
        render = {
            "version": 1, "import_job_id": 1, "dpi": 300,
            "source_pdf_sha256": self.source_sha, "source_page_count": 4,
            "page_start": 1, "page_end": 4, "page_count": 4,
            "pages": render["pages"],
        }
        render_path.write_text(json.dumps(render, ensure_ascii=False), encoding="utf-8")
        regions = []
        legacy_questions = []
        page_by_number = {entry["page_number"]: entry for entry in render["pages"]}
        for entry in crop["questions"]:
            number = entry["question_no"]
            page = entry["regions"][0]["page_number"]
            metadata = page_by_number[page]
            bbox = [0, 0, metadata["pixel_width"], metadata["pixel_height"]]
            crop_bytes = (self.job_dir / metadata["relative_path"]).read_bytes()
            crop_file = self.job_dir / entry["output_relative_path"]
            crop_file.write_bytes(crop_bytes)
            regions.append({
                "question_no": number,
                "regions": [{
                    "page_number": page,
                    "bbox_normalized": [
                        bbox[0] / metadata["pixel_width"],
                        bbox[1] / metadata["pixel_height"],
                        bbox[2] / metadata["pixel_width"],
                        bbox[3] / metadata["pixel_height"],
                    ],
                }],
                "warnings": [], "confidence": "high",
            })
            legacy_questions.append({
                **{key: entry[key] for key in (
                    "question_no", "output_relative_path", "crop_status",
                    "review_status", "warnings",
                )},
                "width": metadata["pixel_width"], "height": metadata["pixel_height"],
                "byte_size": len(crop_bytes),
                "sha256": hashlib.sha256(crop_bytes).hexdigest(),
                "regions": [{"page_number": page, "bbox": bbox}],
                "composition": {"mode": "single", "region_count": 1},
            })
        crop_path.write_text(json.dumps({
            "version": 1, "import_job_id": 1, "question_count": 23,
            "source_pages": [{
                key: entry[key] for key in (
                    "page_number", "relative_path", "pixel_width", "pixel_height", "sha256",
                )
            } for entry in render["pages"]],
            "questions": legacy_questions,
        }, ensure_ascii=False), encoding="utf-8")
        (self.job_dir / "question_regions.json").write_text(json.dumps({
            "version": 1, "import_job_id": 1, "question_count": 23,
            "questions": regions,
        }, ensure_ascii=False), encoding="utf-8")
        figure_path = self.job_dir / "figure_assets.json"
        figure = json.loads(figure_path.read_text())
        page_hashes = {entry["page_number"]: entry["sha256"] for entry in render["pages"]}
        for asset in figure["assets"]:
            asset["source_page_sha256"] = page_hashes[asset["source_page"]]
        figure = sign_manifest(
            load_hmac_key(self.job_dir),
            {key: value for key, value in figure.items() if key != "signature"},
        )
        figure_path.write_text(json.dumps(figure, ensure_ascii=False), encoding="utf-8")

    def _write_no_answer_authority(self):
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT j.source_paper_id,p.sha256,p.file_size
                   FROM import_jobs j JOIN source_papers p ON p.id=j.source_paper_id
                   WHERE j.id=1"""
            ).fetchone()
        receipt = "用户确认：原卷无答案".encode("utf-8")
        payload = {
            "version": 2,
            "scope": "historical_residual_no_answer_authority",
            "import_job_id": 1,
            "source_paper_id": row[0],
            "source_pdf_sha256": row[1],
            "source_pdf_byte_size": row[2],
            "provenance": "archived_user_confirmation_migration",
            "confirmation_text_summary": "用户曾明确确认原卷无答案",
            "confirmation_text_sha256": hashlib.sha256(receipt).hexdigest(),
            "session_reference": "synthetic-session:message-42",
            "recorded_at": "2026-07-16T09:00:00+00:00",
            "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
            "conclusion": "source_has_no_answer",
        }
        signed = sign_manifest(load_hmac_key(self.job_dir), payload)
        path = self.job_dir / "historical_no_answer_authority.json"
        path.write_text(
            json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return path, receipt

    def test_no_answer_registration_requires_independent_archived_authority(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")
        with self.assertRaises(HistoricalResidualError):
            assess_historical_residual_no_answer_source(self.db, self.private, 1)

    def test_application_hmac_cannot_label_no_answer_evidence_as_human(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")
        path, receipt = self._write_no_answer_authority()
        evidence = json.loads(path.read_text())
        payload = {
            key: value for key, value in evidence.items()
            if key not in {
                "signature", "provenance", "confirmation_text_summary",
                "confirmation_text_sha256", "session_reference", "recorded_at",
                "receipt_sha256",
            }
        }
        payload.update({
            "version": 1,
            "reviewer_type": "human",
            "reviewer_identity": "self-asserted-human",
            "review_method": "archived_review_migration",
            "reviewed_at": "2026-07-16T09:00:00+00:00",
        })
        path.write_text(json.dumps(
            sign_manifest(load_hmac_key(self.job_dir), payload),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        with self.assertRaises(HistoricalResidualError):
            assess_historical_residual_no_answer_source(
                self.db, self.private, 1, confirmation_receipt=receipt,
            )

    def test_no_answer_authority_rejects_replaced_archived_source_pdf(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")
        _, receipt = self._write_no_answer_authority()
        original = self.source_path.read_bytes()
        self.source_path.write_bytes(b"%PDF-1.4\nreplaced but same purpose\n%%EOF\n")
        with self.assertRaises(HistoricalResidualError):
            assess_historical_residual_no_answer_source(
                self.db, self.private, 1, confirmation_receipt=receipt,
            )
        self.source_path.write_bytes(original)

    def test_active_classification_claim_is_not_preempted(self):
        first = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_StrictResidualRunner()
        )
        with self.assertRaises(HistoricalResidualError):
            claim_historical_residual_classification(
                self.db, self.private, 1, runner=_StrictResidualRunner()
            )
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("processing", first.claim_token),
                connection.execute(
                    "SELECT status,claim_token FROM historical_residual_classification_runs "
                    "WHERE import_job_id=1"
                ).fetchone(),
            )

    def test_expired_classification_claim_is_reclaimed_only_for_same_snapshot(self):
        first = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_StrictResidualRunner()
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE historical_residual_classification_runs "
                "SET lease_expires_at='2000-01-01T00:00:00+00:00' "
                "WHERE import_job_id=1"
            )
        second = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_StrictResidualRunner()
        )
        self.assertNotEqual(first.claim_token, second.claim_token)
        with sqlite3.connect(self.db) as connection:
            status, token, heartbeat, lease = connection.execute(
                "SELECT status,claim_token,heartbeat_at,lease_expires_at FROM "
                "historical_residual_classification_runs WHERE import_job_id=1"
            ).fetchone()
        self.assertEqual(("processing", second.claim_token), (status, token))
        self.assertIsNotNone(heartbeat)
        self.assertGreater(lease, heartbeat)

    def test_expired_classification_claim_with_snapshot_mismatch_is_not_reclaimed(self):
        first = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_StrictResidualRunner()
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE historical_residual_classification_runs "
                "SET lease_expires_at='2000-01-01T00:00:00+00:00',input_sha256=? "
                "WHERE import_job_id=1",
                ("0" * 64,),
            )
        with self.assertRaises(HistoricalResidualError):
            claim_historical_residual_classification(
                self.db, self.private, 1, runner=_StrictResidualRunner()
            )
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(first.claim_token, connection.execute(
                "SELECT claim_token FROM historical_residual_classification_runs "
                "WHERE import_job_id=1"
            ).fetchone()[0])

    def test_residual_entry_rejects_database_symlink(self):
        linked = self.root / "linked-question-bank.db"
        linked.symlink_to(self.db)
        with self.assertRaises(HistoricalResidualError):
            claim_historical_residual_classification(
                linked, self.private, 1, runner=_StrictResidualRunner()
            )

    def test_residual_entry_rejects_database_hardlink(self):
        linked = self.root / "hardlinked-question-bank.db"
        os.link(self.db, linked)
        with self.assertRaises(HistoricalResidualError):
            assess_historical_residual_admission(self.db, self.private, 1)

    def test_claim_rejects_job_directory_replaced_after_lock(self):
        moved = self.private / "processing" / "import_job_1-moved"
        real_snapshot = residual_module._residual_snapshot

        def replace_after_lock(*args, **kwargs):
            result = real_snapshot(*args, **kwargs)
            self.job_dir.rename(moved)
            self.job_dir.mkdir()
            return result

        try:
            with patch.object(
                residual_module, "_residual_snapshot", side_effect=replace_after_lock,
            ), self.assertRaises(HistoricalResidualError):
                claim_historical_residual_classification(
                    self.db, self.private, 1, runner=_StrictResidualRunner()
                )
        finally:
            shutil.rmtree(self.job_dir)
            moved.rename(self.job_dir)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM historical_residual_classification_runs"
            ).fetchone()[0])

    def test_pending_legacy_evidence_requires_full_schema_taxonomy_and_exact_anchors(self):
        source = {
            "source_question_no": "12", "approved_draft_version": 1,
            "edited_sha256": "a" * 64, "classification_scope_sha256": "b" * 64,
        }
        snapshot = type("Snapshot", (), {
            "candidate_sha256": "c" * 64, "audit_sha256": "d" * 64,
            "draft_bindings_sha256": "e" * 64, "taxonomy_sha256": "f" * 64,
        })()
        taxonomy = (
            {"code": "01.01", "level": 2, "parent_code": "01"},
            {"code": "01.01.06", "level": 3, "parent_code": "01.01"},
            {"code": "01.01.07", "level": 3, "parent_code": "01.01"},
            {"code": "01.01.08", "level": 3, "parent_code": "01.01"},
        )
        row = {
            **source, "candidate_sha256": snapshot.candidate_sha256,
            "audit_sha256": snapshot.audit_sha256,
            "draft_bindings_sha256": snapshot.draft_bindings_sha256,
            "taxonomy_sha256": snapshot.taxonomy_sha256,
            "level2": {"source_question_no": "12", "level2_code": "01.01",
                       "confidence": "high", "reason": "一级"},
            "proposal": {"source_question_no": "12", "primary_code": "01.01.06",
                         "related_codes": [], "confidence": "high", "reason": "提议"},
            "verifier": {"source_question_no": "12", "primary_code": "01.01.07",
                         "related_codes": [], "confidence": "high", "reason": "复核"},
            "adjudicator": {
                "source_question_no": "12", "primary_code": "01.01.08",
                "related_codes": ["01.01.07"], "confidence": "high",
                "reason": "独立裁决",
            },
            "final_primary_code": "01.01.06", "final_related_codes": [],
            "final_reason": "提议", "status": "pending", "approval_source": None,
        }
        effective = _effective_evidence_row(
            row, source=source, snapshot=snapshot, taxonomy=taxonomy,
        )
        self.assertIsNotNone(effective)
        assert effective is not None
        self.assertEqual("approved", effective["status"])
        self.assertEqual("01.01.08", effective["final_primary_code"])
        self.assertEqual(["01.01.07"], effective["final_related_codes"])
        self.assertEqual("codex_adjudicated", effective["approval_source"])

        attacks = {
            "extra schema key": lambda value: value.__setitem__("manual", True),
            "wrong source anchor": lambda value: value.__setitem__("edited_sha256", "0" * 64),
            "unknown adjudicator code": lambda value: value["adjudicator"].__setitem__(
                "primary_code", "99.99.99"
            ),
            "level2 mismatch": lambda value: value["level2"].__setitem__(
                "level2_code", "01.02"
            ),
            "low adjudicator confidence": lambda value: value["adjudicator"].__setitem__(
                "confidence", "medium"
            ),
            "wrong nested number": lambda value: value["adjudicator"].__setitem__(
                "source_question_no", "13"
            ),
            "wrong pending final primary": lambda value: value.__setitem__(
                "final_primary_code", "01.01.07"
            ),
            "wrong pending final related": lambda value: value.__setitem__(
                "final_related_codes", ["01.01.08"]
            ),
            "wrong pending final reason": lambda value: value.__setitem__(
                "final_reason", "伪造结论"
            ),
            "pending approval source": lambda value: value.__setitem__(
                "approval_source", "codex_adjudicated"
            ),
            "adjudicator duplicate related": lambda value: value["adjudicator"].__setitem__(
                "related_codes", ["01.01.07", "01.01.07"]
            ),
            "adjudicator malformed related": lambda value: value["adjudicator"].__setitem__(
                "related_codes", [[]]
            ),
            "wrong snapshot anchor": lambda value: value.__setitem__(
                "audit_sha256", "0" * 64
            ),
        }
        for label, mutate in attacks.items():
            with self.subTest(label=label):
                attacked = copy.deepcopy(row)
                mutate(attacked)
                self.assertIsNone(_effective_evidence_row(
                    attacked, source=source, snapshot=snapshot, taxonomy=taxonomy,
                ))

        crafted_double = copy.deepcopy(row)
        crafted_double["verifier"] = copy.deepcopy(crafted_double["proposal"])
        crafted_double.update({
            "status": "approved", "approval_source": "codex_double_pass",
        })
        self.assertIsNone(_effective_evidence_row(
            crafted_double, source=source, snapshot=snapshot, taxonomy=taxonomy,
        ))

    def test_residual_no_answer_registration_is_exact_and_idempotent(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")
        _, receipt = self._write_no_answer_authority()
        with self.assertRaises(HistoricalResidualError):
            assess_historical_residual_no_answer_source(self.db, self.private, 1)
        with self.assertRaises(HistoricalResidualError):
            assess_historical_residual_no_answer_source(
                self.db, self.private, 1, confirmation_receipt=receipt + b"changed",
            )
        decision = assess_historical_residual_no_answer_source(
            self.db, self.private, 1, confirmation_receipt=receipt,
        )
        with self.assertRaises(HistoricalResidualError):
            register_historical_residual_no_answer_source(
                self.db, self.private, 1, confirmation_token="0" * 64,
                confirmation_receipt=receipt,
            )
        first = register_historical_residual_no_answer_source(
            self.db, self.private, 1, confirmation_token=decision["confirmation_token"],
            confirmation_receipt=receipt,
        )
        self.assertFalse(first["already_registered"])
        second = register_historical_residual_no_answer_source(
            self.db, self.private, 1, confirmation_token=decision["confirmation_token"],
            confirmation_receipt=receipt,
        )
        self.assertTrue(second["already_registered"])
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT source_answer_state,answer_page_start,answer_page_end,
                          expected_question_count,candidate_sha256
                   FROM import_answer_sources WHERE import_job_id=1"""
            ).fetchone()
            authority = connection.execute(
                "SELECT * FROM historical_residual_no_answer_decisions WHERE import_job_id=1"
            ).fetchone()
            self.assertIsNotNone(authority)
            assert authority is not None
            evidence = json.loads(authority["evidence_json"])
            self.assertEqual(authority["evidence_sha256"], hashlib.sha256(
                authority["evidence_json"].encode("utf-8")
            ).hexdigest())
            self.assertEqual(decision["confirmation_token"], authority["confirmation_token"])
            self.assertEqual("local_archived_user_confirmation", evidence["authority"])
            self.assertEqual(
                "archived_user_confirmation_migration", evidence["provenance"],
            )
            self.assertEqual(hashlib.sha256(receipt).hexdigest(), evidence["receipt_sha256"])
            self.assertEqual(64, len(evidence["recovery_record_sha256"]))
            self.assertEqual(64, len(evidence["resumption_record_sha256"]))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE historical_residual_no_answer_decisions SET evidence_json='{}' "
                    "WHERE import_job_id=1"
                )
        self.assertEqual("source_has_no_answer", row[0])
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertEqual(23, row[3])
        self.assertEqual(64, len(row[4]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.job_dir = create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        source_content = b"%PDF-1.4\n% synthetic residual source\n%%EOF\n"
        source_sha = hashlib.sha256(source_content).hexdigest()
        source_path = self.private / "raw_papers/TJ/2025/synthetic.pdf"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source_content)
        self.source_path = source_path
        self.source_sha = source_sha
        with sqlite3.connect(self.db) as connection:
            source = connection.execute(
                """INSERT INTO source_papers
                   (id,sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES(1,?,?,'synthetic-paper.pdf','raw_papers/TJ/2025/synthetic.pdf',
                          'TJ',2025,'YK',?)""",
                (source_sha, len(source_content), SYNTHETIC_PAPER_NAME),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_jobs
                   (id,source_paper_id,page_start,page_end,status)
                   VALUES(1,?,1,4,'needs_review')""", (source,),
            )
        anchor_synthetic_candidate_audit(self.db, self.job_dir)
        anchor_synthetic_figure_reviews(self.db, self.private)
        ordinary = admit_questions(self.db, self.private, 1)
        self.assertEqual(22, ordinary.inserted)
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            formal_count, formal_sha, formal_numbers = _formal_snapshot(connection, 1)
            self.assertEqual(22, formal_count)
            self.assertNotIn("12", formal_numbers)
            candidate_path = self.job_dir / "candidate_questions.json"
            candidate_raw = candidate_path.read_bytes()
            candidate = json.loads(candidate_raw)
            original = next(
                item for item in candidate["questions"]
                if item["source_question_no"] == "12"
            )
            edited = copy.deepcopy(original)
            edited["stem_markdown"] += "（人工修订）"
            reviewed_at = "2026-07-16T10:00:00+08:00"
            connection.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,status,version,reviewed_at,
                    approval_source,approval_evidence_json)
                   VALUES(1,'12',?,?,?,'approved',1,?,'human',?)""",
                (
                    hashlib.sha256(candidate_raw).hexdigest(),
                    json.dumps(original, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(edited, ensure_ascii=False, separators=(",", ":")),
                    reviewed_at,
                    json.dumps(
                        {"method": "workbench_quick", "reviewed_at": reviewed_at},
                        separators=(",", ":"),
                    ),
                ),
            )
            crop_raw = (self.job_dir / "question_crops.json").read_bytes()
            crop = json.loads(crop_raw)
            connection.execute(
                """INSERT INTO historical_v1_crop_recoveries
                   (import_job_id,source_paper_id,source_pdf_sha256,
                    render_manifest_sha256,render_manifest_byte_size,
                    regions_manifest_sha256,regions_manifest_byte_size,
                    legacy_crop_manifest_sha256,legacy_crop_manifest_byte_size,
                    question_nos_json,prior_job_status,prior_split_status,
                    preserved_codex_run_id,new_crop_manifest_sha256,
                    new_crop_generation_id,new_crop_manifest_signature,
                    formal_question_count,formal_batch_sha256,candidate_sha256,
                    candidate_byte_size,draft_batch_sha256,migration_evidence_kind,
                    migration_evidence_json,recovered_at)
                   VALUES(1,1,?,?,?,?,?,?,?,?,'needs_review',NULL,NULL,?,?,?,?,?,?,?,?,
                          'system_migration_placeholder',?,'2026-07-16T00:00:00+00:00')""",
                (
                    source_sha, "b" * 64, 1, "c" * 64, 1, "d" * 64, 1,
                    json.dumps(list(range(1, 24))), hashlib.sha256(crop_raw).hexdigest(),
                    crop["generation_id"], crop["signature"], formal_count, formal_sha,
                    hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw), "e" * 64,
                    json.dumps({"synthetic": True}),
                ),
            )
            connection.execute(
                """INSERT INTO historical_v1_pipeline_resumptions
                   (import_job_id,source_paper_id,source_pdf_sha256,
                    formal_question_count,formal_batch_sha256,crop_question_count,
                    crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                    reviewer_run_id,review_request_sha256,review_evidence_signature,
                    reviewed_at,resumed_at)
                   VALUES(1,1,?,?,?,?,?,?,?,'synthetic-independent-review',?,?,?,?)""",
                (
                    source_sha, formal_count, formal_sha, 23,
                    hashlib.sha256(crop_raw).hexdigest(), crop["generation_id"], crop["signature"],
                    "f" * 64, "1" * 64, "2026-07-16T00:00:00+00:00",
                    "2026-07-16T00:00:01+00:00",
                ),
            )

    def tearDown(self):
        self.temp.cleanup()

    def _classify_and_apply(self):
        runner = _StrictResidualRunner()
        claim = claim_historical_residual_classification(
            self.db, self.private, 1, runner=runner
        )
        self.assertEqual(("12",), claim.snapshot.residual_question_nos)
        result = run_claimed_historical_residual_classification(claim)
        self.assertEqual(1, result.question_count)
        self.assertEqual(["level2", "proposal", "verifier"], runner.stages)
        applied = apply_historical_residual_classification(self.db, self.private, 1)
        self.assertEqual(1, applied.inserted)
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")
        _, receipt = self._write_no_answer_authority()
        no_answer = assess_historical_residual_no_answer_source(
            self.db, self.private, 1, confirmation_receipt=receipt,
        )
        register_historical_residual_no_answer_source(
            self.db, self.private, 1,
            confirmation_token=no_answer["confirmation_token"],
            confirmation_receipt=receipt,
        )

    def test_exact_twenty_two_plus_one_classify_dry_admit_and_replay(self):
        with sqlite3.connect(self.db) as connection:
            draft = connection.execute(
                "SELECT edited_json FROM candidate_review_drafts "
                "WHERE import_job_id=1 AND source_question_no='12'"
            ).fetchone()
            edited = json.loads(draft[0])
            edited["primary_knowledge_point_code"] = "01.01.08"
            edited["related_knowledge_point_codes"] = []
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=? "
                "WHERE import_job_id=1 AND source_question_no='12'",
                (json.dumps(edited, ensure_ascii=False),),
            )
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        self.assertEqual("ready", dry.status)
        self.assertEqual("synthetic-paper.pdf", dry.source_filename)
        self.assertEqual(23, dry.total_question_count)
        self.assertEqual(("12",), dry.residual_question_nos)
        self.assertEqual(("12",), dry.eligible)
        with sqlite3.connect(self.db) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0]
        with self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(before, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])
        result = admit_historical_residual_questions(
            self.db, self.private, 1, confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "backups",
        )
        self.assertEqual(1, result.inserted)
        self.assertTrue(Path(result.backup_path).is_file())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(23, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM import_jobs WHERE id=1"
            ).fetchone()[0])
            primary, related = connection.execute(
                """SELECT kp.code,
                          (SELECT group_concat(rkp.code, ',')
                           FROM question_related_knowledge_points qr
                           JOIN knowledge_points rkp ON rkp.id=qr.knowledge_point_id
                           WHERE qr.question_id=q.id)
                   FROM question_sources s JOIN questions q ON q.id=s.question_id
                   JOIN knowledge_points kp ON kp.id=q.primary_knowledge_point_id
                   WHERE s.import_job_id=1 AND s.source_question_no='12'"""
            ).fetchone()
            self.assertEqual("01.01.06", primary)
            self.assertEqual("01.01.07", related)
        replay = admit_historical_residual_questions(
            self.db, self.private, 1, confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "backups",
        )
        self.assertEqual(0, replay.inserted)
        self.assertEqual(1, replay.already_present)

        with sqlite3.connect(self.db) as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE questions SET stem_markdown=stem_markdown || 'tampered'
                   WHERE id=(SELECT question_id FROM question_sources
                             WHERE import_job_id=1 AND source_question_no='12')"""
            )
        mutations = (
            "UPDATE question_options SET content_markdown='tampered' WHERE question_id=(SELECT question_id FROM question_sources WHERE import_job_id=1 AND source_question_no='12')",
            "DELETE FROM question_related_knowledge_points WHERE question_id=(SELECT question_id FROM question_sources WHERE import_job_id=1 AND source_question_no='12')",
            "DELETE FROM question_assets WHERE question_id=(SELECT question_id FROM question_sources WHERE import_job_id=1 AND source_question_no='12')",
        )
        for statement in mutations:
            with self.subTest(statement=statement), sqlite3.connect(self.db) as connection, \
                    self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement)
        with sqlite3.connect(self.db) as connection:
            count, graph_sha, _ = _formal_snapshot(connection, 1)
            frozen = connection.execute(
                "SELECT final_formal_batch_sha256 FROM historical_residual_admissions "
                "WHERE import_job_id=1"
            ).fetchone()[0]
        self.assertEqual(23, count)
        self.assertEqual(frozen, graph_sha)

    def test_completed_admission_freezes_every_formal_child_table(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        admit_historical_residual_questions(
            self.db, self.private, 1, confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "backups",
        )
        question = "(SELECT question_id FROM question_sources WHERE import_job_id=1 AND source_question_no='12')"
        mutations = (
            f"UPDATE question_sources SET source_pages_json='[]' WHERE question_id={question}",
            f"INSERT INTO question_options(question_id,option_code,content_markdown,display_order) VALUES({question},'Z','tampered',99)",
            f"INSERT INTO subquestions(question_id,display_order,stem_markdown) VALUES({question},99,'tampered')",
            f"INSERT INTO question_formulas(question_id,formula_latex,location,display_order) VALUES({question},'x','tampered',99)",
            f"INSERT INTO question_figures(question_id,relative_path,purpose,display_order,source_type,image_hash) VALUES({question},'tampered.png','tampered',99,'generated','{'0' * 64}')",
            f"DELETE FROM question_assets WHERE question_id={question}",
            f"DELETE FROM question_related_knowledge_points WHERE question_id={question}",
            f"INSERT INTO question_tags(question_id,tag_id) VALUES({question},(SELECT MIN(id) FROM tag_definitions))",
            f"INSERT INTO question_reviews(question_id,review_item,new_status,reviewer,reviewed_at) VALUES({question},'ocr','passed','attacker','2026-09-01T00:00:00+00:00')",
            f"INSERT INTO question_versions(question_id,version_no,version_status,snapshot_json) VALUES({question},99,'current','{{}}')",
        )
        for statement in mutations:
            with self.subTest(statement=statement), sqlite3.connect(self.db) as connection, \
                    self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement)

    def test_initialize_protects_legacy_completed_residual_formal_graph(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        admit_historical_residual_questions(
            self.db, self.private, 1, confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "backups",
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_web_admission_runs "
                "WHERE import_job_id=1 AND status='completed'"
            ).fetchone()[0])
            connection.execute("DROP VIEW completed_formal_admissions")
            connection.execute(
                "CREATE VIEW completed_formal_admissions AS "
                "SELECT import_job_id FROM import_web_admission_runs "
                "WHERE status='completed'"
            )
            connection.execute("DROP TRIGGER web_admission_protect_sources_insert")
            connection.execute(
                """CREATE TRIGGER web_admission_protect_sources_insert
                   BEFORE INSERT ON question_sources
                   WHEN EXISTS (
                       SELECT 1 FROM import_web_admission_runs r
                       WHERE r.import_job_id=NEW.import_job_id
                         AND r.status='completed'
                   )
                   BEGIN
                       SELECT RAISE(
                           ABORT, 'completed web admission source is immutable'
                       );
                   END"""
            )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            question_id = connection.execute(
                "SELECT question_id FROM question_sources "
                "WHERE import_job_id=1 ORDER BY question_id LIMIT 1"
            ).fetchone()[0]
            source_paper_id = connection.execute(
                "SELECT source_paper_id FROM import_jobs WHERE id=1"
            ).fetchone()[0]
            knowledge_ids = [row[0] for row in connection.execute(
                "SELECT id FROM knowledge_points ORDER BY id LIMIT 2"
            )]
            tag_id = connection.execute(
                "INSERT INTO tag_definitions(category,code,name) "
                "VALUES('method','legacy-residual-guard','历史保护测试')"
            ).lastrowid
            connection.execute(
                "INSERT INTO question_options"
                "(question_id,option_code,content_markdown,display_order) "
                "VALUES(?,'Z','legacy guard',99)", (question_id,),
            )
            subquestion_id = connection.execute(
                "INSERT INTO subquestions(question_id,display_order,stem_markdown) "
                "VALUES(?,99,'legacy guard')", (question_id,),
            ).lastrowid
            formula_id = connection.execute(
                "INSERT INTO question_formulas"
                "(question_id,formula_latex,location,display_order) "
                "VALUES(?,'x','legacy-guard',99)", (question_id,),
            ).lastrowid
            figure_id = connection.execute(
                "INSERT INTO question_figures"
                "(question_id,relative_path,purpose,display_order,source_type,image_hash) "
                "VALUES(?,'legacy-guard.png','legacy guard',99,'generated',?)",
                (question_id, "1" * 64),
            ).lastrowid
            connection.execute(
                "INSERT OR IGNORE INTO question_related_knowledge_points VALUES(?,?)",
                (question_id, knowledge_ids[1]),
            )
            connection.execute(
                "INSERT INTO question_tags(question_id,tag_id,note) VALUES(?,?,'legacy')",
                (question_id, tag_id),
            )
            asset_id = connection.execute(
                "INSERT INTO question_assets"
                "(question_id,import_job_id,asset_kind,relative_path,width,height,"
                "byte_size,sha256,review_status,display_order) "
                "VALUES(?,1,'question_figure','legacy-guard.png',1,1,1,?,"
                "'ai_review_passed',99)",
                (question_id, "2" * 64),
            ).lastrowid
            review_id = connection.execute(
                "INSERT INTO question_reviews"
                "(question_id,review_item,new_status,reviewer,reviewed_at,notes) "
                "VALUES(?,'ocr','passed','legacy','2026-09-01','legacy')",
                (question_id,),
            ).lastrowid
            version_id = connection.execute(
                "INSERT INTO question_versions"
                "(question_id,version_no,version_status,snapshot_json) "
                "VALUES(?,99,'archived','{}')", (question_id,),
            ).lastrowid
            extra_question_id = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,answer_markdown,region_code,
                    exam_year,exam_type_code,paper_name,source_question_no,
                    question_type_code,primary_knowledge_point_id,content_hash)
                   VALUES('LEGACY-RESIDUAL-ATTACK','attack','answer','TJ',2025,
                          'YK','attack','99','solution',?,'legacy-attack')""",
                (knowledge_ids[0],),
            ).lastrowid

        def database_rows():
            with closing(sqlite3.connect(self.db)) as connection:
                tables = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )]
                return {
                    table: connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    ).fetchall()
                    for table in tables
                }, connection.execute(
                    "SELECT name,seq FROM sqlite_sequence ORDER BY name"
                ).fetchall()

        before_rows, before_sequence = database_rows()
        initialize_database(self.db).close()
        initialize_database(self.db).close()
        self.assertEqual((before_rows, before_sequence), database_rows())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM historical_residual_admissions "
                "WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_web_admission_runs "
                "WHERE import_job_id=1 AND status='completed'"
            ).fetchone()[0])

        attacks = (
            ("question update", "UPDATE questions SET stem_markdown='tampered' WHERE id=?", (question_id,)),
            ("question delete", "DELETE FROM questions WHERE id=?", (question_id,)),
            ("source insert", "INSERT INTO question_sources VALUES(?,?,1,'99','[1]')", (extra_question_id, source_paper_id)),
            ("source update", "UPDATE question_sources SET source_pages_json='[]' WHERE question_id=?", (question_id,)),
            ("source delete", "DELETE FROM question_sources WHERE question_id=?", (question_id,)),
            ("option insert", "INSERT INTO question_options(question_id,option_code,content_markdown,display_order) VALUES(?,'Y','attack',100)", (question_id,)),
            ("option update", "UPDATE question_options SET content_markdown='attack' WHERE question_id=? AND display_order=99", (question_id,)),
            ("option delete", "DELETE FROM question_options WHERE question_id=? AND display_order=99", (question_id,)),
            ("subquestion insert", "INSERT INTO subquestions(question_id,display_order,stem_markdown) VALUES(?,100,'attack')", (question_id,)),
            ("subquestion update", "UPDATE subquestions SET stem_markdown='attack' WHERE id=?", (subquestion_id,)),
            ("subquestion delete", "DELETE FROM subquestions WHERE id=?", (subquestion_id,)),
            ("formula insert", "INSERT INTO question_formulas(question_id,formula_latex,location,display_order) VALUES(?,'y','attack',100)", (question_id,)),
            ("formula update", "UPDATE question_formulas SET formula_latex='attack' WHERE id=?", (formula_id,)),
            ("formula delete", "DELETE FROM question_formulas WHERE id=?", (formula_id,)),
            ("figure insert", "INSERT INTO question_figures(question_id,relative_path,purpose,display_order,source_type,image_hash) VALUES(?,'attack.png','attack',100,'generated',?)", (question_id, "3" * 64)),
            ("figure update", "UPDATE question_figures SET purpose='attack' WHERE id=?", (figure_id,)),
            ("figure delete", "DELETE FROM question_figures WHERE id=?", (figure_id,)),
            ("tag insert", "INSERT INTO question_tags VALUES(?,(SELECT MIN(id) FROM tag_definitions),'attack')", (question_id,)),
            ("tag update", "UPDATE question_tags SET note='attack' WHERE question_id=? AND tag_id=?", (question_id, tag_id)),
            ("tag delete", "DELETE FROM question_tags WHERE question_id=? AND tag_id=?", (question_id, tag_id)),
            ("knowledge insert", "INSERT INTO question_related_knowledge_points VALUES(?,(SELECT MAX(id) FROM knowledge_points))", (question_id,)),
            ("knowledge update", "UPDATE question_related_knowledge_points SET knowledge_point_id=(SELECT MIN(id) FROM knowledge_points) WHERE question_id=? AND knowledge_point_id=?", (question_id, knowledge_ids[1])),
            ("knowledge delete", "DELETE FROM question_related_knowledge_points WHERE question_id=? AND knowledge_point_id=?", (question_id, knowledge_ids[1])),
            ("asset insert", "INSERT INTO question_assets(question_id,import_job_id,asset_kind,relative_path,width,height,byte_size,sha256,review_status,display_order) VALUES(?,1,'question_figure','attack.png',1,1,1,?,'ai_review_passed',100)", (question_id, "4" * 64)),
            ("asset update", "UPDATE question_assets SET width=2 WHERE id=?", (asset_id,)),
            ("asset delete", "DELETE FROM question_assets WHERE id=?", (asset_id,)),
            ("review insert", "INSERT INTO question_reviews(question_id,review_item,new_status,reviewer,reviewed_at) VALUES(?,'ocr','passed','attack','2026-09-01')", (question_id,)),
            ("review update", "UPDATE question_reviews SET notes='attack' WHERE id=?", (review_id,)),
            ("review delete", "DELETE FROM question_reviews WHERE id=?", (review_id,)),
            ("version insert", "INSERT INTO question_versions(question_id,version_no,version_status,snapshot_json) VALUES(?,100,'archived','{}')", (question_id,)),
            ("version update", "UPDATE question_versions SET snapshot_json='[]' WHERE id=?", (version_id,)),
            ("version delete", "DELETE FROM question_versions WHERE id=?", (version_id,)),
        )
        for name, statement, parameters in attacks:
            with self.subTest(attack=name), closing(sqlite3.connect(self.db)) as connection, \
                    self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)

        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE questions SET deleted_at='2026-09-01',"
                "deletion_reason='unneeded',deletion_note='lifecycle',"
                "updated_at='2026-09-01' WHERE id=?", (question_id,),
            )
            connection.execute(
                "UPDATE questions SET deleted_at=NULL,deletion_reason=NULL,"
                "deletion_note=NULL,updated_at='2026-09-02' WHERE id=?",
                (question_id,),
            )
            self.assertEqual((None, None, None, "2026-09-02"), connection.execute(
                "SELECT deleted_at,deletion_reason,deletion_note,updated_at "
                "FROM questions WHERE id=?", (question_id,),
            ).fetchone())

    def test_production_recovery_resume_then_residual_admission_is_exact_and_idempotent(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP TRIGGER historical_v1_pipeline_resumptions_immutable")
            connection.execute("DROP TRIGGER historical_v1_pipeline_resumptions_delete_immutable")
            connection.execute("DROP TRIGGER historical_v1_crop_recoveries_immutable")
            connection.execute("DROP TRIGGER historical_v1_crop_recoveries_delete_immutable")
            connection.execute("DELETE FROM historical_v1_pipeline_resumptions WHERE import_job_id=1")
            connection.execute("DELETE FROM historical_v1_crop_recoveries WHERE import_job_id=1")
            connection.execute("DELETE FROM import_answer_sources WHERE import_job_id=1")
            connection.execute("DELETE FROM import_candidate_audit_runs WHERE import_job_id=1")
            connection.execute("UPDATE import_jobs SET status='needs_review' WHERE id=1")
            candidate_raw = (self.job_dir / "candidate_questions.json").read_bytes()
            candidate = json.loads(candidate_raw)
            candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
            for question in candidate["questions"]:
                number = question["source_question_no"]
                if number == "12":
                    continue
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at,
                        approval_source,approval_evidence_json)
                       VALUES(1,?,?,?,?,'approved',1,?,'human',?)""",
                    (number, candidate_sha,
                     json.dumps(question, ensure_ascii=False, separators=(",", ":")),
                     json.dumps(question, ensure_ascii=False, separators=(",", ":")),
                     "2026-07-16T00:00:00+00:00",
                     json.dumps({"method": "historical-migration"})),
                )
        initialize_database(self.db).close()
        self._replace_synthetic_crop_with_legacy_v1()
        old_graph = self._old_formal_graph()

        recovered = recover_historical_v1_crops(self.db, self.private, 1)
        self.assertEqual("recovered", recovered.status)
        crop_raw = (self.job_dir / "question_crops.json").read_bytes()
        crop = json.loads(crop_raw)
        record_crop_ai_review(self.db, self.private, {
            "version": 1, "import_job_id": 1,
            "input_generation_id": crop["generation_id"],
            "input_manifest_sha256": hashlib.sha256(crop_raw).hexdigest(),
            "reviewer_run_id": "production-e2e-independent-review",
            "questions": [{
                "question_no": entry["question_no"],
                "status": "ai_review_passed", "warnings": [],
            } for entry in crop["questions"]],
        })
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE import_jobs SET status='needs_review' WHERE id=1")
        resumed = resume_historical_v1_fresh_pipeline(self.db, self.private, 1)
        self.assertEqual("resumed", resumed.status)

        crop_raw = (self.job_dir / "question_crops.json").read_bytes()
        crop = json.loads(crop_raw)
        candidate_raw = (self.job_dir / "candidate_questions.json").read_bytes()
        audit_raw = (self.job_dir / "ai_audit.json").read_bytes()
        candidate = json.loads(candidate_raw)
        reviewed_at = "2026-09-01T01:00:00+00:00"
        audit_completed_at = "2026-07-16T00:00:00+00:00"
        q12 = next(item for item in candidate["questions"] if item["source_question_no"] == "12")
        edited = {**q12, "stem_markdown": q12["stem_markdown"] + "（生产链路人工确认）"}
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_candidate_extraction_runs
                   SET status='completed',input_crop_generation_id=?,input_manifest_sha256=?,
                       input_manifest_signature=?,output_sha256=?,output_byte_size=?
                   WHERE import_job_id=1""",
                (crop["generation_id"], hashlib.sha256(crop_raw).hexdigest(),
                 crop["signature"], hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw)),
            )
            connection.execute(
                """INSERT INTO import_candidate_audit_runs
                   (import_job_id,status,question_count,processed_questions,codex_run_id,
                    input_candidate_sha256,input_candidate_byte_size,input_crop_generation_id,
                    input_manifest_sha256,input_manifest_signature,output_sha256,
                    output_byte_size,completed_at,updated_at)
                   VALUES(1,'completed',23,23,'production-e2e-audit',?,?,?,?,?,?,?, ?,?)""",
                (hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw),
                 crop["generation_id"], hashlib.sha256(crop_raw).hexdigest(), crop["signature"],
                 hashlib.sha256(audit_raw).hexdigest(), len(audit_raw),
                 audit_completed_at, audit_completed_at),
            )
            connection.execute(
                """UPDATE candidate_review_drafts SET source_candidate_sha256=?,
                   source_snapshot_json=?,edited_json=?,status='approved',version=version+1,
                   reviewed_at=?,approval_source='human',approval_evidence_json=?
                   WHERE import_job_id=1 AND source_question_no='12'""",
                (hashlib.sha256(candidate_raw).hexdigest(),
                 json.dumps(q12, ensure_ascii=False, separators=(",", ":")),
                 json.dumps(edited, ensure_ascii=False, separators=(",", ":")),
                 reviewed_at, json.dumps({
                     "method": "workbench_quick", "reviewed_at": reviewed_at,
                 }, separators=(",", ":"))),
            )
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        self.assertEqual(("12",), dry.residual_question_nos)
        self.assertEqual("ready", dry.status)
        first = admit_historical_residual_questions(
            self.db, self.private, 1, confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "production-backups",
        )
        self.assertEqual(1, first.inserted)
        self.assertEqual(old_graph, self._old_formal_graph())
        second = admit_historical_residual_questions(
            self.db, self.private, 1, confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "production-backups",
        )
        self.assertEqual((0, 1), (second.inserted, second.already_present))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1 "
                "AND source_question_no='12'"
            ).fetchone()[0])

    def test_frozen_formal_baseline_drift_fails_closed(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE questions SET stem_markdown=stem_markdown || 'drift'
                   WHERE id=(SELECT question_id FROM question_sources
                             WHERE import_job_id=1 LIMIT 1)"""
            )
        with self.assertRaisesRegex(HistoricalResidualError, "冻结正式题基线"):
            claim_historical_residual_classification(
                self.db, self.private, 1, runner=_StrictResidualRunner()
            )

    def test_stale_or_unapproved_residual_draft_fails_closed(self):
        for assignment in (
            ("status='pending',approval_source=NULL,reviewed_at=NULL",),
            ("source_candidate_sha256=?", "0" * 64),
            ("deleted_at=?", "2026-07-16T00:00:00+00:00"),
        ):
            with self.subTest(assignment=assignment[0]):
                with sqlite3.connect(self.db) as connection:
                    original = connection.execute(
                        "SELECT * FROM candidate_review_drafts WHERE import_job_id=1"
                    ).fetchone()
                    connection.execute(
                        f"UPDATE candidate_review_drafts SET {assignment[0]} WHERE import_job_id=1",
                        assignment[1:],
                    )
                with self.assertRaises(HistoricalResidualError):
                    claim_historical_residual_classification(
                        self.db, self.private, 1, runner=_StrictResidualRunner()
                    )
                with sqlite3.connect(self.db) as connection:
                    connection.execute("DELETE FROM candidate_review_drafts WHERE import_job_id=1")
                    columns = [row[1] for row in connection.execute(
                        "PRAGMA table_info(candidate_review_drafts)"
                    )]
                    connection.execute(
                        f"INSERT INTO candidate_review_drafts ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)})", tuple(original),
                    )

    def test_classification_anchor_drift_and_confirmation_mismatch_fail_closed(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        with self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1, confirmation_token="0" * 64,
                backup_dir=self.root / "backups",
            )
        self.assertFalse((self.root / "backups").exists())
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP TRIGGER candidate_knowledge_classifications_immutable")
            connection.execute(
                "UPDATE candidate_knowledge_classifications SET evidence_sha256=?",
                ("9" * 64,),
            )
        blocked = assess_historical_residual_admission(self.db, self.private, 1)
        self.assertEqual("blocked", blocked.status)
        self.assertNotEqual(dry.confirmation_token, blocked.confirmation_token)

    def test_independent_high_confidence_adjudicator_can_set_a_third_valid_vote(self):
        claim = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_AdjudicatingResidualRunner()
        )
        run_claimed_historical_residual_classification(claim)
        applied = apply_historical_residual_classification(self.db, self.private, 1)
        self.assertTrue(applied.applied)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("01.01.08", "codex_adjudicated"),
                connection.execute(
                    """SELECT primary_knowledge_point_code,approval_source
                       FROM candidate_knowledge_classifications
                       WHERE import_job_id=1 AND source_question_no='12'"""
                ).fetchone(),
            )

    def test_concurrent_drift_after_confirmation_rolls_back_insert(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)

        def drift(connection):
            connection.execute(
                "UPDATE candidate_review_drafts SET status='pending',approval_source=NULL,reviewed_at=NULL "
                "WHERE import_job_id=1 AND source_question_no='12'"
            )

        with self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1, confirmation_token=dry.confirmation_token,
                backup_dir=self.root / "backups", _transaction_callback=drift,
            )
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(22, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])
        self.assertFalse((self.root / "backups").exists())

    def test_database_replaced_after_begin_rolls_back_without_writing_decoy(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        moved = self.root / "original-question-bank.db"

        def replace_database(_connection):
            self.db.rename(moved)
            shutil.copy2(moved, self.db)

        try:
            with self.assertRaises(HistoricalResidualError):
                admit_historical_residual_questions(
                    self.db, self.private, 1,
                    confirmation_token=dry.confirmation_token,
                    backup_dir=self.root / "backups",
                    _transaction_callback=replace_database,
                )
            with sqlite3.connect(self.db) as decoy:
                self.assertEqual(22, decoy.execute(
                    "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
                ).fetchone()[0])
        finally:
            if self.db.exists():
                self.db.unlink()
            moved.rename(self.db)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(22, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])

    def test_backup_fsyncs_file_then_open_target_directory_before_return(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        real_fsync = admit_module.os.fsync
        fsync_kinds = []

        def record_fsync(descriptor):
            mode = os.fstat(descriptor).st_mode
            fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
            return real_fsync(descriptor)

        with patch.object(admit_module.os, "fsync", side_effect=record_fsync):
            backup, _digest = admit_module.backup_database(self.db, backup_dir)

        self.assertTrue(backup.is_file())
        self.assertEqual(["file", "directory"], fsync_kinds)

    def test_backup_target_swap_after_precreate_fails_closed(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        backup_dir = self.root / "backups"
        victim = self.root / "victim.db"
        victim.write_bytes(b"must-not-change")
        displaced = self.root / "displaced-backup.db"
        real_write = admit_module.os.write
        attacked = False

        def swap_target(descriptor, data):
            nonlocal attacked
            if not attacked:
                candidates = list(backup_dir.glob("question-bank-*.db"))
                if candidates:
                    attacked = True
                    candidates[0].rename(displaced)
                    candidates[0].symlink_to(victim)
            return real_write(descriptor, data)

        with patch.object(admit_module.os, "write", side_effect=swap_target), \
                self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1,
                confirmation_token=dry.confirmation_token,
                backup_dir=backup_dir,
            )
        self.assertTrue(attacked)
        self.assertEqual(b"must-not-change", victim.read_bytes())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(22, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])

    def test_backup_target_hardlink_after_precreate_fails_closed(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        backup_dir = self.root / "backups"
        extra_link = self.root / "attacker-hardlink.db"
        real_write = admit_module.os.write
        attacked = False

        def hardlink_target(descriptor, data):
            nonlocal attacked
            if not attacked:
                candidates = list(backup_dir.glob("question-bank-*.db"))
                if candidates:
                    attacked = True
                    os.link(candidates[0], extra_link)
            return real_write(descriptor, data)

        with patch.object(admit_module.os, "write", side_effect=hardlink_target), \
                self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1,
                confirmation_token=dry.confirmation_token,
                backup_dir=backup_dir,
            )
        self.assertTrue(attacked)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM historical_residual_admissions"
            ).fetchone()[0])

    def test_backup_parent_replacement_after_open_fails_closed_without_bad_record(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        backup_dir = self.root / "backups"
        displaced_dir = self.root / "backups-displaced"
        real_write = admit_module.os.write
        attacked = False

        def swap_parent(descriptor, data):
            nonlocal attacked
            if not attacked and backup_dir.is_dir():
                attacked = True
                backup_dir.rename(displaced_dir)
                backup_dir.mkdir()
            return real_write(descriptor, data)

        with patch.object(admit_module.os, "write", side_effect=swap_parent), \
                self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1,
                confirmation_token=dry.confirmation_token,
                backup_dir=backup_dir,
            )
        self.assertTrue(attacked)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM historical_residual_admissions"
            ).fetchone()[0])
            self.assertEqual(22, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])

    def test_post_backup_failure_rolls_back_and_removes_only_new_backup(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        existing = backup_dir / "existing.db"
        existing.write_bytes(b"preexisting backup")

        with patch(
            "src.reviewing.historical_residual._insert_one",
            side_effect=sqlite3.IntegrityError("synthetic post-backup insert failure"),
        ), self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1,
                confirmation_token=dry.confirmation_token,
                backup_dir=backup_dir,
            )

        self.assertEqual([existing], list(backup_dir.iterdir()))
        self.assertEqual(b"preexisting backup", existing.read_bytes())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                22,
                connection.execute(
                    "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM historical_residual_admissions WHERE import_job_id=1"
                ).fetchone()[0],
            )

    def test_cleanup_does_not_unlink_inode_swapped_after_identity_check(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        backup_dir = self.root / "backups"
        displaced = self.root / "displaced-created-backup.db"
        swapped_bytes = b"synthetic swapped-in inode"
        real_unlink = os.unlink
        attacks = []

        def swap_before_unlink(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == backup_dir and candidate.name.startswith("question-bank-"):
                attacks.append(candidate)
                os.rename(candidate, displaced)
                candidate.write_bytes(swapped_bytes)
            return real_unlink(path, *args, **kwargs)

        with patch(
            "src.reviewing.historical_residual._insert_one",
            side_effect=sqlite3.IntegrityError("synthetic post-backup insert failure"),
        ), patch(
            "src.reviewing.historical_residual.os.unlink",
            side_effect=swap_before_unlink,
        ), self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1,
                confirmation_token=dry.confirmation_token,
                backup_dir=backup_dir,
            )

        candidates = list(backup_dir.glob("question-bank-*.db"))
        if attacks:
            self.assertEqual(1, len(candidates))
            self.assertEqual(swapped_bytes, candidates[0].read_bytes())
            self.assertTrue(displaced.is_file())
        else:
            self.assertEqual([], candidates)
            self.assertFalse(displaced.exists())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM historical_residual_admissions WHERE import_job_id=1"
            ).fetchone()[0])

    def test_cleanup_mismatch_restores_preexisting_same_name_inode(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        candidate = backup_dir / "question-bank-fixed.db"
        original_bytes = b"preexisting exact-name backup"
        candidate.write_bytes(original_bytes)
        other = backup_dir / "other.db"
        other.write_bytes(b"different inode")
        other_info = other.stat()

        _remove_unreferenced_new_backup(
            self.db, candidate,
            (other_info.st_dev, other_info.st_ino, other_info.st_size),
        )

        self.assertEqual(original_bytes, candidate.read_bytes())
        self.assertEqual([], list(backup_dir.glob(".historical-residual-cleanup-*")))

    def test_referenced_backup_survives_exact_inode_cleanup_attempt(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        result = admit_historical_residual_questions(
            self.db, self.private, 1,
            confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "backups",
        )
        backup = Path(result.backup_path)
        info = backup.stat()

        _remove_unreferenced_new_backup(
            self.db, backup, (info.st_dev, info.st_ino, info.st_size),
        )

        self.assertTrue(backup.is_file())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                (str(backup),),
                connection.execute(
                    "SELECT backup_path FROM historical_residual_admissions WHERE import_job_id=1"
                ).fetchone(),
            )

    def test_cleanup_rejects_swap_query_swap_back_database(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        result = admit_historical_residual_questions(
            self.db, self.private, 1,
            confirmation_token=dry.confirmation_token,
            backup_dir=self.root / "backups",
        )
        backup = Path(result.backup_path)
        info = backup.stat()
        identity = (info.st_dev, info.st_ino, info.st_size)
        moved = self.root / "cleanup-original.db"
        decoy = self.root / "cleanup-decoy.db"
        shutil.copy2(self.db, decoy)
        with sqlite3.connect(decoy) as connection:
            connection.execute("DROP TRIGGER historical_residual_admissions_delete_immutable")
            connection.execute("DELETE FROM historical_residual_admissions")
        real_connect = sqlite3.connect
        attacked = False

        def swap_query_swap_back(*args, **kwargs):
            nonlocal attacked
            if not attacked:
                attacked = True
                self.db.rename(moved)
                decoy.rename(self.db)
                connection = real_connect(*args, **kwargs)
                self.db.rename(decoy)
                moved.rename(self.db)
                return connection
            return real_connect(*args, **kwargs)

        with patch.object(
            residual_module.sqlite3, "connect", side_effect=swap_query_swap_back,
        ):
            _remove_unreferenced_new_backup(self.db, backup, identity)
        self.assertTrue(attacked)
        self.assertTrue(backup.is_file())

    def test_ambiguous_commit_ack_keeps_durable_reference_and_backup(self):
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        real_connect = sqlite3.connect

        class AmbiguousCommitConnection(sqlite3.Connection):
            def commit(connection_self):
                super().commit()
                if connection_self.execute(
                    "SELECT 1 FROM historical_residual_admissions WHERE import_job_id=1"
                ).fetchone() is not None:
                    raise sqlite3.OperationalError("synthetic ambiguous commit acknowledgement")

        def ambiguous_connect(*args, **kwargs):
            kwargs["factory"] = AmbiguousCommitConnection
            return real_connect(*args, **kwargs)

        with patch(
            "src.reviewing.historical_residual.sqlite3.connect",
            side_effect=ambiguous_connect,
        ), self.assertRaises(HistoricalResidualError):
            admit_historical_residual_questions(
                self.db, self.private, 1,
                confirmation_token=dry.confirmation_token,
                backup_dir=self.root / "backups",
            )

        with real_connect(self.db) as connection:
            row = connection.execute(
                "SELECT backup_path,backup_sha256 FROM historical_residual_admissions "
                "WHERE import_job_id=1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM import_jobs WHERE id=1"
            ).fetchone()[0])
        backup = Path(row[0])
        self.assertTrue(backup.is_file())
        self.assertEqual(row[1], hashlib.sha256(backup.read_bytes()).hexdigest())

    def test_concurrent_reference_commit_precedes_cleanup_and_preserves_inode(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        backup = backup_dir / "question-bank-concurrent.db"
        backup.write_bytes(b"synthetic concurrent referenced backup")
        info = backup.stat()
        identity = (info.st_dev, info.st_ino, info.st_size)
        cleanup_errors = []

        writer = sqlite3.connect(self.db)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            """INSERT INTO historical_residual_admissions
               (import_job_id,confirmation_token,assessment_sha256,
                full_question_nos_json,existing_question_nos_json,
                residual_question_nos_json,baseline_formal_question_count,
                baseline_formal_batch_sha256,candidate_sha256,audit_sha256,
                crop_manifest_sha256,classification_evidence_sha256,
                backup_path,backup_sha256,inserted_count,
                final_formal_question_count,final_formal_batch_sha256,completed_at)
               VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "1" * 64, "2" * 64, "[1]", "[]", "[1]", 22,
                "3" * 64, "4" * 64, "5" * 64, "6" * 64, "7" * 64,
                str(backup), hashlib.sha256(backup.read_bytes()).hexdigest(),
                1, 23, "8" * 64, "2026-09-01T00:00:00+00:00",
            ),
        )

        def cleanup():
            try:
                _remove_unreferenced_new_backup(self.db, backup, identity)
            except BaseException as error:
                cleanup_errors.append(error)

        thread = threading.Thread(target=cleanup)
        thread.start()
        writer.commit()
        writer.close()
        thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual([], cleanup_errors)
        self.assertEqual(b"synthetic concurrent referenced backup", backup.read_bytes())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                (str(backup),),
                connection.execute(
                    "SELECT backup_path FROM historical_residual_admissions WHERE import_job_id=1"
                ).fetchone(),
            )

    def test_failed_classification_releases_processing_lease_durably(self):
        class FailingRunner:
            def run(self, _stage, _prompt):
                raise RuntimeError("synthetic runner failure")

        claim = claim_historical_residual_classification(
            self.db, self.private, 1, runner=FailingRunner()
        )
        with self.assertRaises(HistoricalResidualError):
            run_claimed_historical_residual_classification(claim)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("failed", None, "历史恢复剩余题证据无效"),
                connection.execute(
                    "SELECT status,claim_token,error_message FROM "
                    "historical_residual_classification_runs WHERE import_job_id=1"
                ).fetchone(),
            )

    def test_classification_stage_rejects_swap_query_swap_back_database(self):
        claim = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_StrictResidualRunner()
        )
        moved = self.root / "classification-original.db"
        real_connect = sqlite3.connect
        attacked = False

        def swap_query_swap_back(*args, **kwargs):
            nonlocal attacked
            if not attacked:
                attacked = True
                self.db.rename(moved)
                shutil.copy2(moved, self.db)
                connection = real_connect(*args, **kwargs)
                self.db.unlink()
                moved.rename(self.db)
                return connection
            return real_connect(*args, **kwargs)

        with patch.object(
            residual_module.sqlite3, "connect", side_effect=swap_query_swap_back,
        ), self.assertRaises(HistoricalResidualError):
            run_claimed_historical_residual_classification(claim)
        self.assertTrue(attacked)
        with real_connect(self.db) as connection:
            self.assertNotEqual("completed", connection.execute(
                "SELECT status FROM historical_residual_classification_runs "
                "WHERE import_job_id=1"
            ).fetchone()[0])

    def test_candidate_audit_or_recovery_number_drift_fails_closed(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_candidate_audit_runs SET output_sha256=? WHERE import_job_id=1",
                ("0" * 64,),
            )
        with self.assertRaises(HistoricalResidualError):
            claim_historical_residual_classification(
                self.db, self.private, 1, runner=_StrictResidualRunner()
            )
        with sqlite3.connect(self.db) as connection:
            audit_sha = hashlib.sha256((self.job_dir / "ai_audit.json").read_bytes()).hexdigest()
            connection.execute(
                "UPDATE import_candidate_audit_runs SET output_sha256=? WHERE import_job_id=1",
                (audit_sha,),
            )
            connection.execute("DROP TRIGGER historical_v1_crop_recoveries_immutable")
            connection.execute(
                "UPDATE historical_v1_crop_recoveries SET question_nos_json=? WHERE import_job_id=1",
                (json.dumps(list(range(1, 25))),),
            )
        with self.assertRaises(HistoricalResidualError):
            claim_historical_residual_classification(
                self.db, self.private, 1, runner=_StrictResidualRunner()
            )

    def test_preexisting_classification_is_not_reused_or_overwritten(self):
        candidate = json.loads((self.job_dir / "candidate_questions.json").read_text())
        edited = next(item for item in candidate["questions"] if item["source_question_no"] == "12")
        with sqlite3.connect(self.db) as connection:
            draft = connection.execute(
                "SELECT version,edited_json FROM candidate_review_drafts WHERE import_job_id=1"
            ).fetchone()
            edited = json.loads(draft[1])
            connection.execute(
                """INSERT INTO candidate_knowledge_classifications
                   (import_job_id,source_question_no,approved_draft_version,edited_sha256,
                    classification_scope_sha256,primary_knowledge_point_code,
                    related_knowledge_point_codes_json,classifier,reviewer,approval_source,
                    classifier_run_id,evidence_sha256,reason,created_at)
                   VALUES(1,'12',?,?,NULL,'01.01.06','["01.01.07"]','old','old',
                          'human','stale-old-classification',?,'old',?)""",
                (
                    draft[0], hashlib.sha256(json.dumps(
                        edited, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest(), "8" * 64, "2026-01-01T00:00:00+00:00",
                ),
            )
        claim = claim_historical_residual_classification(
            self.db, self.private, 1, runner=_StrictResidualRunner()
        )
        run_claimed_historical_residual_classification(claim)
        with self.assertRaises(HistoricalResidualError):
            apply_historical_residual_classification(self.db, self.private, 1)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("stale-old-classification", connection.execute(
                "SELECT classifier_run_id FROM candidate_knowledge_classifications"
            ).fetchone()[0])

    def test_residual_required_figure_without_approved_asset_is_blocked(self):
        candidate_path = self.job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text())
        question = next(
            item for item in candidate["questions"] if item["source_question_no"] == "12"
        )
        question["figure_required"] = True
        question["figure_notes"] = "合成必要配图缺失"
        candidate_path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        candidate_raw = candidate_path.read_bytes()
        figure_path = self.job_dir / "figure_assets.json"
        figure = json.loads(figure_path.read_text())
        figure["assets"] = []
        figure = sign_manifest(
            load_hmac_key(self.job_dir),
            {key: value for key, value in figure.items() if key != "signature"},
        )
        figure_path.write_text(json.dumps(figure, ensure_ascii=False), encoding="utf-8")
        reviewed_at = "2026-07-16T10:00:00+08:00"
        edited = copy.deepcopy(question)
        edited["stem_markdown"] += "（人工修订）"
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_candidate_audit_runs
                   SET input_candidate_sha256=?,input_candidate_byte_size=?
                   WHERE import_job_id=1""",
                (hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw)),
            )
            connection.execute(
                "UPDATE import_answer_sources SET candidate_sha256=? WHERE import_job_id=1",
                (hashlib.sha256(candidate_raw).hexdigest(),),
            )
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET source_candidate_sha256=?,source_snapshot_json=?,edited_json=?,
                       reviewed_at=?,approval_evidence_json=?
                   WHERE import_job_id=1 AND source_question_no='12'""",
                (
                    hashlib.sha256(candidate_raw).hexdigest(),
                    json.dumps(question, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(edited, ensure_ascii=False, separators=(",", ":")),
                    reviewed_at,
                    json.dumps(
                        {"method": "workbench_quick", "reviewed_at": reviewed_at},
                        separators=(",", ":"),
                    ),
                ),
            )
        self._classify_and_apply()
        dry = assess_historical_residual_admission(self.db, self.private, 1)
        self.assertEqual("blocked", dry.status)
        self.assertIn("missing_approved_figure", dry.ineligible[0]["reasons"])


if __name__ == "__main__":
    unittest.main()
