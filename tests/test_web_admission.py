import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import src.importing.web_admission as web_admission_module
from src.database.initialize import initialize_database
from src.database.initialize import SCHEMA_PATH
from src.importing.web_admission import (
    SAFE_APPLY_FAILED,
    SAFE_BACKUP_STALE,
    SAFE_BUSY,
    SAFE_COMPLETED_DRIFT,
    SAFE_FINALIZE_FAILED,
    WebAdmissionError,
    apply_web_admission,
    load_admission_page,
)
from src.importing.admit_questions import backup_database
from src.importing.admit_questions import admit_questions
from src.reviewing.knowledge_classification import adopt_knowledge_classifications
from src.reviewing.finalize import finalize_review
from src.web.app import create_app
from src.web.app import MAX_ADMISSION_FORM_BYTES
from tests.fixture_factory import (
    anchor_synthetic_candidate_audit,
    create_import_job_fixture,
)


class WebAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "fixture-private"
        self.job_dir = create_import_job_fixture(self.private)
        self.db = self.root / "question-bank.db"
        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            source = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES(?,1,'synthetic.pdf','raw_papers/TJ/2026/synthetic.pdf',
                          'TJ',2026,'YK','Web 严格入库合成卷')""",
                ("a" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) VALUES(1,?,'needs_review')",
                (source,),
            )
        anchor_synthetic_candidate_audit(self.db, self.job_dir)

    def tearDown(self):
        self.temp.cleanup()

    def _approve_and_classify_all(self, *, local_run=False):
        raw = (self.job_dir / "candidate_questions.json").read_bytes()
        payload = json.loads(raw)
        candidate_sha = hashlib.sha256(raw).hexdigest()
        reviewed_at = "2026-07-18T10:00:00+08:00"
        approval = json.dumps(
            {"method": "workbench", "reviewed_at": reviewed_at},
            separators=(",", ":"),
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            for question in payload["questions"]:
                encoded = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at,
                        approval_source,approval_evidence_json)
                       VALUES(1,?,?,?,?, 'approved',1,?,'human',?)""",
                    (question["source_question_no"], candidate_sha, encoded, encoded,
                     reviewed_at, approval),
                )
            if local_run:
                connection.execute(
                """INSERT INTO import_knowledge_classification_runs
                   (import_job_id,status,stage,question_count,processed_questions,
                    input_digest,taxonomy_digest,output_sha256,output_byte_size,
                    completed_at,updated_at,applied_at)
                   VALUES(1,'completed','review_ready',23,23,?,?,?,1,?,?,?)""",
                ("c" * 64, "d" * 64, "e" * 64, reviewed_at, reviewed_at, reviewed_at),
            )
        evidence = {
            "version": 1,
            "import_job_id": 1,
            "source_classifier": "strict-external-fixture",
            "reviewer": "teacher",
            "scope": "knowledge_only_no_solution",
            "question_count": len(payload["questions"]),
            "questions": [
                {
                    "source_question_no": question["source_question_no"],
                    "primary_code": question["primary_knowledge_point_code"],
                    "related_codes": question["related_knowledge_point_codes"],
                    "reason": "严格外部分证据",
                }
                for question in payload["questions"]
            ],
        }
        adopt_knowledge_classifications(
            self.db, 1, json.dumps(evidence, ensure_ascii=False),
            "strict-external-classification-run",
        )

    def test_get_blocked_is_byte_for_byte_read_only_and_has_no_apply_button(self):
        app = create_app(self.db, self.private)
        before_db = self.db.read_bytes()
        before_tree = sorted(
            (path.relative_to(self.root).as_posix(), path.stat().st_size)
            for path in self.root.rglob("*") if path.is_file()
        )
        response = TestClient(app).get("/imports/1/admission")
        after_tree = sorted(
            (path.relative_to(self.root).as_posix(), path.stat().st_size)
            for path in self.root.rglob("*") if path.is_file()
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("不满足整批严格门禁", response.text)
        self.assertNotIn("正式入库并完成任务</button>", response.text)
        self.assertEqual(before_db, self.db.read_bytes())
        self.assertEqual(before_tree, after_tree)

    def test_get_missing_job_is_404_and_post_requires_exact_csrf_form(self):
        client = TestClient(create_app(self.db, self.private))
        self.assertEqual(404, client.get("/imports/999/admission").status_code)
        self.assertEqual(405, client.get("/imports/1/admission/apply").status_code)
        self.assertEqual(403, client.post("/imports/1/admission/apply", data={}).status_code)
        client.get("/imports/1/admission")
        csrf = client.cookies.get("basket_csrf")
        self.assertEqual(400, client.post(
            "/imports/1/admission/apply",
            data={"csrf_token": csrf, "count": "23"},
        ).status_code)
        duplicate = f"csrf_token={csrf}&csrf_token={csrf}".encode()
        self.assertEqual(400, client.post(
            "/imports/1/admission/apply", content=duplicate,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code)

    def test_happy_path_and_completed_repeat_are_idempotent(self):
        self._approve_and_classify_all()
        page = load_admission_page(self.db, self.private, 1)
        self.assertTrue(page.can_apply)
        self.assertEqual((23, 0), (page.eligible_count, page.ineligible_count))
        apply_web_admission(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            run = connection.execute(
                "SELECT * FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()
            counts = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] for table in (
                "questions", "question_versions", "question_reviews",
            ))
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM import_jobs WHERE id=1"
            ).fetchone()[0])
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("completed", run[1])
        backup = self.db.parent / run[5]
        self.assertEqual(run[6], hashlib.sha256(backup.read_bytes()).hexdigest())
        backup_files = sorted((self.db.parent / "backups").iterdir())
        apply_web_admission(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            repeated = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] for table in (
                "questions", "question_versions", "question_reviews",
            ))
        self.assertEqual(counts, repeated)
        self.assertEqual(backup_files, sorted((self.db.parent / "backups").iterdir()))

    def test_strict_external_adoption_without_local_run_is_ready_and_can_complete(self):
        self._approve_and_classify_all()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_knowledge_classification_runs"
            ).fetchone()[0])
        self.assertTrue(load_admission_page(self.db, self.private, 1).can_apply)
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))

    def test_stale_or_nonexact_external_classification_evidence_is_blocked(self):
        self._approve_and_classify_all()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE candidate_review_drafts SET version=version+1 "
                "WHERE source_question_no='23'"
            )
        self.assertFalse(load_admission_page(self.db, self.private, 1).can_apply)

    def test_finalize_failure_is_visible_and_retry_does_not_duplicate_questions(self):
        self._approve_and_classify_all()

        def fail_finalize(*args, **kwargs):
            raise ValueError("private injected detail")

        with self.assertRaises(WebAdmissionError):
            apply_web_admission(
                self.db, self.private, 1, finalize_fn=fail_finalize
            )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(23, connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0])
            stage, error = connection.execute(
                "SELECT stage,safe_error FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()
        self.assertEqual("admitted_pending_finalize", stage)
        self.assertEqual(SAFE_FINALIZE_FAILED, error)
        response = TestClient(create_app(self.db, self.private)).get(
            "/imports/1/admission"
        )
        self.assertIn(SAFE_FINALIZE_FAILED, response.text)
        self.assertIn("正式入库并完成任务</button>", response.text)
        self.assertNotIn("private injected detail", response.text)
        self.assertNotIn(str(self.root), response.text)
        backups = sorted((self.db.parent / "backups").iterdir())
        apply_web_admission(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(23, connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0])
        self.assertEqual(backups, sorted((self.db.parent / "backups").iterdir()))

    def test_finalize_retry_rejects_backup_after_unrelated_business_drift(self):
        self._approve_and_classify_all()

        def fail_finalize(*args, **kwargs):
            raise ValueError("injected finalize failure")

        with self.assertRaisesRegex(WebAdmissionError, SAFE_FINALIZE_FAILED):
            apply_web_admission(
                self.db, self.private, 1, finalize_fn=fail_finalize,
            )
        backups = sorted((self.db.parent / "backups").iterdir())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO tag_definitions(category,code,name)
                   VALUES('scenario','after-finalize-backup','收口备份后的合法变化')"""
            )

        with self.assertRaisesRegex(
            WebAdmissionError, f"^{SAFE_BACKUP_STALE}$"
        ) as raised:
            apply_web_admission(self.db, self.private, 1)

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(backups, sorted((self.db.parent / "backups").iterdir()))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                ("needs_review", "failed", "admitted_pending_finalize"),
                connection.execute(
                    """SELECT j.status,r.status,r.stage FROM import_jobs j
                       JOIN import_web_admission_runs r ON r.import_job_id=j.id
                       WHERE j.id=1"""
                ).fetchone(),
            )

    def test_finalize_transaction_preflight_rejects_snapshot_race(self):
        self._approve_and_classify_all()

        def mutate_then_finalize(*args, **kwargs):
            with closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute(
                    """INSERT INTO tag_definitions(category,code,name)
                       VALUES('scenario','finalize-race','收口事务前并发变化')"""
                )
            return finalize_review(*args, **kwargs)

        with self.assertRaisesRegex(
            WebAdmissionError, f"^{SAFE_BACKUP_STALE}$"
        ) as raised:
            apply_web_admission(
                self.db, self.private, 1, finalize_fn=mutate_then_finalize,
            )
        self.assertEqual(409, raised.exception.status_code)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual("needs_review", connection.execute(
                "SELECT status FROM import_jobs WHERE id=1"
            ).fetchone()[0])

    def test_admit_failure_after_backup_is_retryable_without_duplicate_backup(self):
        self._approve_and_classify_all()
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(f"private injected {self.root}/secret")
            return admit_questions(*args, **kwargs)

        with self.assertRaisesRegex(WebAdmissionError, f"^{SAFE_APPLY_FAILED}$") as raised:
            apply_web_admission(
                self.db, self.private, 1, admit_fn=fail_once,
            )
        self.assertEqual(500, raised.exception.status_code)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0])
            self.assertEqual(("failed", "processing"), connection.execute(
                "SELECT status,stage FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone())
        backups = sorted((self.db.parent / "backups").iterdir())
        self.assertEqual(1, len(backups))

        self.assertEqual("completed", apply_web_admission(
            self.db, self.private, 1, admit_fn=fail_once,
        ))

        self.assertEqual(2, calls)
        self.assertEqual(backups, sorted((self.db.parent / "backups").iterdir())[:-1])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual((23, "completed", "completed"), (
                connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
                connection.execute("SELECT status FROM import_jobs WHERE id=1").fetchone()[0],
                connection.execute(
                    "SELECT status FROM import_web_admission_runs WHERE import_job_id=1"
                ).fetchone()[0],
            ))

    def test_retry_rejects_old_backup_when_unrelated_business_data_drifted(self):
        self._approve_and_classify_all()

        def fail_admit(*args, **kwargs):
            raise RuntimeError("injected admission failure")

        with self.assertRaisesRegex(WebAdmissionError, f"^{SAFE_APPLY_FAILED}$"):
            apply_web_admission(
                self.db, self.private, 1, admit_fn=fail_admit,
            )
        backups = sorted((self.db.parent / "backups").iterdir())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT INTO tag_definitions(category,code,name) VALUES('task','external-drift','外部合法变化')"
            )

        with self.assertRaisesRegex(WebAdmissionError, f"^{SAFE_BACKUP_STALE}$") as raised:
            apply_web_admission(self.db, self.private, 1)

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(backups, sorted((self.db.parent / "backups").iterdir()))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0])

    def test_inexact_historical_completed_job_does_not_create_retroactive_run(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE import_jobs SET status='completed' WHERE id=1")
        page = load_admission_page(self.db, self.private, 1)
        self.assertEqual("pending", page.stage)
        self.assertFalse(page.can_apply)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_web_admission_runs"
            ).fetchone()[0])

    def test_completed_job_with_stale_failed_run_is_authoritative_and_read_only(self):
        self._approve_and_classify_all()

        def completes_then_raises(*args, **kwargs):
            finalize_review(*args, **kwargs)
            raise RuntimeError(f"injected {self.root}/secret digest={'f' * 64}")

        with self.assertRaises(WebAdmissionError):
            apply_web_admission(
                self.db, self.private, 1, finalize_fn=completes_then_raises
            )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            before = connection.execute(
                "SELECT * FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()
            self.assertEqual(("completed", "completed", "completed"), connection.execute(
                """SELECT j.status,r.status,r.stage FROM import_jobs j
                   JOIN import_web_admission_runs r ON r.import_job_id=j.id
                   WHERE j.id=1"""
            ).fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE questions SET stem_markdown=stem_markdown || ' split'
                       WHERE id=(SELECT question_id FROM question_sources
                                 WHERE import_job_id=1 LIMIT 1)"""
                )
        client = TestClient(create_app(self.db, self.private))
        response = client.get("/imports/1/admission")
        self.assertEqual(200, response.status_code)
        self.assertIn("completed", response.text)
        self.assertNotIn("正式入库并完成任务</button>", response.text)
        self.assertNotIn(SAFE_FINALIZE_FAILED, response.text)
        self.assertNotIn(str(self.root), response.text)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            after = connection.execute(
                "SELECT * FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()
        self.assertEqual(before, after)

    def test_completed_job_page_does_not_require_intermediate_artifacts(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        shutil.rmtree(self.job_dir)
        app = create_app(self.db, self.private)
        before = self.db.read_bytes()

        response = TestClient(app).get("/imports/1/admission")

        self.assertEqual(200, response.status_code)
        self.assertIn("completed", response.text)
        self.assertNotIn("正式入库并完成任务</button>", response.text)
        self.assertEqual(before, self.db.read_bytes())

    def test_historical_completed_job_without_web_run_needs_no_artifacts(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TRIGGER web_admission_completed_immutable")
            connection.execute("DROP TRIGGER web_admission_completed_delete_immutable")
            connection.execute(
                "DELETE FROM import_web_admission_runs WHERE import_job_id=1"
            )
        shutil.rmtree(self.job_dir)
        app = create_app(self.db, self.private)
        before = self.db.read_bytes()

        response = TestClient(app).get(
            "/imports/1/admission"
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("completed", response.text)
        self.assertEqual(before, self.db.read_bytes())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_web_admission_runs"
            ).fetchone()[0])

    def test_historical_completed_job_five_without_web_run_is_read_only(self):
        source_sha = "b" * 64
        with closing(sqlite3.connect(self.db)) as connection, connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES(?,1,'history.pdf','raw_papers/TJ/2025/history.pdf',
                          'TJ',2025,'QT','历史任务 5')""",
                (source_sha,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,status) "
                "VALUES(5,?,'completed')",
                (source_id,),
            )
            knowledge_id = connection.execute(
                "SELECT id FROM knowledge_points ORDER BY id LIMIT 1"
            ).fetchone()[0]
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,answer_markdown,region_code,
                    exam_year,exam_type_code,paper_name,source_question_no,
                    question_type_code,primary_knowledge_point_id,content_hash)
                   VALUES(?,'历史题干','历史答案','TJ',2025,'QT','历史任务 5','1',
                          'solution',?,'history-content')""",
                (f"Q-{source_sha[:16]}-001", knowledge_id),
            ).lastrowid
            connection.execute(
                """INSERT INTO question_sources
                   (question_id,source_paper_id,import_job_id,source_question_no,
                    source_pages_json) VALUES(?,?,5,'1','[1]')""",
                (question_id, source_id),
            )

        app = create_app(self.db, self.private)
        before = self.db.read_bytes()
        response = TestClient(app).get("/imports/5/admission")

        self.assertEqual(200, response.status_code)
        self.assertIn("整批入库与任务收口已完成。本页仅供查看。", response.text)
        self.assertIn("原卷答案缺失时始终保持 missing", response.text)
        self.assertNotIn("不满足整批严格门禁", response.text)
        self.assertNotIn("正式入库并完成任务</button>", response.text)
        self.assertEqual(before, self.db.read_bytes())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM import_web_admission_runs WHERE import_job_id=5"
            ).fetchone())

    def test_coordinated_completed_batch_rejects_all_protected_content_drift(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        client = TestClient(create_app(self.db, self.private))
        ready = client.get("/imports/1/admission")
        self.assertEqual(200, ready.status_code)
        csrf = client.cookies.get("basket_csrf")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            digest = connection.execute(
                "SELECT formal_batch_digest FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()[0]
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            connection.execute(
                """INSERT INTO tag_definitions(category,code,name)
                   VALUES('method','formal-drift-tag','正式摘要漂移标签')"""
            )
            triggers = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'web_admission_protect_%'"
            ).fetchall()
            for (name,) in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')

        mutations = (
            (
                "stem",
                "UPDATE questions SET stem_markdown=stem_markdown || ' tampered' "
                "WHERE id=(SELECT question_id FROM question_sources WHERE import_job_id=1 LIMIT 1)",
                "UPDATE questions SET stem_markdown=substr(stem_markdown,1,length(stem_markdown)-9) "
                "WHERE id=(SELECT question_id FROM question_sources WHERE import_job_id=1 LIMIT 1)",
            ),
            (
                "subquestion score",
                "UPDATE subquestions SET score=COALESCE(score,0)+1 WHERE id=(SELECT s.id FROM subquestions s JOIN question_sources q ON q.question_id=s.question_id WHERE q.import_job_id=1 LIMIT 1)",
                "UPDATE subquestions SET score=CASE WHEN score=1 THEN NULL ELSE score-1 END WHERE id=(SELECT s.id FROM subquestions s JOIN question_sources q ON q.question_id=s.question_id WHERE q.import_job_id=1 LIMIT 1)",
            ),
            (
                "option",
                "UPDATE question_options SET content_markdown=content_markdown || ' tampered' WHERE id=(SELECT o.id FROM question_options o JOIN question_sources q ON q.question_id=o.question_id WHERE q.import_job_id=1 LIMIT 1)",
                "UPDATE question_options SET content_markdown=substr(content_markdown,1,length(content_markdown)-9) WHERE id=(SELECT o.id FROM question_options o JOIN question_sources q ON q.question_id=o.question_id WHERE q.import_job_id=1 LIMIT 1)",
            ),
            (
                "knowledge relation",
                "DELETE FROM question_related_knowledge_points WHERE rowid=(SELECT r.rowid FROM question_related_knowledge_points r JOIN question_sources q ON q.question_id=r.question_id WHERE q.import_job_id=1 LIMIT 1)",
                None,
            ),
            (
                "formula",
                "INSERT INTO question_formulas(question_id,formula_latex,location,display_order) SELECT question_id,'x','drift-check',99 FROM question_sources WHERE import_job_id=1 LIMIT 1",
                "DELETE FROM question_formulas WHERE location='drift-check'",
            ),
            (
                "figure",
                "INSERT INTO question_figures(question_id,relative_path,purpose,display_order,source_type,image_hash) SELECT question_id,'figures/drift-check.png','drift',99,'generated','drift-hash' FROM question_sources WHERE import_job_id=1 LIMIT 1",
                "DELETE FROM question_figures WHERE relative_path='figures/drift-check.png'",
            ),
            (
                "tag",
                "INSERT INTO question_tags(question_id,tag_id,note) SELECT question_id,(SELECT id FROM tag_definitions WHERE code='formal-drift-tag'),'drift' FROM question_sources WHERE import_job_id=1 LIMIT 1",
                "DELETE FROM question_tags WHERE tag_id=(SELECT id FROM tag_definitions WHERE code='formal-drift-tag')",
            ),
        )
        for name, mutate, restore in mutations:
            with self.subTest(field=name):
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    relation = None
                    if name == "knowledge relation":
                        relation = connection.execute(
                            "SELECT r.question_id,r.knowledge_point_id FROM question_related_knowledge_points r JOIN question_sources q ON q.question_id=r.question_id WHERE q.import_job_id=1 LIMIT 1"
                        ).fetchone()
                    cursor = connection.execute(mutate)
                    self.assertEqual(1, cursor.rowcount)
                self.assertEqual(409, client.get("/imports/1/admission").status_code)
                response = client.post(
                    "/imports/1/admission/apply", data={"csrf_token": csrf},
                )
                self.assertEqual(409, response.status_code)
                self.assertIn(SAFE_COMPLETED_DRIFT, response.text)
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    if restore is not None:
                        connection.execute(restore)
                    else:
                        connection.execute(
                            "INSERT INTO question_related_knowledge_points VALUES(?,?)",
                            relation,
                        )

    def test_completed_batch_allows_question_soft_delete_and_restore(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection:
            code = connection.execute(
                """SELECT q.question_code FROM questions q JOIN question_sources s
                   ON s.question_id=q.id WHERE s.import_job_id=1 ORDER BY q.id LIMIT 1"""
            ).fetchone()[0]
        client = TestClient(create_app(self.db, self.private))
        client.get(f"/questions/{code}")
        csrf = str(client.cookies.get("basket_csrf") or "")
        self.assertTrue(csrf)

        deleted = client.post(
            f"/questions/{code}/delete",
            data={
                "csrf_token": csrf,
                "reason": "unneeded",
                "note": "生命周期测试",
                "confirmed": "yes",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, deleted.status_code)
        self.assertEqual(200, client.get("/imports/1/admission").status_code)

        restored = client.post(
            f"/questions/{code}/restore",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(303, restored.status_code)
        self.assertEqual(200, client.get("/imports/1/admission").status_code)

    def test_initialize_upgrades_same_name_noop_protection_triggers_idempotently(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        protected = {
            "formulas": "question_formulas",
            "figures": "question_figures",
            "tags": "question_tags",
        }
        with closing(sqlite3.connect(self.db)) as connection, connection:
            question_id = connection.execute(
                "SELECT question_id FROM question_sources WHERE import_job_id=1 LIMIT 1"
            ).fetchone()[0]
            tag_id = connection.execute(
                "SELECT id FROM tag_definitions ORDER BY id LIMIT 1"
            ).fetchone()[0]
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'web_admission_protect_%'"
            ).fetchall():
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(
                "INSERT INTO question_formulas(question_id,formula_latex,location,display_order) "
                "VALUES(?, 'x=1', 'legacy-protected', 99)",
                (question_id,),
            )
            connection.execute(
                "INSERT INTO question_figures(question_id,relative_path,purpose,display_order,source_type,image_hash) "
                "VALUES(?,'figures/legacy-protected.png','legacy',99,'generated','legacy-hash')",
                (question_id,),
            )
            connection.execute(
                "INSERT INTO question_tags(question_id,tag_id,note) VALUES(?,?,'legacy')",
                (question_id, tag_id),
            )
            for stem, table in protected.items():
                for operation in ("insert", "update", "delete"):
                    connection.execute(
                        f"CREATE TRIGGER web_admission_protect_{stem}_{operation} "
                        f"BEFORE {operation.upper()} ON {table} WHEN 0 "
                        "BEGIN SELECT 1; END"
                    )

        initialize_database(self.db).close()
        initialize_database(self.db).close()

        with closing(sqlite3.connect(self.db)) as connection:
            definitions = dict(connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'web_admission_protect_%'"
            ))
            for stem in protected:
                for operation in ("insert", "update", "delete"):
                    sql = definitions[f"web_admission_protect_{stem}_{operation}"]
                    self.assertIn("completed web admission", sql)
            attempts = (
                ("INSERT INTO question_formulas(question_id,formula_latex,location,display_order) VALUES(?, 'y=2', 'blocked', 100)", (question_id,)),
                ("UPDATE question_formulas SET formula_latex='changed' WHERE location='legacy-protected'", ()),
                ("DELETE FROM question_formulas WHERE location='legacy-protected'", ()),
                ("INSERT INTO question_figures(question_id,relative_path,purpose,display_order,source_type,image_hash) VALUES(?,'figures/blocked.png','blocked',100,'generated','blocked')", (question_id,)),
                ("UPDATE question_figures SET purpose='changed' WHERE relative_path='figures/legacy-protected.png'", ()),
                ("DELETE FROM question_figures WHERE relative_path='figures/legacy-protected.png'", ()),
                ("INSERT INTO question_tags(question_id,tag_id,note) VALUES(?,?, 'blocked')", (question_id, tag_id + 1)),
                ("UPDATE question_tags SET note='changed' WHERE question_id=? AND tag_id=?", (question_id, tag_id)),
                ("DELETE FROM question_tags WHERE question_id=? AND tag_id=?", (question_id, tag_id)),
            )
            for sql, params in attempts:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql, params)
                connection.rollback()
            self.assertEqual((1, 1, 1), (
                connection.execute("SELECT COUNT(*) FROM question_formulas WHERE location='legacy-protected'").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM question_figures WHERE relative_path='figures/legacy-protected.png'").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM question_tags WHERE question_id=? AND tag_id=?", (question_id, tag_id)).fetchone()[0],
            ))

    def test_completed_web_run_prevents_job_status_rollback_in_database(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE import_jobs SET status='needs_review' WHERE id=1"
                )
            connection.rollback()
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM import_jobs WHERE id=1"
            ).fetchone()[0])

    def test_completed_shortcut_rejects_split_job_and_run_statuses(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "DROP TRIGGER IF EXISTS web_admission_protect_job_status_update"
            )
            connection.execute(
                "UPDATE import_jobs SET status='needs_review' WHERE id=1"
            )

        with self.assertRaisesRegex(WebAdmissionError, SAFE_COMPLETED_DRIFT):
            apply_web_admission(self.db, self.private, 1)

    def test_fresh_claim_conflicts_and_stale_claim_recovers(self):
        self._approve_and_classify_all()
        backup, digest = backup_database(self.db)
        relative = backup.relative_to(self.db.parent).as_posix()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO import_web_admission_runs
                   (import_job_id,status,stage,claim_token,expected_count,
                    backup_relative_path,backup_sha256,claimed_at,heartbeat_at,
                    lease_expires_at,created_at,updated_at)
                   VALUES(1,'processing','processing',?,23,?,?,datetime('now'),
                          datetime('now'),'2999-01-01T00:00:00+00:00',
                          datetime('now'),datetime('now'))""",
                ("f" * 64, relative, digest),
            )
        with self.assertRaisesRegex(WebAdmissionError, SAFE_BUSY):
            apply_web_admission(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """UPDATE import_web_admission_runs
                   SET lease_expires_at='2000-01-01T00:00:00+00:00',
                       updated_at='2000-01-01T00:00:00+00:00' WHERE import_job_id=1"""
            )
        apply_web_admission(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(("completed", 23), connection.execute(
                """SELECT r.status,COUNT(q.id) FROM import_web_admission_runs r
                   JOIN question_sources s ON s.import_job_id=r.import_job_id
                   JOIN questions q ON q.id=s.question_id WHERE r.import_job_id=1"""
            ).fetchone())

    def test_double_submit_allows_only_one_active_worker(self):
        self._approve_and_classify_all()
        entered = threading.Event()
        release = threading.Event()

        def paused_admit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return admit_questions(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                apply_web_admission, self.db, self.private, 1,
                admit_fn=paused_admit,
            )
            self.assertTrue(entered.wait(5))
            second = pool.submit(apply_web_admission, self.db, self.private, 1)
            with self.assertRaisesRegex(WebAdmissionError, SAFE_BUSY):
                second.result(timeout=5)
            release.set()
            self.assertEqual("completed", first.result(timeout=10))
        self.assertFalse(any(
            thread.name.startswith("web-admission-lease-")
            for thread in threading.enumerate()
        ))
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(23, connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0])

    def test_first_backup_anchor_drift_is_rejected_inside_admission_transaction(self):
        self._approve_and_classify_all()
        original_digest = web_admission_module._database_snapshot_digest
        injected = False

        def digest_then_commit(database_path):
            nonlocal injected
            digest = original_digest(database_path)
            if not injected:
                injected = True
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute(
                        "INSERT INTO baskets(basket_key,name) VALUES('concurrent-first','并发合法提交')"
                    )
            return digest

        with mock.patch.object(
            web_admission_module,
            "_database_snapshot_digest",
            side_effect=digest_then_commit,
        ):
            with self.assertRaisesRegex(WebAdmissionError, SAFE_BACKUP_STALE):
                apply_web_admission(self.db, self.private, 1)

        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()[0])

    def test_reused_backup_anchor_drift_is_rejected_inside_admission_transaction(self):
        self._approve_and_classify_all()

        def fail_after_backup(*_args, **_kwargs):
            raise RuntimeError("fixture admission failure")

        with self.assertRaisesRegex(WebAdmissionError, SAFE_APPLY_FAILED):
            apply_web_admission(
                self.db, self.private, 1, admit_fn=fail_after_backup
            )
        original_digest = web_admission_module._database_snapshot_digest
        injected = False

        def digest_then_commit(database_path):
            nonlocal injected
            digest = original_digest(database_path)
            if not injected:
                injected = True
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute(
                        "INSERT INTO baskets(basket_key,name) VALUES('concurrent-reuse','并发合法提交')"
                    )
            return digest

        with mock.patch.object(
            web_admission_module,
            "_database_snapshot_digest",
            side_effect=digest_then_commit,
        ):
            with self.assertRaisesRegex(WebAdmissionError, SAFE_BACKUP_STALE):
                apply_web_admission(self.db, self.private, 1)

        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=1"
            ).fetchone()[0])
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()[0])

    def test_keeper_renews_short_lease_while_admit_is_paused(self):
        self._approve_and_classify_all()
        entered = threading.Event()
        release = threading.Event()

        def paused_admit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return admit_questions(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                apply_web_admission, self.db, self.private, 1,
                admit_fn=paused_admit, lease_seconds=0.15, keeper_interval=0.02,
            )
            self.assertTrue(entered.wait(5))
            time.sleep(0.25)
            with self.assertRaisesRegex(WebAdmissionError, SAFE_BUSY):
                apply_web_admission(
                    self.db, self.private, 1,
                    lease_seconds=0.15, keeper_interval=0.02,
                )
            release.set()
            self.assertEqual("completed", first.result(timeout=10))

    def test_old_worker_stops_after_claim_token_is_replaced(self):
        self._approve_and_classify_all()
        entered = threading.Event()
        release = threading.Event()

        def paused_admit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return admit_questions(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                apply_web_admission, self.db, self.private, 1,
                admit_fn=paused_admit, lease_seconds=1, keeper_interval=0.02,
            )
            self.assertTrue(entered.wait(5))
            with closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute(
                    "UPDATE import_web_admission_runs SET claim_token=? WHERE import_job_id=1",
                    ("e" * 64,),
                )
            release.set()
            with self.assertRaisesRegex(WebAdmissionError, SAFE_BUSY):
                future.result(timeout=10)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertNotEqual("completed", connection.execute(
                "SELECT status FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()[0])
        self.assertFalse(any(
            thread.name.startswith("web-admission-lease-")
            for thread in threading.enumerate()
        ))

    def test_finalize_completed_job_stays_authoritative_after_token_loss(self):
        self._approve_and_classify_all()

        def finalize_then_replace(*args, **kwargs):
            result = finalize_review(*args, **kwargs)
            with closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute(
                    "UPDATE import_web_admission_runs SET claim_token=? WHERE import_job_id=1",
                    ("d" * 64,),
                )
            return result

        with self.assertRaisesRegex(WebAdmissionError, SAFE_FINALIZE_FAILED):
            apply_web_admission(
                self.db, self.private, 1, finalize_fn=finalize_then_replace,
                lease_seconds=1, keeper_interval=0.02,
            )
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(("completed", "completed"), connection.execute(
                """SELECT j.status,r.status FROM import_jobs j
                   JOIN import_web_admission_runs r ON r.import_job_id=j.id
                   WHERE j.id=1"""
            ).fetchone())
        page = load_admission_page(self.db, self.private, 1)
        self.assertEqual("completed", page.stage)
        self.assertFalse(page.can_apply)

    def test_fake_backups_are_rejected_and_new_orphans_are_removed(self):
        self._approve_and_classify_all()
        backups = self.db.parent / "backups"
        backups.mkdir()
        outside = self.root / "outside.db"
        outside.write_bytes(b"outside")

        def fake_backup(_database):
            path = backups / "fake.db"
            path.write_bytes(b"fake")
            return path, hashlib.sha256(path.read_bytes()).hexdigest()

        with self.assertRaisesRegex(WebAdmissionError, SAFE_APPLY_FAILED):
            apply_web_admission(self.db, self.private, 1, backup_fn=fake_backup)
        self.assertFalse((backups / "fake.db").exists())
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual("failed", connection.execute(
                "SELECT status FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()[0])

        for kind in ("symlink", "hardlink", "escape"):
            with self.subTest(kind=kind):
                def malicious(_database, kind=kind):
                    if kind == "escape":
                        return outside, hashlib.sha256(outside.read_bytes()).hexdigest()
                    path = backups / f"{kind}.db"
                    if kind == "symlink":
                        path.symlink_to(outside)
                    else:
                        path.hardlink_to(outside)
                    return path, hashlib.sha256(outside.read_bytes()).hexdigest()

                with self.assertRaisesRegex(WebAdmissionError, SAFE_APPLY_FAILED):
                    apply_web_admission(self.db, self.private, 1, backup_fn=malicious)
                self.assertFalse((backups / f"{kind}.db").exists())
        self.assertTrue(outside.exists())

        def dotdot_escape(_database):
            return backups / ".." / outside.name, hashlib.sha256(
                outside.read_bytes()
            ).hexdigest()

        with self.assertRaisesRegex(WebAdmissionError, SAFE_APPLY_FAILED):
            apply_web_admission(self.db, self.private, 1, backup_fn=dotdot_escape)
        self.assertTrue(outside.exists())

    def test_healthy_but_empty_or_old_sqlite_backup_is_rejected_and_removed(self):
        old = self.root / "old-source.db"
        with closing(sqlite3.connect(self.db)) as source, closing(sqlite3.connect(old)) as target:
            source.backup(target)
        self._approve_and_classify_all()

        for kind in ("empty", "old"):
            with self.subTest(kind=kind):
                backups = self.db.parent / "backups"
                backups.mkdir(exist_ok=True)
                path = backups / f"{kind}.db"

                def stale_backup(_database, backup=path, backup_kind=kind):
                    if backup_kind == "empty":
                        initialize_database(backup).close()
                    else:
                        with closing(sqlite3.connect(old)) as source, closing(
                            sqlite3.connect(backup)
                        ) as target:
                            source.backup(target)
                    return backup, hashlib.sha256(backup.read_bytes()).hexdigest()

                with self.assertRaisesRegex(WebAdmissionError, SAFE_APPLY_FAILED):
                    apply_web_admission(self.db, self.private, 1, backup_fn=stale_backup)
                self.assertFalse(path.exists())

    def test_fake_finalize_backup_is_rejected_and_orphan_is_removed(self):
        self._approve_and_classify_all()
        calls = 0

        def fake_second_backup(database):
            nonlocal calls
            calls += 1
            if calls == 1:
                return backup_database(database)
            path = self.db.parent / "backups" / "fake-finalize.db"
            path.write_bytes(b"not a verified backup")
            return path, "a" * 64

        with self.assertRaisesRegex(WebAdmissionError, SAFE_FINALIZE_FAILED):
            apply_web_admission(
                self.db, self.private, 1, backup_fn=fake_second_backup
            )
        self.assertFalse(
            (self.db.parent / "backups" / "fake-finalize.db").exists()
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual("admitted_pending_finalize", connection.execute(
                "SELECT stage FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()[0])

    def test_schema_repeat_initialization_checks_and_completed_immutability(self):
        initialize_database(self.db).close()
        initialize_database(self.db).close()
        connection = sqlite3.connect(self.db)
        try:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='import_web_admission_runs'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIn("preparing_backup", sql)
        self.assertIn("formal_batch_digest NOT GLOB '*[^0-9a-f]*'", sql)
        self._approve_and_classify_all()
        apply_web_admission(self.db, self.private, 1)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE import_web_admission_runs SET expected_count=1 WHERE import_job_id=1"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM import_web_admission_runs WHERE import_job_id=1"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE questions SET stem_markdown=stem_markdown || 'x' "
                    "WHERE id=(SELECT question_id FROM question_sources "
                    "WHERE import_job_id=1 LIMIT 1)"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM question_related_knowledge_points WHERE rowid=("
                    "SELECT r.rowid FROM question_related_knowledge_points r "
                    "JOIN question_sources s ON s.question_id=r.question_id "
                    "WHERE s.import_job_id=1 LIMIT 1)"
                )
            question_id = connection.execute(
                "SELECT question_id FROM question_sources WHERE import_job_id=1 LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO tag_definitions(category,code,name)
                   VALUES('method','immutable-tag','不可变测试标签')"""
            )
            tag_id = connection.execute(
                "SELECT id FROM tag_definitions WHERE code='immutable-tag'"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO question_formulas
                       (question_id,formula_latex,location,display_order)
                       VALUES(?,'x','immutable-check',99)""",
                    (question_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO question_figures
                       (question_id,relative_path,purpose,display_order,source_type,image_hash)
                       VALUES(?,'figures/immutable-check.png','check',99,'generated','hash')""",
                    (question_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO question_tags(question_id,tag_id) VALUES(?,?)",
                    (question_id, tag_id),
                )

    def test_preparing_backup_schema_migration_is_repeatable_and_preserves_run(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE IF NOT EXISTS import_web_admission_runs (")
        end = schema.index("\nCREATE INDEX IF NOT EXISTS idx_web_admission_run_claim", start)
        legacy = schema[start:end].replace("'preparing_backup',", "")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DROP TRIGGER web_admission_completed_immutable")
            connection.execute("DROP TRIGGER web_admission_completed_delete_immutable")
            connection.execute("DROP INDEX idx_web_admission_run_claim")
            connection.execute("DROP TABLE import_web_admission_runs")
            connection.execute(legacy)
            connection.execute(
                """INSERT INTO import_web_admission_runs
                   (import_job_id,status,stage,expected_count,safe_error,
                    created_at,updated_at)
                   VALUES(1,'failed','failed',23,'legacy detail','2026-01-01','2026-01-01')"""
            )
        initialize_database(self.db).close()
        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            row = connection.execute(
                "SELECT status,stage,expected_count,claim_token,lease_expires_at "
                "FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='import_web_admission_runs'"
            ).fetchone()[0]
        self.assertEqual(("failed", "failed", 23, None, None), row)
        self.assertIn("preparing_backup", table_sql)

    def test_legacy_admitted_status_migrates_to_recoverable_failed_stage(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE IF NOT EXISTS import_web_admission_runs (")
        end = schema.index("\nCREATE INDEX IF NOT EXISTS idx_web_admission_run_claim", start)
        legacy = schema[start:end]
        legacy = legacy.replace(
            """    formal_batch_digest TEXT CHECK (
        formal_batch_digest IS NULL OR (
            length(formal_batch_digest)=64
            AND formal_batch_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
""",
            "",
        ).replace("\n        AND formal_batch_digest IS NOT NULL", "")
        legacy = legacy.replace(
            "status IN ('processing','completed','failed')",
            "status IN ('processing','completed','failed','admitted_pending_finalize')",
        ).replace(
            "status IN ('completed','failed') AND claim_token IS NULL",
            "status IN ('completed','failed','admitted_pending_finalize') AND claim_token IS NULL",
        ).replace(
            "OR (status='completed' AND stage='completed')",
            "OR (status='completed' AND stage='completed') "
            "OR (status='admitted_pending_finalize' AND stage='admitted_pending_finalize')",
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            for trigger in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND (name LIKE 'web_admission_protect_%' "
                "OR name LIKE 'web_admission_completed_%')"
            ).fetchall():
                connection.execute(f'DROP TRIGGER "{trigger[0]}"')
            connection.execute("DROP INDEX idx_web_admission_run_claim")
            connection.execute("DROP TABLE import_web_admission_runs")
            connection.execute(legacy)
            connection.execute(
                """INSERT INTO import_web_admission_runs
                   (import_job_id,status,stage,expected_count,backup_relative_path,
                    backup_sha256,question_code_digest,inserted_count,
                    already_present_count,eligible_count,created_at,updated_at)
                   VALUES(1,'admitted_pending_finalize','admitted_pending_finalize',23,
                          'backups/legacy.db',?,?,23,0,23,'2026-01-01','2026-01-01')""",
                ("a" * 64, "b" * 64),
            )

        initialize_database(self.db).close()

        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT status,stage,claim_token,lease_expires_at "
                "FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone()
        self.assertEqual(
            ("failed", "admitted_pending_finalize", None, None), row
        )

    def test_legacy_completed_run_without_formal_digest_is_anchored_on_migration(self):
        self._approve_and_classify_all()
        self.assertEqual("completed", apply_web_admission(self.db, self.private, 1))
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE IF NOT EXISTS import_web_admission_runs (")
        end = schema.index("\nCREATE INDEX IF NOT EXISTS idx_web_admission_run_claim", start)
        legacy = schema[start:end].replace(
            """    formal_batch_digest TEXT CHECK (
        formal_batch_digest IS NULL OR (
            length(formal_batch_digest)=64
            AND formal_batch_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
""",
            "",
        ).replace("\n        AND formal_batch_digest IS NOT NULL", "")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute(
                "SELECT * FROM import_web_admission_runs WHERE import_job_id=1"
            ).fetchone())
            for trigger in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND (name LIKE 'web_admission_protect_%' "
                "OR name LIKE 'web_admission_completed_%')"
            ).fetchall():
                connection.execute(f'DROP TRIGGER "{trigger[0]}"')
            connection.execute("DROP INDEX idx_web_admission_run_claim")
            connection.execute("DROP TABLE import_web_admission_runs")
            connection.execute(legacy)
            columns = [item[1] for item in connection.execute(
                "PRAGMA table_info(import_web_admission_runs)"
            )]
            connection.execute(
                f"INSERT INTO import_web_admission_runs ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(row.get(column) for column in columns),
            )

        initialize_database(self.db).close()
        with closing(sqlite3.connect(self.db)) as connection:
            digest = connection.execute(
                "SELECT formal_batch_digest FROM import_web_admission_runs "
                "WHERE import_job_id=1"
            ).fetchone()[0]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            200,
            TestClient(create_app(self.db, self.private)).get(
                "/imports/1/admission"
            ).status_code,
        )

    def test_testclient_complete_batch_post_redirect_and_dependency_isolation(self):
        self._approve_and_classify_all()
        forbidden = mock.Mock(side_effect=AssertionError("forbidden dependency"))
        app = create_app(
            self.db, self.private, weekly_checker=forbidden,
            candidate_runner=forbidden, audit_runner=forbidden,
            classification_runner=forbidden,
        )
        client = TestClient(app)
        ready = client.get("/imports/1/admission")
        self.assertIn("正式入库并完成任务</button>", ready.text)
        self.assertNotIn("不满足整批严格门禁", ready.text)
        response = client.post(
            "/imports/1/admission/apply",
            data={"csrf_token": client.cookies.get("basket_csrf")},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)
        completed = client.get(response.headers["location"])
        self.assertIn("completed", completed.text)
        self.assertNotIn("正式入库并完成任务</button>", completed.text)
        forbidden.assert_not_called()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual((23, "completed", "completed"), (
                connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
                connection.execute("SELECT status FROM import_jobs WHERE id=1").fetchone()[0],
                connection.execute(
                    "SELECT status FROM import_web_admission_runs WHERE import_job_id=1"
                ).fetchone()[0],
            ))
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_oversized_admission_form_is_413_and_changes_nothing(self):
        self._approve_and_classify_all()
        client = TestClient(create_app(self.db, self.private))
        client.get("/imports/1/admission")
        before_db = self.db.read_bytes()
        before_tree = sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        )
        response = client.post(
            "/imports/1/admission/apply",
            content=b"x" * (MAX_ADMISSION_FORM_BYTES + 1),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(413, response.status_code)
        self.assertEqual(before_db, self.db.read_bytes())
        self.assertEqual(before_tree, sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        ))


if __name__ == "__main__":
    unittest.main()
