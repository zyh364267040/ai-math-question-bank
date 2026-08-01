import hashlib
import errno
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
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
from src.reviewing.knowledge_classification import (
    KnowledgeClassificationError,
    _parse as parse_classification_evidence,
    adopt_knowledge_classifications_in_connection,
)
from src.reviewing.local_knowledge_classification import (
    SAFE_CLASSIFICATION_INPUT,
    SAFE_CLASSIFICATION_MODEL,
    CodexKnowledgeClassificationRunner,
    KnowledgeClassificationRunError,
    STAGE_OUTPUT_SCHEMAS,
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
        confidence = (
            "low" if self.low and stage == "proposal"
            else "medium" if self.low and stage == "adjudicator"
            else "high"
        )
        if stage == "level2":
            rows = [
                {"source_question_no": number, "level2_code": "01.01",
                 "confidence": "high", "reason": "属于集合模块"}
                for number in ("1", "2")
            ]
        else:
            rows = []
            numbers = [
                item["source_question_no"]
                for item in json.loads(prompt)["questions"]
            ]
            for number in numbers:
                primary = "01.01.01"
                if self.conflict and stage == "verifier" and number == "2":
                    primary = "01.01.02"
                if self.conflict and stage == "adjudicator" and number == "2":
                    primary = "01.01.03"
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

    def _completed_snapshot(self):
        output = (self.job_dir / "knowledge_classification.json").read_bytes()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            run = dict(connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=1"
            ).fetchone())
            drafts = [dict(row) for row in connection.execute(
                "SELECT * FROM candidate_knowledge_classification_drafts "
                "WHERE import_job_id=1 ORDER BY source_question_no"
            )]
        return output, run, drafts

    def _assert_completed_snapshot(self, snapshot):
        output, run, drafts = snapshot
        self.assertEqual(
            output, (self.job_dir / "knowledge_classification.json").read_bytes()
        )
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            current = dict(connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=1"
            ).fetchone())
            current_drafts = [dict(row) for row in connection.execute(
                "SELECT * FROM candidate_knowledge_classification_drafts "
                "WHERE import_job_id=1 ORDER BY source_question_no"
            )]
        for anchor in (
            "status", "question_count", "processed_questions", "model",
            "input_digest", "taxonomy_digest", "output_sha256",
            "output_byte_size", "completed_at", "applied_at",
        ):
            self.assertEqual(run[anchor], current[anchor], anchor)
        self.assertEqual(drafts, current_drafts)

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
            run_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='import_knowledge_classification_runs'"
            ).fetchone()[0]
            draft_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='candidate_knowledge_classification_drafts'"
            ).fetchone()[0]
            evidence_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(candidate_knowledge_classifications)"
                )
            }
            self.assertIn("adjudicator", run_sql)
            self.assertIn("codex_adjudicated", draft_sql)
            self.assertIn("approval_source", evidence_columns)
            self.assertTrue({
                "replacement_active", "replacement_attempted_at",
                "replacement_completed_at", "replacement_result",
            }.issubset(columns))

    def test_legacy_classification_tables_migrate_without_losing_rows(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TABLE candidate_knowledge_classification_drafts")
            connection.execute("DROP TABLE import_knowledge_classification_runs")
            connection.execute("DROP TABLE candidate_knowledge_classifications")
            connection.executescript("""
                CREATE TABLE import_knowledge_classification_runs (
                    import_job_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    stage TEXT NOT NULL DEFAULT 'waiting' CHECK (
                        stage IN ('waiting','level2','proposal','verifier',
                                  'publishing','review_ready')
                    ),
                    question_count INTEGER,processed_questions INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL DEFAULT 'codex-cli',input_digest TEXT,
                    taxonomy_digest TEXT,output_sha256 TEXT,output_byte_size INTEGER,
                    error_message TEXT,claim_token TEXT,started_at TEXT,completed_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,applied_at TEXT
                );
                CREATE TABLE candidate_knowledge_classification_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,import_job_id INTEGER NOT NULL,
                    source_question_no TEXT NOT NULL,approved_draft_version INTEGER NOT NULL,
                    edited_sha256 TEXT NOT NULL,proposal_primary_code TEXT NOT NULL,
                    proposal_related_codes_json TEXT NOT NULL,proposal_confidence TEXT NOT NULL,
                    proposal_reason TEXT NOT NULL,verifier_primary_code TEXT NOT NULL,
                    verifier_related_codes_json TEXT NOT NULL,verifier_confidence TEXT NOT NULL,
                    verifier_reason TEXT NOT NULL,final_primary_code TEXT NOT NULL,
                    final_related_codes_json TEXT NOT NULL,status TEXT NOT NULL,
                    approval_source TEXT CHECK (
                        approval_source IN ('codex_double_pass','local_double_pass','human')
                        OR approval_source IS NULL
                    ),
                    human_review_note TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL DEFAULT 1,
                    reviewed_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(import_job_id,source_question_no)
                );
                CREATE TABLE candidate_knowledge_classifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,import_job_id INTEGER NOT NULL,
                    source_question_no TEXT NOT NULL,approved_draft_version INTEGER NOT NULL,
                    edited_sha256 TEXT NOT NULL,primary_knowledge_point_code TEXT NOT NULL,
                    related_knowledge_point_codes_json TEXT NOT NULL,classifier TEXT NOT NULL,
                    reviewer TEXT NOT NULL,classifier_run_id TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(import_job_id,source_question_no,approved_draft_version,edited_sha256),
                    UNIQUE(classifier_run_id,source_question_no)
                );
                INSERT INTO import_knowledge_classification_runs
                    (import_job_id,status,stage,updated_at)
                    VALUES(1,'pending','waiting','2026-07-18T00:00:00+00:00');
                INSERT INTO candidate_knowledge_classification_drafts
                    (import_job_id,source_question_no,approved_draft_version,edited_sha256,
                     proposal_primary_code,proposal_related_codes_json,proposal_confidence,
                     proposal_reason,verifier_primary_code,verifier_related_codes_json,
                     verifier_confidence,verifier_reason,final_primary_code,
                     final_related_codes_json,status,approval_source,reviewed_at,created_at,updated_at)
                    VALUES(1,'1',2,printf('%064d',0),'01.01.01','[]','high','旧初审',
                           '01.01.01','[]','high','旧复核','01.01.01','[]','approved',
                           'local_double_pass','2026-07-18T00:00:00+00:00',
                           '2026-07-18T00:00:00+00:00','2026-07-18T00:00:00+00:00');
                INSERT INTO candidate_knowledge_classifications
                    (import_job_id,source_question_no,approved_draft_version,edited_sha256,
                     primary_knowledge_point_code,related_knowledge_point_codes_json,
                     classifier,reviewer,classifier_run_id,evidence_sha256,reason,created_at)
                    VALUES(1,'1',2,printf('%064d',0),'01.01.01','[]','legacy',
                           'local_double_pass','legacy-run',printf('%064d',1),'旧证据',
                           '2026-07-18T00:00:00+00:00');
            """)

        initialize_database(self.db).close()
        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                ("pending", "waiting"),
                connection.execute(
                    "SELECT status,stage FROM import_knowledge_classification_runs"
                ).fetchone(),
            )
            self.assertEqual(
                ("local_double_pass", None),
                connection.execute(
                    "SELECT approval_source,adjudicator_primary_code "
                    "FROM candidate_knowledge_classification_drafts"
                ).fetchone(),
            )
            self.assertEqual(
                ("local_double_pass", None),
                connection.execute(
                    "SELECT reviewer,approval_source "
                    "FROM candidate_knowledge_classifications"
                ).fetchone(),
            )

    def test_classification_schema_migration_replaces_keyword_stale_tables_and_triggers(self):
        self._complete()
        trigger_names = (
            "candidate_knowledge_classifications_immutable",
            "candidate_knowledge_classifications_delete_immutable",
            "knowledge_classification_applied_run_immutable",
            "knowledge_classification_completed_run_delete_immutable",
            "knowledge_classification_completed_output_immutable",
            "knowledge_classification_applied_draft_immutable",
            "knowledge_classification_applied_draft_delete_immutable",
        )
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for name in trigger_names:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(
                "UPDATE import_knowledge_classification_runs SET status='pending', "
                "stage='waiting',processed_questions=0,completed_at=NULL"
            )
            connection.execute(
                "UPDATE candidate_knowledge_classification_drafts SET id=7 WHERE id=2"
            )
            for table in (
                "candidate_knowledge_classification_drafts",
                "import_knowledge_classification_runs",
            ):
                create_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                columns = [
                    row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                connection.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_old"')
                stale_sql = create_sql
                if table == "import_knowledge_classification_runs":
                    stale_sql = stale_sql.replace(
                        "'adjudicator',", "'adjudicator','bogus',", 1
                    ).replace(
                        "CHECK (model='codex-cli')",
                        "CHECK (model='codex-cli' OR model='qwen-stale')",
                        1,
                    )
                else:
                    stale_sql = stale_sql.replace(
                        "'local_double_pass','human'",
                        "'local_double_pass','human','bogus'",
                        1,
                    )
                connection.execute(stale_sql)
                quoted = ",".join(f'"{column}"' for column in columns)
                connection.execute(
                    f'INSERT INTO "{table}" ({quoted}) '
                    f'SELECT {quoted} FROM "{table}_old"'
                )
                connection.execute(f'DROP TABLE "{table}_old"')
            connection.execute(
                "UPDATE sqlite_sequence SET seq=999 "
                "WHERE name='candidate_knowledge_classification_drafts'"
            )
            for name in trigger_names:
                target = (
                    "candidate_knowledge_classifications"
                    if name.startswith("candidate_knowledge_classifications_")
                    else "candidate_knowledge_classification_drafts"
                    if "draft" in name
                    else "import_knowledge_classification_runs"
                )
                connection.execute(
                    f'CREATE TRIGGER "{name}" BEFORE UPDATE ON "{target}" '
                    "BEGIN SELECT 1; END"
                )
            connection.commit()

        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection:
            first_schema = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name LIKE '%knowledge_classification%' ORDER BY type,name"
            ).fetchall()
            self.assertEqual(999, connection.execute(
                "SELECT seq FROM sqlite_sequence "
                "WHERE name='candidate_knowledge_classification_drafts'"
            ).fetchone()[0])
            cursor = connection.execute(
                """INSERT INTO candidate_knowledge_classification_drafts
                   (import_job_id,source_question_no,approved_draft_version,edited_sha256,
                    proposal_primary_code,proposal_related_codes_json,proposal_confidence,
                    proposal_reason,verifier_primary_code,verifier_related_codes_json,
                    verifier_confidence,verifier_reason,adjudicator_primary_code,
                    adjudicator_related_codes_json,adjudicator_confidence,
                    adjudicator_reason,final_primary_code,final_related_codes_json,
                    final_reason,status,approval_source,human_review_note,version,
                    reviewed_at,created_at,updated_at)
                   SELECT import_job_id,'3',approved_draft_version,edited_sha256,
                    proposal_primary_code,proposal_related_codes_json,proposal_confidence,
                    proposal_reason,verifier_primary_code,verifier_related_codes_json,
                    verifier_confidence,verifier_reason,adjudicator_primary_code,
                    adjudicator_related_codes_json,adjudicator_confidence,
                    adjudicator_reason,final_primary_code,final_related_codes_json,
                    final_reason,status,approval_source,human_review_note,version,
                    reviewed_at,created_at,updated_at
                   FROM candidate_knowledge_classification_drafts WHERE id=1"""
            )
            self.assertGreater(cursor.lastrowid, 999)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE import_knowledge_classification_runs SET stage='bogus'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE import_knowledge_classification_runs SET model='qwen-stale'"
                )
            connection.execute(
                "UPDATE import_knowledge_classification_runs SET applied_at='2026-08-01'"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE import_knowledge_classification_runs SET error_message='changed'"
                )
        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(first_schema, connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name LIKE '%knowledge_classification%' ORDER BY type,name"
            ).fetchall())
            self.assertEqual(999, connection.execute(
                "SELECT seq FROM sqlite_sequence "
                "WHERE name='candidate_knowledge_classification_drafts'"
            ).fetchone()[0])

    def test_incomplete_visual_approvals_never_call_runner(self):
        runner = FakeRunner()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE candidate_review_drafts SET status='pending' WHERE source_question_no='2'")
        with self.assertRaisesRegex(KnowledgeClassificationRunError, SAFE_CLASSIFICATION_INPUT):
            claim_knowledge_classification(self.db, self.private, 1, runner=runner)
        self.assertEqual([], runner.calls)

    def test_double_pass_auto_approval_and_conflict_pending(self):
        runner = self._complete(FakeRunner(conflict=True))
        self.assertEqual(
            ["level2", "proposal", "verifier", "adjudicator"],
            [x[0] for x in runner.calls],
        )
        page = load_classification_page(self.db, 1)
        self.assertEqual((1, 1, 1), (page.auto_approved, page.pending, page.approved))
        self.assertEqual("2", page.drafts[0]["source_question_no"])
        self.assertEqual("codex_double_pass", page.drafts[1]["approval_source"])

    def test_related_order_is_normalized_for_double_pass_without_adjudicator(self):
        class ReorderedRelatedRunner(FakeRunner):
            def run(inner_self, stage, prompt):
                raw = super().run(stage, prompt)
                if stage in {"proposal", "verifier"}:
                    payload = json.loads(raw)
                    for row in payload["questions"]:
                        row["related_codes"] = (
                            ["01.01.02", "01.01.03"]
                            if stage == "proposal"
                            else ["01.01.03", "01.01.02"]
                        )
                    return json.dumps(payload, ensure_ascii=False)
                return raw

        runner = self._complete(ReorderedRelatedRunner())
        self.assertEqual(
            ["level2", "proposal", "verifier"],
            [stage for stage, _ in runner.calls],
        )
        page = load_classification_page(self.db, 1)
        self.assertEqual((2, 0), (page.auto_approved, page.pending))
        self.assertTrue(all(
            draft["final_related_codes"] == ["01.01.02", "01.01.03"]
            for draft in page.drafts
        ))

    def test_adjudicator_is_independent_scoped_fourth_pass_and_can_match_proposal(self):
        marker = "PROPOSAL-VERIFIER-PRIVATE-MARKER"

        class MatchingRunner(FakeRunner):
            def run(inner_self, stage, prompt):
                self.assertNotIn(marker, prompt)
                raw = super().run(stage, prompt)
                payload = json.loads(raw)
                if stage in {"proposal", "verifier"}:
                    payload["questions"][1]["reason"] = marker
                if stage == "adjudicator":
                    self.assertEqual(
                        ["2"],
                        [row["source_question_no"] for row in json.loads(prompt)["questions"]],
                    )
                    payload["questions"][0].update({
                        "primary_code": "01.01.01",
                        "related_codes": ["01.01.02"],
                        "confidence": "high",
                        "reason": "独立第三票依据",
                    })
                return json.dumps(payload, ensure_ascii=False)

        runner = self._complete(MatchingRunner(conflict=True))
        self.assertEqual(
            ["level2", "proposal", "verifier", "adjudicator"],
            [stage for stage, _ in runner.calls],
        )
        adjudicator_prompt = next(
            prompt for stage, prompt in runner.calls if stage == "adjudicator"
        )
        self.assertNotIn(marker, adjudicator_prompt)
        page = load_classification_page(self.db, 1)
        adjudicated = next(
            item for item in page.drafts if item["source_question_no"] == "2"
        )
        self.assertEqual("approved", adjudicated["status"])
        self.assertEqual("codex_adjudicated", adjudicated["approval_source"])
        self.assertEqual("独立第三票依据", adjudicated["adjudicator_reason"])
        self.assertEqual("01.01.01", adjudicated["final_primary_code"])

    def test_adjudicator_high_can_match_medium_initial_vote_but_level2_must_be_high(self):
        class ConfidenceRunner(FakeRunner):
            def __init__(inner_self, *, level2_medium=False):
                super().__init__(conflict=True)
                inner_self.level2_medium = level2_medium

            def run(inner_self, stage, prompt):
                raw = super().run(stage, prompt)
                payload = json.loads(raw)
                if stage == "level2" and inner_self.level2_medium:
                    payload["questions"][1]["confidence"] = "medium"
                if stage == "proposal":
                    payload["questions"][1]["confidence"] = "medium"
                if stage == "adjudicator":
                    payload["questions"][0].update({
                        "primary_code": "01.01.01",
                        "related_codes": ["01.01.02"],
                        "confidence": "high",
                    })
                return json.dumps(payload, ensure_ascii=False)

        self._complete(ConfidenceRunner())
        page = load_classification_page(self.db, 1)
        self.assertEqual(
            "codex_adjudicated",
            next(d for d in page.drafts if d["source_question_no"] == "2")[
                "approval_source"
            ],
        )

        self.tearDown()
        self.setUp()
        runner = self._complete(ConfidenceRunner(level2_medium=True))
        self.assertEqual("adjudicator", runner.calls[-1][0])
        question = next(
            d for d in load_classification_page(self.db, 1).drafts
            if d["source_question_no"] == "2"
        )
        self.assertEqual(("pending", None), (
            question["status"], question["approval_source"],
        ))

    def test_adjudicator_medium_or_third_result_remains_pending(self):
        for mode in ("medium", "third"):
            with self.subTest(mode=mode):
                self.tearDown()
                self.setUp()

                class NoConsensusRunner(FakeRunner):
                    def run(inner_self, stage, prompt):
                        raw = super().run(stage, prompt)
                        if stage == "adjudicator":
                            payload = json.loads(raw)
                            if mode == "medium":
                                payload["questions"][0]["confidence"] = "medium"
                                payload["questions"][0]["primary_code"] = "01.01.01"
                                payload["questions"][0]["related_codes"] = ["01.01.02"]
                            return json.dumps(payload, ensure_ascii=False)
                        return raw

                self._complete(NoConsensusRunner(conflict=True))
                question = next(
                    d for d in load_classification_page(self.db, 1).drafts
                    if d["source_question_no"] == "2"
                )
                self.assertEqual(("pending", None), (
                    question["status"], question["approval_source"],
                ))

    def test_bad_adjudicator_batch_fails_closed_without_partial_drafts(self):
        cases = {
            "missing": [],
            "extra": [
                {"source_question_no": "2", "primary_code": "01.01.01",
                 "related_codes": ["01.01.02"], "confidence": "high", "reason": "x"},
                {"source_question_no": "1", "primary_code": "01.01.01",
                 "related_codes": ["01.01.02"], "confidence": "high", "reason": "x"},
            ],
            "duplicate": [
                {"source_question_no": "2", "primary_code": "01.01.01",
                 "related_codes": ["01.01.02"], "confidence": "high", "reason": "x"},
                {"source_question_no": "2", "primary_code": "01.01.01",
                 "related_codes": ["01.01.02"], "confidence": "high", "reason": "x"},
            ],
            "illegal": [
                {"source_question_no": "2", "primary_code": "99.99.99",
                 "related_codes": [], "confidence": "high", "reason": "x"},
            ],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                runner = FakeRunner(
                    conflict=True,
                    invalid=("adjudicator", json.dumps({"questions": rows})),
                )
                self._complete(runner)
                page = load_classification_page(self.db, 1)
                self.assertEqual("failed", page.status)
                self.assertEqual((), page.drafts)

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

    def test_codex_four_stages_use_independent_ephemeral_commands_without_shell(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            output = Path(command[command.index("-o") + 1])
            output.write_text('{"questions":[]}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="secret")

        runner = CodexKnowledgeClassificationRunner(subprocess_run=run)
        for stage in ("level2", "proposal", "verifier", "adjudicator"):
            self.assertEqual('{"questions":[]}', runner.run(stage, '{"questions":[]}'))

        self.assertEqual(4, len(calls))
        workdirs = []
        for command, kwargs in calls:
            self.assertEqual(["codex", "exec"], command[:2])
            self.assertIn("--ephemeral", command)
            self.assertEqual("read-only", command[command.index("-s") + 1])
            self.assertEqual("-", command[-1])
            self.assertNotIn("shell", kwargs)
            self.assertNotIn("/status", command)
            self.assertNotIn("weekly", " ".join(command).lower())
            workdirs.append(command[command.index("-C") + 1])
        self.assertEqual(4, len(set(workdirs)))
        self.assertTrue(all(not Path(path).exists() for path in workdirs))
        prompts = [kwargs["input"] for _, kwargs in calls]
        self.assertEqual(4, len(set(prompts)))
        self.assertIn("不得假定", prompts[2])
        self.assertIn("替代", prompts[2])
        self.assertIn("不得接收", prompts[3])

    def test_codex_uses_exact_stage_specific_structured_output_schema_files(self):
        formats = []

        def run(command, **kwargs):
            schema_path = Path(command[command.index("--output-schema") + 1])
            formats.append(json.loads(schema_path.read_text(encoding="utf-8")))
            Path(command[command.index("-o") + 1]).write_text(
                '{"questions":[]}', encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner = CodexKnowledgeClassificationRunner(subprocess_run=run)
        for stage in ("level2", "proposal", "verifier", "adjudicator"):
            runner.run(stage, _prompt(stage, [], []))

        self.assertEqual(
            [
                STAGE_OUTPUT_SCHEMAS[stage]
                for stage in ("level2", "proposal", "verifier", "adjudicator")
            ],
            formats,
        )
        for schema in formats:
            self.assertIsInstance(schema, dict)
            self.assertEqual("object", schema["type"])
            self.assertEqual({"questions"}, set(schema["properties"]))
            self.assertEqual(["questions"], schema["required"])
            self.assertIs(schema["additionalProperties"], False)

        level2_item = formats[0]["properties"]["questions"]["items"]
        level3_items = [schema["properties"]["questions"]["items"] for schema in formats[1:]]
        self.assertEqual(
            {"source_question_no", "level2_code", "confidence", "reason"},
            set(level2_item["properties"]),
        )
        expected_level3 = {
            "source_question_no", "primary_code", "related_codes", "confidence", "reason",
        }
        self.assertEqual([expected_level3, expected_level3, expected_level3], [
            set(item["properties"]) for item in level3_items
        ])
        self.assertNotEqual(level2_item, level3_items[0])
        self.assertEqual(level3_items[0], level3_items[1])
        self.assertEqual(level3_items[1], level3_items[2])

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
            self.assertNotIn("uniqueItems", related)

    def test_codex_instructions_require_literal_fields_and_string_question_numbers(self):
        prompts = []

        def run(command, **kwargs):
            prompts.append(kwargs["input"])
            Path(command[command.index("-o") + 1]).write_text(
                '{"questions":[]}', encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner = CodexKnowledgeClassificationRunner(subprocess_run=run)
        for stage in ("level2", "proposal", "verifier", "adjudicator"):
            runner.run(stage, _prompt(stage, [], []))

        for prompt in prompts:
            self.assertIn("字段名必须逐字使用", prompt)
            self.assertIn("题号必须字符串", prompt)

    def test_codex_unknown_stage_rejected_before_subprocess(self):
        process = mock.Mock()
        runner = CodexKnowledgeClassificationRunner(subprocess_run=process)
        with self.assertRaisesRegex(
            KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
        ):
            runner.run("unknown", "{}")
        process.assert_not_called()

    def test_codex_runner_rejects_legacy_content_shape(self):
        legacy = json.dumps({
            "questions": [{
                "question_number": 1,
                "category_code": "01.01",
                "reason": "题干中提到求两个给定集合的交集，属于集合的基本运算。",
            }]
        }, ensure_ascii=False)
        def run(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text(legacy, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner = CodexKnowledgeClassificationRunner(subprocess_run=run)
        with self.assertRaisesRegex(
            KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
        ):
            runner.run("level2", _prompt("level2", [], []))

    def test_low_confidence_is_pending(self):
        self._complete(FakeRunner(low=True))
        page = load_classification_page(self.db, 1)
        self.assertEqual(2, page.pending)

    def test_verifier_input_never_contains_proposal_output(self):
        marker = "PROPOSAL-PRIVATE-OUTPUT-MUST-NOT-LEAK"

        class MarkerRunner(FakeRunner):
            def run(inner_self, stage, prompt):
                self.assertNotIn(marker, prompt)
                result = super().run(stage, prompt)
                if stage == "proposal":
                    payload = json.loads(result)
                    payload["questions"][0]["reason"] = marker
                    return json.dumps(payload, ensure_ascii=False)
                return result

        runner = self._complete(MarkerRunner())
        verifier_prompt = next(prompt for stage, prompt in runner.calls if stage == "verifier")
        self.assertNotIn(marker, verifier_prompt)

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

    def test_completed_default_still_none_and_valid_explicit_replacement_claim(self):
        self._complete()
        snapshot = self._completed_snapshot()
        self.assertIsNone(claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner()
        ))
        claim = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(),
            replace_unapplied=True,
        )
        self.assertIsNotNone(claim)
        self.assertTrue(claim.replace_unapplied)
        self.assertEqual(
            snapshot[0],
            (self.job_dir / "knowledge_classification.json").read_bytes(),
        )
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            active_run = dict(connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=1"
            ).fetchone())
            active_drafts = [dict(row) for row in connection.execute(
                "SELECT * FROM candidate_knowledge_classification_drafts "
                "WHERE import_job_id=1 ORDER BY source_question_no"
            )]
        for anchor in (
            "question_count", "model", "input_digest", "taxonomy_digest",
            "output_sha256", "output_byte_size", "completed_at", "applied_at",
        ):
            self.assertEqual(snapshot[1][anchor], active_run[anchor], anchor)
        self.assertEqual(snapshot[2], active_drafts)
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT status,replacement_active FROM "
                "import_knowledge_classification_runs WHERE import_job_id=1"
            ).fetchone()
        self.assertEqual(("processing", 1), row)
        self.assertIsNone(claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner()
        ))
        self.assertIsNone(claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(),
            replace_unapplied=True,
        ))

    def test_replacement_claim_rejects_every_untrusted_or_consumed_state(self):
        mutators = {
            "applied": lambda c: c.execute(
                "UPDATE import_knowledge_classification_runs SET applied_at='2026-01-01' "
                "WHERE import_job_id=1"
            ),
            "evidence": lambda c: c.execute(
                """INSERT INTO candidate_knowledge_classifications
                   (import_job_id,source_question_no,approved_draft_version,
                    edited_sha256,primary_knowledge_point_code,
                    related_knowledge_point_codes_json,classifier,reviewer,
                    approval_source,classifier_run_id,evidence_sha256,reason,
                    created_at)
                   SELECT import_job_id,source_question_no,
                          approved_draft_version,edited_sha256,
                          final_primary_code,final_related_codes_json,
                          'test','test','codex_double_pass','test-evidence',
                          ?,proposal_reason,'2026-01-01'
                   FROM candidate_knowledge_classification_drafts
                   WHERE import_job_id=1 AND source_question_no='1'""",
                ("e" * 64,),
            ),
            "formal": lambda c: (
                c.execute(
                    """INSERT INTO questions
                       (question_code,stem_markdown,region_code,exam_type_code,
                        question_type_code,primary_knowledge_point_id,content_hash,
                        answer_status)
                       SELECT 'FORMAL-1','题','TJ','GK','fill_blank',id,?,'missing'
                       FROM knowledge_points WHERE level=3 LIMIT 1""",
                    ("b" * 64,),
                ),
                c.execute(
                    "INSERT INTO question_sources VALUES(last_insert_rowid(),1,1,'1','[1]')"
                ),
            ),
            "import_completed": lambda c: c.execute(
                "UPDATE import_jobs SET status='completed' WHERE id=1"
            ),
            "input_anchor": lambda c: c.execute(
                "UPDATE candidate_review_drafts SET version=version+1 "
                "WHERE import_job_id=1 AND source_question_no='1'"
            ),
            "taxonomy_anchor": lambda c: c.execute(
                "UPDATE knowledge_points SET name=name || '漂移' WHERE code='01.01.01'"
            ),
            "draft_missing": lambda c: c.execute(
                "DELETE FROM candidate_knowledge_classification_drafts "
                "WHERE import_job_id=1 AND source_question_no='1'"
            ),
            "draft_binding": lambda c: c.execute(
                "UPDATE candidate_knowledge_classification_drafts "
                "SET approved_draft_version=approved_draft_version+1 "
                "WHERE import_job_id=1 AND source_question_no='1'"
            ),
        }
        for name, mutate in mutators.items():
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                self._complete()
                before = self._completed_snapshot()
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    mutate(connection)
                self.assertIsNone(claim_knowledge_classification(
                    self.db, self.private, 1, runner=FakeRunner(),
                    replace_unapplied=True,
                ))
                if name not in {
                    "applied", "evidence", "formal", "import_completed",
                    "input_anchor", "taxonomy_anchor", "draft_missing",
                    "draft_binding",
                }:
                    self._assert_completed_snapshot(before)

        for damage in ("missing", "bytes", "symlink"):
            with self.subTest(output=damage):
                self.tearDown()
                self.setUp()
                self._complete()
                output = self.job_dir / "knowledge_classification.json"
                if damage == "missing":
                    output.unlink()
                elif damage == "bytes":
                    output.write_bytes(b"corrupt")
                else:
                    output.unlink()
                    output.symlink_to(self.job_dir / "candidate_questions.json")
                self.assertIsNone(claim_knowledge_classification(
                    self.db, self.private, 1, runner=FakeRunner(),
                    replace_unapplied=True,
                ))

    def test_replacement_model_failures_restore_completed_generation_and_allow_retry(self):
        for stage in ("level2", "proposal", "verifier", "adjudicator"):
            with self.subTest(stage=stage):
                self.tearDown()
                self.setUp()
                self._complete(FakeRunner(conflict=True))
                snapshot = self._completed_snapshot()

                class FailingRunner(FakeRunner):
                    def run(inner_self, current_stage, prompt):
                        if current_stage == stage:
                            raise KnowledgeClassificationRunError(
                                SAFE_CLASSIFICATION_MODEL
                            )
                        return super().run(current_stage, prompt)

                claim = claim_knowledge_classification(
                    self.db, self.private, 1,
                    runner=FailingRunner(conflict=True),
                    replace_unapplied=True,
                )
                run_claimed_knowledge_classification(claim)
                self._assert_completed_snapshot(snapshot)
                retry = claim_knowledge_classification(
                    self.db, self.private, 1, runner=FakeRunner(),
                    replace_unapplied=True,
                )
                self.assertIsNotNone(retry)

    def test_replacement_publication_failure_windows_restore_old_generation(self):
        patches = (
            mock.patch(
                "src.reviewing.local_knowledge_classification._publish_output",
                side_effect=KnowledgeClassificationRunError(
                    "Codex 知识点分类结果保存失败，请重试"
                ),
            ),
            mock.patch(
                "src.reviewing.local_knowledge_classification._commit_completed_run",
                side_effect=sqlite3.OperationalError("synthetic commit failure"),
            ),
        )
        for index, failure in enumerate(patches):
            with self.subTest(window=index):
                self.tearDown()
                self.setUp()
                self._complete()
                snapshot = self._completed_snapshot()
                claim = claim_knowledge_classification(
                    self.db, self.private, 1,
                    runner=FakeRunner(conflict=True),
                    replace_unapplied=True,
                )
                with failure:
                    run_claimed_knowledge_classification(claim)
                self._assert_completed_snapshot(snapshot)

    def test_successful_replacement_atomically_replaces_output_drafts_and_anchors(self):
        self._complete()
        old_output, old_run, old_drafts = self._completed_snapshot()
        claim = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(conflict=True),
            replace_unapplied=True,
        )
        run_claimed_knowledge_classification(claim)
        new_output, new_run, new_drafts = self._completed_snapshot()
        self.assertNotEqual(old_output, new_output)
        self.assertNotEqual(old_drafts, new_drafts)
        self.assertNotEqual(old_run["output_sha256"], new_run["output_sha256"])
        self.assertEqual("completed", new_run["status"])
        self.assertEqual(0, new_run["replacement_active"])
        self.assertIsNotNone(new_run["replacement_completed_at"])
        self.assertFalse(list(self.job_dir.glob(".classification-*.bak")))

    def test_post_commit_backup_cleanup_failure_never_restores_old_generation(self):
        self._complete()
        old_output = self._completed_snapshot()[0]
        claim = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(conflict=True),
            replace_unapplied=True,
        )
        with mock.patch(
            "src.reviewing.local_knowledge_classification._PublishedOutput.finalize",
            side_effect=OSError("synthetic cleanup failure"),
        ):
            run_claimed_knowledge_classification(claim)
        new_output, run, drafts = self._completed_snapshot()
        self.assertNotEqual(old_output, new_output)
        self.assertEqual("completed", run["status"])
        self.assertEqual(
            hashlib.sha256(new_output).hexdigest(), run["output_sha256"]
        )
        self.assertEqual(2, len(drafts))

    def test_only_one_concurrent_explicit_replacement_claim_wins(self):
        self._complete()
        barrier = threading.Barrier(2)

        def replace_claim():
            barrier.wait()
            return claim_knowledge_classification(
                self.db, self.private, 1, runner=FakeRunner(),
                replace_unapplied=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: replace_claim(), range(2)))
        self.assertEqual(1, sum(claim is not None for claim in claims))

    def test_stale_replacement_lease_recovers_durable_completed_generation_and_reclaims(self):
        self._complete()
        old_output, old_run, old_drafts = self._completed_snapshot()
        abandoned = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(conflict=True),
            replace_unapplied=True,
        )
        self.assertIsNotNone(abandoned)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE import_knowledge_classification_runs "
                "SET updated_at='2000-01-01T00:00:00+00:00'"
            )
        recovered = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(),
            replace_unapplied=True,
        )
        self.assertIsNotNone(recovered)
        self.assertNotEqual(abandoned.claim_token, recovered.claim_token)
        self.assertEqual(old_output, (self.job_dir / "knowledge_classification.json").read_bytes())
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            current = dict(connection.execute(
                "SELECT * FROM import_knowledge_classification_runs"
            ).fetchone())
            drafts = [dict(row) for row in connection.execute(
                "SELECT * FROM candidate_knowledge_classification_drafts "
                "ORDER BY source_question_no"
            )]
            self.assertEqual("processing", current["status"])
            self.assertEqual(1, current["replacement_active"])
            for anchor in (
                "question_count", "model", "input_digest", "taxonomy_digest",
                "output_sha256", "output_byte_size", "completed_at",
            ):
                self.assertEqual(old_run[anchor], current[anchor])
            self.assertEqual(old_drafts, drafts)

    def test_stale_replacement_recovers_old_digest_after_half_published_artifact(self):
        self._complete()
        old_output = (self.job_dir / "knowledge_classification.json").read_bytes()
        abandoned = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(),
            replace_unapplied=True,
        )
        output = self.job_dir / "knowledge_classification.json"
        backup = self.job_dir / abandoned.replacement_backup_name
        os.replace(output, backup)
        output.write_bytes(b"half-published-untrusted-generation")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE import_knowledge_classification_runs "
                "SET updated_at='2000-01-01T00:00:00+00:00'"
            )
        recovered = claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(),
            replace_unapplied=True,
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(old_output, output.read_bytes())
        self.assertFalse(backup.exists())

    def test_only_one_worker_recovers_and_reclaims_stale_replacement(self):
        self._complete()
        claim_knowledge_classification(
            self.db, self.private, 1, runner=FakeRunner(),
            replace_unapplied=True,
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE import_knowledge_classification_runs "
                "SET updated_at='2000-01-01T00:00:00+00:00'"
            )
        barrier = threading.Barrier(2)

        def recover():
            barrier.wait()
            return claim_knowledge_classification(
                self.db, self.private, 1, runner=FakeRunner(),
                replace_unapplied=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: recover(), range(2)))
        self.assertEqual(1, sum(claim is not None for claim in claims))

    def test_classification_migration_failure_rolls_back_schema_rows_and_sequences(self):
        self._complete()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "DROP TRIGGER knowledge_classification_applied_run_immutable"
            )
            connection.execute(
                "CREATE TRIGGER knowledge_classification_applied_run_immutable "
                "BEFORE UPDATE ON import_knowledge_classification_runs "
                "BEGIN SELECT 1; END"
            )

        def snapshot():
            with closing(sqlite3.connect(self.db)) as connection:
                tables = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )]
                return (
                    connection.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                    ).fetchall(),
                    {
                        table: connection.execute(
                            f'SELECT * FROM "{table}" ORDER BY rowid'
                        ).fetchall()
                        for table in tables
                    },
                    connection.execute(
                        "SELECT name,seq FROM sqlite_sequence ORDER BY name"
                    ).fetchall(),
                )

        before = snapshot()
        from src.database import initialize as initialize_module
        original = initialize_module._refresh_knowledge_classification_schema

        def fail_after_refresh(connection, schema):
            original(connection, schema)
            raise sqlite3.OperationalError("synthetic migration interruption")

        with mock.patch.object(
            initialize_module,
            "_refresh_knowledge_classification_schema",
            side_effect=fail_after_refresh,
        ), self.assertRaises(sqlite3.OperationalError):
            initialize_database(self.db)
        self.assertEqual(before, snapshot())

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
        self.assertEqual("codex_double_pass", untouched["approval_source"])

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
        self.assertEqual("codex_double_pass", rows[0][1])
        self.assertEqual("teacher_human_review", rows[1][1])
        self.assertIn("教师复核", rows[1][2])
        self.assertIn("原始建议", rows[1][2])
        self.assertNotIn("教师复核", rows[0][2])

    def test_apply_rejects_codex_labeled_draft_drift_from_hashed_output_atomically(self):
        self._complete()
        artifact = json.loads(
            (self.job_dir / "knowledge_classification.json").read_text(encoding="utf-8")
        )
        self.assertEqual("01.01.01", artifact["questions"][0]["final_primary_code"])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_knowledge_classification_drafts "
                "SET final_primary_code='01.01.03',final_related_codes_json='[]',"
                "final_reason='篡改后仍伪装自动审核',approval_source='codex_double_pass' "
                "WHERE source_question_no='1'"
            )
        with self.assertRaises(KnowledgeClassificationRunError):
            apply_classification_evidence(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications"
            ).fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT applied_at FROM import_knowledge_classification_runs"
            ).fetchone()[0])

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
            side_effect=KnowledgeClassificationRunError(
                "Codex 知识点分类结果保存失败，请重试"
            ),
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

    def test_new_applied_evidence_is_explicitly_codex_double_pass(self):
        self._complete()
        with mock.patch(
            "src.reviewing.local_knowledge_classification."
            "adopt_knowledge_classifications_in_connection",
            wraps=adopt_knowledge_classifications_in_connection,
        ) as adopter:
            apply_classification_evidence(self.db, self.private, 1)
        payload = json.loads(adopter.call_args.args[2])
        self.assertEqual("codex_double_pass", payload["source_classifier"])
        self.assertEqual("codex_double_pass", payload["reviewer"])
        for item in payload["questions"]:
            self.assertEqual("codex_double_pass", item["reviewer"])
            self.assertEqual("codex_double_pass", item["approval_source"])
        with closing(sqlite3.connect(self.db)) as connection:
            rows = connection.execute(
                "SELECT classifier,reviewer FROM candidate_knowledge_classifications "
                "WHERE import_job_id=1"
            ).fetchall()
        self.assertEqual(
            [("codex_double_pass", "codex_double_pass")] * 2,
            rows,
        )

    def test_adjudicated_apply_preserves_per_question_provenance_in_evidence_and_db(self):
        class MatchingRunner(FakeRunner):
            def run(inner_self, stage, prompt):
                raw = super().run(stage, prompt)
                if stage == "adjudicator":
                    payload = json.loads(raw)
                    payload["questions"][0].update({
                        "primary_code": "01.01.01",
                        "related_codes": ["01.01.02"],
                        "confidence": "high",
                        "reason": "第三票独立依据",
                    })
                    return json.dumps(payload, ensure_ascii=False)
                return raw

        self._complete(MatchingRunner(conflict=True))
        output = json.loads(
            (self.job_dir / "knowledge_classification.json").read_text(encoding="utf-8")
        )
        by_number = {
            item["source_question_no"]: item for item in output["questions"]
        }
        self.assertEqual(
            ("codex_double_pass", "codex_double_pass"),
            (by_number["1"]["approval_source"], by_number["1"]["reviewer"]),
        )
        self.assertEqual(
            ("codex_adjudicated", "codex_adjudicator"),
            (by_number["2"]["approval_source"], by_number["2"]["reviewer"]),
        )

        with mock.patch(
            "src.reviewing.local_knowledge_classification."
            "adopt_knowledge_classifications_in_connection",
            wraps=adopt_knowledge_classifications_in_connection,
        ) as adopter:
            apply_classification_evidence(self.db, self.private, 1)
        payload = json.loads(adopter.call_args.args[2])
        evidence = {
            item["source_question_no"]: item for item in payload["questions"]
        }
        self.assertEqual(
            ("codex_adjudicated", "codex_adjudicator", "第三票独立依据"),
            (
                evidence["2"]["approval_source"],
                evidence["2"]["reviewer"],
                evidence["2"]["reason"],
            ),
        )
        with closing(sqlite3.connect(self.db)) as connection:
            rows = dict(connection.execute(
                "SELECT source_question_no,reviewer || ':' || approval_source "
                "FROM candidate_knowledge_classifications WHERE import_job_id=1"
            ))
        self.assertEqual({
            "1": "codex_double_pass:codex_double_pass",
            "2": "codex_adjudicator:codex_adjudicated",
        }, rows)

    def test_strict_evidence_parser_accepts_adjudicated_only_with_fixed_reviewer(self):
        payload = {
            "version": 1,
            "import_job_id": 1,
            "source_classifier": "codex-multi-pass",
            "reviewer": "mixed_codex_review",
            "scope": "knowledge_only_no_solution",
            "question_count": 1,
            "questions": [{
                "source_question_no": "1",
                "primary_code": "01.01.01",
                "related_codes": [],
                "reason": "第三票独立依据",
                "reviewer": "codex_adjudicator",
                "approval_source": "codex_adjudicated",
            }],
        }
        self.assertEqual(
            payload,
            parse_classification_evidence(json.dumps(payload, ensure_ascii=False), 1),
        )
        payload["questions"][0]["reviewer"] = "codex_double_pass"
        with self.assertRaises(KnowledgeClassificationError):
            parse_classification_evidence(json.dumps(payload, ensure_ascii=False), 1)

    def test_legacy_local_double_pass_evidence_remains_readable(self):
        payload = {
            "version": 1,
            "import_job_id": 1,
            "source_classifier": "legacy-local-classifier",
            "reviewer": "legacy-local-review",
            "scope": "knowledge_only_no_solution",
            "question_count": 1,
            "questions": [{
                "source_question_no": "1",
                "primary_code": "01.01.01",
                "related_codes": [],
                "reason": "历史证据",
                "reviewer": "local_double_pass",
                "approval_source": "local_double_pass",
            }],
        }
        self.assertEqual(
            payload,
            parse_classification_evidence(
                json.dumps(payload, ensure_ascii=False), 1
            ),
        )
        payload["questions"][0]["reviewer"] = "teacher_human_review"
        with self.assertRaises(KnowledgeClassificationError):
            parse_classification_evidence(
                json.dumps(payload, ensure_ascii=False), 1
            )

    def test_existing_evidence_is_read_only_completed(self):
        self._complete()
        apply_classification_evidence(self.db, self.private, 1)
        self.assertIsNone(claim_knowledge_classification(self.db, self.private, 1, runner=FakeRunner()))
        page = load_classification_page(self.db, 1)
        self.assertTrue(page.applied)

    def test_default_claim_uses_codex_runner(self):
        claim = claim_knowledge_classification(self.db, self.private, 1)
        self.assertIsInstance(claim.runner, CodexKnowledgeClassificationRunner)

    def test_runtime_and_current_docs_have_no_retired_backend(self):
        project = Path(__file__).resolve().parents[1]
        targets = [
            project / "src/reviewing/local_knowledge_classification.py",
            project / "README.md",
            *sorted((project / "docs").rglob("*.md")),
        ]
        retired_model = "qwen2" + ".5:14b"
        retired_service = "Olla" + "ma"
        for path in targets:
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotIn(retired_model, content)
                self.assertNotIn(retired_service, content)
        runtime = targets[0].read_text(encoding="utf-8")
        self.assertNotIn("urllib.request", runtime)
        self.assertNotIn("urllib.error", runtime)

    def test_codex_process_failures_are_bounded_and_safe(self):
        failures = [
            FileNotFoundError("private executable path"),
            subprocess.TimeoutExpired(["codex"], 120, stderr="private timeout"),
            subprocess.CalledProcessError(9, ["codex"], stderr="private stderr"),
        ]
        for failure in failures:
            runner = CodexKnowledgeClassificationRunner(
                subprocess_run=mock.Mock(side_effect=failure)
            )
            with self.subTest(failure=type(failure).__name__), self.assertRaisesRegex(
                KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
            ) as caught:
                runner.run("level2", "{}")
            self.assertNotIn("private", str(caught.exception))

    def test_codex_output_failures_are_bounded_schema_checked_and_safe(self):
        outputs = [None, "", "not-json", "x" * (512 * 1024 + 1), '{"questions":{}}']
        for value in outputs:
            def run(command, **kwargs):
                if value is not None:
                    Path(command[command.index("-o") + 1]).write_text(
                        value, encoding="utf-8"
                    )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="private")

            runner = CodexKnowledgeClassificationRunner(subprocess_run=run)
            with self.subTest(value=str(value)[:20]), self.assertRaisesRegex(
                KnowledgeClassificationRunError, f"^{SAFE_CLASSIFICATION_MODEL}$"
            ):
                runner.run("level2", "{}")

    def test_web_get_is_read_only_and_post_security_and_html_escape(self):
        runner = FakeRunner(conflict=True)
        app = create_app(
            self.db, self.private, classification_runner=runner,
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
        self.assertIn("<strong>1</strong><span>剩余人工复核", completed.text)
        self.assertIn("<strong>1</strong><span>Codex双重一致", completed.text)
        self.assertIn("<strong>0</strong><span>Codex第三票仲裁", completed.text)
        self.assertIn("&lt;b&gt;1&lt;/b&gt;", completed.text)
        self.assertNotIn("<b>1</b>", completed.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", completed.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", completed.text)

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
        self.assertEqual({"1": "codex_double_pass", "2": "human"}, sources)

    def test_web_explicit_replacement_button_csrf_and_strict_parameters(self):
        self._complete()
        runner = FakeRunner(conflict=True)
        client, _, token = self._web_client(runner)
        page = client.get("/imports/1/classification")
        self.assertIn("重新运行未应用分类", page.text)
        route = "/imports/1/classification/replace"
        self.assertEqual(405, client.get(route).status_code)
        self.assertEqual(403, client.post(route, data={}).status_code)
        self.assertEqual(400, client.post(
            route, data={"csrf_token": token, "replace_unapplied": "false"}
        ).status_code)
        self.assertEqual(400, client.post(
            route, data={
                "csrf_token": token,
                "replace_unapplied": "true",
                "extra": "x",
            },
        ).status_code)
        response = client.post(
            route,
            data={"csrf_token": token, "replace_unapplied": "true"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        self.assertTrue(runner.calls)

    def test_web_replacement_control_hidden_after_evidence_or_formal_questions(self):
        for state in ("evidence", "formal"):
            with self.subTest(state=state):
                self.tearDown()
                self.setUp()
                self._complete()
                if state == "evidence":
                    apply_classification_evidence(self.db, self.private, 1)
                else:
                    with closing(sqlite3.connect(self.db)) as connection, connection:
                        connection.execute(
                            """INSERT INTO questions
                               (question_code,stem_markdown,region_code,exam_type_code,
                                question_type_code,primary_knowledge_point_id,
                                content_hash,answer_status)
                               SELECT 'FORMAL-WEB','题','TJ','GK','fill_blank',id,?,'missing'
                               FROM knowledge_points WHERE level=3 LIMIT 1""",
                            ("c" * 64,),
                        )
                        connection.execute(
                            "INSERT INTO question_sources "
                            "VALUES(last_insert_rowid(),1,1,'1','[1]')"
                        )
                client, _, _ = self._web_client()
                self.assertNotIn(
                    "重新运行未应用分类",
                    client.get("/imports/1/classification").text,
                )

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
        self.assertNotIn("开始 Codex 知识点分类", existing.text)
        self.assertNotIn("保存并批准", existing.text)
        self.assertNotIn("登记整批分类证据", existing.text)
        self.assertEqual(3, len(runner.calls))


if __name__ == "__main__":
    unittest.main()
