import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.database.initialize import initialize_database
from src.processing.crop_review import record_crop_ai_review
from src.processing.mask_review import (
    MaskReviewError,
    apply_approved_masks,
    record_mask_review,
)
from src.processing.question_splitter import (
    SAFE_CODEX_MISSING,
    SAFE_SPLIT_ERROR,
    CodexExecutionError,
    CodexCliRunner,
    CodexRunResult,
    QuestionSplitError,
    _codex_output_schema,
    _prompt,
    _restore_outputs,
    _snapshot_outputs,
    claim_split_job,
    parse_codex_question_plan,
    record_split_claim_failure,
    run_claimed_split,
)
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest


def _png(path, size=(200, 300), color="white"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    content = path.read_bytes()
    return {
        "page_number": int(path.stem.rsplit("_", 1)[1]),
        "relative_path": f"pages/{path.name}",
        "pixel_width": size[0],
        "pixel_height": size[1],
        "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _anchor_render(database_path, job_id, job_dir, source_sha="a" * 64):
    content = (job_dir / "render_manifest.json").read_bytes()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """UPDATE import_page_render_runs SET manifest_sha256=?,
                      manifest_byte_size=?,published_batch_id=?,source_pdf_sha256=?
               WHERE import_job_id=?""",
            (hashlib.sha256(content).hexdigest(), len(content),
             f"test-render-{job_id}", source_sha, job_id),
        )


class FakeRunner:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        if self.error:
            raise self.error
        return CodexRunResult(json.dumps(self.payload), "fake-run-1")


class RawRunner:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error

    def run(self, *, image_paths, prompt):
        if self.error:
            raise self.error
        return CodexRunResult(self.raw, "bad-run")


class CommitFaultConnection:
    """Proxy the split completion commit without changing unrelated commits."""

    def __init__(self, connection, fault, state):
        self._connection = connection
        self._fault = fault
        self._state = state
        self._publishing = False

    def execute(self, sql, parameters=()):
        if "UPDATE import_question_split_runs SET status='completed'" in sql:
            self._publishing = True
        return self._connection.execute(sql, parameters)

    def commit(self):
        if self._publishing and not self._state["raised"]:
            self._state["raised"] = True
            if self._fault == "after":
                self._connection.commit()
            raise OSError(f"simulated {self._fault}-commit failure")
        return self._connection.commit()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class QuestionSplitterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.db = self.root / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name)
                   VALUES (?,1,'synthetic.pdf','raw_papers/TJ/unknown/synthetic.pdf',
                           'TJ','QT','合成')""", ("a" * 64,)
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (source_id,),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'completed',300,2,2)""", (self.job_id,)
            )
        self.job_dir = self.private / "processing" / f"import_job_{self.job_id}"
        pages = [
            _png(self.job_dir / "pages/page_001.png", color="white"),
            _png(self.job_dir / "pages/page_002.png", color="ivory"),
        ]
        (self.job_dir / "render_manifest.json").write_text(json.dumps({
            "version": 1, "import_job_id": self.job_id, "dpi": 300,
            "source_pdf_sha256": "a" * 64, "source_page_count": 2,
            "page_start": 1, "page_end": 2, "page_count": 2, "pages": pages,
        }), encoding="utf-8")
        _anchor_render(self.db, self.job_id, self.job_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def valid(self):
        return {
            "version": 1, "import_job_id": self.job_id, "question_count": 2,
            "questions": [
                {"question_no": 1, "regions": [
                    {"page_number": 1, "bbox_normalized": [0.05, 0.05, 0.95, 0.45]},
                    {"page_number": 2, "bbox_normalized": [0.05, 0.05, 0.95, 0.20]},
                ], "warnings": ["跨页"], "confidence": 0.9},
                {"question_no": 2, "regions": [
                    {"page_number": 2, "bbox_normalized": [0.05, 0.20, 0.95, 0.80]}
                ], "warnings": [], "confidence": 0.8},
            ],
        }

    def review_completed_split(self, *, recrop=()):
        manifest_path = self.job_dir / "question_crops.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        return record_crop_ai_review(self.db, self.private, {
            "version": 1,
            "import_job_id": self.job_id,
            "input_generation_id": manifest["generation_id"],
            "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "reviewer_run_id": "split-regression-review",
            "questions": [{
                "question_no": item["question_no"],
                "status": (
                    "needs_recrop"
                    if item["question_no"] in recrop
                    else "ai_review_passed"
                ),
                "warnings": (
                    ["下边界混入下一题，请在题间空白处重新切分"]
                    if item["question_no"] in recrop else []
                ),
            } for item in manifest["questions"]],
        })

    def test_strict_parser_converts_normalized_boxes_deterministically(self):
        plan = parse_codex_question_plan(
            json.dumps(self.valid()), self.job_id, {1: (200, 300), 2: (200, 300)}
        )
        self.assertEqual([10, 15, 190, 135], plan["questions"][0]["regions"][0]["bbox"])
        self.assertEqual([10, 60, 190, 240], plan["questions"][1]["regions"][0]["bbox"])
        self.assertEqual([], plan["questions"][0]["mask_regions_normalized"])
        self.assertEqual([], plan["questions"][0]["mask_regions"])

    def test_parser_converts_single_and_cross_page_masks_with_reasons(self):
        payload = self.valid()
        payload["questions"][0]["regions"][0]["bbox_normalized"] = [
            0.05, 0.05, 0.95, 0.15,
        ]
        payload["questions"][0]["regions"][1]["bbox_normalized"] = [
            0.05, 0.15, 0.95, 0.20,
        ]
        payload["questions"][0]["mask_regions_normalized"] = [
            {
                "bbox_normalized": [0.10, 0.10, 0.20, 0.14],
                "reason": "独立确认不覆盖正文的二维码",
            },
            {
                "bbox_normalized": [0.70, 0.16, 0.90, 0.18],
                "reason": "群组宣传层",
            },
        ]

        plan = parse_codex_question_plan(
            json.dumps(payload), self.job_id, {1: (200, 300), 2: (200, 300)}
        )

        question = plan["questions"][0]
        self.assertEqual(
            payload["questions"][0]["mask_regions_normalized"],
            question["mask_regions_normalized"],
        )
        self.assertEqual([
            {
                "page_number": 1,
                "bbox": [20, 30, 40, 43],
                "reason": "独立确认不覆盖正文的二维码",
            },
            {
                "page_number": 2,
                "bbox": [140, 48, 180, 54],
                "reason": "群组宣传层",
            },
        ], question["mask_regions"])

    def test_parser_rejects_every_invalid_mask_contract(self):
        def with_masks(masks):
            payload = self.valid()
            payload["questions"][1]["mask_regions_normalized"] = masks
            return payload

        valid_mask = {
            "bbox_normalized": [0.10, 0.30, 0.20, 0.40],
            "reason": "二维码",
        }
        cases = {
            "not_list": with_masks({}),
            "too_many": with_masks([valid_mask] * 11),
            "unknown": with_masks([{**valid_mask, "extra": True}]),
            "missing_reason": with_masks([{"bbox_normalized": [0.1, 0.3, 0.2, 0.4]}]),
            "empty_reason": with_masks([{
                "bbox_normalized": [0.1, 0.3, 0.2, 0.4], "reason": " \n",
            }]),
            "long_reason": with_masks([{
                "bbox_normalized": [0.1, 0.3, 0.2, 0.4], "reason": "x" * 201,
            }]),
            "bool": with_masks([{
                "bbox_normalized": [False, 0.3, 0.2, 0.4], "reason": "二维码",
            }]),
            "nan": with_masks([{
                "bbox_normalized": [float("nan"), 0.3, 0.2, 0.4], "reason": "二维码",
            }]),
            "inf": with_masks([{
                "bbox_normalized": [0.1, 0.3, float("inf"), 0.4], "reason": "二维码",
            }]),
            "zero_area": with_masks([{
                "bbox_normalized": [0.1, 0.3, 0.1, 0.4], "reason": "二维码",
            }]),
            "out_of_bounds": with_masks([{
                "bbox_normalized": [-0.1, 0.3, 0.2, 0.4], "reason": "二维码",
            }]),
            "outside_question_region": with_masks([{
                "bbox_normalized": [0.1, 0.85, 0.2, 0.9], "reason": "二维码",
            }]),
            "crosses_region": with_masks([{
                "bbox_normalized": [0.1, 0.75, 0.2, 0.85], "reason": "二维码",
            }]),
            "duplicate": with_masks([valid_mask, {**valid_mask, "reason": "重复说明"}]),
        }
        for name, payload in cases.items():
            with self.subTest(name=name), self.assertRaises(QuestionSplitError):
                parse_codex_question_plan(
                    json.dumps(payload), self.job_id,
                    {1: (200, 300), 2: (200, 300)},
                )

    def test_parser_rejects_all_contract_violations(self):
        cases = {}
        cases["fence"] = "```json\n{}\n```"
        cases["extra"] = json.dumps(self.valid()) + " trailing"
        for name, mutate in {
            "top": lambda p: p.update(extra=True),
            "job": lambda p: p.update(import_job_id=99),
            "version_bool": lambda p: p.update(version=True),
            "job_bool": lambda p: p.update(import_job_id=True),
            "count": lambda p: p.update(question_count=3),
            "skip": lambda p: p["questions"][1].update(question_no=3),
            "duplicate": lambda p: p["questions"][1].update(question_no=1),
            "question_bool": lambda p: p["questions"][0].update(question_no=True),
            "empty": lambda p: p["questions"][0].update(regions=[]),
            "warnings_overflow": lambda p: p["questions"][0].update(
                warnings=["x"] * 101
            ),
            "confidence_null": lambda p: p["questions"][0].update(confidence=None),
            "page": lambda p: p["questions"][0]["regions"][0].update(page_number=3),
            "box": lambda p: p["questions"][0]["regions"][0].update(
                bbox_normalized=[0, 0.5, 1.1, 0.4]
            ),
        }.items():
            payload = self.valid()
            mutate(payload)
            cases[name] = json.dumps(payload)
        missing = self.valid()
        missing["questions"][0].pop("warnings")
        cases["missing_required"] = json.dumps(missing)
        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaises(QuestionSplitError):
                parse_codex_question_plan(raw, self.job_id, {1: (200, 300), 2: (200, 300)})

    def test_every_malformed_or_execution_failure_marks_run_failed_without_outputs(self):
        bad_payloads = [
            "```json\n{}\n```",
            json.dumps(self.valid()) + " extra",
            json.dumps({**self.valid(), "extra": True}),
        ]
        skipped = self.valid()
        skipped["questions"][1]["question_no"] = 3
        bad_payloads.append(json.dumps(skipped))
        duplicate = self.valid()
        duplicate["questions"][1]["question_no"] = 1
        bad_payloads.append(json.dumps(duplicate))
        overflow = self.valid()
        overflow["questions"][0]["regions"][0]["bbox_normalized"] = [0, 0, 1.1, 1]
        bad_payloads.append(json.dumps(overflow))
        wrong_job = self.valid()
        wrong_job["import_job_id"] = self.job_id + 1
        bad_payloads.append(json.dumps(wrong_job))
        runners = [RawRunner(raw) for raw in bad_payloads] + [
            RawRunner(error=CodexExecutionError(kind))
            for kind in ("timeout", "oversized", "nonzero")
        ]
        for index, runner in enumerate(runners):
            with self.subTest(index=index):
                claim = claim_split_job(
                    self.db, self.private, self.job_id, runner=runner,
                )
                self.assertIsNone(run_claimed_split(claim))
                with sqlite3.connect(self.db) as connection:
                    self.assertEqual("failed", connection.execute(
                        "SELECT status FROM import_question_split_runs WHERE import_job_id=?",
                        (self.job_id,),
                    ).fetchone()[0])
                self.assertFalse((self.job_dir / "question_regions.json").exists())
                self.assertFalse((self.job_dir / "question_crops.json").exists())

    def test_claim_failure_recording_is_safe_and_idempotent(self):
        error = CodexExecutionError(SAFE_CODEX_MISSING)

        self.assertTrue(record_split_claim_failure(
            self.db, self.private, self.job_id, error
        ))
        self.assertTrue(record_split_claim_failure(
            self.db, self.private, self.job_id, error
        ))

        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT status,processed_pages,error_message,question_count,
                          codex_run_id,result_manifest_sha256
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(
            ("failed", 0, SAFE_CODEX_MISSING, None, None, None),
            row,
        )

    def test_claim_failure_recording_normalizes_unknown_errors(self):
        secret = str(self.root / "private/secret-codex-error")

        self.assertTrue(record_split_claim_failure(
            self.db, self.private, self.job_id, QuestionSplitError(secret)
        ))

        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT status,error_message FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(("failed", SAFE_SPLIT_ERROR), row)

    def test_claim_failure_recording_downgrades_stale_processing_run(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,processed_pages,error_message)
                   VALUES (?,'processing',0,NULL)""",
                (self.job_id,),
            )

        self.assertTrue(record_split_claim_failure(
            self.db, self.private, self.job_id,
            CodexExecutionError(SAFE_CODEX_MISSING),
        ))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(("failed", SAFE_CODEX_MISSING), connection.execute(
                """SELECT status,error_message FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone())

    def test_claim_failure_recording_never_downgrades_active_split_claim(self):
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid())
        )
        try:
            self.assertFalse(record_split_claim_failure(
                self.db, self.private, self.job_id,
                CodexExecutionError(SAFE_CODEX_MISSING),
            ))
            with sqlite3.connect(self.db) as connection:
                self.assertEqual(("processing", None), connection.execute(
                    """SELECT status,error_message FROM import_question_split_runs
                       WHERE import_job_id=?""",
                    (self.job_id,),
                ).fetchone())
        finally:
            claim.close()

    def test_claim_failure_recording_never_downgrades_completed_run(self):

        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid())
        ))
        with sqlite3.connect(self.db) as connection:
            completed_before = connection.execute(
                """SELECT * FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()

        self.assertFalse(record_split_claim_failure(
            self.db, self.private, self.job_id,
            CodexExecutionError(SAFE_CODEX_MISSING),
        ))
        with sqlite3.connect(self.db) as connection:
            completed_after = connection.execute(
                """SELECT * FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(completed_before, completed_after)

    def test_concurrent_claim_failure_recorders_are_idempotent(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,processed_pages,error_message)
                   VALUES (?,'processing',0,NULL)""",
                (self.job_id,),
            )
        barrier = threading.Barrier(4)
        results = []
        errors = []

        def record():
            try:
                barrier.wait(timeout=5)
                results.append(record_split_claim_failure(
                    self.db, self.private, self.job_id,
                    CodexExecutionError(SAFE_CODEX_MISSING),
                ))
            except Exception as error:  # pragma: no cover - assertion reports details
                errors.append(error)

        threads = [threading.Thread(target=record) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual([False, False, False, True], sorted(results))
        self.assertTrue(record_split_claim_failure(
            self.db, self.private, self.job_id,
            CodexExecutionError(SAFE_CODEX_MISSING),
        ))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(("failed", SAFE_CODEX_MISSING), connection.execute(
                """SELECT status,error_message FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone())

    def test_claim_failure_recording_requires_pending_job_and_completed_render(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_page_render_runs SET status='failed' WHERE import_job_id=?",
                (self.job_id,),
            )
        self.assertFalse(record_split_claim_failure(
            self.db, self.private, self.job_id,
            CodexExecutionError(SAFE_CODEX_MISSING),
        ))
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute(
                """SELECT status FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone())

    def test_claim_run_generates_crops_review_and_persists_completion(self):
        runner = FakeRunner(self.valid())
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        result = run_claimed_split(claim)
        self.assertEqual(2, result["question_count"])
        self.assertEqual(1, len(runner.calls))
        self.assertEqual(2, len(runner.calls[0][0]))
        self.assertTrue(all(not Path(path).exists() for path in runner.calls[0][0]))
        self.assertTrue((self.job_dir / "question_crops/Q001.png").is_file())
        self.assertTrue((self.job_dir / "question_crops/Q002.png").is_file())
        self.assertTrue((self.job_dir / "review/crops_01_04.jpg").is_file())
        regions = json.loads((self.job_dir / "question_regions.json").read_text())
        self.assertEqual(2, regions["question_count"])
        crops = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual("pending_ai_review", crops["questions"][0]["review_status"])
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT status,question_count,processed_pages,codex_run_id,
                          result_manifest_sha256,render_manifest_sha256,
                          source_pdf_sha256,crop_manifest_sha256,
                          crop_generation_id,crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(("completed", 2, 2, "fake-run-1"), row[:4])
        self.assertEqual([64, 64, 64, 64, 32, 64], [len(value) for value in row[4:]])

    def test_completed_all_passed_split_remains_idempotent(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split()
        runner = FakeRunner(self.valid())

        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        ))
        self.assertEqual([], runner.calls)

    def test_valid_recrop_review_can_replace_and_guides_prompt(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        runner = FakeRunner(self.valid())

        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertIsNotNone(claim)
        self.assertTrue(claim.replace_invalid)
        result = run_claimed_split(claim)

        self.assertEqual(2, result["question_count"])
        self.assertEqual(1, len(runner.calls))
        prompt = runner.calls[0][1]
        self.assertIn('"question_no":2', prompt)
        self.assertIn('"status":"needs_recrop"', prompt)
        self.assertIn("下边界混入下一题，请在题间空白处重新切分", prompt)
        manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual(
            ["ai_review_passed", "pending_ai_review"],
            [item["review_status"] for item in manifest["questions"]],
        )

    def test_mask_is_db_anchored_frozen_and_stays_pending_after_recrop(self):
        masked_payload = self.valid()
        masked_payload["questions"][0]["mask_regions_normalized"] = [{
            "bbox_normalized": [0.10, 0.30, 0.20, 0.40],
            "reason": "qr_code",
        }]
        first = run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(masked_payload),
        ))
        self.assertEqual([{
            "page_number": 1,
            "bbox": [20, 90, 40, 120],
            "reason": "qr_code",
        }], first["questions"][0]["mask_regions"])
        initial_manifest = json.loads(
            (self.job_dir / "question_crops.json").read_text()
        )
        proposal = initial_manifest["questions"][0]["mask_regions"][0]
        record_mask_review(self.db, self.private, {
            "version": 1, "import_job_id": self.job_id,
            "input_generation_id": initial_manifest["generation_id"],
            "question_no": 1, "page_number": proposal["page_number"],
            "bbox": proposal["bbox"], "reason": "qr_code",
            "reviewer": "independent-mask-freeze-review", "decision": "approved",
        })
        apply_approved_masks(self.db, self.private, self.job_id)
        first_manifest_bytes = (self.job_dir / "question_crops.json").read_bytes()
        first_manifest = json.loads(first_manifest_bytes)
        first_entry = first_manifest["questions"][0]
        self.assertEqual(
            masked_payload["questions"][0]["mask_regions_normalized"],
            first_entry["mask_regions_normalized"],
        )
        self.assertEqual(first["questions"][0]["mask_regions"],
                         first_entry["mask_regions"])
        with Image.open(self.job_dir / first_entry["output_relative_path"]) as image:
            self.assertEqual((255, 255, 255), image.getpixel((15, 80)))
        old_hash = first_entry["sha256"]
        with sqlite3.connect(self.db) as connection:
            anchors = connection.execute(
                """SELECT result_manifest_sha256,crop_manifest_sha256,
                          crop_generation_id,crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(
            hashlib.sha256(
                (self.job_dir / "question_regions.json").read_bytes()
            ).hexdigest(),
            anchors[0],
        )
        self.assertEqual(hashlib.sha256(first_manifest_bytes).hexdigest(), anchors[1])
        self.assertTrue(all(anchors))

        self.review_completed_split(recrop={2})
        recrop_payload = self.valid()
        recrop_payload["questions"][1]["regions"][0]["bbox_normalized"] = [
            0.05, 0.20, 0.95, 0.75,
        ]
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(recrop_payload),
        )
        self.assertEqual(
            masked_payload["questions"][0]["mask_regions_normalized"],
            claim.frozen_regions[0]["mask_regions_normalized"],
        )

        run_claimed_split(claim)

        recropped = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual(old_hash, recropped["questions"][0]["sha256"])
        self.assertEqual(first_entry["mask_regions"],
                         recropped["questions"][0]["mask_regions"])
        self.assertEqual(
            ["ai_review_passed", "pending_ai_review"],
            [item["review_status"] for item in recropped["questions"]],
        )
        self.assertTrue((self.job_dir / "crop_ai_review.json").exists())

    def test_recrop_freezes_passed_regions_and_png_bytes_despite_codex_changes(self):
        original_plan = run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        old_passed_entry = json.loads(json.dumps(old_manifest["questions"][0]))
        old_review_evidence = (self.job_dir / "crop_ai_review.json").read_bytes()
        old_hashes = {
            item["question_no"]: item["sha256"] for item in old_manifest["questions"]
        }
        changed = self.valid()
        changed["questions"][0].update(
            regions=[{
                "page_number": 1,
                "bbox_normalized": [0.25, 0.30, 0.75, 0.70],
            }],
            warnings=["Codex错误改动已通过题"],
            confidence="low",
        )
        changed["questions"][1].update(
            regions=[{
                "page_number": 2,
                "bbox_normalized": [0.10, 0.35, 0.90, 0.95],
            }],
            warnings=["已按反馈重切"],
            confidence="high",
        )
        runner = FakeRunner(changed)

        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertEqual((1,), tuple(
            item["question_no"] for item in claim.frozen_regions
        ))
        result = run_claimed_split(claim)

        self.assertEqual(
            original_plan["questions"][0],
            result["questions"][0],
        )
        self.assertEqual(
            [20, 105, 180, 285],
            result["questions"][1]["regions"][0]["bbox"],
        )
        prompt = runner.calls[0][1]
        self.assertIn("冻结题号=1", prompt)
        self.assertIn("重点只修needs_recrop题号=2", prompt)
        manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        self.assertEqual(
            ["ai_review_passed", "pending_ai_review"],
            [item["review_status"] for item in manifest["questions"]],
        )
        new_hashes = {
            item["question_no"]: item["sha256"] for item in manifest["questions"]
        }
        self.assertEqual(old_hashes[1], new_hashes[1])
        self.assertNotEqual(old_hashes[2], new_hashes[2])
        self.assertEqual(old_passed_entry, manifest["questions"][0])
        with sqlite3.connect(self.db) as connection:
            frozen = connection.execute(
                """SELECT crop_sha256,review_evidence_sha256,
                          review_evidence_signature,evidence_relative_path
                   FROM import_frozen_crop_reviews
                   WHERE import_job_id=? AND question_no=1""", (self.job_id,),
            ).fetchone()
        self.assertEqual(old_hashes[1], frozen[0])
        self.assertEqual(hashlib.sha256(old_review_evidence).hexdigest(), frozen[1])
        archived = self.job_dir / frozen[3]
        self.assertEqual(old_review_evidence, archived.read_bytes())

    def test_recrop_fails_closed_when_db_anchored_frozen_region_is_invalid(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        regions_path = self.job_dir / "question_regions.json"
        forged = json.loads(regions_path.read_text())
        forged["questions"][0]["regions"][0]["page_number"] = 99
        forged_bytes = (
            json.dumps(forged, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        regions_path.write_bytes(forged_bytes)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_question_split_runs
                   SET result_manifest_sha256=? WHERE import_job_id=?""",
                (hashlib.sha256(forged_bytes).hexdigest(), self.job_id),
            )
        runner = FakeRunner(self.valid())

        with self.assertRaises(QuestionSplitError):
            claim_split_job(
                self.db, self.private, self.job_id, runner=runner,
            )

        self.assertEqual([], runner.calls)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())

    def test_recrop_fails_closed_when_frozen_question_numbers_mismatch_evidence(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        regions_path = self.job_dir / "question_regions.json"
        mismatched = json.loads(regions_path.read_text())
        mismatched["questions"][0]["question_no"] = 2
        mismatched_bytes = (
            json.dumps(mismatched, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        regions_path.write_bytes(mismatched_bytes)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_question_split_runs
                   SET result_manifest_sha256=? WHERE import_job_id=?""",
                (hashlib.sha256(mismatched_bytes).hexdigest(), self.job_id),
            )
        runner = FakeRunner(self.valid())

        with self.assertRaises(QuestionSplitError):
            claim_split_job(
                self.db, self.private, self.job_id, runner=runner,
            )

        self.assertEqual([], runner.calls)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())

    def test_recrop_fails_closed_when_valid_regions_differ_from_reviewed_crop(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        regions_path = self.job_dir / "question_regions.json"
        mismatched = json.loads(regions_path.read_text())
        mismatched["questions"][0]["regions"] = [{
            "page_number": 1,
            "bbox_normalized": [0.10, 0.10, 0.90, 0.40],
            "bbox": [20, 30, 180, 120],
        }]
        mismatched_bytes = (
            json.dumps(mismatched, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        regions_path.write_bytes(mismatched_bytes)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_question_split_runs
                   SET result_manifest_sha256=? WHERE import_job_id=?""",
                (hashlib.sha256(mismatched_bytes).hexdigest(), self.job_id),
            )
        runner = FakeRunner(self.valid())

        with self.assertRaises(QuestionSplitError):
            claim_split_job(
                self.db, self.private, self.job_id, runner=runner,
            )

        self.assertEqual([], runner.calls)
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())

    def test_first_split_has_no_frozen_regions_and_keeps_original_behavior(self):
        runner = FakeRunner(self.valid())

        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )

        self.assertEqual((), claim.frozen_regions)
        result = run_claimed_split(claim)
        self.assertEqual(
            [10, 15, 190, 135],
            result["questions"][0]["regions"][0]["bbox"],
        )
        self.assertNotIn("冻结题号=1", runner.calls[0][1])

    def test_recrop_review_evidence_failures_do_not_claim(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        evidence_path = self.job_dir / "crop_ai_review.json"
        valid_evidence = evidence_path.read_bytes()

        evidence_path.unlink()
        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))

        evidence_path.write_bytes(b"{broken")
        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))

        evidence = json.loads(valid_evidence)
        evidence["output_manifest_sha256"] = "0" * 64
        evidence = sign_manifest(load_hmac_key(self.job_dir), evidence)
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        runner = FakeRunner(self.valid())
        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        ))
        self.assertEqual([], runner.calls)

    def _assert_failed_recrop_can_be_claimed_again(
        self, failing_runner, *, publication_failure=False,
    ):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        failed_claim = claim_split_job(
            self.db, self.private, self.job_id, runner=failing_runner,
        )
        self.assertTrue(failed_claim.replace_invalid)
        if publication_failure:
            with patch(
                "src.processing.question_splitter.generate_question_crops_report",
                side_effect=QuestionSplitError(SAFE_SPLIT_ERROR),
            ):
                self.assertIsNone(run_claimed_split(failed_claim))
        else:
            self.assertIsNone(run_claimed_split(failed_claim))

        retry_runner = FakeRunner(self.valid())
        retry = claim_split_job(
            self.db, self.private, self.job_id, runner=retry_runner,
        )
        self.assertIsNotNone(retry)
        self.assertTrue(retry.replace_invalid)
        self.assertEqual((2,), tuple(
            item["question_no"] for item in retry.review_feedback
        ))
        self.assertEqual((1,), tuple(
            item["question_no"] for item in retry.frozen_regions
        ))
        self.assertIsNotNone(run_claimed_split(retry))
        self.assertIn('"question_no":2', retry_runner.calls[0][1])

    def test_runner_failure_keeps_trusted_recrop_feedback_for_next_claim(self):
        self._assert_failed_recrop_can_be_claimed_again(
            FakeRunner(error=CodexExecutionError("timeout")),
        )

    def test_parser_failure_keeps_trusted_recrop_feedback_for_next_claim(self):
        self._assert_failed_recrop_can_be_claimed_again(RawRunner("{}"))

    def test_publish_failure_restores_evidence_and_feedback_for_next_claim(self):
        self._assert_failed_recrop_can_be_claimed_again(
            FakeRunner(self.valid()), publication_failure=True,
        )

    def test_codex_and_crop_use_same_pinned_page_snapshot(self):
        original = (self.job_dir / "pages/page_001.png").read_bytes()
        outer = self

        class MutatingRunner(FakeRunner):
            def run(self, *, image_paths, prompt):
                self.calls.append((tuple(image_paths), prompt))
                self.assert_snapshot = Path(image_paths[0]).read_bytes()
                Image.new("RGB", (200, 300), "black").save(
                    outer.job_dir / "pages/page_001.png", "PNG"
                )
                return CodexRunResult(json.dumps(self.payload), "snapshot-run")

        runner = MutatingRunner(self.valid())
        result = run_claimed_split(
            claim_split_job(
                self.db, self.private, self.job_id, runner=runner,
            )
        )
        self.assertEqual(2, result["question_count"])
        self.assertEqual(original, runner.assert_snapshot)
        with Image.open(self.job_dir / "question_crops/Q001.png") as crop:
            crop.load()
            self.assertEqual((255, 255, 255), crop.convert("RGB").getpixel((1, 1)))

    def test_render_trust_anchors_and_link_attacks_fail_before_runner(self):
        runner = FakeRunner(self.valid())
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_page_render_runs SET manifest_sha256=NULL WHERE import_job_id=?",
                (self.job_id,),
            )
        with self.assertRaises(QuestionSplitError):
            claim_split_job(
                self.db, self.private, self.job_id, runner=runner,
            )
        self.assertEqual([], runner.calls)
        _anchor_render(self.db, self.job_id, self.job_dir)

        page = self.job_dir / "pages/page_001.png"
        saved = page.read_bytes()
        page.unlink()
        other = self.root / "hardlinked-page.png"
        other.write_bytes(saved)
        page.hardlink_to(other)
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertIsNone(run_claimed_split(claim))
        self.assertEqual([], runner.calls)
        page.unlink()
        other.unlink()
        page.write_bytes(saved)

        manifest = self.job_dir / "render_manifest.json"
        manifest_copy = self.root / "render-manifest-copy.json"
        manifest_copy.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(manifest_copy)
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertIsNone(run_claimed_split(claim))
        self.assertEqual([], runner.calls)

    def test_failures_mark_failed_without_replacing_old_complete_results(self):
        first = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        run_claimed_split(first)
        old_regions = (self.job_dir / "question_regions.json").read_bytes()
        old_crop = (self.job_dir / "question_crops/Q001.png").read_bytes()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_question_split_runs SET status='failed' WHERE import_job_id=?",
                (self.job_id,),
            )
        bad = FakeRunner(error=CodexExecutionError("timeout"))
        retry = claim_split_job(
            self.db, self.private, self.job_id, runner=bad,
        )
        self.assertIsNotNone(retry)
        self.assertIsNone(run_claimed_split(retry))
        self.assertEqual(old_regions, (self.job_dir / "question_regions.json").read_bytes())
        self.assertEqual(old_crop, (self.job_dir / "question_crops/Q001.png").read_bytes())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_question_split_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0])

    def test_next_claim_recovers_interrupted_top_level_publication(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_regions = (self.job_dir / "question_regions.json").read_bytes()
        old_crop = (self.job_dir / "question_crops/Q001.png").read_bytes()
        old_evidence = (self.job_dir / "crop_ai_review.json").read_bytes()
        _snapshot_outputs(self.job_dir, self.db, self.job_id)
        (self.job_dir / "question_regions.json").write_text('{"partial":true}')
        (self.job_dir / "question_crops/Q001.png").write_bytes(b"partial")
        (self.job_dir / "crop_ai_review.json").write_bytes(b"{partial")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_question_split_runs SET status='processing',
                          completed_at=NULL WHERE import_job_id=?""", (self.job_id,)
            )
        runner = FakeRunner(self.valid())
        recovered_claim = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertIsNotNone(recovered_claim)
        self.assertTrue(recovered_claim.replace_invalid)
        self.assertEqual((2,), tuple(
            item["question_no"] for item in recovered_claim.review_feedback
        ))
        recovered_claim.close()
        self.assertEqual([], runner.calls)
        self.assertEqual(old_regions, (self.job_dir / "question_regions.json").read_bytes())
        self.assertEqual(old_crop, (self.job_dir / "question_crops/Q001.png").read_bytes())
        self.assertEqual(
            old_evidence, (self.job_dir / "crop_ai_review.json").read_bytes()
        )
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("processing", connection.execute(
                "SELECT status FROM import_question_split_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0])

    def test_recovery_finishes_new_generation_after_db_commit_crash(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )

        class SimulatedCrash(BaseException):
            pass

        original_unlink = Path.unlink

        def crash_before_journal_cleanup(path, *args, **kwargs):
            if path.name == ".split-publish-journal.json":
                raise SimulatedCrash
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", crash_before_journal_cleanup):
            with self.assertRaises(SimulatedCrash):
                run_claimed_split(claim)

        new_manifest = (self.job_dir / "question_crops.json").read_bytes()
        self.assertNotEqual(old_manifest, new_manifest)
        self.assertTrue((self.job_dir / ".split-publish-journal.json").exists())
        with sqlite3.connect(self.db) as connection:
            committed = connection.execute(
                """SELECT status,crop_manifest_sha256,crop_generation_id,
                          crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual("completed", committed[0])
        self.assertEqual(hashlib.sha256(new_manifest).hexdigest(), committed[1])

        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.assertEqual(new_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())
        with sqlite3.connect(self.db) as connection:
            recovered = connection.execute(
                """SELECT status,crop_manifest_sha256,crop_generation_id,
                          crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(committed, recovered)

    def test_journal_cleanup_permission_error_keeps_db_committed_generation(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        original_unlink = Path.unlink
        failed_once = False

        def fail_first_journal_cleanup(path, *args, **kwargs):
            nonlocal failed_once
            if path.name == ".split-publish-journal.json" and not failed_once:
                failed_once = True
                raise PermissionError("simulated committed journal cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_first_journal_cleanup):
            result = run_claimed_split(claim)

        self.assertIsNotNone(result)
        new_manifest = (self.job_dir / "question_crops.json").read_bytes()
        self.assertNotEqual(old_manifest, new_manifest)
        with sqlite3.connect(self.db) as connection:
            anchors = connection.execute(
                """SELECT status,crop_manifest_sha256,crop_generation_id,
                          crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual("completed", anchors[0])
        self.assertEqual(hashlib.sha256(new_manifest).hexdigest(), anchors[1])
        self.assertTrue(all(anchors[1:]))
        self.assertTrue((self.job_dir / ".split-publish-journal.json").exists())

        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.assertEqual(new_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())

    def test_backup_cleanup_oserror_keeps_db_committed_generation(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        backup = self.job_dir / ".split-backup-current"
        original_rmtree = shutil.rmtree

        def fail_backup_cleanup(path, *args, **kwargs):
            if Path(path) == backup:
                raise OSError("simulated committed backup cleanup failure")
            return original_rmtree(path, *args, **kwargs)

        with patch(
            "src.processing.question_splitter.shutil.rmtree",
            side_effect=fail_backup_cleanup,
        ):
            result = run_claimed_split(claim)

        self.assertIsNotNone(result)
        new_manifest = (self.job_dir / "question_crops.json").read_bytes()
        self.assertNotEqual(old_manifest, new_manifest)
        with sqlite3.connect(self.db) as connection:
            anchors = connection.execute(
                """SELECT status,crop_manifest_sha256,crop_generation_id,
                          crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual("completed", anchors[0])
        self.assertEqual(hashlib.sha256(new_manifest).hexdigest(), anchors[1])
        self.assertTrue(all(anchors[1:]))
        self.assertTrue((self.job_dir / ".split-publish-journal.json").exists())

        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.assertEqual(new_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())

    def test_completion_commit_then_oserror_keeps_new_generation_and_not_failed(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        real_connect = sqlite3.connect
        state = {"raised": False}

        def connect(*args, **kwargs):
            return CommitFaultConnection(real_connect(*args, **kwargs), "after", state)

        with patch("src.processing.question_splitter.sqlite3.connect", side_effect=connect):
            result = run_claimed_split(claim)

        self.assertTrue(state["raised"])
        self.assertIsNotNone(result)
        new_manifest = (self.job_dir / "question_crops.json").read_bytes()
        self.assertNotEqual(old_manifest, new_manifest)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """SELECT status,crop_manifest_sha256,crop_generation_id,
                          crop_manifest_signature
                   FROM import_question_split_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
        self.assertEqual("completed", row[0])
        self.assertEqual(hashlib.sha256(new_manifest).hexdigest(), row[1])
        self.assertTrue(all(row[1:]))
        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())

    def test_completion_precommit_oserror_restores_old_generation_and_retries(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_regions = (self.job_dir / "question_regions.json").read_bytes()
        old_manifest = (self.job_dir / "question_crops.json").read_bytes()
        old_crop = (self.job_dir / "question_crops/Q001.png").read_bytes()
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        real_connect = sqlite3.connect
        state = {"raised": False}

        def connect(*args, **kwargs):
            return CommitFaultConnection(real_connect(*args, **kwargs), "before", state)

        with patch("src.processing.question_splitter.sqlite3.connect", side_effect=connect):
            self.assertIsNone(run_claimed_split(claim))

        self.assertTrue(state["raised"])
        self.assertEqual(old_regions, (self.job_dir / "question_regions.json").read_bytes())
        self.assertEqual(old_manifest, (self.job_dir / "question_crops.json").read_bytes())
        self.assertEqual(old_crop, (self.job_dir / "question_crops/Q001.png").read_bytes())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())
        retry = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        self.assertIsNotNone(retry)
        self.assertIsNotNone(run_claimed_split(retry))

    def test_uncertain_commit_keeps_witness_and_next_claim_reconciles(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        real_connect = sqlite3.connect
        state = {"raised": False}

        def connect(*args, **kwargs):
            return CommitFaultConnection(real_connect(*args, **kwargs), "after", state)

        with patch("src.processing.question_splitter.sqlite3.connect", side_effect=connect), \
                patch(
                    "src.processing.question_splitter._authoritative_split_commit_state",
                    return_value="unknown",
                ):
            self.assertIsNone(run_claimed_split(claim))

        self.assertTrue((self.job_dir / ".split-publish-journal.json").exists())
        self.assertTrue((self.job_dir / ".split-backup-current").exists())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM import_question_split_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0])
        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())

    def test_independent_mask_approval_applies_only_small_controlled_proposal(self):
        payload = self.valid()
        payload["questions"][0]["regions"][0]["bbox_normalized"] = [0.05, 0.05, 0.80, 0.45]
        payload["questions"][0]["mask_regions_normalized"] = [{
            "bbox_normalized": [0.85, 0.05, 0.90, 0.08333333333333333],
            "reason": "qr_code",
        }]
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(payload),
        ))
        manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        before = (self.job_dir / "question_crops/Q001.png").read_bytes()
        proposal = manifest["questions"][0]["mask_regions"][0]
        evidence = record_mask_review(self.db, self.private, {
            "version": 1, "import_job_id": self.job_id,
            "input_generation_id": manifest["generation_id"], "question_no": 1,
            "page_number": proposal["page_number"], "bbox": proposal["bbox"],
            "reason": "qr_code", "reviewer": "independent-mask-review-1",
            "decision": "approved",
        })

        applied = apply_approved_masks(self.db, self.private, self.job_id)

        after = (self.job_dir / "question_crops/Q001.png").read_bytes()
        self.assertNotEqual(before, after)
        self.assertEqual(applied["questions"][0]["sha256"], hashlib.sha256(after).hexdigest())
        self.assertEqual("approved", evidence["decision"])
        self.assertRegex(evidence["signature"], r"\A[0-9a-f]{64}\Z")
        with sqlite3.connect(self.db) as connection:
            anchor = connection.execute(
                """SELECT evidence_sha256,evidence_signature,decision
                   FROM import_crop_security_reviews
                   WHERE import_job_id=? AND evidence_kind='mask'""", (self.job_id,),
            ).fetchone()
            crop_anchor = connection.execute(
                "SELECT crop_manifest_sha256 FROM import_question_split_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0]
        self.assertEqual((hashlib.sha256(
            (self.job_dir / "mask_review" / f"evidence_{evidence['subject_digest']}.json").read_bytes()
        ).hexdigest(), evidence["signature"], "approved"), anchor)
        self.assertEqual(
            hashlib.sha256((self.job_dir / "question_crops.json").read_bytes()).hexdigest(),
            crop_anchor,
        )

    def test_mask_application_rejects_preview_evidence_and_db_anchor_tampering(self):
        payload = self.valid()
        payload["questions"][0]["regions"][0]["bbox_normalized"] = [0.05, 0.05, 0.80, 0.45]
        payload["questions"][0]["mask_regions_normalized"] = [{
            "bbox_normalized": [0.85, 0.05, 0.90, 0.08333333333333333],
            "reason": "qr_code",
        }]
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(payload),
        ))
        manifest = json.loads((self.job_dir / "question_crops.json").read_text())
        proposal = manifest["questions"][0]["mask_regions"][0]
        evidence = record_mask_review(self.db, self.private, {
            "version": 1, "import_job_id": self.job_id,
            "input_generation_id": manifest["generation_id"], "question_no": 1,
            "page_number": proposal["page_number"], "bbox": proposal["bbox"],
            "reason": "qr_code", "reviewer": "independent-mask-review-2",
            "decision": "approved",
        })
        preview = self.job_dir / "mask_review" / f"preview_{evidence['subject_digest']}.png"
        original = preview.read_bytes()
        preview.write_bytes(b"tampered-preview")
        with self.assertRaises(MaskReviewError):
            apply_approved_masks(self.db, self.private, self.job_id)
        preview.write_bytes(original)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_crop_security_reviews SET evidence_sha256=?
                   WHERE import_job_id=? AND evidence_kind='mask'""",
                ("0" * 64, self.job_id),
            )
        with self.assertRaises(MaskReviewError):
            apply_approved_masks(self.db, self.private, self.job_id)

    def test_artifact_context_exit_oserror_still_closes_split_locks(self):
        claim = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        job_stream = claim.lock_stream
        global_stream = claim.global_lock_stream
        artifact_context = claim.artifact_lock_context

        class FailingArtifactContext:
            def __exit__(self, exc_type, exc_value, traceback):
                raise OSError("simulated artifact context exit failure")

        claim.artifact_lock_context = FailingArtifactContext()
        try:
            with self.assertRaises(OSError):
                claim.close()
            self.assertIsNone(claim.lock_stream)
            self.assertIsNone(claim.global_lock_stream)
            self.assertTrue(job_stream.closed)
            self.assertTrue(global_stream.closed)
        finally:
            artifact_context.__exit__(None, None, None)
            for stream in (job_stream, global_stream):
                if not stream.closed:
                    stream.close()

    def test_crop_review_write_waits_for_recrop_publication_lock(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        self.review_completed_split(recrop={2})
        old_manifest_bytes = (self.job_dir / "question_crops.json").read_bytes()
        old_manifest = json.loads(old_manifest_bytes)
        runner_entered = threading.Event()
        release_runner = threading.Event()
        review_finished = threading.Event()
        errors = []

        class BlockingRunner(FakeRunner):
            def run(inner_self, *, image_paths, prompt):
                inner_self.calls.append((tuple(image_paths), prompt))
                runner_entered.set()
                if not release_runner.wait(timeout=5):
                    raise AssertionError("test did not release split runner")
                return CodexRunResult(json.dumps(inner_self.payload), "blocking-run")

        claim = claim_split_job(
            self.db, self.private, self.job_id,
            runner=BlockingRunner(self.valid()),
        )

        split_thread = threading.Thread(target=run_claimed_split, args=(claim,))
        split_thread.start()
        self.assertTrue(runner_entered.wait(timeout=5))

        def write_review():
            try:
                record_crop_ai_review(self.db, self.private, {
                    "version": 1,
                    "import_job_id": self.job_id,
                    "input_generation_id": old_manifest["generation_id"],
                    "input_manifest_sha256": hashlib.sha256(
                        old_manifest_bytes
                    ).hexdigest(),
                    "reviewer_run_id": "concurrent-review-write",
                    "questions": [
                        {
                            "question_no": item["question_no"],
                            "status": "ai_review_passed",
                            "warnings": [],
                        }
                        for item in old_manifest["questions"]
                    ],
                })
            except Exception as error:
                errors.append(error)
            finally:
                review_finished.set()

        review_thread = threading.Thread(target=write_review)
        review_thread.start()
        self.assertFalse(
            review_finished.wait(timeout=0.2),
            "review transaction escaped the split publication lock domain",
        )
        release_runner.set()
        split_thread.join(timeout=5)
        review_thread.join(timeout=5)
        self.assertFalse(split_thread.is_alive())
        self.assertFalse(review_thread.is_alive())
        self.assertTrue(review_finished.is_set())
        self.assertEqual(1, len(errors))

    def test_tampered_recovery_journal_fails_closed(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        old_regions = (self.job_dir / "question_regions.json").read_bytes()
        _snapshot_outputs(self.job_dir, self.db, self.job_id)
        journal_path = self.job_dir / ".split-publish-journal.json"
        journal = json.loads(journal_path.read_text())
        journal["saved_outputs"] = []
        journal_path.write_text(json.dumps(journal))
        runner = FakeRunner(self.valid())
        with self.assertRaises(QuestionSplitError):
            claim_split_job(
                self.db, self.private, self.job_id, runner=runner,
            )
        self.assertEqual([], runner.calls)
        self.assertEqual(old_regions, (self.job_dir / "question_regions.json").read_bytes())

    def test_snapshot_rejects_directory_swapped_to_external_symlink_after_check(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        outside = self.root / "outside-crops"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"external-secret")
        source = self.job_dir / "question_crops"
        displaced = self.job_dir / ".displaced-question-crops"
        original_open = os.open
        swapped = False

        def race_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "question_crops" and flags & getattr(os, "O_DIRECTORY", 0) and not swapped:
                swapped = True
                os.replace(source, displaced)
                source.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        try:
            with patch(
                "src.processing.question_splitter.os.open",
                side_effect=race_open,
            ):
                with self.assertRaises(QuestionSplitError):
                    _snapshot_outputs(self.job_dir, self.db, self.job_id)
        finally:
            if source.is_symlink():
                source.unlink()
            if displaced.exists():
                os.replace(displaced, source)

        backup = self.job_dir / ".split-backup-current"
        self.assertFalse((backup / "question_crops/secret.txt").exists())
        self.assertFalse(backup.exists())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())

    def test_restore_rejects_backup_directory_swapped_to_external_symlink(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        backup = _snapshot_outputs(self.job_dir, self.db, self.job_id)
        outside = self.root / "outside-restore"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"external-secret")
        saved = backup / "question_crops"
        displaced = backup / ".displaced-question-crops"
        original_open = os.open
        swapped = False

        def race_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "question_crops" and flags & getattr(os, "O_DIRECTORY", 0) and not swapped:
                swapped = True
                os.replace(saved, displaced)
                saved.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch(
            "src.processing.question_splitter.os.open",
            side_effect=race_open,
        ):
            with self.assertRaises(QuestionSplitError):
                _restore_outputs(self.job_dir, backup)

        self.assertFalse((self.job_dir / "question_crops/secret.txt").exists())
        self.assertTrue((self.job_dir / ".split-publish-journal.json").exists())

    def test_snapshot_rejects_recursive_file_inode_swap_to_external_symlink(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        source = self.job_dir / "question_crops/Q001.png"
        displaced = self.job_dir / "question_crops/.Q001.displaced"
        secret = self.root / "outside-secret.bin"
        secret.write_bytes(b"external-secret")
        original_open = os.open
        swapped = False

        def race_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "Q001.png" and not swapped:
                swapped = True
                os.replace(source, displaced)
                source.symlink_to(secret)
            return original_open(path, flags, *args, **kwargs)

        try:
            with patch(
                "src.processing.question_splitter.os.open", side_effect=race_open,
            ):
                with self.assertRaises(QuestionSplitError):
                    _snapshot_outputs(self.job_dir, self.db, self.job_id)
        finally:
            if source.is_symlink():
                source.unlink()
            if displaced.exists():
                os.replace(displaced, source)

        self.assertFalse((
            self.job_dir / ".split-backup-current/question_crops/Q001.png"
        ).exists())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())

    def test_two_claims_only_invoke_runner_once_and_stale_can_resume(self):
        runner = FakeRunner(self.valid())
        first = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        second = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertIsNone(second)
        run_claimed_split(first)
        self.assertEqual(1, len(runner.calls))
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_question_split_runs SET status='processing' WHERE import_job_id=?",
                (self.job_id,),
            )
        resumed = claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        )
        self.assertIsNotNone(resumed)
        run_claimed_split(resumed)
        self.assertEqual(2, len(runner.calls))

    def test_global_lock_allows_only_one_split_job(self):
        with sqlite3.connect(self.db) as connection:
            source_id = connection.execute(
                "SELECT source_paper_id FROM import_jobs WHERE id=?", (self.job_id,)
            ).fetchone()[0]
            other_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (source_id,),
            ).lastrowid
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages)
                   VALUES (?,'completed',300,2,2)""", (other_id,)
            )
        other_dir = self.private / "processing" / f"import_job_{other_id}"
        shutil.copytree(self.job_dir, other_dir)
        manifest_path = other_dir / "render_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["import_job_id"] = other_id
        manifest_path.write_text(json.dumps(manifest))
        _anchor_render(self.db, other_id, other_dir)
        first = claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        )
        try:
            second = claim_split_job(
                self.db, self.private, other_id, runner=FakeRunner(self.valid()),
            )
            self.assertIsNone(second)
        finally:
            first.close()

    def test_claim_does_not_read_local_usage_sessions_before_running(self):
        runner = FakeRunner(self.valid())
        with patch(
            "src.processing.question_splitter.Path.home",
            side_effect=AssertionError("local sessions must not be inspected"),
        ):
            claim = claim_split_job(
                self.db, self.private, self.job_id, runner=runner
            )

        self.assertIsNotNone(claim)
        run_claimed_split(claim)
        self.assertEqual(1, len(runner.calls))

class CodexCliRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image = self.root / "page.png"
        Image.new("RGB", (10, 10), "white").save(self.image)

    def tearDown(self):
        self.temporary.cleanup()

    def test_output_schema_const_version_has_explicit_integer_type(self):
        version = _codex_output_schema()["properties"]["version"]
        self.assertEqual("integer", version["type"])
        self.assertEqual(1, version["const"])

    def test_output_schema_confidence_uses_supported_string_enum(self):
        confidence = _codex_output_schema()["properties"]["questions"]["items"][
            "properties"
        ]["confidence"]
        self.assertEqual({
            "type": "string", "enum": ["low", "medium", "high"]
        }, confidence)

    def test_prompt_requires_complete_nonoverlapping_questions_and_string_confidence(self):
        prompt = _prompt(5, [(1, b"page", (100, 200))], {"pages": []})
        for required in (
            "题号、公共条件、题干、公式、选项、小问和必要配图",
            "不得包含下一题题号或文字",
            "试卷后的答案和解析页不作为新题",
            "low、medium或high",
            "版面提示仅作弱参考",
            "mask_regions_normalized可省略，默认空列表",
            "独立确认不覆盖试题内容",
            "绝不能用于隐藏题干、选项、答案、解析",
            "不能用于掩盖相邻题边界错误",
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("confidence\":0到1", prompt)

    def script(self, body):
        path = self.root / f"fake-{len(list(self.root.glob('fake-*')))}"
        path.write_text(f"#!{sys.executable}\nimport pathlib,sys,time\n{body}\n")
        path.chmod(0o700)
        return path

    def test_local_fake_executable_success_nonzero_timeout_and_output_budgets(self):
        success = self.script(
            "p=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]);"
            "p.write_text('{}');print('bounded')"
        )
        result = CodexCliRunner(success, timeout=2).run(
            image_paths=[self.image], prompt="only json"
        )
        self.assertEqual("{}", result.final_message)
        self.assertTrue(result.run_id.startswith("codex-"))

        cases = [
            self.script("sys.exit(7)"),
            self.script("time.sleep(1)"),
            self.script("sys.stdout.write('x'*10000)"),
            self.script(
                "p=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]);"
                "p.write_text('x'*10000)"
            ),
        ]
        for index, executable in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(CodexExecutionError):
                CodexCliRunner(
                    executable, timeout=0.05 if index == 1 else 2,
                    max_output_bytes=100, max_stderr_bytes=100,
                ).run(image_paths=[self.image], prompt="only json")

    def test_runner_disables_shell_tools_and_kills_spawned_process_group(self):
        arguments = self.root / "arguments.txt"
        success = self.script(
            f"pathlib.Path({str(arguments)!r}).write_text('\\n'.join(sys.argv));"
            "p=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]);"
            "p.write_text('{}')"
        )
        CodexCliRunner(success, timeout=2).run(
            image_paths=[self.image], prompt="only json"
        )
        argv = arguments.read_text().splitlines()
        for feature in ("shell_tool", "unified_exec", "shell_snapshot"):
            index = argv.index(feature)
            self.assertEqual("--disable", argv[index - 1])

        pid_file = self.root / "child.pid"
        sleeper = self.script(
            "p=__import__('subprocess').Popen([sys.executable,'-c',"
            "'import time;time.sleep(30)']);"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));time.sleep(30)"
        )
        with self.assertRaises(CodexExecutionError):
            # Python 3.13 can spend more than 0.5s importing subprocess on a
            # cold macOS runner.  Keep this bounded while allowing the fake to
            # create the child whose process-group cleanup is under test.
            CodexCliRunner(sleeper, timeout=2).run(
                image_paths=[self.image], prompt="only json"
            )
        pid = int(pid_file.read_text())
        alive = True
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.02)
        self.assertFalse(alive, "Codex超时后孙进程仍存活")


if __name__ == "__main__":
    unittest.main()
