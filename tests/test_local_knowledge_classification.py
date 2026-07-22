import hashlib
import errno
import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.reviewing.local_knowledge_classification import (
    SAFE_CLASSIFICATION_INPUT,
    SAFE_CLASSIFICATION_MODEL,
    KnowledgeClassificationRunError,
    OllamaKnowledgeClassificationRunner,
    _publish_output,
    _read_bounded,
    _heartbeat,
    _parse_level2,
    _parse_level3,
    _prompt,
    apply_classification_evidence,
    claim_knowledge_classification,
    load_classification_page,
    review_classification_draft,
    run_claimed_knowledge_classification,
)
from src.web.app import create_app


class FakeRunner:
    def __init__(self, *, conflict=False, low=False, invalid=None):
        self.calls = []
        self.conflict = conflict
        self.low = low
        self.invalid = invalid

    def run(self, stage, prompt):
        self.calls.append((stage, prompt))
        if self.invalid and stage == self.invalid[0]:
            return self.invalid[1]
        confidence = "low" if self.low and stage == "proposal" else "high"
        if stage == "level2":
            rows = [
                {"source_question_no": number, "level2_code": "01.01",
                 "confidence": "high", "reason": "属于集合模块"}
                for number in ("1", "2")
            ]
        else:
            rows = []
            for number in ("1", "2"):
                primary = "01.01.01"
                if self.conflict and stage == "verifier" and number == "2":
                    primary = "01.01.02"
                rows.append({
                    "source_question_no": number, "primary_code": primary,
                    "related_codes": ["01.01.02"] if primary != "01.01.02" else [],
                    "confidence": confidence, "reason": f"{stage} 简短理由",
                })
        return json.dumps({"questions": rows}, ensure_ascii=False)


class PromptInspectingRunner:
    def __init__(self, taxonomy):
        self.calls = []
        children = {}
        for row in taxonomy:
            if row["level"] == 3:
                children.setdefault(row["parent_code"], []).append(row["code"])
        self.parents = [
            row["code"] for row in taxonomy
            if row["level"] == 2 and children.get(row["code"])
        ]
        self.children = children

    def run(self, stage, prompt):
        payload = json.loads(prompt)
        self.calls.append((stage, payload))
        numbers = [row["source_question_no"] for row in payload["questions"]]
        if stage == "level2":
            rows = [{
                "source_question_no": number,
                "level2_code": self.parents[index],
                "confidence": "high",
                "reason": "独立二级判断",
            } for index, number in enumerate(numbers)]
        else:
            rows = []
            for item in payload["questions"]:
                candidates = item["level3_candidates"]
                rows.append({
                    "source_question_no": item["source_question_no"],
                    "primary_code": candidates[0]["code"],
                    "related_codes": [],
                    "confidence": "high",
                    "reason": f"{stage} 独立判断",
                })
        return json.dumps({"questions": rows}, ensure_ascii=False)


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum):
        return self.payload[:maximum]


class LocalKnowledgeClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "question-bank.db"
        self.private = self.root / "private"
        self.job_dir = self.private / "processing" / "import_job_1"
        self.job_dir.mkdir(parents=True)
        initialize_database(self.db).close()
        self._seed()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        questions = [
            {"source_question_no": str(number), "stem_markdown": f"合成题干 <b>{number}</b>",
             "question_type_code": "fill_blank", "options": [], "subquestions": [],
             "primary_knowledge_point_code": "", "related_knowledge_point_codes": []}
            for number in (1, 2)
        ]
        candidate = {"import_job_id": 1, "source_paper_id": 1,
                     "question_count": 2, "questions": questions}
        audit = {
            "import_job_id": 1, "question_count": 2,
            "counts": {"auto_pass": 2, "disputed": 0, "human_required": 0},
            "questions": [
                {"source_question_no": str(number), "audit_status": "auto_pass",
                 "issues": [], "suggested_corrections": [], "evidence_page": 1,
                 "audit_confidence": "high"} for number in (1, 2)
            ],
        }
        candidate_raw = json.dumps(candidate, ensure_ascii=False).encode()
        audit_raw = json.dumps(audit, ensure_ascii=False).encode()
        (self.job_dir / "candidate_questions.json").write_bytes(candidate_raw)
        (self.job_dir / "ai_audit.json").write_bytes(audit_raw)
        with closing(sqlite3.connect(self.db)) as connection:
            source = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_type_code,paper_name) VALUES(?,1,'x.pdf',
                    'raw_papers/TJ/unknown/x.pdf','TJ','GK','合成卷')""", ("a" * 64,)
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES(1,?,'needs_review')",
                (source,),
            )
            connection.execute(
                """INSERT INTO import_candidate_audit_runs
                   (import_job_id,status,question_count,processed_questions,codex_run_id,
                    input_candidate_sha256,input_candidate_byte_size,input_crop_generation_id,
                    input_manifest_sha256,input_manifest_signature,output_sha256,
                    output_byte_size,completed_at,updated_at)
                   VALUES(1,'completed',2,2,'fake-audit',?,?,?, ?,?,?,?,
                          '2026-07-18T00:00:00+00:00','2026-07-18T00:00:00+00:00')""",
                (hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw),
                 "1" * 32, "2" * 64, "3" * 64,
                 hashlib.sha256(audit_raw).hexdigest(), len(audit_raw)),
            )
            candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
            for question in questions:
                encoded = json.dumps(question, ensure_ascii=False)
                evidence = json.dumps({"method": "workbench", "reviewed_at": "2026-07-18T00:00:00+00:00"})
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at,
                        approval_source,approval_evidence_json)
                       VALUES(1,?,?,?,?, 'approved',2,'2026-07-18T00:00:00+00:00',
                              'human',?)""",
                    (question["source_question_no"], candidate_sha, encoded, encoded, evidence),
                )
            connection.commit()

    def _complete(self, runner=None):
        runner = runner or FakeRunner()
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=runner)
        self.assertIsNotNone(claim)
        run_claimed_knowledge_classification(claim)
        return runner

    def _web_client(self, runner=None):
        runner = runner or FakeRunner()
        app = create_app(self.db, self.private, classification_runner=runner)
        client = TestClient(app)
        client.get("/imports/1/classification")
        return client, runner, client.cookies.get("basket_csrf")

    def test_schema_and_repeat_migration_are_safe(self):
        initialize_database(self.db).close()
        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("import_knowledge_classification_runs", tables)
            self.assertIn("candidate_knowledge_classification_drafts", tables)
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_review_drafts").fetchone()[0])
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(import_knowledge_classification_runs)")}
            self.assertIn("stage", columns)

    def test_incomplete_visual_approvals_never_call_runner(self):
        runner = FakeRunner()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE candidate_review_drafts SET status='pending' WHERE source_question_no='2'")
        with self.assertRaisesRegex(KnowledgeClassificationRunError, SAFE_CLASSIFICATION_INPUT):
            claim_knowledge_classification(self.db, self.private, 1, runner=runner)
        self.assertEqual([], runner.calls)

    def test_double_pass_auto_approval_and_conflict_pending(self):
        runner = self._complete(FakeRunner(conflict=True))
        self.assertEqual(["level2", "proposal", "verifier"], [x[0] for x in runner.calls])
        page = load_classification_page(self.db, 1)
        self.assertEqual((1, 1, 1), (page.auto_approved, page.pending, page.approved))
        self.assertEqual("2", page.drafts[0]["source_question_no"])
        self.assertEqual("local_double_pass", page.drafts[1]["approval_source"])

    def test_level3_prompts_include_only_each_selected_subtree_codes_and_names(self):
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        parents = []
        for row in claim.taxonomy:
            if row["level"] == 2 and any(
                child["level"] == 3 and child["parent_code"] == row["code"]
                for child in claim.taxonomy
            ):
                parents.append(row["code"])
        self.assertGreaterEqual(len(parents), 2)
        runner = PromptInspectingRunner(claim.taxonomy)
        claim = claim.__class__(
            claim.database_path, claim.private_root, claim.job_id, runner,
            claim.claim_token, claim.input_digest, claim.taxonomy_digest,
            claim.questions, claim.taxonomy,
        )
        run_claimed_knowledge_classification(claim)
        self.assertEqual(["level2", "proposal", "verifier"], [item[0] for item in runner.calls])
        self.assertEqual("completed", load_classification_page(self.db, 1).status)
        for stage, payload in runner.calls[1:]:
            self.assertIn(stage, {"proposal", "verifier"})
            for index, question in enumerate(payload["questions"]):
                expected = {
                    row["code"]: row["name"] for row in claim.taxonomy
                    if row["level"] == 3 and row["parent_code"] == parents[index]
                }
                actual = {item["code"]: item["name"] for item in question["level3_candidates"]}
                self.assertEqual(expected, actual)
                leaked = {
                    row["code"] for row in claim.taxonomy
                    if row["level"] == 3 and row["parent_code"] != parents[index]
                }
                self.assertTrue(leaked.isdisjoint(actual))

    def test_ollama_three_stages_use_distinct_system_and_user_requests(self):
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return FakeHTTPResponse(b'{"message":{"content":"{}"}}')

        runner = OllamaKnowledgeClassificationRunner(opener=opener)
        for stage in ("level2", "proposal", "verifier"):
            self.assertEqual("{}", runner.run(stage, '{"questions":[]}'))
        systems = [body["messages"][0]["content"] for body in requests]
        users = [body["messages"][1]["content"] for body in requests]
        self.assertEqual(3, len(set(systems)))
        self.assertEqual(3, len(set(users)))
        self.assertIn("不得假定", systems[2])
        self.assertIn("替代", systems[2])
        self.assertNotIn("proposal", users[2].lower())
        self.assertNotEqual(requests[0]["options"], requests[1]["options"])
        self.assertNotEqual(requests[1]["options"], requests[2]["options"])

    def test_ollama_uses_exact_stage_specific_structured_output_schemas(self):
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return FakeHTTPResponse(b'{"message":{"content":"{\\"questions\\":[]}"}}')

        runner = OllamaKnowledgeClassificationRunner(opener=opener)
        for stage in ("level2", "proposal", "verifier"):
            runner.run(stage, _prompt(stage, [], []))

        formats = [request["format"] for request in requests]
        for schema in formats:
            self.assertIsInstance(schema, dict)
            self.assertEqual("object", schema["type"])
            self.assertEqual({"questions"}, set(schema["properties"]))
            self.assertEqual(["questions"], schema["required"])
            self.assertIs(schema["additionalProperties"], False)

        level2_item = formats[0]["properties"]["questions"]["items"]
        level3_items = [
            schema["properties"]["questions"]["items"] for schema in formats[1:]
        ]
        self.assertEqual(
            {"source_question_no", "level2_code", "confidence", "reason"},
            set(level2_item["properties"]),
        )
        expected_level3 = {
            "source_question_no", "primary_code", "related_codes", "confidence", "reason",
        }
        self.assertEqual([expected_level3, expected_level3], [
            set(item["properties"]) for item in level3_items
        ])
        self.assertNotEqual(level2_item, level3_items[0])
        self.assertEqual(level3_items[0], level3_items[1])

        for item in [level2_item, *level3_items]:
            self.assertEqual("object", item["type"])
            self.assertEqual(set(item["properties"]), set(item["required"]))
            self.assertIs(item["additionalProperties"], False)
            self.assertEqual("string", item["properties"]["source_question_no"]["type"])
            self.assertEqual(
                ["low", "medium", "high"],
                item["properties"]["confidence"]["enum"],
            )
            self.assertEqual(
                {"type": "string", "minLength": 1, "maxLength": 200},
                item["properties"]["reason"],
            )
        for item in level3_items:
            related = item["properties"]["related_codes"]
            self.assertEqual("array", related["type"])
            self.assertEqual({"type": "string"}, related["items"])
            self.assertEqual(2, related["maxItems"])
            self.assertIs(related["uniqueItems"], True)

    def test_ollama_instructions_require_literal_fields_and_string_question_numbers(self):
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return FakeHTTPResponse(b'{"message":{"content":"{}"}}')

        runner = OllamaKnowledgeClassificationRunner(opener=opener)
        for stage in ("level2", "proposal", "verifier"):
            runner.run(stage, _prompt(stage, [], []))

        for request in requests:
            system = request["messages"][0]["content"]
            user = request["messages"][1]["content"]
            prompt_payload = json.loads(user.split("\n", 1)[1])
            self.assertIn("字段名必须逐字使用", system)
            self.assertIn("题号必须字符串", system)
            self.assertIn("字段名必须逐字使用", user)
            self.assertIn("题号必须字符串", user)
            self.assertIn("字段名必须逐字使用", prompt_payload["instruction"])
            self.assertIn("题号必须字符串", prompt_payload["instruction"])

    def test_ollama_unknown_stage_rejected_before_http(self):
        opener = mock.Mock()
        runner = OllamaKnowledgeClassificationRunner(opener=opener)
        with self.assertRaisesRegex(
            KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
        ):
            runner.run("unknown", "{}")
        opener.assert_not_called()

    def test_ollama_returns_legacy_content_but_strict_parser_rejects_it(self):
        legacy = json.dumps({
            "questions": [{
                "question_number": 1,
                "category_code": "01.01",
                "reason": "题干中提到求两个给定集合的交集，属于集合的基本运算。",
            }]
        }, ensure_ascii=False)
        envelope = json.dumps({"message": {"content": legacy}}, ensure_ascii=False).encode()
        runner = OllamaKnowledgeClassificationRunner(
            opener=lambda *args, **kwargs: FakeHTTPResponse(envelope)
        )
        content = runner.run("level2", _prompt("level2", [], []))
        self.assertEqual(legacy, content)
        with self.assertRaisesRegex(
            KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
        ):
            _parse_level2(content, {"1"}, {"01.01"})

    def test_low_confidence_is_pending(self):
        self._complete(FakeRunner(low=True))
        page = load_classification_page(self.db, 1)
        self.assertEqual(2, page.pending)

    def test_strict_parser_fails_closed_for_bad_outputs(self):
        bad = [
            "```json\n{}\n```",
            json.dumps({"questions": [{"source_question_no": "1", "level2_code": "01.01", "confidence": "high", "reason": "x"}]}),
            json.dumps({"questions": [
                {"source_question_no": "1", "level2_code": "99.99", "confidence": "high", "reason": "x"},
                {"source_question_no": "2", "level2_code": "01.01", "confidence": "high", "reason": "x"},
            ]}),
            "x" * 600_000,
        ]
        for raw in bad:
            with self.subTest(raw=raw[:20]):
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute("DELETE FROM import_knowledge_classification_runs")
                claim = claim_knowledge_classification(
                    self.db, self.private, 1, runner=FakeRunner(invalid=("level2", raw)))
                run_claimed_knowledge_classification(claim)
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    self.assertEqual("failed", connection.execute(
                        "SELECT status FROM import_knowledge_classification_runs").fetchone()[0])
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts").fetchone()[0])

    def test_parser_rejects_all_level2_and_level3_shape_and_code_failures(self):
        numbers = {"1", "2"}
        level2_valid = [
            {"source_question_no": number, "level2_code": "01.01",
             "confidence": "high", "reason": "依据"}
            for number in ("1", "2")
        ]
        level3_valid = [
            {"source_question_no": number, "primary_code": "01.01.01",
             "related_codes": ["01.01.02"], "confidence": "high", "reason": "依据"}
            for number in ("1", "2")
        ]
        level2_cases = []
        missing = [dict(row) for row in level2_valid]
        missing.pop()
        level2_cases.append({"questions": missing})
        duplicate = [dict(level2_valid[0]), dict(level2_valid[0])]
        level2_cases.append({"questions": duplicate})
        extra = [dict(row) for row in level2_valid]
        extra[0]["extra"] = True
        level2_cases.append({"questions": extra})
        invalid = [dict(row) for row in level2_valid]
        invalid[0]["level2_code"] = "99.99"
        level2_cases.append({"questions": invalid})
        for payload in level2_cases:
            with self.subTest(kind="level2", payload=payload), self.assertRaises(KnowledgeClassificationRunError):
                _parse_level2(json.dumps(payload), numbers, {"01.01"})

        level3_cases = []
        level3_cases.append({"questions": [dict(level3_valid[0])]})
        level3_cases.append({"questions": [dict(level3_valid[0]), dict(level3_valid[0])]})
        for mutation in ("extra", "primary", "duplicate_related", "same", "too_many"):
            rows = [dict(row) for row in level3_valid]
            rows[0]["related_codes"] = list(rows[0]["related_codes"])
            if mutation == "extra":
                rows[0]["unexpected"] = 1
            elif mutation == "primary":
                rows[0]["primary_code"] = "99.99.99"
            elif mutation == "duplicate_related":
                rows[0]["related_codes"] = ["01.01.02", "01.01.02"]
            elif mutation == "same":
                rows[0]["related_codes"] = ["01.01.01"]
            else:
                rows[0]["related_codes"] = ["01.01.02", "01.01.03", "01.01.04"]
            level3_cases.append({"questions": rows})
        allowed = {number: {"01.01.01", "01.01.02", "01.01.03", "01.01.04"} for number in numbers}
        raw_cases = [
            "```json\n{}\n```",
            json.dumps({"questions": level3_valid}) + " trailing",
            "x" * (512 * 1024 + 1),
            *(json.dumps(payload) for payload in level3_cases),
        ]
        for raw in raw_cases:
            with self.subTest(kind="level3", raw=raw[:40]), self.assertRaises(KnowledgeClassificationRunError):
                _parse_level3(raw, numbers, allowed)

    def test_concurrent_claim_and_fresh_processing_call_once(self):
        runner = FakeRunner()
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            return claim_knowledge_classification(self.db, self.private, 1, runner=runner)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(1, sum(item is not None for item in claims))
        self.assertIsNone(claim_knowledge_classification(self.db, self.private, 1, runner=runner))

    def test_stale_processing_is_recovered(self):
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE import_knowledge_classification_runs SET updated_at='2000-01-01T00:00:00+00:00'")
        recovered = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        self.assertIsNotNone(recovered)
        self.assertNotEqual(claim.claim_token, recovered.claim_token)

    def test_fresh_direct_heartbeat_blocks_reclaim_until_truly_stale(self):
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        _heartbeat(claim, "proposal")
        self.assertIsNone(claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner()
        ))
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE import_knowledge_classification_runs "
                "SET updated_at='2000-01-01T00:00:00+00:00'"
            )
        recovered = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        self.assertIsNotNone(recovered)
        self.assertNotEqual(claim.claim_token, recovered.claim_token)

    def test_lost_claim_stops_old_worker_before_next_model_stage(self):
        calls = []

        class StealingRunner(FakeRunner):
            def run(inner_self, stage, prompt):
                calls.append(stage)
                result = super().run(stage, prompt)
                if stage == "level2":
                    with closing(sqlite3.connect(self.db)) as connection, connection:
                        connection.execute(
                            "UPDATE import_knowledge_classification_runs "
                            "SET claim_token=?,updated_at=? WHERE import_job_id=1",
                            ("f" * 64, datetime.now(timezone.utc).isoformat()),
                        )
                return result

        claim = claim_knowledge_classification(self.db, self.private, 1, runner=StealingRunner())
        run_claimed_knowledge_classification(claim)
        self.assertEqual(["level2"], calls)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts").fetchone()[0])

    def test_global_lock_is_requested_nonblocking_for_heartbeat_polling(self):
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        flags = []
        blocked_once = False

        def recording_flock(fd, operation):
            nonlocal blocked_once
            flags.append(operation)
            if operation & fcntl.LOCK_NB and not blocked_once:
                blocked_once = True
                raise BlockingIOError(errno.EAGAIN, "synthetic contention")
            return None

        with (
            mock.patch(
                "src.reviewing.local_knowledge_classification.fcntl.flock",
                side_effect=recording_flock,
            ),
            mock.patch(
                "src.reviewing.local_knowledge_classification.threading.Event.wait",
                return_value=True,
            ),
            mock.patch(
                "src.reviewing.local_knowledge_classification._heartbeat",
                wraps=_heartbeat,
            ) as heartbeat,
        ):
            run_claimed_knowledge_classification(claim)
        self.assertTrue(flags[0] & fcntl.LOCK_NB)
        waiting_calls = [call for call in heartbeat.call_args_list if call.args[1] == "waiting"]
        self.assertGreaterEqual(len(waiting_calls), 2)

    def test_review_optimistic_lock_taxonomy_and_source(self):
        self._complete(FakeRunner(conflict=True))
        with self.assertRaises(KnowledgeClassificationRunError):
            review_classification_draft(self.db, 1, "2", version=99,
                                        primary_code="01.01.01", related_codes=[])
        saved = review_classification_draft(
            self.db, 1, "2", version=1, primary_code="01.01.03",
            related_codes=["01.01.02"], approve=False)
        self.assertEqual("pending", saved["status"])
        self.assertIsNone(saved["approval_source"])
        reviewed = review_classification_draft(
            self.db, 1, "2", version=2, primary_code="01.01.03",
            related_codes=["01.01.02"], approve=True)
        self.assertEqual("human", reviewed["approval_source"])
        untouched = review_classification_draft(
            self.db, 1, "1", version=1, primary_code="01.01.01",
            related_codes=["01.01.02"], approve=True)
        self.assertEqual("local_double_pass", untouched["approval_source"])

    def test_web_changed_auto_approval_requires_explicit_reapproval_before_apply(self):
        self._complete()
        client = TestClient(create_app(self.db, self.private))
        client.get("/imports/1/classification")
        csrf = client.cookies.get("basket_csrf")
        saved = client.post(
            "/imports/1/classification/questions/1",
            data={
                "csrf_token": csrf, "version": "1", "action": "save",
                "primary_code": "01.01.03", "related_codes": "01.01.02",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, saved.status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(("pending", None, None), connection.execute(
                "SELECT status,approval_source,reviewed_at FROM "
                "candidate_knowledge_classification_drafts WHERE source_question_no='1'"
            ).fetchone())
        blocked = client.post(
            "/imports/1/classification/apply", data={"csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(409, blocked.status_code)
        approved = client.post(
            "/imports/1/classification/questions/1",
            data={
                "csrf_token": csrf, "version": "2", "action": "approve",
                "primary_code": "01.01.03", "related_codes": "01.01.02",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, approved.status_code)
        applied = client.post(
            "/imports/1/classification/apply", data={"csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, applied.status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(("approved", "human"), connection.execute(
                "SELECT status,approval_source FROM "
                "candidate_knowledge_classification_drafts WHERE source_question_no='1'"
            ).fetchone())

    def test_completed_and_applied_classification_evidence_cannot_be_deleted(self):
        self._complete()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM import_knowledge_classification_runs WHERE import_job_id=1"
                )
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM import_knowledge_classification_runs"
            ).fetchone()[0])
        apply_classification_evidence(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            for statement in (
                "DELETE FROM candidate_knowledge_classification_drafts WHERE import_job_id=1",
                "UPDATE candidate_knowledge_classification_drafts SET human_review_note='x' WHERE import_job_id=1",
                "DELETE FROM candidate_knowledge_classifications WHERE import_job_id=1",
                "UPDATE candidate_knowledge_classifications SET reason='x' WHERE import_job_id=1",
                "DELETE FROM import_knowledge_classification_runs WHERE import_job_id=1",
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts"
            ).fetchone()[0])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications"
            ).fetchone()[0])

    def test_apply_is_atomic_idempotent_and_stale_visual_or_taxonomy_fails(self):
        self._complete()
        first = apply_classification_evidence(self.db, self.private, 1)
        second = apply_classification_evidence(self.db, self.private, 1)
        self.assertEqual((2, 2), (first.question_count, first.inserted))
        self.assertEqual(0, second.inserted)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications").fetchone()[0])

    def test_human_evidence_reason_and_reviewer_do_not_masquerade_as_model(self):
        self._complete(FakeRunner(conflict=True))
        reviewed = review_classification_draft(
            self.db, 1, "2", version=1, primary_code="01.01.03",
            related_codes=["01.01.02"], approve=True,
        )
        self.assertEqual("human", reviewed["approval_source"])
        self.assertIn("教师复核", reviewed["human_review_note"])
        self.assertLessEqual(len(reviewed["human_review_note"]), 200)
        apply_classification_evidence(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            rows = connection.execute(
                "SELECT source_question_no,reviewer,reason "
                "FROM candidate_knowledge_classifications ORDER BY source_question_no"
            ).fetchall()
        self.assertEqual("local_double_pass", rows[0][1])
        self.assertEqual("teacher_human_review", rows[1][1])
        self.assertIn("教师复核", rows[1][2])
        self.assertIn("原始建议", rows[1][2])
        self.assertNotIn("教师复核", rows[0][2])

    def test_visual_change_before_apply_fails_closed(self):
        self._complete()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE candidate_review_drafts SET version=version+1 WHERE source_question_no='1'")
        with self.assertRaises(KnowledgeClassificationRunError):
            apply_classification_evidence(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications").fetchone()[0])

    def test_taxonomy_change_before_apply_fails_closed(self):
        self._complete()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE knowledge_points SET is_active=0 WHERE code='01.01.03'")
        with self.assertRaises(KnowledgeClassificationRunError):
            apply_classification_evidence(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications").fetchone()[0])

    def test_file_publication_failure_has_no_trusted_partial_result(self):
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        with mock.patch(
            "src.reviewing.local_knowledge_classification._publish_output",
            side_effect=KnowledgeClassificationRunError("本地知识点分类结果保存失败，请重试"),
        ):
            run_claimed_knowledge_classification(claim)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_knowledge_classification_runs").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts").fetchone()[0])

    def test_sqlite_completion_commit_failure_restores_old_output_and_trusts_no_draft(self):
        old_output = self.job_dir / "knowledge_classification.json"
        old_output.write_bytes(b"trusted-old")
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())
        with mock.patch(
            "src.reviewing.local_knowledge_classification._commit_completed_run",
            side_effect=sqlite3.OperationalError("synthetic commit failure"),
        ):
            run_claimed_knowledge_classification(claim)
        self.assertEqual(b"trusted-old", old_output.read_bytes())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_knowledge_classification_runs"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts"
            ).fetchone()[0])

    def test_rollback_does_not_overwrite_concurrent_replacement(self):
        output = self.job_dir / "knowledge_classification.json"
        output.write_bytes(b"trusted-old")
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner())

        def replace_then_fail(connection):
            replacement = self.job_dir / "concurrent.tmp"
            replacement.write_bytes(b"third-party-new")
            os.replace(replacement, output)
            raise sqlite3.OperationalError("synthetic commit failure")

        with mock.patch(
            "src.reviewing.local_knowledge_classification._commit_completed_run",
            side_effect=replace_then_fail,
        ):
            run_claimed_knowledge_classification(claim)
        self.assertEqual(b"third-party-new", output.read_bytes())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertNotEqual("completed", connection.execute(
                "SELECT status FROM import_knowledge_classification_runs"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts"
            ).fetchone()[0])

    def test_processing_and_job_directory_symlinks_are_rejected_before_runner(self):
        for component in ("processing", "import_job_1"):
            with self.subTest(component=component):
                self.tearDown()
                self.setUp()
                outside = self.root / "outside"
                outside.mkdir()
                if component == "processing":
                    real = self.private / "processing"
                    real.rename(self.private / "processing-real")
                    shutil.copytree(
                        self.private / "processing-real" / "import_job_1",
                        outside / "import_job_1",
                    )
                    (self.private / "processing").symlink_to(outside, target_is_directory=True)
                else:
                    real = self.job_dir
                    real.rename(self.private / "processing" / "job-real")
                    shutil.copytree(
                        self.private / "processing" / "job-real", outside,
                        dirs_exist_ok=True,
                    )
                    self.job_dir.symlink_to(outside, target_is_directory=True)
                runner = FakeRunner()
                with self.assertRaisesRegex(
                    KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_INPUT}$"
                ):
                    claim_knowledge_classification(self.db, self.private, 1, runner=runner)
                self.assertEqual([], runner.calls)
                self.assertFalse((outside / "knowledge_classification.json").exists())

    def test_replacing_processing_ancestor_while_running_fails_closed(self):
        outside = self.root / "outside"
        shutil.copytree(self.job_dir, outside / "import_job_1")

        class ReplacingRunner(FakeRunner):
            def run(inner_self, stage, prompt):
                result = super().run(stage, prompt)
                if stage == "level2":
                    processing = self.private / "processing"
                    processing.rename(self.private / "processing-replaced")
                    processing.symlink_to(outside, target_is_directory=True)
                return result

        claim = claim_knowledge_classification(
            self.db, self.private, 1, runner=ReplacingRunner()
        )
        run_claimed_knowledge_classification(claim)
        self.assertFalse((outside / "knowledge_classification.json").exists())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertNotEqual("completed", connection.execute(
                "SELECT status FROM import_knowledge_classification_runs"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts"
            ).fetchone()[0])

    def test_non_object_candidate_and_audit_fail_safely_without_runner(self):
        originals = {
            filename: (self.job_dir / filename).read_bytes()
            for filename in ("candidate_questions.json", "ai_audit.json")
        }
        cases = tuple(
            (filename, value)
            for filename in ("candidate_questions.json", "ai_audit.json")
            for value in ([], "not-an-object", None)
        )
        for filename, value in cases:
            with self.subTest(filename=filename):
                target = self.job_dir / filename
                try:
                    target.write_text(json.dumps(value), encoding="utf-8")
                    runner = FakeRunner()
                    with self.assertRaisesRegex(
                        KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_INPUT}$"
                    ):
                        claim_knowledge_classification(
                            self.db, self.private, 1, runner=runner
                        )
                    self.assertEqual([], runner.calls)
                    with closing(sqlite3.connect(self.db)) as connection:
                        self.assertEqual(0, connection.execute(
                            "SELECT COUNT(*) FROM candidate_knowledge_classification_drafts"
                        ).fetchone()[0])
                finally:
                    target.write_bytes(originals[filename])

    def test_bounded_read_rejects_symlink_and_hardlink_but_reads_one_regular_fd(self):
        regular = self.root / "regular.json"
        regular.write_bytes(b'{"ok":true}')
        self.assertEqual(b'{"ok":true}', _read_bounded(regular, 100))
        symlink = self.root / "symlink.json"
        symlink.symlink_to(regular)
        hardlink = self.root / "hardlink.json"
        os.link(regular, hardlink)
        for path in (symlink, hardlink, regular):
            with self.subTest(path=path.name), self.assertRaises(KnowledgeClassificationRunError):
                _read_bounded(path, 100)

    def test_publish_rejects_symlink_job_dir_and_existing_hardlink_output(self):
        real_dir = self.root / "real-job"
        real_dir.mkdir()
        linked_dir = self.root / "linked-job"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        with self.assertRaises(KnowledgeClassificationRunError):
            _publish_output(linked_dir, b"new")
        self.assertFalse((real_dir / "knowledge_classification.json").exists())

        output = real_dir / "knowledge_classification.json"
        output.write_bytes(b"trusted-old")
        alias = self.root / "output-alias"
        os.link(output, alias)
        with self.assertRaises(KnowledgeClassificationRunError):
            _publish_output(real_dir, b"new")
        self.assertEqual(b"trusted-old", output.read_bytes())
        self.assertEqual(b"trusted-old", alias.read_bytes())

    def test_publish_replace_failure_preserves_existing_output(self):
        output = self.job_dir / "knowledge_classification.json"
        output.write_bytes(b"trusted-old")
        with mock.patch(
            "src.reviewing.local_knowledge_classification.os.replace",
            side_effect=OSError("synthetic replace failure"),
        ):
            with self.assertRaises(KnowledgeClassificationRunError):
                _publish_output(self.job_dir, b"new")
        self.assertEqual(b"trusted-old", output.read_bytes())

    def test_apply_sqlite_failure_rolls_back_entire_evidence_batch(self):
        self._complete()
        with mock.patch(
            "src.reviewing.local_knowledge_classification.adopt_knowledge_classifications_in_connection",
            side_effect=sqlite3.OperationalError("synthetic"),
        ):
            with self.assertRaises(KnowledgeClassificationRunError):
                apply_classification_evidence(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications").fetchone()[0])

    def test_existing_evidence_is_read_only_completed(self):
        self._complete()
        apply_classification_evidence(self.db, self.private, 1)
        self.assertIsNone(claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner()))
        page = load_classification_page(self.db, 1)
        self.assertTrue(page.applied)

    def test_ollama_runner_has_fixed_loopback_and_safe_network_error(self):
        runner = OllamaKnowledgeClassificationRunner(opener=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("secret")))
        with self.assertRaisesRegex(KnowledgeClassificationRunError, SAFE_CLASSIFICATION_MODEL):
            runner.run("level2", "prompt")
        self.assertEqual("http://127.0.0.1:11434/api/chat", runner.endpoint)

    def test_ollama_envelope_failures_are_bounded_and_use_fixed_safe_message(self):
        payloads = [
            b"{}",
            b'{"message":{}}',
            b'{"message":{"content":12}}',
            b"not-json",
            b"x" * (512 * 1024 + 1),
        ]
        for payload in payloads:
            runner = OllamaKnowledgeClassificationRunner(
                opener=lambda *args, value=payload, **kwargs: FakeHTTPResponse(value)
            )
            with self.subTest(payload=payload[:20]), self.assertRaisesRegex(
                KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
            ):
                runner.run("level2", "{}")

    def test_web_get_is_read_only_and_post_security_and_html_escape(self):
        runner = FakeRunner(conflict=True)
        weekly_calls = []
        app = create_app(
            self.db, self.private, classification_runner=runner,
            weekly_checker=lambda: weekly_calls.append(True),
        )
        client = TestClient(app)
        before = self.db.read_bytes()
        page = client.get("/imports/1/classification")
        self.assertEqual(200, page.status_code)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual([], runner.calls)
        self.assertEqual(405, client.get("/imports/1/classification/start").status_code)
        self.assertEqual(403, client.post("/imports/1/classification/start", data={}).status_code)
        token = client.cookies.get("basket_csrf")
        self.assertEqual(400, client.post("/imports/1/classification/start",
                                         data={"csrf_token": token, "extra": "x"}).status_code)
        response = client.post("/imports/1/classification/start",
                               data={"csrf_token": token}, follow_redirects=False)
        self.assertEqual(303, response.status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_knowledge_classification_drafts "
                "SET proposal_reason='<img src=x onerror=alert(1)>' "
                "WHERE source_question_no='1'"
            )
        completed = client.get("/imports/1/classification")
        self.assertIn("<strong>1</strong><span>待教师复核", completed.text)
        self.assertIn("<strong>1</strong><span>双重一致自动通过", completed.text)
        self.assertIn("&lt;b&gt;1&lt;/b&gt;", completed.text)
        self.assertNotIn("<b>1</b>", completed.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", completed.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", completed.text)
        self.assertEqual([], weekly_calls)

    def test_web_review_post_contract_validation_and_optimistic_lock(self):
        self._complete(FakeRunner(conflict=True))
        client, _, token = self._web_client()
        route = "/imports/1/classification/questions/2"
        self.assertEqual(405, client.get(route).status_code)
        self.assertEqual(403, client.post(route, data={}).status_code)
        valid = {
            "csrf_token": token, "version": "1", "action": "save",
            "primary_code": "01.01.01", "related_codes": "01.01.02",
        }
        invalid_cases = [
            ({**valid, "unknown": "x"}, 400),
            ({key: value for key, value in valid.items() if key != "action"}, 400),
            ({**valid, "action": "delete"}, 400),
            ({**valid, "primary_code": "x" * 51}, 400),
            ({**valid, "primary_code": "99.99.99"}, 400),
            ({**valid, "related_codes": "01.01.01"}, 400),
        ]
        for data, expected in invalid_cases:
            with self.subTest(data=data):
                self.assertEqual(expected, client.post(route, data=data).status_code)
        duplicate_body = (
            f"csrf_token={token}&version=1&action=save&primary_code=01.01.01"
            "&related_codes=01.01.02&related_codes=01.01.02"
        )
        self.assertEqual(400, client.post(
            route, content=duplicate_body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code)
        too_many_body = duplicate_body + "&related_codes=01.01.03"
        self.assertEqual(400, client.post(
            route, content=too_many_body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            before = connection.execute(
                "SELECT version FROM candidate_knowledge_classification_drafts "
                "WHERE source_question_no='2'"
            ).fetchone()[0]
        oversized = "x=" + "x" * 17_000
        self.assertIn(client.post(
            route, content=oversized,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code, {400, 413})
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(before, connection.execute(
                "SELECT version FROM candidate_knowledge_classification_drafts "
                "WHERE source_question_no='2'"
            ).fetchone()[0])
        approve = {**valid, "action": "approve"}
        self.assertEqual(303, client.post(route, data=approve, follow_redirects=False).status_code)
        self.assertEqual(409, client.post(route, data=approve).status_code)
        auto = {**valid, "version": "1", "action": "approve"}
        self.assertEqual(303, client.post(
            "/imports/1/classification/questions/1", data=auto, follow_redirects=False
        ).status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            sources = dict(connection.execute(
                "SELECT source_question_no,approval_source "
                "FROM candidate_knowledge_classification_drafts"
            ))
        self.assertEqual({"1": "local_double_pass", "2": "human"}, sources)

    def test_web_apply_contract_pending_atomic_success_idempotency_and_immutability(self):
        self._complete(FakeRunner(conflict=True))
        client, _, token = self._web_client()
        route = "/imports/1/classification/apply"
        self.assertEqual(405, client.get(route).status_code)
        self.assertEqual(403, client.post(route, data={}).status_code)
        self.assertEqual(400, client.post(
            route, data={"csrf_token": token, "unknown": "x"}
        ).status_code)
        self.assertEqual(409, client.post(route, data={"csrf_token": token}).status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications"
            ).fetchone()[0])
        review = {
            "csrf_token": token, "version": "1", "action": "approve",
            "primary_code": "01.01.01", "related_codes": "01.01.02",
        }
        self.assertEqual(303, client.post(
            "/imports/1/classification/questions/2", data=review,
            follow_redirects=False,
        ).status_code)
        self.assertEqual(303, client.post(
            route, data={"csrf_token": token}, follow_redirects=False
        ).status_code)
        self.assertEqual(303, client.post(
            route, data={"csrf_token": token}, follow_redirects=False
        ).status_code)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications"
            ).fetchone()[0])
        review["version"] = "2"
        self.assertEqual(409, client.post(
            "/imports/1/classification/questions/2", data=review
        ).status_code)

    def test_all_classification_get_states_are_read_only_and_applied_evidence_has_no_controls(self):
        runner = FakeRunner()
        client, _, token = self._web_client(runner)
        self.assertEqual([], runner.calls)
        claim = claim_knowledge_classification(self.db, self.private, 1, runner=runner)
        self.assertEqual(200, client.get("/imports/1/classification").status_code)
        self.assertEqual([], runner.calls)
        run_claimed_knowledge_classification(claim)
        completed = client.get("/imports/1/classification")
        self.assertEqual(200, completed.status_code)
        self.assertEqual(3, len(runner.calls))
        self.assertIn("复核就绪", completed.text)
        self.assertEqual(303, client.post(
            "/imports/1/classification/apply", data={"csrf_token": token},
            follow_redirects=False,
        ).status_code)
        existing = client.get("/imports/1/classification")
        self.assertNotIn("开始本地知识点分类", existing.text)
        self.assertNotIn("保存并批准", existing.text)
        self.assertNotIn("登记整批分类证据", existing.text)
        self.assertEqual(3, len(runner.calls))


if __name__ == "__main__":
    unittest.main()
