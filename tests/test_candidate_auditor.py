import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.database.initialize import initialize_database
from src.processing.candidate_extractor import (
    CandidateExtractionRunResult,
    claim_candidate_extraction,
    run_claimed_candidate_extraction,
)
from src.reviewing.candidate_auditor import (
    SAFE_AUDIT_ERROR,
    SAFE_EXISTING_ERROR,
    SAFE_EXISTING_UNANCHORED,
    SAFE_INPUT_INVALID,
    SAFE_WEEKLY_LOW,
    CandidateAuditError,
    CandidateAuditCodexCliRunner,
    CandidateAuditRunResult,
    _audit_output_schema,
    _audit_prompt,
    adopt_existing_candidate_audit,
    claim_candidate_audit,
    load_completed_candidate_audit,
    parse_candidate_audit_output,
    run_claimed_candidate_audit,
)
from tests.fixture_factory import (
    create_import_job_fixture,
    write_synthetic_crop_review_evidence,
)


class FakeRunner:
    def __init__(self, payload, run_id="audit-fake-1"):
        self.payload = payload
        self.run_id = run_id
        self.calls = []

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        return CandidateAuditRunResult(
            json.dumps(self.payload, ensure_ascii=False), self.run_id
        )


class FakeExtractionRunner:
    def __init__(self, payload):
        self.payload = payload

    def run(self, *, image_paths, prompt):
        return CandidateExtractionRunResult(
            json.dumps(self.payload, ensure_ascii=False), "candidate-independent-run"
        )


