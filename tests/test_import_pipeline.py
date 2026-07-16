import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.database.initialize import initialize_database
from src.pipeline.import_pipeline import PipelineResult, inspect_pipeline, main, run_pipeline


class ImportPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "question-bank.db"
        self.private_root = self.root / "private"
        self.private_root.mkdir()
        initialize_database(self.database).close()
        with sqlite3.connect(self.database) as connection:
            self.source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES(?,100,'卷.pdf','raw_papers/TJ/2026/test.pdf','TJ',2026,'YK','测试卷')""",
                ("a" * 64,),
            ).lastrowid

    def tearDown(self):
        self.temporary.cleanup()

    def create_job(self, status="pending"):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES(1,?,?)",
                (self.source_id, status),
            )
        job_dir = self.private_root / "processing" / "import_job_1"
        job_dir.mkdir(parents=True)
        return job_dir

    @staticmethod
    def write_json(job_dir, name, value):
        path = job_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def mark_rendered(self, job_dir):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,total_pages,rendered_pages)
                   VALUES(1,'completed',1,1)"""
            )
        self.write_json(job_dir, "render_manifest.json", {"import_job_id": 1})

    def candidate(self):
        return {
            "import_job_id": 1,
            "source_paper_id": self.source_id,
            "question_count": 1,
            "questions": [{"source_question_no": "1"}],
        }

    def crops(self, review_status="pending"):
        return {
            "import_job_id": 1,
            "question_count": 1,
            "questions": [{
                "question_no": 1,
                "crop_status": "generated",
                "review_status": review_status,
            }],
        }

    @staticmethod
    def audit():
        return {
            "import_job_id": 1,
            "question_count": 1,
            "questions": [{"source_question_no": "1"}],
        }

    def test_states_are_derived_without_pipeline_state_file(self):
        job_dir = self.create_job()
        self.assertEqual("needs_render", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

        self.mark_rendered(job_dir)
        self.assertEqual("needs_candidates", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

        self.write_json(job_dir, "candidate_questions.json", self.candidate())
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("needs_crops", "provide_crop_plan"), (
            state.stage, state.next_action
        ))

        self.write_json(job_dir, "question_regions.json", {
            "import_job_id": 1,
            "question_count": 1,
            "questions": [{"question_no": "1", "regions": []}],
        })
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("needs_crops", "provide_crop_plan"), (
            state.stage, state.next_action
        ))

        self.write_json(job_dir, "question_crops.json", self.crops())
        self.assertEqual("needs_crop_review", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

        self.write_json(job_dir, "question_crops.json", self.crops("ai_review_passed"))
        self.assertEqual("needs_ai_review", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

        self.write_json(job_dir, "ai_audit.json", self.audit())
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("ready", "run_strict_admission"), (
            state.stage, state.next_action
        ))
        self.assertFalse((job_dir / "pipeline_state.json").exists())

    def test_non_pending_job_statuses_never_offer_page_render(self):
        for status, expected in (
            ("processing", ("in_progress", "wait_or_recover")),
            ("failed", ("failed", "manual_review")),
        ):
            with self.subTest(status=status):
                self.create_job(status=status)
                result = inspect_pipeline(
                    self.database, self.private_root, 1
                )
                self.assertEqual(expected, (result.stage, result.next_action))
                self.assertNotEqual("render_pages", result.next_action)
                with sqlite3.connect(self.database) as connection:
                    connection.execute("DELETE FROM import_jobs WHERE id=1")
                job_dir = self.private_root / "processing" / "import_job_1"
                job_dir.rmdir()

    def test_database_completed_status_is_authoritative(self):
        self.create_job(status="completed")
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("completed", "none"), (state.stage, state.next_action))
        applied = run_pipeline(
            self.database, self.private_root, 1, apply=True
        )
        self.assertEqual(("completed", False), (applied.stage, applied.changed))

    def test_mutable_candidate_cannot_repair_stale_pending_status(self):
        job_dir = self.create_job()
        self.mark_rendered(job_dir)
        self.write_json(job_dir, "candidate_questions.json", self.candidate())
        with sqlite3.connect(self.database) as connection:
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,region_code,exam_type_code,
                    question_type_code,primary_knowledge_point_id,content_hash,
                    answer_status)
                   SELECT 'Q-STALE-001','题','TJ','YK','single_choice',id,?,'missing'
                   FROM knowledge_points LIMIT 1""",
                ("b" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO question_sources VALUES(?,?,?,?,?)",
                (question_id, self.source_id, 1, "1", "[1]"),
            )
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("needs_review", "manual_review"), (
            state.stage, state.next_action
        ))
        applied = run_pipeline(self.database, self.private_root, 1, apply=True)
        with sqlite3.connect(self.database) as connection:
            status = connection.execute(
                "SELECT status FROM import_jobs WHERE id=1"
            ).fetchone()[0]
        self.assertEqual("pending", status)
        self.assertFalse(applied.changed)

    def test_any_existing_admitted_source_forces_manual_review(self):
        job_dir = self.create_job()
        self.mark_rendered(job_dir)
        candidate = self.candidate()
        candidate["question_count"] = 2
        candidate["questions"].append({"source_question_no": "2"})
        self.write_json(job_dir, "candidate_questions.json", candidate)
        with sqlite3.connect(self.database) as connection:
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,region_code,exam_type_code,
                    question_type_code,primary_knowledge_point_id,content_hash,
                    answer_status)
                   SELECT 'Q-PARTIAL-001','题','TJ','YK','single_choice',id,?,'missing'
                   FROM knowledge_points LIMIT 1""",
                ("d" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO question_sources VALUES(?,?,?,?,?)",
                (question_id, self.source_id, 1, "1", "[1]"),
            )

        state = inspect_pipeline(self.database, self.private_root, 1)

        self.assertEqual(("needs_review", "manual_review"), (
            state.stage, state.next_action
        ))

    def test_old_needs_review_job_with_sources_never_restarts_render(self):
        self.create_job(status="needs_review")
        with sqlite3.connect(self.database) as connection:
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,region_code,exam_type_code,
                    question_type_code,primary_knowledge_point_id,content_hash,
                    answer_status)
                   SELECT 'Q-REVIEW-001','题','TJ','YK','single_choice',id,?,'missing'
                   FROM knowledge_points LIMIT 1""",
                ("c" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO question_sources VALUES(?,?,?,?,?)",
                (question_id, self.source_id, 1, "1", "[1]"),
            )
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("needs_review", "manual_review"), (
            state.stage, state.next_action
        ))

    def test_default_check_is_read_only_and_missing_database_is_not_created(self):
        job_dir = self.create_job()
        before_db = hashlib.sha256(self.database.read_bytes()).hexdigest()
        before_files = sorted(path.relative_to(self.root).as_posix()
                              for path in self.root.rglob("*") if path.is_file())
        run_pipeline(self.database, self.private_root, 1)
        self.assertEqual(before_db, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertEqual(before_files, sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*") if path.is_file()
        ))
        self.assertEqual([], list(job_dir.iterdir()))

        missing = self.root / "missing.db"
        state = inspect_pipeline(missing, self.private_root, 1)
        self.assertEqual("unavailable", state.stage)
        self.assertFalse(missing.exists())

    def test_symlinked_job_directory_is_rejected(self):
        job_dir = self.create_job()
        job_dir.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        job_dir.symlink_to(outside, target_is_directory=True)
        state = inspect_pipeline(self.database, self.private_root, 1)
        self.assertEqual(("unavailable", "check_artifacts"), (
            state.stage, state.next_action
        ))
        self.assertEqual([], list(outside.iterdir()))

    def test_cross_job_and_malformed_artifacts_fail_closed(self):
        job_dir = self.create_job()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,total_pages,rendered_pages)
                   VALUES(1,'completed',1,1)"""
            )
        self.write_json(job_dir, "render_manifest.json", {"import_job_id": 2})
        self.assertEqual("needs_render", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

        self.write_json(job_dir, "render_manifest.json", {"import_job_id": 1})
        self.write_json(job_dir, "candidate_questions.json", {
            **self.candidate(), "import_job_id": 2
        })
        self.assertEqual("needs_candidates", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

        (job_dir / "candidate_questions.json").write_text("{invalid", encoding="utf-8")
        self.assertEqual("needs_candidates", inspect_pipeline(
            self.database, self.private_root, 1
        ).stage)

    def test_malformed_ai_audit_types_return_stable_review_action(self):
        job_dir = self.create_job()
        self.mark_rendered(job_dir)
        self.write_json(job_dir, "candidate_questions.json", self.candidate())
        self.write_json(
            job_dir,
            "question_crops.json",
            self.crops("ai_review_passed"),
        )
        for questions in (123, [{"source_question_no": []}]):
            self.write_json(job_dir, "ai_audit.json", {
                "import_job_id": 1,
                "question_count": 1,
                "questions": questions,
            })
            state = inspect_pipeline(self.database, self.private_root, 1)
            self.assertEqual(("needs_ai_review", "provide_ai_audit"), (
                state.stage, state.next_action
            ))

    def test_apply_only_runs_existing_page_renderer(self):
        self.create_job()
        stages = [
            PipelineResult(1, "needs_render", "render_pages"),
            PipelineResult(1, "needs_render", "render_pages"),
            PipelineResult(1, "needs_candidates", "provide_candidate_questions"),
        ]
        claim = object()
        with (
            mock.patch("src.pipeline.import_pipeline.inspect_pipeline", side_effect=stages),
            mock.patch("src.pipeline.import_pipeline.claim_render_job", return_value=claim) as claim_job,
            mock.patch("src.pipeline.import_pipeline.run_claimed_render") as render,
        ):
            result = run_pipeline(self.database, self.private_root, 1, apply=True)
        claim_job.assert_called_once_with(self.database, self.private_root, 1)
        render.assert_called_once_with(claim)
        self.assertEqual(("needs_candidates", True), (result.stage, result.changed))

    def test_concurrent_completed_render_is_not_reported_as_changed(self):
        self.create_job()
        states = [
            PipelineResult(1, "needs_render", "render_pages"),
            PipelineResult(1, "needs_candidates", "provide_candidate_questions"),
        ]
        claim = mock.Mock()
        with (
            mock.patch(
                "src.pipeline.import_pipeline.inspect_pipeline",
                side_effect=states,
            ),
            mock.patch(
                "src.pipeline.import_pipeline.claim_render_job",
                return_value=claim,
            ),
            mock.patch(
                "src.pipeline.import_pipeline.run_claimed_render",
            ) as render,
        ):
            result = run_pipeline(
                self.database, self.private_root, 1, apply=True
            )
        claim.close.assert_called_once_with()
        render.assert_not_called()
        self.assertEqual(("needs_candidates", False), (
            result.stage, result.changed
        ))

    def test_apply_never_runs_crop_or_admission(self):
        ready = PipelineResult(1, "ready", "run_strict_admission")
        with (
            mock.patch("src.pipeline.import_pipeline.inspect_pipeline", return_value=ready),
            mock.patch("src.processing.question_crop.generate_question_crops") as crop,
            mock.patch("src.importing.admit_questions.admit_questions") as admit,
            mock.patch("src.importing.admit_questions.backup_database") as backup,
        ):
            result = run_pipeline(self.database, self.private_root, 1, apply=True)
        crop.assert_not_called()
        admit.assert_not_called()
        backup.assert_not_called()
        self.assertEqual(("ready", False), (result.stage, result.changed))

    def test_render_failure_returns_stable_failed_result(self):
        current = PipelineResult(1, "needs_render", "render_pages")
        with (
            mock.patch("src.pipeline.import_pipeline.inspect_pipeline", return_value=current),
            mock.patch(
                "src.pipeline.import_pipeline.claim_render_job",
                side_effect=RuntimeError("secret detail"),
            ),
        ):
            result = run_pipeline(self.database, self.private_root, 1, apply=True)
        self.assertEqual(("failed", "retry", False), (
            result.stage, result.next_action, result.changed
        ))
        self.assertNotIn("secret detail", result.message)

        with (
            mock.patch(
                "src.pipeline.import_pipeline.inspect_pipeline",
                return_value=current,
            ),
            mock.patch(
                "src.pipeline.import_pipeline.claim_render_job",
                return_value=object(),
            ),
            mock.patch(
                "src.pipeline.import_pipeline.run_claimed_render",
                return_value=None,
            ),
        ):
            returned_failure = run_pipeline(
                self.database, self.private_root, 1, apply=True
            )
        self.assertEqual(("failed", "retry", False), (
            returned_failure.stage,
            returned_failure.next_action,
            returned_failure.changed,
        ))

    def test_cli_errors_are_json_and_nonzero(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([
                "--database", str(self.root / "missing.db"),
                "--private-root", str(self.private_root),
                "--job-id", "1", "--json",
            ])
        self.assertNotEqual(0, code)
        self.assertEqual("unavailable", json.loads(output.getvalue())["stage"])

        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [
                "import_pipeline", "--job-id", "not-an-integer", "--json"
            ]),
            contextlib.redirect_stdout(output),
        ):
            code = main()
        self.assertNotEqual(0, code)
        self.assertEqual("failed", json.loads(output.getvalue())["stage"])


if __name__ == "__main__":
    unittest.main()
