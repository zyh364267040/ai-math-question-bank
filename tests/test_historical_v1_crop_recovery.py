import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import src.processing.historical_v1_crop_recovery as recovery_module
from src.database.initialize import initialize_database
from src.processing.historical_v1_crop_recovery import (
    HistoricalV1RecoveryError,
    assess_historical_v1_recovery,
    assess_historical_v1_resume,
    recover_historical_v1_crops,
    resume_historical_v1_fresh_pipeline,
)
from src.processing.crop_review import record_crop_ai_review
from src.processing.candidate_extractor import (
    CandidateExtractionError,
    claim_candidate_extraction,
)
from src.reviewing.candidate_auditor import _database_input


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _write_png(path, size, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    content = path.read_bytes()
    return len(content), hashlib.sha256(content).hexdigest()


class HistoricalV1CropRecoveryTests(unittest.TestCase):
    def test_intermediate_symlink_and_missing_nofollow_primitive_fail_closed(self):
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(self.root, target_is_directory=True)
        for operation in (assess_historical_v1_recovery, recover_historical_v1_crops):
            with self.subTest(operation=operation.__name__, attack="intermediate_symlink"):
                with self.assertRaises(HistoricalV1RecoveryError):
                    operation(
                        linked_root / self.db.name,
                        linked_root / self.private.relative_to(self.root),
                        self.job_id,
                    )
        with patch.object(recovery_module.os, "O_NOFOLLOW", 0):
            with self.assertRaises(HistoricalV1RecoveryError):
                assess_historical_v1_recovery(self.db, self.private, self.job_id)
        with patch.object(recovery_module.os, "O_DIRECTORY", 0):
            with self.assertRaises(HistoricalV1RecoveryError):
                assess_historical_v1_resume(self.db, self.private, self.job_id)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.db = self.root / "question-bank.db"
        initialize_database(self.db).close()
        # Mirrors the real legacy shape: 22 already-formal questions plus one
        # additional historical candidate/draft that must never be auto-admitted.
        self.question_count = 23
        self.formal_count = 22
        source_content = b"%PDF-1.4\n% historical synthetic source\n%%EOF\n"
        source_sha = hashlib.sha256(source_content).hexdigest()
        self.source_sha = source_sha
        source_path = self.private / "raw_papers/TJ/unknown/legacy.pdf"
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(source_content)
        with sqlite3.connect(self.db) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name)
                   VALUES (?,?,'legacy.pdf','raw_papers/TJ/unknown/legacy.pdf',
                           'TJ','QT','历史合成卷')""",
                (source_sha, len(source_content)),
            ).lastrowid
            self.source_id = source_id
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'needs_review')",
                (source_id,),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages,
                    published_batch_id,source_pdf_sha256)
                   VALUES (?,'completed',300,2,2,'legacy-render',?)""",
                (self.job_id, source_sha),
            )

        self.job_dir = self.private / "processing" / f"import_job_{self.job_id}"
        page_entries = []
        for number, color in ((1, "white"), (2, "ivory")):
            relative = f"pages/page_{number:03d}.png"
            size, digest = _write_png(self.job_dir / relative, (240, 320), color)
            page_entries.append({
                "page_number": number,
                "relative_path": relative,
                "pixel_width": 240,
                "pixel_height": 320,
                "byte_size": size,
                "sha256": digest,
            })
        render = {
            "version": 1,
            "import_job_id": self.job_id,
            "dpi": 300,
            "source_pdf_sha256": source_sha,
            "source_page_count": 2,
            "page_start": 1,
            "page_end": 2,
            "page_count": 2,
            "pages": page_entries,
        }
        render_raw = _json_bytes(render)
        self.production_render = render
        (self.job_dir / "render_manifest.json").write_bytes(render_raw)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET manifest_sha256=?,manifest_byte_size=? WHERE import_job_id=?""",
                (hashlib.sha256(render_raw).hexdigest(), len(render_raw), self.job_id),
            )

        region_questions = []
        legacy_questions = []
        for number in range(1, self.question_count + 1):
            page = 1 if number <= 2 else 2
            top = 0.05 if number % 2 else 0.52
            bottom = 0.47 if number % 2 else 0.94
            pixels = [12, math.floor(top * 320), 228, math.ceil(bottom * 320)]
            region_questions.append({
                "question_no": number,
                "regions": [{"page_number": page, "bbox_normalized": [0.05, top, 0.95, bottom]}],
                "warnings": [],
                "confidence": "high",
            })
            relative = f"question_crops/Q{number:03d}.png"
            size, digest = _write_png(
                self.job_dir / relative,
                (pixels[2] - pixels[0], pixels[3] - pixels[1]),
                (number * 20, 80, 120),
            )
            legacy_questions.append({
                "question_no": number,
                "regions": [{"page_number": page, "bbox": pixels}],
                "composition": {"mode": "single", "region_count": 1},
                "output_relative_path": relative,
                "width": pixels[2] - pixels[0],
                "height": pixels[3] - pixels[1],
                "byte_size": size,
                "sha256": digest,
                "crop_status": "generated",
                "review_status": "ai_review_passed",
                "warnings": [],
            })
        regions = {
            "version": 1,
            "import_job_id": self.job_id,
            "question_count": self.question_count,
            "questions": region_questions,
        }
        regions_raw = _json_bytes(regions)
        (self.job_dir / "question_regions.json").write_bytes(regions_raw)
        legacy = {
            "version": 1,
            "import_job_id": self.job_id,
            "question_count": self.question_count,
            "source_pages": [{
                key: entry[key]
                for key in ("page_number", "relative_path", "pixel_width", "pixel_height", "sha256")
            } for entry in page_entries],
            "questions": legacy_questions,
            "review_status": "approved",
            "review_summary": {
                "approved_count": self.question_count,
                "rejected_count": 0,
                "pending_count": 0,
            },
        }
        self.legacy_raw = _json_bytes(legacy)
        (self.job_dir / "question_crops.json").write_bytes(self.legacy_raw)

        candidate_questions = []
        for number in range(1, self.question_count + 1):
            candidate_questions.append({
                "source_question_no": str(number),
                "stem_markdown": f"历史合成题 {number}",
                "question_type_code": "solution",
                "options": [],
                "subquestions": [],
                "answer_markdown": "",
                "analysis_markdown": "",
                "source_pages": [1 if number <= 2 else 2],
                "primary_knowledge_point_code": "",
                "related_knowledge_point_codes": [],
                "figure_required": False,
                "extraction_confidence": "high",
                "warnings": [],
            })
        candidate = {
            "version": 1,
            "import_job_id": self.job_id,
            "source_paper_id": source_id,
            "question_count": self.question_count,
            "questions": candidate_questions,
        }
        candidate_raw = _json_bytes(candidate)
        (self.job_dir / "candidate_questions.json").write_bytes(candidate_raw)
        candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
        now = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,question_count,processed_pages,error_message,
                    codex_run_id,result_manifest_sha256,render_manifest_sha256,
                    source_pdf_sha256,updated_at)
                   VALUES (?,'failed',?,?,NULL,'legacy-codex-run',?,?,?,?)""",
                (self.job_id, self.question_count, 2,
                 hashlib.sha256(regions_raw).hexdigest(),
                 hashlib.sha256(render_raw).hexdigest(), source_sha, now),
            )
            connection.execute(
                """INSERT INTO import_candidate_extraction_runs
                   (import_job_id,status,question_count,processed_questions,codex_run_id,
                    input_crop_generation_id,input_manifest_sha256,input_manifest_signature,
                    output_sha256,output_byte_size,completed_at,updated_at)
                   VALUES (?,'completed',?,?,'legacy-extract',?,?,?, ?,?,?,?)""",
                (self.job_id, self.question_count, self.question_count,
                 "0" * 32, "b" * 64, "c" * 64,
                 candidate_sha, len(candidate_raw), now, now),
            )
            for question in candidate_questions:
                number = question["source_question_no"]
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at,
                        approval_source,approval_evidence_json)
                       VALUES (?,?,?,?,?,'approved',2,?,'human',?)""",
                    (self.job_id, number, candidate_sha,
                     json.dumps(question, ensure_ascii=False),
                     json.dumps({**question, "stem_markdown": question["stem_markdown"] + "（人工编辑）"}, ensure_ascii=False),
                     now, json.dumps({"method": "historical-human"})),
                )
            kp = connection.execute(
                "SELECT id FROM knowledge_points WHERE level=3 ORDER BY id LIMIT 1"
            ).fetchone()[0]
            for number in range(1, self.formal_count + 1):
                question_id = connection.execute(
                    """INSERT INTO questions
                       (question_code,stem_markdown,answer_markdown,answer_status,
                        region_code,exam_type_code,question_type_code,
                        primary_knowledge_point_id,content_hash)
                       VALUES (?,?, '', 'missing','TJ','QT','solution',?,?)""",
                    (f"FORMAL-{self.job_id}-{number}", f"正式题 {number}", kp,
                     hashlib.sha256(f"formal-{number}".encode()).hexdigest()),
                ).lastrowid
                connection.execute(
                    """INSERT INTO question_sources
                       (question_id,source_paper_id,import_job_id,source_question_no,source_pages_json)
                       VALUES (?,?,?,?,?)""",
                    (question_id, source_id, self.job_id, str(number), json.dumps([1])),
                )

    def tearDown(self):
        self.temporary.cleanup()

    def _tree_digest(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            if any(part in {
                ".split_locks", ".crop_artifacts.lock", ".upload_manifest_hmac.key",
            } for part in path.parts):
                continue
            if path.is_symlink():
                result[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                result[relative] = "dir"
        return result

    def _formal_rows(self):
        with sqlite3.connect(self.db) as connection:
            return connection.execute(
                """SELECT q.*,s.* FROM questions q JOIN question_sources s
                   ON s.question_id=q.id WHERE s.import_job_id=? ORDER BY q.id""",
                (self.job_id,),
            ).fetchall()

    def _install_legacy_render_manifest(self, mutate=None, *, keep_anchor=False):
        manifest = {
            "import_job_id": self.job_id,
            "source_paper_id": self.source_id,
            "pdf_sha256": self.source_sha,
            "dpi": 300,
            "page_count": self.production_render["page_count"],
            "pages": json.loads(json.dumps(self.production_render["pages"])),
        }
        if mutate is not None:
            mutate(manifest)
        raw = _json_bytes(manifest)
        (self.job_dir / "render_manifest.json").write_bytes(raw)
        with sqlite3.connect(self.db) as connection:
            if keep_anchor:
                connection.execute(
                    """UPDATE import_page_render_runs
                       SET manifest_sha256=?,manifest_byte_size=? WHERE import_job_id=?""",
                    (hashlib.sha256(raw).hexdigest(), len(raw), self.job_id),
                )
            else:
                connection.execute(
                    "DELETE FROM import_page_render_runs WHERE import_job_id=?", (self.job_id,)
                )
            connection.execute(
                """UPDATE import_question_split_runs SET render_manifest_sha256=?
                   WHERE import_job_id=?""",
                (hashlib.sha256(raw).hexdigest(), self.job_id),
            )
        return manifest, raw

    def _remove_regions_and_split_anchor(self, *, remove_render_anchor=False):
        (self.job_dir / "question_regions.json").unlink()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "DELETE FROM import_question_split_runs WHERE import_job_id=?", (self.job_id,)
            )
            if remove_render_anchor:
                connection.execute(
                    "DELETE FROM import_page_render_runs WHERE import_job_id=?", (self.job_id,)
                )

    def _write_legacy(self, mutate):
        value = json.loads(self.legacy_raw)
        mutate(value)
        (self.job_dir / "question_crops.json").write_bytes(_json_bytes(value))

    def _recover_review_and_hold(self, reviewer_run_id="fresh-synthetic-crop-review"):
        recover_historical_v1_crops(self.db, self.private, self.job_id)
        manifest_path = self.job_dir / "question_crops.json"
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        record_crop_ai_review(self.db, self.private, {
            "version": 1,
            "import_job_id": self.job_id,
            "input_generation_id": manifest["generation_id"],
            "input_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "reviewer_run_id": reviewer_run_id,
            "questions": [{
                "question_no": entry["question_no"],
                "status": "ai_review_passed",
                "warnings": [],
            } for entry in manifest["questions"]],
        })
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_jobs SET status='needs_review',error_message='manual review' "
                "WHERE id=?", (self.job_id,),
            )

    def _resume_snapshot(self):
        with sqlite3.connect(self.db) as connection:
            return {
                table: connection.execute(
                    f"SELECT * FROM {table} WHERE import_job_id=?", (self.job_id,)
                ).fetchall()
                for table in (
                    "historical_v1_crop_recoveries",
                    "historical_v1_pipeline_resumptions",
                    "import_page_render_runs",
                    "import_question_split_runs",
                    "import_candidate_extraction_runs",
                    "import_candidate_audit_runs",
                    "import_knowledge_classification_runs",
                    "import_web_admission_runs",
                    "candidate_review_drafts",
                )
            } | {
                "job": connection.execute(
                    "SELECT * FROM import_jobs WHERE id=?", (self.job_id,)
                ).fetchone(),
                "questions": connection.execute(
                    """SELECT q.* FROM questions q JOIN question_sources s
                       ON s.question_id=q.id WHERE s.import_job_id=? ORDER BY q.id""",
                    (self.job_id,),
                ).fetchall(),
            }

    def test_resume_rejects_incomplete_review_and_is_zero_write(self):
        recover_historical_v1_crops(self.db, self.private, self.job_id)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_jobs SET status='needs_review' WHERE id=?", (self.job_id,)
            )
        before_tree = self._tree_digest()
        before_database = self.db.read_bytes()
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_resume(self.db, self.private, self.job_id)
        self.assertEqual(before_tree, self._tree_digest())
        self.assertEqual(before_database, self.db.read_bytes())

    def test_resume_assess_and_apply_reject_intermediate_root_symlink(self):
        self._recover_review_and_hold()
        before = self._resume_snapshot()
        linked_root = self.root / "resume-linked-root"
        linked_root.symlink_to(self.root, target_is_directory=True)
        linked_database = linked_root / self.db.name
        linked_private = linked_root / self.private.relative_to(self.root)

        for operation in (
            assess_historical_v1_resume,
            resume_historical_v1_fresh_pipeline,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(HistoricalV1RecoveryError):
                    operation(linked_database, linked_private, self.job_id)
                self.assertEqual(before, self._resume_snapshot())

        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("needs_review", 0),
                connection.execute(
                    """SELECT j.status,COUNT(r.import_job_id)
                       FROM import_jobs j
                       LEFT JOIN historical_v1_pipeline_resumptions r
                         ON r.import_job_id=j.id
                       WHERE j.id=? GROUP BY j.id""",
                    (self.job_id,),
                ).fetchone(),
            )

    def test_resume_assessment_does_not_follow_root_replaced_after_pinning(self):
        self._recover_review_and_hold()
        original_context = recovery_module._pinned_recovery_paths
        moved = self.root.with_name(f"{self.root.name}-pinned")
        decoy = self.root.with_name(f"{self.root.name}-decoy")

        @contextmanager
        def swap_after_pinning(database_path, private_root):
            with original_context(database_path, private_root) as pinned:
                self.root.rename(moved)
                shutil.copytree(moved, decoy)
                with sqlite3.connect(decoy / self.db.name) as connection:
                    connection.execute(
                        "UPDATE import_jobs SET status='completed' WHERE id=?",
                        (self.job_id,),
                    )
                self.root.symlink_to(decoy, target_is_directory=True)
                try:
                    yield pinned
                finally:
                    self.root.unlink()
                    shutil.rmtree(decoy)
                    moved.rename(self.root)

        with patch.object(
            recovery_module, "_pinned_recovery_paths", swap_after_pinning,
        ):
            with self.assertRaisesRegex(HistoricalV1RecoveryError, recovery_module.SAFE_INVALID):
                assess_historical_v1_resume(self.db, self.private, self.job_id)

    def test_resume_apply_does_not_write_root_replaced_after_pinning(self):
        self._recover_review_and_hold()
        original_context = recovery_module._pinned_recovery_paths
        moved = self.root.with_name(f"{self.root.name}-pinned")
        decoy = self.root.with_name(f"{self.root.name}-decoy")
        decoy_outcome = []

        @contextmanager
        def swap_after_pinning(database_path, private_root):
            with original_context(database_path, private_root) as pinned:
                self.root.rename(moved)
                shutil.copytree(moved, decoy)
                with sqlite3.connect(decoy / self.db.name) as connection:
                    connection.execute(
                        "UPDATE import_jobs SET status='completed' WHERE id=?",
                        (self.job_id,),
                    )
                self.root.symlink_to(decoy, target_is_directory=True)
                try:
                    yield pinned
                finally:
                    with sqlite3.connect(decoy / self.db.name) as connection:
                        decoy_outcome.append(connection.execute(
                            """SELECT j.status,COUNT(r.import_job_id)
                               FROM import_jobs j
                               LEFT JOIN historical_v1_pipeline_resumptions r
                                 ON r.import_job_id=j.id
                               WHERE j.id=? GROUP BY j.id""",
                            (self.job_id,),
                        ).fetchone())
                    self.root.unlink()
                    shutil.rmtree(decoy)
                    moved.rename(self.root)

        with patch.object(
            recovery_module, "_pinned_recovery_paths", swap_after_pinning,
        ):
            with self.assertRaisesRegex(HistoricalV1RecoveryError, recovery_module.SAFE_INVALID):
                resume_historical_v1_fresh_pipeline(
                    self.db, self.private, self.job_id,
                )
        self.assertEqual([("completed", 0)], decoy_outcome)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("needs_review", 0),
                connection.execute(
                    """SELECT j.status,COUNT(r.import_job_id)
                       FROM import_jobs j
                       LEFT JOIN historical_v1_pipeline_resumptions r
                         ON r.import_job_id=j.id
                       WHERE j.id=? GROUP BY j.id""",
                    (self.job_id,),
                ).fetchone(),
            )

    def test_resume_rejects_placeholder_and_splitter_reviewer(self):
        for reviewer in ("system_migration_placeholder", "legacy-codex-run"):
            with self.subTest(reviewer=reviewer):
                self.tearDown()
                self.setUp()
                if reviewer == "legacy-codex-run":
                    recover_historical_v1_crops(self.db, self.private, self.job_id)
                    manifest_path = self.job_dir / "question_crops.json"
                    manifest_raw = manifest_path.read_bytes()
                    manifest = json.loads(manifest_raw)
                    # Simulate independently signed evidence from a compromised old
                    # writer; resume must repeat the identity check itself.
                    with sqlite3.connect(self.db) as connection:
                        connection.execute(
                            "UPDATE import_question_split_runs SET codex_run_id='other-splitter' "
                            "WHERE import_job_id=?", (self.job_id,),
                        )
                    record_crop_ai_review(self.db, self.private, {
                        "version": 1, "import_job_id": self.job_id,
                        "input_generation_id": manifest["generation_id"],
                        "input_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                        "reviewer_run_id": reviewer,
                        "questions": [{"question_no": entry["question_no"],
                                       "status": "ai_review_passed", "warnings": []}
                                      for entry in manifest["questions"]],
                    })
                    with sqlite3.connect(self.db) as connection:
                        connection.execute(
                            "UPDATE import_question_split_runs SET codex_run_id=? WHERE import_job_id=?",
                            (reviewer, self.job_id),
                        )
                        connection.execute(
                            "UPDATE import_jobs SET status='needs_review' WHERE id=?", (self.job_id,),
                        )
                else:
                    self._recover_review_and_hold(reviewer)
                with self.assertRaises(HistoricalV1RecoveryError):
                    assess_historical_v1_resume(self.db, self.private, self.job_id)

    def test_resume_rejects_formal_drift_and_active_runs(self):
        self._recover_review_and_hold()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE questions SET stem_markdown=stem_markdown || ' drift' "
                "WHERE id=(SELECT MIN(question_id) FROM question_sources WHERE import_job_id=?)",
                (self.job_id,),
            )
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_resume(self.db, self.private, self.job_id)

        self.tearDown()
        self.setUp()
        self._recover_review_and_hold()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_candidate_extraction_runs SET status='processing' "
                "WHERE import_job_id=?", (self.job_id,),
            )
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_resume(self.db, self.private, self.job_id)

    def test_resume_dry_run_apply_idempotency_and_fresh_claim(self):
        self._recover_review_and_hold()
        before = self._resume_snapshot()
        assessment = assess_historical_v1_resume(self.db, self.private, self.job_id)
        self.assertEqual("ready", assessment.status)
        self.assertEqual(before, self._resume_snapshot())

        applied = resume_historical_v1_fresh_pipeline(
            self.db, self.private, self.job_id,
        )
        self.assertEqual("resumed", applied.status)
        after = self._resume_snapshot()
        self.assertEqual("pending", after["job"][4])
        self.assertIsNone(after["job"][5])
        self.assertEqual(before["historical_v1_crop_recoveries"],
                         after["historical_v1_crop_recoveries"])
        self.assertEqual(before["candidate_review_drafts"], after["candidate_review_drafts"])
        self.assertEqual(before["questions"], after["questions"])
        self.assertEqual(1, len(after["historical_v1_pipeline_resumptions"]))

        repeated = resume_historical_v1_fresh_pipeline(
            self.db, self.private, self.job_id,
        )
        self.assertEqual("already_resumed", repeated.status)
        self.assertEqual(after, self._resume_snapshot())
        claim = claim_candidate_extraction(
            self.db, self.private, self.job_id, runner=object(),
        )
        self.assertIsNotNone(claim)
        claim.close()

    def test_resume_accepts_fresh_review_that_adds_a_warning(self):
        recover_historical_v1_crops(self.db, self.private, self.job_id)
        manifest_path = self.job_dir / "question_crops.json"
        initial_raw = manifest_path.read_bytes()
        initial = json.loads(initial_raw)
        self.assertEqual([], initial["questions"][0]["warnings"])

        review = {
            "version": 1,
            "import_job_id": self.job_id,
            "input_generation_id": initial["generation_id"],
            "input_manifest_sha256": hashlib.sha256(initial_raw).hexdigest(),
            "reviewer_run_id": "fresh-independent-warning-review",
            "questions": [{
                "question_no": entry["question_no"],
                "status": "ai_review_passed",
                "warnings": (["题图边缘接近作答区"] if index == 0 else []),
            } for index, entry in enumerate(initial["questions"])],
        }
        record_crop_ai_review(self.db, self.private, review)
        reviewed_raw = manifest_path.read_bytes()
        reviewed = json.loads(reviewed_raw)
        self.assertEqual(
            ["题图边缘接近作答区"], reviewed["questions"][0]["warnings"],
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_jobs SET status='needs_review',error_message='manual review' "
                "WHERE id=?", (self.job_id,),
            )
            recovery_anchors = connection.execute(
                """SELECT new_crop_manifest_sha256,new_crop_generation_id
                   FROM historical_v1_crop_recoveries WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
            split_anchors = connection.execute(
                """SELECT crop_manifest_sha256,crop_generation_id,crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        evidence = json.loads((self.job_dir / "crop_ai_review.json").read_bytes())
        self.assertEqual(recovery_anchors, (
            evidence["input_manifest_sha256"], evidence["input_generation_id"],
        ))
        self.assertEqual(split_anchors, (
            evidence["output_manifest_sha256"], evidence["input_generation_id"],
            evidence["output_manifest_signature"],
        ))
        self.assertEqual(hashlib.sha256(reviewed_raw).hexdigest(), split_anchors[0])

        assessment = assess_historical_v1_resume(self.db, self.private, self.job_id)
        self.assertEqual("ready", assessment.status)
        applied = resume_historical_v1_fresh_pipeline(
            self.db, self.private, self.job_id,
        )
        self.assertEqual("resumed", applied.status)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("pending", 1),
                connection.execute(
                    """SELECT j.status,COUNT(r.import_job_id)
                       FROM import_jobs j
                       LEFT JOIN historical_v1_pipeline_resumptions r
                         ON r.import_job_id=j.id
                       WHERE j.id=? GROUP BY j.id""",
                    (self.job_id,),
                ).fetchone(),
            )

    def test_pending_recovery_without_resume_row_can_anchor_in_place(self):
        self._recover_review_and_hold()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_jobs SET status='pending' WHERE id=?", (self.job_id,)
            )
        assessment = assess_historical_v1_resume(self.db, self.private, self.job_id)
        self.assertEqual("ready_pending_anchor", assessment.status)
        applied = resume_historical_v1_fresh_pipeline(
            self.db, self.private, self.job_id,
        )
        self.assertEqual("anchored_pending", applied.status)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                ("pending",),
                connection.execute(
                    "SELECT status FROM import_jobs WHERE id=?", (self.job_id,)
                ).fetchone(),
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM historical_v1_pipeline_resumptions WHERE import_job_id=?",
                    (self.job_id,),
                ).fetchone()[0],
            )
        repeated = resume_historical_v1_fresh_pipeline(
            self.db, self.private, self.job_id,
        )
        self.assertEqual("already_resumed", repeated.status)

    def test_initialize_adds_resume_schema_to_completed_live_recovery(self):
        self._recover_review_and_hold()
        with sqlite3.connect(self.db) as connection:
            recovery_before = connection.execute(
                "SELECT * FROM historical_v1_crop_recoveries WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()
            connection.execute(
                "DROP TRIGGER historical_v1_pipeline_resumptions_immutable"
            )
            connection.execute(
                "DROP TRIGGER historical_v1_pipeline_resumptions_delete_immutable"
            )
            connection.execute("DROP TABLE historical_v1_pipeline_resumptions")
        initialize_database(self.db).close()
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as connection:
            recovery_after = connection.execute(
                "SELECT * FROM historical_v1_crop_recoveries WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()
            columns = connection.execute(
                "PRAGMA table_info(historical_v1_pipeline_resumptions)"
            ).fetchall()
        self.assertEqual(recovery_before, recovery_after)
        self.assertTrue(columns)

    def test_resume_transaction_rolls_back_on_status_drift(self):
        self._recover_review_and_hold()
        before = self._resume_snapshot()
        original = recovery_module._validate_resume_database_state

        def drift_after_validation(connection, private_root, job_id, review):
            result = original(connection, private_root, job_id, review)
            connection.execute(
                "UPDATE import_jobs SET status='failed' WHERE id=?", (job_id,),
            )
            return result

        with patch.object(
            recovery_module, "_validate_resume_database_state",
            side_effect=drift_after_validation,
        ), self.assertRaises(HistoricalV1RecoveryError):
            resume_historical_v1_fresh_pipeline(
                self.db, self.private, self.job_id,
            )
        self.assertEqual(before, self._resume_snapshot())

    def test_resume_rejects_job_directory_replaced_after_lock(self):
        self._recover_review_and_hold()
        before = self._resume_snapshot()
        moved = self.job_dir.with_name(f"{self.job_dir.name}-moved")
        original = recovery_module._validate_resume_database_state

        def replace_job(connection, private_root, job_id, review):
            result = original(connection, private_root, job_id, review)
            self.job_dir.rename(moved)
            self.job_dir.mkdir()
            return result

        try:
            with patch.object(
                recovery_module, "_validate_resume_database_state",
                side_effect=replace_job,
            ), self.assertRaises(HistoricalV1RecoveryError):
                resume_historical_v1_fresh_pipeline(self.db, self.private, self.job_id)
        finally:
            shutil.rmtree(self.job_dir)
            moved.rename(self.job_dir)
        self.assertEqual(before, self._resume_snapshot())

    def test_resume_rejects_database_replaced_after_begin(self):
        self._recover_review_and_hold()
        before = self._resume_snapshot()
        moved = self.db.with_name(f"{self.db.name}-moved")
        original = recovery_module._validate_resume_database_state

        def replace_database(connection, private_root, job_id, review):
            result = original(connection, private_root, job_id, review)
            self.db.rename(moved)
            shutil.copy2(moved, self.db)
            return result

        try:
            with patch.object(
                recovery_module, "_validate_resume_database_state",
                side_effect=replace_database,
            ), self.assertRaises(HistoricalV1RecoveryError):
                resume_historical_v1_fresh_pipeline(self.db, self.private, self.job_id)
            with sqlite3.connect(self.db) as decoy:
                self.assertEqual(0, decoy.execute(
                    "SELECT COUNT(*) FROM historical_v1_pipeline_resumptions"
                ).fetchone()[0])
        finally:
            self.db.unlink()
            moved.rename(self.db)
        self.assertEqual(before, self._resume_snapshot())

    def test_dry_run_is_zero_write_and_reports_exact_legacy_identity(self):
        before = self._tree_digest()
        result = assess_historical_v1_recovery(self.db, self.private, self.job_id)
        self.assertEqual(before, self._tree_digest())
        self.assertEqual("ready", result.status)
        self.assertEqual(hashlib.sha256(self.legacy_raw).hexdigest(), result.legacy_manifest_sha256)
        self.assertEqual(len(self.legacy_raw), result.legacy_manifest_byte_size)
        self.assertEqual(list(range(1, self.question_count + 1)), result.question_nos)

    def test_no_regions_and_no_split_is_ready_without_writes_and_apply_fills_both_anchors(self):
        self._remove_regions_and_split_anchor(remove_render_anchor=True)
        before = self._tree_digest()
        database_before = hashlib.sha256(self.db.read_bytes()).hexdigest()

        assessment = assess_historical_v1_recovery(self.db, self.private, self.job_id)

        self.assertEqual("ready", assessment.status)
        self.assertEqual(before, self._tree_digest())
        self.assertEqual(database_before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(
            assessment.legacy_manifest_sha256, assessment.regions_manifest_sha256
        )
        result = recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual(list(range(1, self.question_count + 1)), result.recropped_question_nos)
        self.assertEqual([], result.reused_question_nos)
        with sqlite3.connect(self.db) as connection:
            render = connection.execute(
                "SELECT status FROM import_page_render_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()
            split = connection.execute(
                """SELECT status,codex_run_id,result_manifest_sha256
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
            evidence = connection.execute(
                """SELECT migration_evidence_json FROM historical_v1_crop_recoveries
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()[0]
        self.assertEqual(("completed",), render)
        self.assertEqual("completed", split[0])
        self.assertTrue(split[1].startswith("historical-v1-manifest-unattributed-"))
        self.assertEqual(assessment.legacy_manifest_sha256, split[2])
        self.assertEqual(
            "historical_v1_crop_manifest_unattributed",
            json.loads(evidence)["question_plan_source"],
        )
        with self.assertRaises(CandidateExtractionError):
            claim_candidate_extraction(self.db, self.private, self.job_id)
        repeated = recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual("already_recovered", repeated.status)
        self.assertTrue(repeated.regions_from_legacy_manifest)

    def test_regionsless_recovery_with_null_prior_split_can_resume(self):
        self._remove_regions_and_split_anchor(remove_render_anchor=True)
        recover_historical_v1_crops(self.db, self.private, self.job_id)
        manifest_path = self.job_dir / "question_crops.json"
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        record_crop_ai_review(self.db, self.private, {
            "version": 1,
            "import_job_id": self.job_id,
            "input_generation_id": manifest["generation_id"],
            "input_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "reviewer_run_id": "fresh-regionsless-review",
            "questions": [{
                "question_no": entry["question_no"],
                "status": "ai_review_passed",
                "warnings": [],
            } for entry in manifest["questions"]],
        })
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_jobs SET status='needs_review' WHERE id=?", (self.job_id,)
            )
        assessment = assess_historical_v1_resume(self.db, self.private, self.job_id)
        self.assertEqual("ready", assessment.status)
        resumed = resume_historical_v1_fresh_pipeline(
            self.db, self.private, self.job_id,
        )
        self.assertEqual("resumed", resumed.status)

    def test_missing_regions_with_existing_split_row_is_always_rejected(self):
        (self.job_dir / "question_regions.json").unlink()
        before = self._tree_digest()
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)
        self.assertEqual(before, self._tree_digest())

    def test_regionsless_manifest_plan_and_crop_attacks_are_rejected(self):
        attacks = {
            "top_level": lambda value: value.__setitem__("approved_by", "legacy-reviewer"),
            "source_page_path": lambda value: value["source_pages"][0].__setitem__(
                "relative_path", "pages/../page_001.png"
            ),
            "source_page_hash": lambda value: value["source_pages"][0].__setitem__(
                "sha256", "0" * 64
            ),
            "source_page_dimensions": lambda value: value["source_pages"][0].__setitem__(
                "pixel_width", 239
            ),
            "out_of_bounds": lambda value: value["questions"][0]["regions"][0].__setitem__(
                "bbox", [-1, 16, 228, 151]
            ),
            "question_order": lambda value: value["questions"].__setitem__(
                slice(0, 2), [value["questions"][1], value["questions"][0]]
            ),
            "duplicate_question": lambda value: value["questions"][1].__setitem__(
                "question_no", 1
            ),
            "non_contiguous": lambda value: value["questions"][1].__setitem__(
                "question_no", 24
            ),
            "unsafe_path": lambda value: value["questions"][0].__setitem__(
                "output_relative_path", "question_crops/../Q001.png"
            ),
            "hash": lambda value: value["questions"][0].__setitem__("sha256", "0" * 64),
            "size": lambda value: value["questions"][0].__setitem__("byte_size", 1),
            "dimensions": lambda value: value["questions"][0].__setitem__(
                "width", value["questions"][0]["width"] - 1
            ),
        }
        for label, mutate in attacks.items():
            with self.subTest(label=label):
                self._remove_regions_and_split_anchor()
                self._write_legacy(mutate)
                before = self._tree_digest()
                with self.assertRaises(HistoricalV1RecoveryError):
                    assess_historical_v1_recovery(self.db, self.private, self.job_id)
                self.assertEqual(before, self._tree_digest())
                (self.job_dir / "question_regions.json").write_bytes(
                    _json_bytes({
                        "version": 1,
                        "import_job_id": self.job_id,
                        "question_count": self.question_count,
                        "questions": [],
                    })
                )
                with sqlite3.connect(self.db) as connection:
                    connection.execute(
                        """INSERT INTO import_question_split_runs
                           (import_job_id,status,updated_at) VALUES (?,'failed',CURRENT_TIMESTAMP)""",
                        (self.job_id,),
                    )
                (self.job_dir / "question_crops.json").write_bytes(self.legacy_raw)

    def test_apply_full_recrops_preserves_formal_rows_and_does_not_admit_missing(self):
        formal_before = self._formal_rows()
        result = recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual(list(range(1, self.question_count + 1)), result.recropped_question_nos)
        self.assertEqual([], result.reused_question_nos)
        manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual(2, manifest["version"])
        self.assertEqual("pending", manifest["review_status"])
        self.assertEqual({
            "approved_count": 0,
            "rejected_count": 0,
            "pending_count": self.question_count,
        }, manifest["review_summary"])
        self.assertTrue(all(q["review_status"] == "pending_ai_review" for q in manifest["questions"]))
        self.assertEqual(formal_before, self._formal_rows())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("pending", connection.execute(
                "SELECT status FROM import_jobs WHERE id=?", (self.job_id,)
            ).fetchone()[0])
            self.assertIsNone(connection.execute(
                """SELECT q.id FROM questions q JOIN question_sources s ON s.question_id=q.id
                   WHERE s.import_job_id=? AND s.source_question_no=?""",
                (self.job_id, str(self.question_count)),
            ).fetchone())
            drafts = connection.execute(
                """SELECT status,approval_source,approval_evidence_json
                   FROM candidate_review_drafts WHERE import_job_id=?""", (self.job_id,)
            ).fetchall()
        self.assertEqual([("draft", None, None)] * self.question_count, drafts)

    def test_missing_render_anchor_is_ready_without_writes_and_apply_creates_completed_anchor(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "DELETE FROM import_page_render_runs WHERE import_job_id=?", (self.job_id,)
            )
        database_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        tree_before = self._tree_digest()
        key_path = self.job_dir / ".upload_manifest_hmac.key"

        assessment = assess_historical_v1_recovery(self.db, self.private, self.job_id)

        self.assertEqual("ready", assessment.status)
        self.assertTrue(assessment.render_anchor_missing)
        self.assertEqual(database_before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(tree_before, self._tree_digest())
        self.assertFalse(key_path.exists())

        recover_historical_v1_crops(self.db, self.private, self.job_id)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT status,dpi,total_pages,rendered_pages,manifest_sha256,
                          manifest_byte_size,published_batch_id,source_pdf_sha256
                   FROM import_page_render_runs WHERE import_job_id=?""", (self.job_id,),
            ).fetchone()
        self.assertEqual("completed", row[0])
        self.assertEqual((300, 2, 2), row[1:4])
        self.assertEqual(assessment.render_manifest_sha256, row[4])
        self.assertEqual(assessment.render_manifest_byte_size, row[5])
        self.assertTrue(row[6].startswith("historical-v1-recovery-"))
        self.assertEqual(assessment.source_pdf_sha256, row[7])

    def test_exact_legacy_render_schema_is_ready_without_writes_and_apply_adds_anchor(self):
        manifest, render_raw = self._install_legacy_render_manifest()
        self.assertEqual({
            "import_job_id", "source_paper_id", "pdf_sha256", "dpi", "page_count", "pages",
        }, set(manifest))
        database_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        tree_before = self._tree_digest()

        assessment = assess_historical_v1_recovery(self.db, self.private, self.job_id)

        self.assertEqual("ready", assessment.status)
        self.assertTrue(assessment.render_anchor_missing)
        self.assertEqual(hashlib.sha256(render_raw).hexdigest(), assessment.render_manifest_sha256)
        self.assertEqual(database_before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(tree_before, self._tree_digest())

        recover_historical_v1_crops(self.db, self.private, self.job_id)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT status,dpi,total_pages,rendered_pages,manifest_sha256,
                          manifest_byte_size,source_pdf_sha256
                   FROM import_page_render_runs WHERE import_job_id=?""", (self.job_id,),
            ).fetchone()
        self.assertEqual(("completed", 300, 2, 2), row[:4])
        self.assertEqual(hashlib.sha256(render_raw).hexdigest(), row[4])
        self.assertEqual(len(render_raw), row[5])
        self.assertEqual(self.source_sha, row[6])

    def test_legacy_render_schema_identity_and_page_tampering_are_rejected_without_writes(self):
        attacks = (
            ("source_paper_id", lambda value: value.__setitem__(
                "source_paper_id", self.source_id + 1
            )),
            ("pdf_sha256", lambda value: value.__setitem__("pdf_sha256", "0" * 64)),
            ("extra_field", lambda value: value.__setitem__("version", 1)),
            ("missing_field", lambda value: value.pop("source_paper_id")),
            ("page_number", lambda value: value["pages"][0].__setitem__("page_number", 2)),
            ("relative_path", lambda value: value["pages"][0].__setitem__(
                "relative_path", "pages/page_002.png"
            )),
            ("byte_size", lambda value: value["pages"][0].__setitem__("byte_size", 1)),
            ("dimensions", lambda value: value["pages"][0].__setitem__("pixel_width", 241)),
            ("page_sha256", lambda value: value["pages"][0].__setitem__(
                "sha256", "0" * 64
            )),
        )
        for label, mutate in attacks:
            with self.subTest(label=label):
                self._install_legacy_render_manifest(mutate)
                database_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
                tree_before = self._tree_digest()
                with self.assertRaises(HistoricalV1RecoveryError):
                    assess_historical_v1_recovery(self.db, self.private, self.job_id)
                self.assertEqual(database_before, hashlib.sha256(self.db.read_bytes()).hexdigest())
                self.assertEqual(tree_before, self._tree_digest())

    def test_legacy_render_rejects_non_png_page_even_when_size_and_hash_match(self):
        page_path = self.job_dir / "pages/page_001.png"
        original = page_path.read_bytes()
        invalid = b"not a png despite matching manifest anchors"
        page_path.write_bytes(invalid)
        try:
            self._install_legacy_render_manifest(lambda value: value["pages"][0].update({
                "byte_size": len(invalid),
                "sha256": hashlib.sha256(invalid).hexdigest(),
            }))
            database_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
            tree_before = self._tree_digest()
            with self.assertRaises(HistoricalV1RecoveryError):
                assess_historical_v1_recovery(self.db, self.private, self.job_id)
            self.assertEqual(database_before, hashlib.sha256(self.db.read_bytes()).hexdigest())
            self.assertEqual(tree_before, self._tree_digest())
        finally:
            page_path.write_bytes(original)

    def test_existing_legacy_render_anchor_must_match_normalized_manifest(self):
        self._install_legacy_render_manifest(keep_anchor=True)
        assessment = assess_historical_v1_recovery(self.db, self.private, self.job_id)
        self.assertFalse(assessment.render_anchor_missing)

        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_page_render_runs SET rendered_pages=1 WHERE import_job_id=?",
                (self.job_id,),
            )
        database_before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        tree_before = self._tree_digest()
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)
        self.assertEqual(database_before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(tree_before, self._tree_digest())

    def test_existing_incomplete_or_inconsistent_render_anchor_fails_closed(self):
        mutations = (
            ("status", "failed"),
            ("manifest_sha256", "0" * 64),
            ("rendered_pages", 1),
        )
        for column, value in mutations:
            with self.subTest(column=column):
                with sqlite3.connect(self.db) as connection:
                    original = connection.execute(
                        f"SELECT {column} FROM import_page_render_runs WHERE import_job_id=?",
                        (self.job_id,),
                    ).fetchone()[0]
                    connection.execute(
                        f"UPDATE import_page_render_runs SET {column}=? WHERE import_job_id=?",
                        (value, self.job_id),
                    )
                with self.assertRaises(HistoricalV1RecoveryError):
                    assess_historical_v1_recovery(self.db, self.private, self.job_id)
                with sqlite3.connect(self.db) as connection:
                    connection.execute(
                        f"UPDATE import_page_render_runs SET {column}=? WHERE import_job_id=?",
                        (original, self.job_id),
                    )

    def test_repeated_apply_is_idempotent(self):
        first = recover_historical_v1_crops(self.db, self.private, self.job_id)
        snapshot = self._tree_digest()
        second = recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual("already_recovered", second.status)
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual(snapshot, self._tree_digest())

    def test_completed_and_pending_jobs_are_rejected(self):
        for status in ("completed", "pending"):
            with self.subTest(status=status), sqlite3.connect(self.db) as connection:
                connection.execute("UPDATE import_jobs SET status=? WHERE id=?", (status, self.job_id))
            with self.assertRaises(HistoricalV1RecoveryError):
                assess_historical_v1_recovery(self.db, self.private, self.job_id)

    def test_active_worker_and_downstream_lease_are_rejected(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_candidate_extraction_runs SET status='processing' WHERE import_job_id=?",
                (self.job_id,),
            )
        with self.assertRaisesRegex(HistoricalV1RecoveryError, "活跃"):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)

    def test_status_and_split_anchor_mismatch_is_rejected(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_question_split_runs SET result_manifest_sha256=? WHERE import_job_id=?",
                ("d" * 64, self.job_id),
            )
        with self.assertRaisesRegex(HistoricalV1RecoveryError, "regions"):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)

    def test_path_hash_number_and_bounds_attacks_are_rejected(self):
        attacks = (
            lambda value: value["questions"][0].__setitem__("output_relative_path", "../escape.png"),
            lambda value: value["questions"][0].__setitem__("sha256", "0" * 64),
            lambda value: value["questions"][1].__setitem__("question_no", 1),
        )
        for mutate in attacks:
            with self.subTest(mutate=mutate):
                original = json.loads(self.legacy_raw)
                mutate(original)
                (self.job_dir / "question_crops.json").write_bytes(_json_bytes(original))
                with self.assertRaises(HistoricalV1RecoveryError):
                    assess_historical_v1_recovery(self.db, self.private, self.job_id)
                (self.job_dir / "question_crops.json").write_bytes(self.legacy_raw)
        regions = json.loads((self.job_dir / "question_regions.json").read_text())
        regions["questions"][0]["regions"][0]["bbox_normalized"] = [-0.1, 0.1, 0.5, 0.5]
        (self.job_dir / "question_regions.json").write_bytes(_json_bytes(regions))
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)

    def test_missing_page_symlink_and_hardlink_are_rejected(self):
        page = self.job_dir / "pages/page_002.png"
        original = page.read_bytes()
        page.unlink()
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)
        page.write_bytes(original)
        crop = self.job_dir / "question_crops/Q001.png"
        content = crop.read_bytes()
        crop.unlink()
        crop.symlink_to(self.job_dir / "question_crops/Q002.png")
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)
        crop.unlink()
        crop.write_bytes(content)
        hardlink = self.root / "crop-hardlink.png"
        os.link(crop, hardlink)
        with self.assertRaises(HistoricalV1RecoveryError):
            assess_historical_v1_recovery(self.db, self.private, self.job_id)

    def test_generation_failure_and_database_failure_restore_everything(self):
        before = self._tree_digest()
        with patch(
            "src.processing.historical_v1_crop_recovery.generate_question_crops_report",
            side_effect=RuntimeError("synthetic generation interruption"),
        ), self.assertRaises(HistoricalV1RecoveryError):
            recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual(before, self._tree_digest())
        with patch(
            "src.processing.historical_v1_crop_recovery._commit_recovery",
            side_effect=sqlite3.OperationalError("synthetic database failure"),
        ), self.assertRaises(HistoricalV1RecoveryError):
            recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual(before, self._tree_digest())

    def test_manifest_digest_failure_happens_before_publish(self):
        before = self._tree_digest()
        with patch(
            "src.processing.question_crop._manifest_sha256",
            side_effect=OSError("synthetic staged digest failure"),
        ), self.assertRaises(HistoricalV1RecoveryError):
            recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual(before, self._tree_digest())

    def test_system_placeholder_is_not_review_and_real_independent_review_can_complete(self):
        recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertFalse((self.job_dir / "crop_ai_review.json").exists())
        with sqlite3.connect(self.db) as connection:
            evidence = connection.execute(
                """SELECT migration_evidence_kind,migration_evidence_json
                   FROM historical_v1_crop_recoveries WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
            split_run_id = connection.execute(
                "SELECT codex_run_id FROM import_question_split_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0]
        self.assertEqual("system_migration_placeholder", evidence[0])
        self.assertEqual("awaiting_independent_crop_review", json.loads(evidence[1])["state"])
        manifest_path = self.job_dir / "question_crops.json"
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        payload = {
            "version": 1,
            "import_job_id": self.job_id,
            "input_generation_id": manifest["generation_id"],
            "input_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "reviewer_run_id": split_run_id,
            "questions": [{
                "question_no": entry["question_no"],
                "status": "ai_review_passed",
                "warnings": [],
            } for entry in manifest["questions"]],
        }
        with self.assertRaisesRegex(Exception, "独立"):
            record_crop_ai_review(self.db, self.private, payload)
        payload["reviewer_run_id"] = "fresh-synthetic-crop-review"
        record_crop_ai_review(self.db, self.private, {
            **payload,
        })
        reviewed = json.loads(manifest_path.read_text())
        self.assertEqual("approved", reviewed["review_status"])
        self.assertEqual(self.question_count, reviewed["review_summary"]["approved_count"])
        # The old extraction row remains immutable history and still points at the old generation.
        row = _database_input(self.db, self.job_id)
        self.assertEqual("0" * 32, row[10])

    def test_commit_error_after_durable_commit_finishes_new_generation(self):
        original = recovery_module._commit_recovery

        def commit_then_raise(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("synthetic error after durable commit")

        with patch.object(recovery_module, "_commit_recovery", side_effect=commit_then_raise):
            result = recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual("recovered", result.status)
        self.assertFalse((self.job_dir / recovery_module.JOURNAL_NAME).exists())
        self.assertFalse((self.job_dir / recovery_module.BACKUP_NAME).exists())

    def test_durable_commit_query_rejects_swap_query_swap_back_database(self):
        recovered = recover_historical_v1_crops(self.db, self.private, self.job_id)
        moved = self.root / "pinned-original.db"
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

        with recovery_module._pinned_recovery_paths(
            self.db, self.private,
        ) as (pinned_database, _), patch.object(
            recovery_module.sqlite3, "connect", side_effect=swap_query_swap_back,
        ):
            durable = recovery_module._recovery_commit_is_durable(
                pinned_database, recovered, recovered.crop_manifest_sha256,
                recovered.generation_id, recovered.crop_manifest_signature,
            )
        self.assertTrue(attacked)
        self.assertFalse(durable)

    def test_final_cleanup_uses_locked_job_fd_after_canonical_job_swap(self):
        moved = self.job_dir.with_name(f"{self.job_dir.name}-moved-at-final-cleanup")
        real_verify = recovery_module._verify_locked_job
        verifications = 0

        def swap_after_final_verify(private_root, job_id, descriptor):
            nonlocal verifications
            real_verify(private_root, job_id, descriptor)
            verifications += 1
            if verifications == 2:
                self.job_dir.rename(moved)
                self.job_dir.mkdir()
                (self.job_dir / recovery_module.BACKUP_NAME).mkdir()
                (self.job_dir / recovery_module.JOURNAL_NAME).write_bytes(b"decoy journal")

        try:
            with patch.object(
                recovery_module, "_verify_locked_job", side_effect=swap_after_final_verify,
            ), self.assertRaises(HistoricalV1RecoveryError):
                recover_historical_v1_crops(self.db, self.private, self.job_id)
            self.assertTrue((self.job_dir / recovery_module.BACKUP_NAME).is_dir())
            self.assertEqual(
                b"decoy journal",
                (self.job_dir / recovery_module.JOURNAL_NAME).read_bytes(),
            )
        finally:
            if self.job_dir.exists():
                shutil.rmtree(self.job_dir)
            if moved.exists():
                moved.rename(self.job_dir)

    def test_failure_after_publish_before_commit_restores_legacy_outputs(self):
        before = self._tree_digest()

        def fail_after_publish(*args, **kwargs):
            current = json.loads((self.job_dir / "question_crops.json").read_text())
            self.assertEqual(2, current["version"])
            raise sqlite3.OperationalError("synthetic commit failure after publish")

        with patch.object(recovery_module, "_commit_recovery", side_effect=fail_after_publish), \
                self.assertRaises(HistoricalV1RecoveryError):
            recover_historical_v1_crops(self.db, self.private, self.job_id)
        self.assertEqual(before, self._tree_digest())


if __name__ == "__main__":
    unittest.main()