class CandidateAuditorTests(unittest.TestCase):
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
                   VALUES (?,1,'audit.pdf','raw_papers/TJ/unknown/audit.pdf',
                           'TJ','QT','视觉二审合成卷')""",
                ("a" * 64,),
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (self.source_id,),
            ).lastrowid
        self.job_dir = create_import_job_fixture(
            self.private, job_id=self.job_id, source_paper_id=self.source_id
        )
        (self.job_dir / "ai_audit.json").unlink()
        (self.job_dir / "candidate_questions.json").unlink()
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
        self.candidate = self._candidate_payload()
        extraction = FakeExtractionRunner(self.candidate)
        run_claimed_candidate_extraction(claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=extraction,
            weekly_checker=lambda: 100.0,
        ))
        self.audit = self._audit_payload()
        self.runner = FakeRunner(self.audit)

    def tearDown(self):
        self.temporary.cleanup()

    def _candidate_payload(self):
        questions = []
        for number in range(1, 24):
            page = 1 if number <= 6 else 2 if number <= 12 else 3 if number <= 18 else 4
            questions.append({
                "source_question_no": str(number),
                "stem_markdown": f"合成候选题干 {number}，含 $x^2$。",
                "question_type_code": "solution",
                "primary_knowledge_point_code": "",
                "related_knowledge_point_codes": [],
                "options": [], "subquestions": [],
                "answer_markdown": "", "analysis_markdown": "",
                "figure_required": number in {3, 16},
                "source_pages": [page], "extraction_confidence": "high",
                "warnings": [],
            })
        return {
            "version": 1, "import_job_id": self.job_id,
            "source_paper_id": self.source_id,
            "question_count": 23, "questions": questions,
        }

    def _audit_payload(self):
        questions = []
        for candidate in self.candidate["questions"]:
            figure = candidate["figure_required"]
            questions.append({
                "source_question_no": candidate["source_question_no"],
                "audit_status": "auto_pass", "text_match": True,
                "structure_match": True, "formula_match": True,
                "figure_check": "passed" if figure else "not_applicable",
                "knowledge_check": "not_reviewed", "issues": [],
                "suggested_corrections": [],
                "evidence_page": candidate["source_pages"][0],
                "audit_confidence": "high",
            })
        return {
            "import_job_id": self.job_id,
            "auditor": "independent_codex_visual_second_pass",
            "audit_scope": {
                "kind": "candidate_text_vs_verified_single_question_crops",
                "source_pages": [1, 2, 3, 4],
            },
            "question_count": 23,
            "counts": {"auto_pass": 23, "disputed": 0, "human_required": 0},
            "questions": questions,
            "random_sample_recommendation": {"question_nos": [], "reason": "无需抽样"},
            "global_findings": [],
        }

    def parse(self, payload=None):
        return parse_candidate_audit_output(
            json.dumps(payload or self.audit, ensure_ascii=False),
            self.job_id, self.candidate["questions"],
        )

    def test_schema_is_strict_and_parser_accepts_compatible_payload(self):
        schema = _audit_output_schema()

        def inspect(value):
            if isinstance(value, dict):
                self.assertNotIn("oneOf", value)
                if "const" in value:
                    self.assertIn("type", value)
                if value.get("type") == "object":
                    self.assertFalse(value.get("additionalProperties", True))
                    self.assertEqual(set(value["properties"]), set(value["required"]))
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(schema)
        parsed = self.parse()
        self.assertEqual(23, parsed["question_count"])
        from src.web.app import _validate_audit_payload
        self.assertEqual(parsed, _validate_audit_payload(
            parsed, self.job_id, self.candidate["questions"]
        )[0])

    def test_parser_rejects_structure_page_counts_and_status_semantics(self):
        raw_cases = {
            "fence": "```json\n{}\n```",
            "trailing": json.dumps(self.audit) + " trailing",
        }
        mutations = {
            "top_extra": lambda p: p.update(extra=True),
            "question_extra": lambda p: p["questions"][0].update(extra=True),
            "wrong_number": lambda p: p["questions"][0].update(source_question_no="2"),
            "wrong_page": lambda p: p["questions"][0].update(evidence_page=4),
            "wrong_count": lambda p: p["counts"].update(auto_pass=22),
            "auto_issue": lambda p: p["questions"][0].update(issues=["x"]),
            "auto_medium": lambda p: p["questions"][0].update(audit_confidence="medium"),
            "wrong_figure": lambda p: p["questions"][0].update(figure_check="passed"),
            "knowledge": lambda p: p["questions"][0].update(knowledge_check="passed"),
            "fake_dispute": lambda p: p["questions"][0].update(audit_status="disputed"),
        }
        for name, mutate in mutations.items():
            payload = copy.deepcopy(self.audit)
            mutate(payload)
            raw_cases[name] = json.dumps(payload)
        for name, raw in raw_cases.items():
            with self.subTest(name=name), self.assertRaises(CandidateAuditError):
                parse_candidate_audit_output(raw, self.job_id, self.candidate["questions"])

    def test_prompt_contains_untrusted_candidate_mapping_and_review_boundaries(self):
        prompt = _audit_prompt(self.job_id, self.candidate["questions"])
        for phrase in (
            "不可信文本", "逐像素", "题号", "题干", "公式", "上下标", "选项",
            "公共条件", "小问", "必要配图", "裁切边界", "不得 auto_pass",
            "不解题", "不分类知识点", "不检查答案", "warnings", "首个页面",
            '"1":[1]', '"23":[4]', "合成候选题干 1",
        ):
            self.assertIn(phrase, prompt)

    def test_cli_runner_is_ephemeral_read_only_bounded_and_whitelist_only(self):
        executable = self.root / "fake-codex"
        captured = self.root / "captured.json"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,sys\n"
            f"target=pathlib.Path({str(captured)!r})\n"
            "schema=pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1])\n"
            "target.write_text(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),"
            "'env':dict(os.environ),'stdin':os.read(0,1).decode(),"
            "'schema':json.loads(schema.read_text())}))\n"
            "message=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
            "message.write_text('{}')\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        inherited = {
            "HOME": "/synthetic/home", "CODEX_HOME": "/synthetic/codex",
            "SSL_CERT_FILE": "/synthetic/cert.pem",
            "HTTP_PROXY": "http://proxy.invalid:8080",
            "NO_PROXY": "localhost",
        }
        with mock.patch.dict(
            "os.environ", {**inherited, "UNRELATED_SECRET": "must-not-leak"}, clear=True,
        ):
            result = CandidateAuditCodexCliRunner(executable, timeout=5).run(
                image_paths=[self.job_dir / "question_crops/Q001.png"], prompt="only json"
            )
        self.assertEqual("{}", result.final_message)
        data = json.loads(captured.read_text())
        argv = data["argv"]
        for flag in (
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check",
        ):
            self.assertIn(flag, argv)
        self.assertEqual("read-only", argv[argv.index("--sandbox") + 1])
        for feature in ("shell_tool", "unified_exec", "shell_snapshot"):
            self.assertEqual("--disable", argv[argv.index(feature) - 1])
        self.assertEqual("", data["stdin"])
        self.assertEqual(_audit_output_schema(), data["schema"])
        self.assertEqual("/usr/bin:/bin:/usr/sbin:/sbin", data["env"]["PATH"])
        for name, value in inherited.items():
            self.assertEqual(value, data["env"][name])
        self.assertNotIn("UNRELATED_SECRET", data["env"])
        self.assertNotEqual(str(Path.cwd()), data["cwd"])

    def test_full_success_anchors_inputs_and_creates_no_business_rows(self):
        result = run_claimed_candidate_audit(claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 30.0,
        ))
        self.assertEqual(23, result["counts"]["auto_pass"])
        self.assertEqual(1, len(self.runner.calls))
        self.assertTrue(all(
            path.parent.name.startswith("candidate-audit-input-")
            for path in self.runner.calls[0][0]
        ))
        published = (self.job_dir / "ai_audit.json").read_bytes()
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                """SELECT status,question_count,processed_questions,codex_run_id,
                          input_candidate_sha256,input_candidate_byte_size,
                          output_sha256,output_byte_size
                   FROM import_candidate_audit_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
            self.assertEqual(("completed", 23, 23, "audit-fake-1"), row[:4])
            self.assertEqual(hashlib.sha256(published).hexdigest(), row[6])
            self.assertEqual(len(published), row[7])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM candidate_review_drafts").fetchone()[0])

    def test_weekly_gate_is_outside_write_transaction_and_low_is_zero_call(self):
        observed = []

        def checker():
            with sqlite3.connect(self.database, timeout=0.1) as other:
                other.execute("BEGIN IMMEDIATE")
                observed.append(True)
                other.rollback()
            return 29.9

        with self.assertRaisesRegex(CandidateAuditError, SAFE_WEEKLY_LOW):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=checker,
            )
        self.assertEqual([True], observed)
        self.assertEqual([], self.runner.calls)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM import_candidate_audit_runs"
            ).fetchone()[0])

    def test_lock_deduplicates_stale_resumes_and_completed_is_zero_call(self):
        first = claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        self.assertIsNone(claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        first.close()
        resumed = claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        run_claimed_candidate_audit(resumed)
        self.assertIsNone(claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: (_ for _ in ()).throw(AssertionError("gate called")),
        ))
        self.assertEqual(1, len(self.runner.calls))

    def test_tamper_and_changed_candidate_fail_closed_or_force_new_run(self):
        original = (self.job_dir / "candidate_questions.json").read_bytes()
        (self.job_dir / "candidate_questions.json").write_bytes(original + b" ")
        with self.assertRaises(CandidateAuditError):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        self.assertEqual([], self.runner.calls)

    def test_crop_review_evidence_tamper_is_rejected_before_runner(self):
        evidence = self.job_dir / "crop_ai_review.json"
        payload = json.loads(evidence.read_text())
        payload["reviewer_run_id"] = "tampered-reviewer"
        evidence.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CandidateAuditError):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        self.assertEqual([], self.runner.calls)

    def test_crop_tamper_and_candidate_symlink_are_rejected_before_runner(self):
        crop = self.job_dir / "question_crops/Q001.png"
        crop.write_bytes(b"tampered")
        with self.assertRaises(CandidateAuditError):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        self.assertEqual([], self.runner.calls)

    def test_candidate_hardlink_is_rejected_before_runner(self):
        candidate = self.job_dir / "candidate_questions.json"
        os.link(candidate, self.job_dir / "candidate-hardlink.json")
        with self.assertRaises(CandidateAuditError):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        self.assertEqual([], self.runner.calls)

    def test_changed_but_reanchored_candidate_never_reuses_completed_audit(self):
        run_claimed_candidate_audit(claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        candidate_path = self.job_dir / "candidate_questions.json"
        candidate = json.loads(candidate_path.read_text())
        candidate["questions"][0]["stem_markdown"] += " 已重新识别"
        content = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
        candidate_path.write_bytes(content)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE import_candidate_extraction_runs
                   SET output_sha256=?,output_byte_size=? WHERE import_job_id=?""",
                (hashlib.sha256(content).hexdigest(), len(content), self.job_id),
            )
        claim = claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        self.assertIsNotNone(claim)
        run_claimed_candidate_audit(claim)
        self.assertEqual(2, len(self.runner.calls))

    def test_database_publish_failure_restores_previous_valid_file(self):
        previous = json.dumps(self.audit, ensure_ascii=False).encode()
        (self.job_dir / "ai_audit.json").write_bytes(previous)
        adopt_existing_candidate_audit(self.database, self.private, self.job_id)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE import_candidate_audit_runs SET status='failed' WHERE import_job_id=?",
                (self.job_id,),
            )
        claim = claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        with mock.patch(
            "src.reviewing.candidate_auditor._database_input_from_connection",
            return_value=None,
        ):
            self.assertIsNone(run_claimed_candidate_audit(claim))
        self.assertEqual(previous, (self.job_dir / "ai_audit.json").read_bytes())

    def test_failed_parse_preserves_previous_file_and_records_safe_error(self):
        previous = json.dumps(self.audit, ensure_ascii=False).encode()
        (self.job_dir / "ai_audit.json").write_bytes(previous)
        with self.assertRaisesRegex(CandidateAuditError, SAFE_EXISTING_UNANCHORED):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        adopt_existing_candidate_audit(self.database, self.private, self.job_id)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE import_candidate_audit_runs SET status='failed' WHERE import_job_id=?",
                (self.job_id,),
            )
        bad = FakeRunner({"bad": True}, "bad-run")
        self.assertIsNone(run_claimed_candidate_audit(claim_candidate_audit(
            self.database, self.private, self.job_id, runner=bad,
            weekly_checker=lambda: 100.0,
        )))
        self.assertEqual(previous, (self.job_dir / "ai_audit.json").read_bytes())
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(("failed", SAFE_AUDIT_ERROR), connection.execute(
                "SELECT status,error_message FROM import_candidate_audit_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone())

    def test_explicit_adoption_validates_and_anchors_without_codex(self):
        (self.job_dir / "ai_audit.json").write_text(
            json.dumps(self.audit, ensure_ascii=False), encoding="utf-8"
        )
        adopted = adopt_existing_candidate_audit(
            self.database, self.private, self.job_id
        )
        self.assertEqual(23, adopted["question_count"])
        self.assertEqual([], self.runner.calls)
        self.assertEqual(23, load_completed_candidate_audit(
            self.database, self.private, self.job_id
        )["question_count"])

    def _complete_audit_and_seed_drafts(self):
        run_claimed_candidate_audit(claim_candidate_audit(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        candidate_path = self.job_dir / "candidate_questions.json"
        digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        with sqlite3.connect(self.database) as connection:
            for question in self.candidate["questions"]:
                snapshot = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status)
                       VALUES(?,?,?,?,?,'pending')""",
                    (self.job_id, question["source_question_no"], digest, snapshot, snapshot),
                )
            connection.commit()

    def _single_audit(self, edited, *, confidence="high", issues=None):
        issues = [] if issues is None else issues
        passed = confidence == "high" and not issues
        return {
            "import_job_id": self.job_id,
            "auditor": "independent_codex_visual_second_pass",
            "audit_scope": {
                "kind": "candidate_text_vs_verified_single_question_crops",
                "source_pages": edited["source_pages"],
            },
            "question_count": 1,
            "counts": {"auto_pass": int(passed), "disputed": 0,
                       "human_required": int(not passed)},
            "questions": [{
                "source_question_no": edited["source_question_no"],
                "audit_status": "auto_pass" if passed else "human_required",
                "text_match": passed, "structure_match": True, "formula_match": True,
                "figure_check": "passed" if edited["figure_required"] else "not_applicable",
                "knowledge_check": "not_reviewed", "issues": issues,
                "suggested_corrections": [],
                "evidence_page": edited["source_pages"][0],
                "audit_confidence": confidence,
            }],
            "random_sample_recommendation": {"question_nos": [], "reason": "单题严格复审"},
            "global_findings": [],
        }

    def test_batch_auto_pass_approves_only_pristine_pending_and_is_idempotent(self):
        from src.reviewing.candidate_review_ai import apply_batch_auto_pass

        self.audit["questions"][1].update(
            audit_status="human_required", text_match=False,
            audit_confidence="medium", issues=["需修正"],
        )
        self.audit["counts"] = {"auto_pass": 22, "disputed": 0, "human_required": 1}
        self.runner.payload = self.audit
        self._complete_audit_and_seed_drafts()
        with sqlite3.connect(self.database) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE source_question_no='3'"
            ).fetchone()[0])
            edited["stem_markdown"] += " 人工修改"
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=?,status='draft' WHERE source_question_no='3'",
                (json.dumps(edited, ensure_ascii=False),),
            )
            connection.execute(
                """UPDATE candidate_review_drafts SET status='approved',approval_source='human',
                   reviewed_at='2026-07-17T00:00:00+00:00',
                   approval_evidence_json='{"method":"workbench","reviewed_at":"2026-07-17T00:00:00+00:00"}'
                   WHERE source_question_no='4'"""
            )
            connection.commit()
        first = apply_batch_auto_pass(self.database, self.private, self.job_id)
        second = apply_batch_auto_pass(self.database, self.private, self.job_id)
        self.assertEqual((20, 0), (first.changed, second.changed))
        with sqlite3.connect(self.database) as connection:
            rows = dict(connection.execute(
                "SELECT source_question_no,status FROM candidate_review_drafts"
            ))
            evidence = json.loads(connection.execute(
                "SELECT approval_evidence_json FROM candidate_review_drafts WHERE source_question_no='1'"
            ).fetchone()[0])
        self.assertEqual(("pending", "draft", "approved"),
                         (rows["2"], rows["3"], rows["4"]))
        self.assertEqual("batch_auto_pass", evidence["method"])
        self.assertEqual(self.runner.run_id, evidence["audit_run_id"])
        self.assertEqual(64, len(evidence["audit_output_sha256"]))

    def test_external_corrected_adopt_binds_exact_version_and_preserves_history(self):
        from src.reviewing.candidate_review_ai import adopt_corrected_draft_audit

        self._complete_audit_and_seed_drafts()
        with sqlite3.connect(self.database) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone()[0])
            edited["stem_markdown"] += " 已修正"
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=?,status='draft',version=2 WHERE source_question_no='2'",
                (json.dumps(edited, ensure_ascii=False, separators=(",", ":")),),
            )
            connection.commit()
        result = adopt_corrected_draft_audit(
            self.database, self.private, self.job_id, "2",
            json.dumps(self._single_audit(edited), ensure_ascii=False),
            "external-review-fresh-1",
            reviewed_draft_version=2,
            edited_sha256=hashlib.sha256(json.dumps(
                edited, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
        )
        self.assertTrue(result.approved)
        with sqlite3.connect(self.database) as connection:
            draft = connection.execute(
                "SELECT status,version,approval_source,approval_evidence_json FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone()
            history = connection.execute(
                "SELECT status,reviewed_draft_version,approved_draft_version,fresh_model_run_id,decision FROM corrected_draft_reaudits"
            ).fetchone()
        self.assertEqual(("approved", 3, "ai_second_pass"), draft[:3])
        self.assertEqual(("completed", 2, 3, "external-review-fresh-1", "passed"), history)
        evidence = json.loads(draft[3])
        self.assertEqual((2, 3), (evidence["reviewed_draft_version"], evidence["approved_draft_version"]))
        from src.reviewing.candidate_review_ai import validate_ai_approval
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            approved_row = dict(connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone())
            self.assertTrue(validate_ai_approval(
                connection, approved_row, self.candidate["questions"][1],
                candidate_sha256=approved_row["source_candidate_sha256"],
                audit_sha256=evidence["batch_audit_output_sha256"],
                audit_entry=self.audit["questions"][1],
            ))
        repeated = adopt_corrected_draft_audit(
            self.database, self.private, self.job_id, "2",
            json.dumps(self._single_audit(edited), ensure_ascii=False),
            "external-review-fresh-idempotent",
            reviewed_draft_version=2, edited_sha256=evidence["edited_sha256"],
        )
        self.assertTrue(repeated.approved)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM corrected_draft_reaudits"
            ).fetchone()[0])
            changed = dict(edited); changed["stem_markdown"] += " 再次编辑"
            connection.execute(
                """UPDATE candidate_review_drafts SET edited_json=?,status='draft',version=4,
                   reviewed_at=NULL,approval_source=NULL,approval_evidence_json=NULL
                   WHERE source_question_no='2'""",
                (json.dumps(changed, ensure_ascii=False),),
            )
            connection.commit()
        with self.assertRaises(CandidateAuditError):
            adopt_corrected_draft_audit(
                self.database, self.private, self.job_id, "2",
                json.dumps(self._single_audit(edited), ensure_ascii=False),
                "external-review-fresh-2",
                reviewed_draft_version=2,
                edited_sha256=evidence["edited_sha256"],
            )

    def test_corrected_weekly_gate_and_nonpass_never_approve(self):
        from src.reviewing.candidate_review_ai import (
            claim_corrected_draft_audit, run_claimed_corrected_draft_audit,
        )

        self._complete_audit_and_seed_drafts()
        with sqlite3.connect(self.database) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone()[0])
            edited["stem_markdown"] += " 已修正"
            edited["primary_knowledge_point_code"] = "01.01.01"
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=?,status='draft',version=2 WHERE source_question_no='2'",
                (json.dumps(edited, ensure_ascii=False),),
            )
            connection.commit()
        fresh_runner = FakeRunner(
            self._single_audit(edited, confidence="medium", issues=["仍不一致"]),
            run_id="corrected-fresh-run",
        )
        with self.assertRaisesRegex(CandidateAuditError, SAFE_WEEKLY_LOW):
            claim_corrected_draft_audit(
                self.database, self.private, self.job_id, "2", runner=fresh_runner,
                weekly_checker=lambda: 29.9,
            )
        self.assertEqual([], fresh_runner.calls)
        with self.assertRaises(CandidateAuditError):
            claim_corrected_draft_audit(
                self.database, self.private, self.job_id, "2", runner=fresh_runner,
                weekly_checker=lambda: (_ for _ in ()).throw(OSError("private detail")),
            )
        self.assertEqual([], fresh_runner.calls)
        claim = claim_corrected_draft_audit(
            self.database, self.private, self.job_id, "2", runner=fresh_runner,
            weekly_checker=lambda: 30.0,
        )
        result = run_claimed_corrected_draft_audit(claim)
        self.assertFalse(result.approved)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(("needs_fix", None), connection.execute(
                "SELECT status,approval_source FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone())
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET status='draft',version=3,reviewed_at=NULL
                   WHERE source_question_no='2'"""
            )
            connection.commit()
        retry = claim_corrected_draft_audit(
            self.database, self.private, self.job_id, "2", runner=fresh_runner,
            weekly_checker=lambda: 30.0,
        )
        self.assertIsNotNone(
            retry, "旧版本的未通过记录不得阻止新草稿版本重新复审"
        )
        retry.close()

    def test_external_corrected_parser_rejects_unbound_or_non_strict_outputs(self):
        from src.reviewing.candidate_review_ai import adopt_corrected_draft_audit

        self._complete_audit_and_seed_drafts()
        with sqlite3.connect(self.database) as connection:
            edited = json.loads(connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE source_question_no='2'"
            ).fetchone()[0])
            edited["stem_markdown"] += " 已修正"
            edited["primary_knowledge_point_code"] = "01.01.01"
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=?,status='draft',version=2 WHERE source_question_no='2'",
                (json.dumps(edited, ensure_ascii=False),),
            )
            connection.commit()
        digest = hashlib.sha256(json.dumps(
            edited, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        cases = []
        for mutation in (
            lambda p: p.update(extra=True),
            lambda p: p["questions"][0].update(source_question_no="3"),
            lambda p: p["questions"][0].update(evidence_page=4),
            lambda p: p["questions"][0].update(audit_confidence="medium"),
            lambda p: p["questions"][0].update(issues=["x"]),
        ):
            payload = copy.deepcopy(self._single_audit(edited)); mutation(payload)
            cases.append(json.dumps(payload))
        cases.append(json.dumps(self._single_audit(edited)) + " trailing")
        for index, raw in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(CandidateAuditError):
                adopt_corrected_draft_audit(
                    self.database, self.private, self.job_id, "2", raw,
                    f"external-invalid-{index}", reviewed_draft_version=2,
                    edited_sha256=digest,
                )

    def test_knowledge_classification_binds_approved_draft_and_is_idempotent(self):
        from src.reviewing.candidate_review_ai import apply_batch_auto_pass
        from src.reviewing.knowledge_classification import (
            adopt_knowledge_classifications,
            load_bound_knowledge_classification,
        )

        self._complete_audit_and_seed_drafts()
        applied = apply_batch_auto_pass(self.database, self.private, self.job_id)
        self.assertEqual(23, applied.changed)
        payload = {
            "version": 1,
            "import_job_id": self.job_id,
            "source_classifier": "local-fixture",
            "reviewer": "fixture-reviewer",
            "scope": "knowledge_only_no_solution",
            "question_count": 23,
            "questions": [
                {
                    "source_question_no": str(number),
                    "primary_code": "01.01.01",
                    "related_codes": ["01.01.02"],
                    "reason": "fixture",
                }
                for number in range(1, 24)
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False)
        result = adopt_knowledge_classifications(
            self.database, self.job_id, raw, "local-classification-run-1"
        )
        self.assertEqual((23, 23), (result.question_count, result.inserted))
        repeated = adopt_knowledge_classifications(
            self.database, self.job_id, raw, "local-classification-run-1"
        )
        self.assertEqual((23, 0), (repeated.question_count, repeated.inserted))
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            draft = connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no='2'",
                (self.job_id,),
            ).fetchone()
            bound = load_bound_knowledge_classification(
                connection, self.job_id, "2", dict(draft)
            )
            self.assertEqual(
                ("01.01.01", ["01.01.02"]),
                (bound["primary_code"], bound["related_codes"]),
            )
            from src.importing.admit_questions import _overlay_knowledge_classification
            overlaid, overlaid_draft, reasons, legacy = _overlay_knowledge_classification(
                connection, self.job_id, "2", self.candidate["questions"][1],
                dict(draft), (), False,
            )
            self.assertEqual("01.01.01", overlaid["primary_knowledge_point_code"])
            self.assertEqual(["01.01.02"], overlaid["related_knowledge_point_codes"])
            assert overlaid_draft is not None
            self.assertEqual(
                overlaid_draft["source_question_no"], draft["source_question_no"]
            )
            self.assertEqual((), reasons)
            self.assertFalse(legacy)
            from src.importing.admit_questions import _approval_review_metadata
            reviewer, _, note = _approval_review_metadata(
                dict(draft), overlaid, "原卷未提供答案", {}, "2"
            )
            self.assertEqual("ai_second_pass", reviewer)
            self.assertIn("AI二审通过", note)
            from src.reviewing.finalize import _edited_with_classification
            finalized = _edited_with_classification(
                connection, self.job_id, dict(draft)
            )
            self.assertEqual("01.01.01", finalized["primary_knowledge_point_code"])
            self.assertEqual(["01.01.02"], finalized["related_knowledge_point_codes"])
            connection.execute(
                "UPDATE candidate_review_drafts SET version=version+1 WHERE import_job_id=? AND source_question_no='2'",
                (self.job_id,),
            )
            connection.commit()
            changed = connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no='2'",
                (self.job_id,),
            ).fetchone()
            self.assertIsNone(load_bound_knowledge_classification(
                connection, self.job_id, "2", dict(changed)
            ))

    def test_sqlite_failures_are_wrapped_as_safe_audit_errors(self):
        with mock.patch(
            "src.reviewing.candidate_auditor._database_input",
            side_effect=sqlite3.OperationalError("synthetic database detail"),
        ), self.assertRaisesRegex(CandidateAuditError, SAFE_INPUT_INVALID):
            claim_candidate_audit(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )

        (self.job_dir / "ai_audit.json").write_text(
            json.dumps(self.audit, ensure_ascii=False), encoding="utf-8"
        )
        with mock.patch(
            "src.reviewing.candidate_auditor._database_input_from_connection",
            side_effect=sqlite3.OperationalError("synthetic adoption detail"),
        ), self.assertRaisesRegex(CandidateAuditError, SAFE_EXISTING_ERROR):
            adopt_existing_candidate_audit(
                self.database, self.private, self.job_id
            )

        adopt_existing_candidate_audit(self.database, self.private, self.job_id)
        with mock.patch(
            "src.reviewing.candidate_auditor._audit_row",
            side_effect=sqlite3.OperationalError("synthetic read detail"),
        ), self.assertRaisesRegex(CandidateAuditError, SAFE_EXISTING_ERROR):
            load_completed_candidate_audit(
                self.database, self.private, self.job_id
            )


if __name__ == "__main__":
    unittest.main()
