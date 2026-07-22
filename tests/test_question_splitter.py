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
from src.processing.question_splitter import (
    SAFE_CODEX_MISSING,
    SAFE_SPLIT_ERROR,
    CodexExecutionError,
    CodexCliRunner,
    CodexRunResult,
    QuestionSplitError,
    _codex_output_schema,
    _prompt,
    _snapshot_outputs,
    claim_split_job,
    parse_codex_question_plan,
    record_split_claim_failure,
    run_claimed_split,
)


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

    def test_strict_parser_converts_normalized_boxes_deterministically(self):
        plan = parse_codex_question_plan(
            json.dumps(self.valid()), self.job_id, {1: (200, 300), 2: (200, 300)}
        )
        self.assertEqual([10, 15, 190, 135], plan["questions"][0]["regions"][0]["bbox"])
        self.assertEqual([10, 60, 190, 240], plan["questions"][1]["regions"][0]["bbox"])

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
        old_regions = (self.job_dir / "question_regions.json").read_bytes()
        old_crop = (self.job_dir / "question_crops/Q001.png").read_bytes()
        _snapshot_outputs(self.job_dir)
        (self.job_dir / "question_regions.json").write_text('{"partial":true}')
        (self.job_dir / "question_crops/Q001.png").write_bytes(b"partial")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """UPDATE import_question_split_runs SET status='processing',
                          completed_at=NULL WHERE import_job_id=?""", (self.job_id,)
            )
        runner = FakeRunner(self.valid())
        self.assertIsNone(claim_split_job(
            self.db, self.private, self.job_id, runner=runner,
        ))
        self.assertEqual([], runner.calls)
        self.assertEqual(old_regions, (self.job_dir / "question_regions.json").read_bytes())
        self.assertEqual(old_crop, (self.job_dir / "question_crops/Q001.png").read_bytes())
        self.assertFalse((self.job_dir / ".split-publish-journal.json").exists())
        self.assertFalse((self.job_dir / ".split-backup-current").exists())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM import_question_split_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0])

    def test_tampered_recovery_journal_fails_closed(self):
        run_claimed_split(claim_split_job(
            self.db, self.private, self.job_id, runner=FakeRunner(self.valid()),
        ))
        old_regions = (self.job_dir / "question_regions.json").read_bytes()
        _snapshot_outputs(self.job_dir)
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
