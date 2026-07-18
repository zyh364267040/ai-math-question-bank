import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.database.initialize import initialize_database
from src.processing.candidate_extractor import (
    CandidateExtractionError,
    CandidateExtractionRunResult,
    CandidateCodexCliRunner,
    SAFE_WEEKLY_LOW,
    _candidate_output_schema,
    _prompt,
    claim_candidate_extraction,
    parse_candidate_output,
    run_claimed_candidate_extraction,
)
from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest
from tests.fixture_factory import create_import_job_fixture, write_synthetic_crop_review_evidence


class FakeCandidateRunner:
    def __init__(self, payload=None):
        self.payload = payload
        self.calls = []

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        return CandidateExtractionRunResult(
            json.dumps(self.payload, ensure_ascii=False), "candidate-fake-1"
        )


class CandidateExtractorTests(unittest.TestCase):
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
                   VALUES (? ,1,'synthetic.pdf','raw_papers/TJ/unknown/candidate.pdf',
                           'TJ','QT','候选识别合成卷')""",
                ("a" * 64,),
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES (?,'pending')",
                (source_id,),
            ).lastrowid
        self.job_dir = create_import_job_fixture(
            self.private, job_id=self.job_id, source_paper_id=source_id
        )
        (self.job_dir / "candidate_questions.json").unlink()
        manifest_bytes = (self.job_dir / "question_crops.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        write_synthetic_crop_review_evidence(self.job_dir)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,question_count,processed_pages,codex_run_id,
                    result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                    crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                    completed_at,updated_at)
                   VALUES (?,'completed',23,4,'split-fake',?,?,?,?,?,?,CURRENT_TIMESTAMP,
                           CURRENT_TIMESTAMP)""",
                (
                    self.job_id, "b" * 64, "c" * 64, "a" * 64,
                    hashlib.sha256(manifest_bytes).hexdigest(),
                    manifest["generation_id"], manifest["signature"],
                ),
            )
        self.payload = self.valid_payload(source_id)
        self.runner = FakeCandidateRunner(self.payload)

    def tearDown(self):
        self.temporary.cleanup()

    def valid_payload(self, source_id):
        questions = []
        for number in range(1, 24):
            choice = number == 1
            questions.append({
                "source_question_no": str(number),
                "stem_markdown": f"合成题干 {number}，保留 $x^2$。",
                "question_type_code": "single_choice" if choice else "solution",
                "primary_knowledge_point_code": "",
                "related_knowledge_point_codes": [],
                "options": ([
                    {"code": "A", "content": "选项甲"},
                    {"code": "B", "content": "选项乙"},
                ] if choice else []),
                "subquestions": [],
                "answer_markdown": "",
                "analysis_markdown": "",
                "figure_required": False,
                "source_pages": [1 if number <= 6 else 2 if number <= 12 else 3 if number <= 18 else 4],
                "extraction_confidence": "high",
                "warnings": [],
            })
        return {
            "version": 1, "import_job_id": self.job_id,
            "source_paper_id": source_id, "question_count": 23,
            "questions": questions,
        }

    def test_production_schema_is_strict_cli_compatible(self):
        schema = _candidate_output_schema()

        def inspect(value):
            if isinstance(value, dict):
                self.assertNotIn("oneOf", value)
                if "const" in value:
                    self.assertIn("type", value)
                if value.get("type") == "object":
                    self.assertFalse(value.get("additionalProperties", True))
                    self.assertEqual(
                        set(value.get("properties", {})), set(value.get("required", []))
                    )
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(schema)
        self.assertEqual(
            "^[1-9][0-9]{0,2}$",
            schema["properties"]["questions"]["items"]["properties"]
            ["source_question_no"]["pattern"],
        )
        self.assertEqual(23, parse_candidate_output(
            json.dumps(self.payload), self.job_id, self.payload["source_paper_id"],
            [str(number) for number in range(1, 24)],
            {number: [self.payload["questions"][number - 1]["source_pages"][0]]
             for number in range(1, 24)},
        )["question_count"])

    def test_parser_fails_closed_for_structure_semantics_and_single_json(self):
        cases = {
            "fence": "```json\n{}\n```",
            "trailing": json.dumps(self.payload) + " trailing",
        }
        mutations = {
            "extra": lambda p: p.update(extra=True),
            "wrong_job": lambda p: p.update(import_job_id=999),
            "wrong_source": lambda p: p.update(source_paper_id=999),
            "wrong_count": lambda p: p.update(question_count=22),
            "unordered": lambda p: p["questions"].reverse(),
            "duplicate": lambda p: p["questions"][1].update(source_question_no="1"),
            "empty_stem": lambda p: p["questions"][0].update(stem_markdown="  "),
            "choice_without_options": lambda p: p["questions"][0].update(options=[]),
            "solution_with_options": lambda p: p["questions"][1].update(options=[{"code":"A","content":"x"}]),
            "duplicate_option": lambda p: p["questions"][0]["options"][1].update(code="a"),
            "answer": lambda p: p["questions"][0].update(answer_markdown="A"),
            "analysis": lambda p: p["questions"][0].update(analysis_markdown="解析"),
            "unsafe_page": lambda p: p["questions"][0].update(source_pages=[99]),
            "warning_overflow": lambda p: p["questions"][0].update(warnings=["x"] * 21),
        }
        for name, mutate in mutations.items():
            payload = copy.deepcopy(self.payload)
            mutate(payload)
            cases[name] = json.dumps(payload)
        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaises(CandidateExtractionError):
                parse_candidate_output(
                    raw, self.job_id, self.payload["source_paper_id"],
                    [str(number) for number in range(1, 24)],
                    {number: [self.payload["questions"][number - 1]["source_pages"][0]]
                     for number in range(1, 24)},
                )

    def test_prompt_forbids_guessing_solving_and_answers(self):
        pages = {
            number: self.payload["questions"][number - 1]["source_pages"]
            for number in range(1, 24)
        }
        prompt = _prompt(
            self.job_id, self.payload["source_paper_id"], list(range(1, 24)), pages
        )
        for phrase in (
            "只转录", "不要猜", "不要解题", "LaTeX", "公共条件", "选项", "小问",
            "answer_markdown", "analysis_markdown", "图片顺序",
            "source_question_no 只写纯数字", "source_pages 必须严格使用给定映射",
            '"1":[1]', '"7":[2]', '"19":[4]',
        ):
            self.assertIn(phrase, prompt)

    def test_cli_fake_receives_exact_read_only_isolation_and_production_schema(self):
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
        image = self.job_dir / "question_crops/Q001.png"
        inherited = {
            "HOME": "/synthetic/home",
            "CODEX_HOME": "/synthetic/codex",
            "SSL_CERT_FILE": "/synthetic/cert.pem",
            "SSL_CERT_DIR": "/synthetic/certs",
            "HTTP_PROXY": "http://proxy.invalid:8080",
            "HTTPS_PROXY": "http://proxy.invalid:8443",
            "ALL_PROXY": "socks5://proxy.invalid:1080",
            "NO_PROXY": "localhost,127.0.0.1",
            "http_proxy": "http://lower.invalid:8080",
            "https_proxy": "http://lower.invalid:8443",
            "all_proxy": "socks5://lower.invalid:1080",
            "no_proxy": "localhost",
        }
        with mock.patch.dict(
            "os.environ", {**inherited, "UNRELATED_SECRET": "must-not-leak"}, clear=True,
        ):
            result = CandidateCodexCliRunner(executable, timeout=5).run(
                image_paths=[image], prompt="only json"
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
        self.assertEqual(_candidate_output_schema(), data["schema"])
        self.assertEqual("/usr/bin:/bin:/usr/sbin:/sbin", data["env"]["PATH"])
        for name, value in inherited.items():
            self.assertEqual(value, data["env"][name])
        self.assertNotIn("UNRELATED_SECRET", data["env"])
        self.assertNotEqual(str(Path.cwd()), data["cwd"])

    def test_claim_run_publishes_and_anchors_without_drafts_or_formal_questions(self):
        claim = claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 30.0,
        )
        result = run_claimed_candidate_extraction(claim)
        self.assertEqual(23, result["question_count"])
        self.assertEqual(1, len(self.runner.calls))
        self.assertTrue(all(path.parent.name.startswith("candidate-input-") for path in self.runner.calls[0][0]))
        published = (self.job_dir / "candidate_questions.json").read_bytes()
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                """SELECT status,question_count,processed_questions,codex_run_id,
                          input_crop_generation_id,input_manifest_sha256,
                          output_sha256,output_byte_size
                   FROM import_candidate_extraction_runs WHERE import_job_id=?""",
                (self.job_id,),
            ).fetchone()
            self.assertEqual(("completed", 23, 23, "candidate-fake-1"), row[:4])
            self.assertEqual(hashlib.sha256(published).hexdigest(), row[6])
            self.assertEqual(len(published), row[7])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM candidate_review_drafts").fetchone()[0])

    def test_completed_is_idempotent_and_gate_blocks_before_runner_without_write_lock(self):
        run_claimed_candidate_extraction(claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        self.assertIsNone(claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: (_ for _ in ()).throw(AssertionError("gate should not run")),
        ))
        self.assertEqual(1, len(self.runner.calls))

        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE import_candidate_extraction_runs SET status='failed' WHERE import_job_id=?",
                (self.job_id,),
            )
        observed = []
        def checker():
            other = sqlite3.connect(self.database, timeout=0.1)
            try:
                other.execute("BEGIN IMMEDIATE")
                observed.append(True)
                other.rollback()
            finally:
                other.close()
            return 29.9
        with self.assertRaisesRegex(CandidateExtractionError, SAFE_WEEKLY_LOW):
            claim_candidate_extraction(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=checker,
            )
        self.assertEqual([True], observed)
        self.assertEqual(1, len(self.runner.calls))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_candidate_extraction_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0])

    def test_tampered_crop_or_unverified_review_is_rejected_without_runner(self):
        (self.job_dir / "question_crops/Q001.png").write_bytes(b"tampered")
        with self.assertRaises(CandidateExtractionError):
            claim_candidate_extraction(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        self.assertEqual([], self.runner.calls)

    def test_unverified_review_is_rejected_without_runner(self):
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["questions"][0]["review_status"] = "pending_ai_review"
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        content = manifest_path.read_bytes()
        write_synthetic_crop_review_evidence(self.job_dir)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE import_question_split_runs SET crop_manifest_sha256=?,
                          crop_manifest_signature=? WHERE import_job_id=?""",
                (hashlib.sha256(content).hexdigest(), manifest["signature"], self.job_id),
            )
        with self.assertRaises(CandidateExtractionError):
            claim_candidate_extraction(
                self.database, self.private, self.job_id, runner=self.runner,
                weekly_checker=lambda: 100.0,
            )
        self.assertEqual([], self.runner.calls)

    def test_concurrent_claim_is_deduplicated_and_stale_processing_can_resume(self):
        first = claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        self.assertIsNone(claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        first.close()
        with sqlite3.connect(self.database) as connection:
            self.assertEqual("processing", connection.execute(
                "SELECT status FROM import_candidate_extraction_runs WHERE import_job_id=?",
                (self.job_id,),
            ).fetchone()[0])
        resumed = claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        self.assertIsNotNone(resumed)
        run_claimed_candidate_extraction(resumed)
        self.assertEqual(1, len(self.runner.calls))

    def test_changed_signed_generation_does_not_reuse_completed_result(self):
        run_claimed_candidate_extraction(claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        ))
        manifest_path = self.job_dir / "question_crops.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["generation_id"] = "f" * 32
        manifest = sign_manifest(load_hmac_key(self.job_dir), manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        content = manifest_path.read_bytes()
        write_synthetic_crop_review_evidence(self.job_dir)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE import_question_split_runs SET crop_manifest_sha256=?,
                          crop_generation_id=?,crop_manifest_signature=?
                   WHERE import_job_id=?""",
                (hashlib.sha256(content).hexdigest(), manifest["generation_id"],
                 manifest["signature"], self.job_id),
            )
        claim = claim_candidate_extraction(
            self.database, self.private, self.job_id, runner=self.runner,
            weekly_checker=lambda: 100.0,
        )
        self.assertIsNotNone(claim)
        run_claimed_candidate_extraction(claim)
        self.assertEqual(2, len(self.runner.calls))


if __name__ == "__main__":
    unittest.main()
