import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


from src.database.initialize import initialize_database
from src.processing.secure_crop_artifacts import locked_job
from src.reviewing.finalize import FinalizationError, finalize_review, is_ai_second_pass_eligible


class FinalizeReviewTests(unittest.TestCase):
    def test_ai_second_pass_eligibility_requires_all_four_signals(self):
        valid = {"audit_status": "auto_pass", "audit_confidence": "high",
                 "issues": [], "suggested_corrections": []}
        self.assertTrue(is_ai_second_pass_eligible(valid))
        for field, value in (
            ("audit_status", "human_required"), ("audit_status", "disputed"),
            ("audit_confidence", "medium"), ("issues", ["x"]),
            ("suggested_corrections", ["x"]), ("issues", None),
        ):
            with self.subTest(field=field, value=value):
                audit = dict(valid); audit[field] = value
                self.assertFalse(is_ai_second_pass_eligible(audit))

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "question-bank.db"
        self.private_root = self.root / "private"
        initialize_database(self.db_path).close()
        self.candidate_payload = self._candidate_payload()
        self.candidate_raw = json.dumps(
            self.candidate_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._seed_database()
        self._write_audit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_database(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,exam_year,
                    exam_type_code,paper_name)
                   VALUES(?,100,'卷.pdf','raw_papers/TJ/2026/review.pdf','TJ',2026,'YK','测试卷')""",
                ("a" * 64,),
            ).lastrowid
            job_id = connection.execute(
                "INSERT INTO import_jobs(source_paper_id,status) VALUES(?,'needs_review')",
                (source_id,),
            ).lastrowid
            self.assertEqual(1, job_id)
            other = connection.execute(
                "SELECT id FROM knowledge_points WHERE code='08.04.04'"
            ).fetchone()[0]
            self.ids = {}
            for number in ("1", "2", "22"):
                qid = connection.execute(
                    """INSERT INTO questions
                       (question_code,stem_markdown,answer_markdown,analysis_markdown,
                        region_code,exam_type_code,question_type_code,
                        primary_knowledge_point_id,content_hash,deleted_at,source_question_no,
                        analysis_review_status)
                       VALUES(?,?,?,?, 'TJ','YK',?,?,?,?,?,'passed')""",
                    (
                        f"FORMAL-{number}", f"旧题干{number}", f"答案{number}", f"解析{number}",
                        "solution" if number == "22" else "single_choice",
                        other, f"old-{number}", "2026-01-01" if number == "1" else None,
                        number,
                    ),
                ).lastrowid
                self.ids[number] = qid
                connection.execute(
                    "INSERT INTO question_sources VALUES(?,?,?,?,?)",
                    (qid, source_id, job_id, number, "[1]"),
                )
                connection.execute(
                    "INSERT INTO question_related_knowledge_points VALUES(?,?)", (qid, other)
                )
                connection.execute(
                    """INSERT INTO question_assets
                       (question_id,import_job_id,asset_kind,relative_path,width,height,byte_size,
                        sha256,review_status,display_order)
                       VALUES(?,?,'complete_question',?,10,10,10,?,'ai_review_passed',1)""",
                    (qid, job_id, f"question_crops/Q{int(number):03d}.png", number[-1] * 64),
                )
            connection.execute(
                "INSERT INTO question_options(question_id,option_code,content_markdown,display_order) VALUES(?,?,?,?)",
                (self.ids["1"], "A", "旧选项", 1),
            )
            for order, stem in enumerate(("（1）旧", "（2）公共条件", "（2）①旧", "（2）②旧"), 1):
                connection.execute(
                    """INSERT INTO subquestions
                       (question_id,display_order,stem_markdown,answer_markdown,answer_status,analysis_markdown)
                       VALUES(?,?,?,?, 'provided',?)""",
                    (self.ids["22"], order, stem, f"小问答案{order}", f"小问解析{order}"),
                )
            candidates = {
                item["source_question_no"]: item for item in self.candidate_payload["questions"]
            }
            drafts = {
                "1": ("approved", self._edited("1", "人工修改题干", primary="01.01.01")),
                "2": ("pending", candidates["2"]),
                "12": ("pending", candidates["12"]),
                "22": ("approved", self._edited("22", "复杂题干", primary="08.04.04", complex=True)),
            }
            for number, (status, edited) in drafts.items():
                snapshot = json.dumps(candidates[number], ensure_ascii=False, separators=(",", ":"))
                payload = json.dumps(edited, ensure_ascii=False, separators=(",", ":"))
                reviewed_at = "2026-07-14T10:00:00+08:00" if status == "approved" else None
                evidence = json.dumps(
                    {"method": "workbench", "reviewed_at": reviewed_at}, separators=(",", ":")
                ) if reviewed_at else None
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at,
                        approval_source,approval_evidence_json)
                       VALUES(?,?,?,?,?,?,3,?,?,?)""",
                    (job_id, number, hashlib.sha256(self.candidate_raw).hexdigest(), snapshot,
                     payload, status, reviewed_at, "human" if reviewed_at else None, evidence),
                )

    @staticmethod
    def _edited(number, stem, primary, complex=False):
        result = {
            "source_question_no": number,
            "stem_markdown": stem,
            "question_type_code": "solution" if complex else "single_choice",
            "primary_knowledge_point_code": primary,
            "related_knowledge_point_codes": ["01.01.02"],
            "options": [] if complex else [
                {"code": "A", "content": "新甲"}, {"code": "B", "content": "新乙"}
            ],
            "subquestions": [],
            "answer_markdown": f"候选答案{number}",
            "analysis_markdown": f"候选解析{number}",
        }
        if complex:
            result["subquestions"] = [
                {"label": "（1）", "stem_markdown": "第一问"},
                {"label": "（2）", "stem_markdown": "公共条件"},
                {"label": "（2）①", "stem_markdown": "第一小问"},
                {"label": "（2）②", "stem_markdown": "第二小问"},
            ]
        return result

    @classmethod
    def _candidate_payload(cls):
        questions = [
            cls._edited("1", "候选题干1", primary="01.01.01"),
            cls._edited("2", "AI 修改题干", primary="01.01.01"),
            cls._edited("12", "证据不足", primary="01.01.01"),
            cls._edited("22", "候选复杂题干", primary="08.04.04", complex=True),
        ]
        return {
            "import_job_id": 1,
            "source_paper_id": 1,
            "question_count": len(questions),
            "questions": questions,
        }

    def _write_audit(self):
        job_dir = self.private_root / "processing" / "import_job_1"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "candidate_questions.json").write_bytes(self.candidate_raw)
        audits = []
        for number in ("1", "2", "12", "22"):
            audits.append({
                "source_question_no": number,
                "audit_status": "human_required" if number == "12" else "auto_pass",
                "audit_confidence": "high",
                "issues": ["证据不足"] if number == "12" else [],
                "suggested_corrections": [],
                "evidence_page": 1,
            })
        counts = {"auto_pass": 3, "disputed": 0, "human_required": 1}
        audit_raw = json.dumps(
            {"import_job_id": 1, "question_count": 4, "questions": audits,
             "counts": counts}, separators=(",", ":")
        ).encode("utf-8")
        (job_dir / "ai_audit.json").write_bytes(audit_raw)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """INSERT INTO import_candidate_audit_runs
                   (import_job_id,status,question_count,processed_questions,codex_run_id,
                    input_candidate_sha256,input_candidate_byte_size,
                    input_crop_generation_id,input_manifest_sha256,input_manifest_signature,
                    output_sha256,output_byte_size,completed_at,updated_at)
                   VALUES(1,'completed',4,4,'finalize-fixture-run',?,?,?, ?,?,?,?,
                          '2026-07-16T00:00:00+00:00','2026-07-16T00:00:00+00:00')
                   ON CONFLICT(import_job_id) DO UPDATE SET
                     status=excluded.status,question_count=excluded.question_count,
                     processed_questions=excluded.processed_questions,
                     codex_run_id=excluded.codex_run_id,
                     input_candidate_sha256=excluded.input_candidate_sha256,
                     input_candidate_byte_size=excluded.input_candidate_byte_size,
                     output_sha256=excluded.output_sha256,
                     output_byte_size=excluded.output_byte_size,
                     completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
                (hashlib.sha256(self.candidate_raw).hexdigest(), len(self.candidate_raw),
                 "1" * 32, "2" * 64, "3" * 64,
                 hashlib.sha256(audit_raw).hexdigest(), len(audit_raw)),
            )

    def _approve_q12_with_formal_question(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            primary_id = connection.execute(
                "SELECT id FROM knowledge_points WHERE code='01.01.01'"
            ).fetchone()[0]
            source_id = connection.execute(
                "SELECT source_paper_id FROM import_jobs WHERE id=1"
            ).fetchone()[0]
            qid = connection.execute(
                """INSERT INTO questions
                   (question_code,stem_markdown,answer_markdown,analysis_markdown,
                    region_code,exam_type_code,question_type_code,
                    primary_knowledge_point_id,content_hash,source_question_no,
                    analysis_review_status)
                   VALUES('FORMAL-12','旧题干12','答案12','解析12','TJ','YK',
                          'single_choice',?,'old-12','12','passed')""",
                (primary_id,),
            ).lastrowid
            self.ids["12"] = qid
            connection.execute(
                "INSERT INTO question_sources VALUES(?,?,?,?,?)",
                (qid, source_id, 1, "12", "[12]"),
            )
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET status='approved',reviewed_at='2026-07-15T10:00:00+08:00',
                       approval_source='human',
                       approval_evidence_json='{"method":"workbench","reviewed_at":"2026-07-15T10:00:00+08:00"}'
                   WHERE import_job_id=1 AND source_question_no='12'"""
            )

    def _rows(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            return connection.execute(sql, parameters).fetchall()

    def _business_snapshot(self):
        tables = (
            "import_jobs", "candidate_review_drafts", "questions",
            "question_options", "subquestions",
            "question_related_knowledge_points", "question_versions",
            "question_reviews",
        )
        return {
            table: self._rows(f"SELECT * FROM {table} ORDER BY rowid")
            for table in tables
        }

    def _rewrite_artifact(self, name, mutate):
        path = self.private_root / "processing" / "import_job_1" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _update_draft_json(self, number, mutate):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT edited_json FROM candidate_review_drafts WHERE source_question_no=?",
                (number,),
            ).fetchone()
            payload = json.loads(row[0])
            mutate(payload)
            connection.execute(
                "UPDATE candidate_review_drafts SET edited_json=? WHERE source_question_no=?",
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), number),
            )

    def test_ai_second_pass_never_approves_any_candidate_content_change(self):
        mutations = {
            "stem": lambda item: item.update(stem_markdown="人工改写"),
            "options": lambda item: item["options"][0].update(content="人工改写选项"),
            "subquestions": lambda item: item["subquestions"].append(
                {"label": "（1）", "stem_markdown": "人工新增小问"}
            ),
            "answer": lambda item: item.update(answer_markdown="人工改写答案"),
            "analysis": lambda item: item.update(analysis_markdown="人工改写解析"),
        }
        original = self.db_path.read_bytes()
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                self.db_path.write_bytes(original)
                self._approve_q12_with_formal_question()
                self._update_draft_json("2", mutate)

                result = finalize_review(self.db_path, self.private_root, 1, apply=True)

                self.assertIn("2", result.pending_question_nos)
                self.assertNotIn("2", result.ai_second_pass_question_nos)
                self.assertEqual(("pending", None), self._rows(
                    "SELECT status,approval_source FROM candidate_review_drafts "
                    "WHERE source_question_no='2'"
                )[0])
                self.assertEqual("needs_review", self._rows(
                    "SELECT status FROM import_jobs WHERE id=1"
                )[0][0])
                self.assertEqual("旧题干2", self._rows(
                    "SELECT stem_markdown FROM questions WHERE id=?", (self.ids["2"],)
                )[0][0])

    def test_human_approval_requires_current_candidate_sha_and_snapshot(self):
        original = self.db_path.read_bytes()
        mutations = {
            "sha": lambda connection: connection.execute(
                "UPDATE candidate_review_drafts SET source_candidate_sha256=? "
                "WHERE source_question_no='1'", ("f" * 64,)
            ),
            "snapshot": lambda connection: connection.execute(
                "UPDATE candidate_review_drafts SET source_snapshot_json='{}' "
                "WHERE source_question_no='1'"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(binding=label):
                self.db_path.write_bytes(original)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    mutate(connection)
                before = self._business_snapshot()
                with self.assertRaisesRegex(FinalizationError, "绑定|候选"):
                    finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual(before, self._business_snapshot())

    def test_finalize_rejects_symlink_and_hardlink_artifacts_without_writes(self):
        job_dir = self.private_root / "processing" / "import_job_1"
        for filename in ("candidate_questions.json", "ai_audit.json"):
            for kind in ("symlink", "hardlink"):
                with self.subTest(filename=filename, kind=kind):
                    self._write_audit()
                    path = job_dir / filename
                    peer = job_dir / f"{filename}.{kind}"
                    if kind == "symlink":
                        peer.write_bytes(path.read_bytes())
                        path.unlink()
                        path.symlink_to(peer.name)
                    else:
                        os.link(path, peer)
                    before = self._business_snapshot()
                    try:
                        with self.assertRaises(FinalizationError):
                            finalize_review(self.db_path, self.private_root, 1, apply=True)
                        self.assertEqual(before, self._business_snapshot())
                    finally:
                        path.unlink(missing_ok=True)
                        peer.unlink(missing_ok=True)

    def test_apply_rechecks_replace_and_in_place_artifact_changes_before_commit(self):
        import src.reviewing.finalize as finalize_module

        original_loader = finalize_module._load_authoritative_batch
        for filename in ("candidate_questions.json", "ai_audit.json"):
            for mode in ("replace", "in_place"):
                with self.subTest(filename=filename, mode=mode):
                    self._write_audit()
                    before = self._business_snapshot()
                    path = self.private_root / "processing" / "import_job_1" / filename

                    def load_then_change(*args, **kwargs):
                        batch = original_loader(*args, **kwargs)
                        changed = path.read_bytes() + b" "
                        if mode == "replace":
                            replacement = path.with_suffix(".replacement")
                            replacement.write_bytes(changed)
                            os.replace(replacement, path)
                        else:
                            with path.open("r+b") as stream:
                                stream.seek(0)
                                stream.write(changed)
                                stream.truncate()
                        return batch

                    with patch.object(finalize_module, "_load_authoritative_batch",
                                      side_effect=load_then_change):
                        with self.assertRaisesRegex(FinalizationError, "变化|快照"):
                            finalize_review(self.db_path, self.private_root, 1, apply=True)
                    self.assertEqual(before, self._business_snapshot())

    def test_ai_second_pass_requires_current_source_binding_even_when_unedited(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE candidate_review_drafts SET source_candidate_sha256=? "
                "WHERE source_question_no='2'", ("f" * 64,)
            )

        result = finalize_review(self.db_path, self.private_root, 1, apply=True)

        self.assertIn("2", result.pending_question_nos)
        self.assertEqual(("pending", None), self._rows(
            "SELECT status,approval_source FROM candidate_review_drafts "
            "WHERE source_question_no='2'"
        )[0])

    def test_completed_idempotency_requires_artifacts_bindings_and_formal_content(self):
        self._approve_q12_with_formal_question()
        finalize_review(self.db_path, self.private_root, 1, apply=True)
        completed = self.db_path.read_bytes()
        original_audit = (
            self.private_root / "processing" / "import_job_1" / "ai_audit.json"
        ).read_bytes()
        cases = {
            "candidate": lambda connection: self._rewrite_artifact(
                "candidate_questions.json",
                lambda payload: payload["questions"][1].update(stem_markdown="新候选"),
            ),
            "audit": lambda connection: (
                self.private_root / "processing" / "import_job_1" / "ai_audit.json"
            ).write_bytes(original_audit + b" "),
            "binding": lambda connection: connection.execute(
                "UPDATE candidate_review_drafts SET source_candidate_sha256=? "
                "WHERE source_question_no='2'", ("f" * 64,)
            ),
            "formal": lambda connection: connection.execute(
                "UPDATE questions SET stem_markdown='被篡改' WHERE id=?", (self.ids["2"],)
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                self.db_path.write_bytes(completed)
                (self.private_root / "processing" / "import_job_1" /
                 "candidate_questions.json").write_bytes(self.candidate_raw)
                (self.private_root / "processing" / "import_job_1" /
                 "ai_audit.json").write_bytes(original_audit)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    mutate(connection)
                before = self._business_snapshot()
                with self.assertRaises(FinalizationError):
                    finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual(before, self._business_snapshot())

    def test_dry_run_serializes_on_shared_job_lock(self):
        job_dir = self.private_root / "processing" / "import_job_1"
        finished = threading.Event()
        errors = []

        def run_finalize():
            try:
                finalize_review(self.db_path, self.private_root, 1, apply=False)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                finished.set()

        with locked_job(job_dir):
            worker = threading.Thread(target=run_finalize)
            worker.start()
            time.sleep(0.1)
            self.assertFalse(finished.is_set())
        worker.join(timeout=3)
        self.assertTrue(finished.is_set())
        self.assertEqual([], errors)

    def test_dry_run_reports_eligibility_and_changes_nothing(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        result = finalize_review(self.db_path, self.private_root, 1, apply=False)
        self.assertEqual(("2",), result.ai_second_pass_question_nos)
        self.assertEqual(("12",), result.pending_question_nos)
        self.assertEqual(3, result.approved)
        self.assertEqual(3, result.changed_questions)
        self.assertIsNone(result.backup_path)
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertFalse((self.db_path.parent / "backups").exists())

    def test_cli_defaults_to_dry_run(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        completed = subprocess.run(
            [sys.executable, "scripts/finalize_review.py", "--job-id", "1",
             "--database", str(self.db_path), "--private-root", str(self.private_root)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("dry-run", payload["mode"])
        self.assertEqual({"approved": 3, "pending": 1, "human": 2,
                          "ai_second_pass": 1},
                         {key: payload[key] for key in
                          ("approved", "pending", "human", "ai_second_pass")})
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())

    def test_apply_marks_sources_syncs_content_and_preserves_evidence(self):
        result = finalize_review(self.db_path, self.private_root, 1, apply=True)
        self.assertTrue(result.backup_path.is_file())
        self.assertEqual(
            [("1", "approved", "human"), ("2", "approved", "ai_second_pass"),
             ("12", "pending", None), ("22", "approved", "human")],
            self._rows("""SELECT source_question_no,status,approval_source
                         FROM candidate_review_drafts ORDER BY CAST(source_question_no AS INTEGER)"""),
        )
        question = self._rows(
            """SELECT stem_markdown,answer_markdown,analysis_markdown,question_type_code,
                      deleted_at,content_hash FROM questions WHERE id=?""", (self.ids["1"],)
        )[0]
        self.assertEqual(("人工修改题干", "答案1", "解析1", "single_choice", "2026-01-01"), question[:5])
        self.assertEqual(64, len(question[5]))
        self.assertEqual([("A", "新甲", 1), ("B", "新乙", 2)], self._rows(
            "SELECT option_code,content_markdown,display_order FROM question_options WHERE question_id=? ORDER BY display_order",
            (self.ids["1"],),
        ))
        self.assertEqual([("01.01.02",)], self._rows(
            """SELECT k.code FROM question_related_knowledge_points r
               JOIN knowledge_points k ON k.id=r.knowledge_point_id WHERE r.question_id=?""",
            (self.ids["1"],),
        ))
        self.assertEqual(1, self._rows("SELECT COUNT(*) FROM question_assets WHERE question_id=?", (self.ids["1"],))[0][0])
        self.assertEqual(3, self._rows("SELECT COUNT(*) FROM question_reviews WHERE notes LIKE 'finalize_review:%'")[0][0])
        self.assertEqual(3, self._rows("SELECT COUNT(*) FROM question_versions")[0][0])
        self.assertEqual(
            [("needs_review",)],
            self._rows("SELECT status FROM import_jobs WHERE id=1"),
        )
        self.assertEqual([], self._rows("PRAGMA foreign_key_check"))

    def test_apply_completes_job_when_every_draft_is_approved_and_formal(self):
        self._approve_q12_with_formal_question()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE import_jobs SET error_message='旧错误' WHERE id=1"
            )
            before_updated_at = connection.execute(
                "SELECT updated_at FROM import_jobs WHERE id=1"
            ).fetchone()[0]

        result = finalize_review(self.db_path, self.private_root, 1, apply=True)

        self.assertEqual((), result.pending_question_nos)
        status, error_message, updated_at = self._rows(
            "SELECT status,error_message,updated_at FROM import_jobs WHERE id=1"
        )[0]
        self.assertEqual("completed", status)
        self.assertIsNone(error_message)
        self.assertNotEqual(before_updated_at, updated_at)

    def test_deleted_draft_keeps_job_in_needs_review(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE candidate_review_drafts
                   SET deleted_at='2026-07-15T10:00:00+08:00'
                   WHERE import_job_id=1 AND source_question_no='12'"""
            )

        finalize_review(self.db_path, self.private_root, 1, apply=True)

        self.assertEqual(
            [("needs_review",)],
            self._rows("SELECT status FROM import_jobs WHERE id=1"),
        )

    def test_partial_draft_set_is_rejected_without_business_writes(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM candidate_review_drafts WHERE source_question_no IN ('2','12','22')"
            )
        before = self._business_snapshot()

        with self.assertRaisesRegex(FinalizationError, "草稿.*完整|题号集合"):
            finalize_review(self.db_path, self.private_root, 1, apply=True)

        self.assertEqual(before, self._business_snapshot())

    def test_missing_extra_and_empty_draft_sets_fail_closed(self):
        original = self.db_path.read_bytes()
        mutations = {
            "missing": lambda connection: connection.execute(
                "DELETE FROM candidate_review_drafts WHERE source_question_no='12'"
            ),
            "extra": lambda connection: connection.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,status)
                   SELECT import_job_id,'99',source_candidate_sha256,
                          source_snapshot_json,edited_json,'needs_fix'
                   FROM candidate_review_drafts WHERE source_question_no='1'"""
            ),
            "empty": lambda connection: connection.execute(
                "DELETE FROM candidate_review_drafts"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                self.db_path.write_bytes(original)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    mutate(connection)
                before = self._business_snapshot()
                with self.assertRaises(FinalizationError):
                    finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual(before, self._business_snapshot())

    def test_schema_rejects_duplicate_draft_question_numbers(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status)
                       SELECT import_job_id,source_question_no,source_candidate_sha256,
                              source_snapshot_json,edited_json,status
                       FROM candidate_review_drafts WHERE source_question_no='1'"""
                )

    def test_incomplete_candidate_or_audit_batch_fails_closed(self):
        original_candidate = (
            self.private_root / "processing" / "import_job_1" / "candidate_questions.json"
        ).read_bytes()
        original_audit = (
            self.private_root / "processing" / "import_job_1" / "ai_audit.json"
        ).read_bytes()
        cases = (
            ("candidate_count", "candidate_questions.json",
             lambda payload: payload.update(question_count=3)),
            ("candidate_numbers", "candidate_questions.json",
             lambda payload: payload["questions"].pop()),
            ("audit_count", "ai_audit.json",
             lambda payload: payload.update(question_count=3)),
            ("audit_numbers", "ai_audit.json",
             lambda payload: payload["questions"].pop()),
        )
        for label, filename, mutate in cases:
            with self.subTest(case=label):
                candidate_path = self.private_root / "processing" / "import_job_1" / "candidate_questions.json"
                audit_path = self.private_root / "processing" / "import_job_1" / "ai_audit.json"
                candidate_path.write_bytes(original_candidate)
                audit_path.write_bytes(original_audit)
                self._rewrite_artifact(filename, mutate)
                before = self._business_snapshot()
                with self.assertRaises(FinalizationError):
                    finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual(before, self._business_snapshot())

    def test_nonready_draft_states_finalize_eligible_items_but_do_not_complete(self):
        original = self.db_path.read_bytes()
        for status in ("pending", "draft", "needs_fix", "needs_recrop", "deleted"):
            with self.subTest(status=status):
                self.db_path.write_bytes(original)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    if status == "deleted":
                        connection.execute(
                            "UPDATE candidate_review_drafts SET deleted_at='2026-07-15' "
                            "WHERE source_question_no='12'"
                        )
                    else:
                        connection.execute(
                            "UPDATE candidate_review_drafts SET status=? WHERE source_question_no='12'",
                            (status,),
                        )
                result = finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual("needs_review", self._rows(
                    "SELECT status FROM import_jobs WHERE id=1"
                )[0][0])
                self.assertEqual("approved", self._rows(
                    "SELECT status FROM candidate_review_drafts WHERE source_question_no='2'"
                )[0][0])
                self.assertEqual("AI 修改题干", self._rows(
                    "SELECT stem_markdown FROM questions WHERE id=?", (self.ids["2"],)
                )[0][0])
                if status == "deleted":
                    self.assertNotIn("12", result.pending_question_nos)
                else:
                    self.assertIn("12", result.pending_question_nos)

    def test_complex_subquestions_keep_hierarchy_and_answers(self):
        finalize_review(self.db_path, self.private_root, 1, apply=True)
        rows = self._rows(
            """SELECT display_order,stem_markdown,answer_markdown,analysis_markdown
               FROM subquestions WHERE question_id=? ORDER BY display_order""", (self.ids["22"],)
        )
        self.assertEqual(
            ["（1） 第一问", "（2） 公共条件", "（2）① 第一小问", "（2）② 第二小问"],
            [row[1] for row in rows],
        )
        self.assertEqual([f"小问答案{x}" for x in range(1, 5)], [row[2] for row in rows])
        self.assertEqual([f"小问解析{x}" for x in range(1, 5)], [row[3] for row in rows])

    def test_apply_is_idempotent(self):
        self._approve_q12_with_formal_question()
        first = finalize_review(self.db_path, self.private_root, 1, apply=True)
        versions = self._rows("SELECT id,question_id,version_no,snapshot_json FROM question_versions")
        reviews = self._rows("SELECT id,question_id,notes FROM question_reviews")
        draft_versions = self._rows("SELECT source_question_no,version FROM candidate_review_drafts ORDER BY id")
        second = finalize_review(self.db_path, self.private_root, 1, apply=True)
        self.assertEqual("completed", self._rows(
            "SELECT status FROM import_jobs WHERE id=1"
        )[0][0])
        self.assertEqual(0, second.changed_questions)
        self.assertEqual(versions, self._rows("SELECT id,question_id,version_no,snapshot_json FROM question_versions"))
        self.assertEqual(reviews, self._rows("SELECT id,question_id,notes FROM question_reviews"))
        self.assertEqual(draft_versions, self._rows("SELECT source_question_no,version FROM candidate_review_drafts ORDER BY id"))
        self.assertNotEqual(first.backup_path, second.backup_path)

    def test_non_admitted_job_statuses_are_rejected_before_any_business_write(self):
        original = self.db_path.read_bytes()
        for status in ("failed", "pending", "processing"):
            with self.subTest(status=status):
                self.db_path.write_bytes(original)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        "UPDATE import_jobs SET status=?,error_message='boom' WHERE id=1",
                        (status,),
                    )
                before = self._business_snapshot()
                with self.assertRaisesRegex(FinalizationError, "任务状态"):
                    finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual(before, self._business_snapshot())

    def test_completed_job_rejects_non_idempotent_draft_states_without_writes(self):
        self._approve_q12_with_formal_question()
        finalize_review(self.db_path, self.private_root, 1, apply=True)
        completed = self.db_path.read_bytes()
        mutations = {
            "needs_fix": lambda connection: connection.execute(
                "UPDATE candidate_review_drafts SET status='needs_fix' WHERE source_question_no='12'"
            ),
            "needs_recrop": lambda connection: connection.execute(
                "UPDATE candidate_review_drafts SET status='needs_recrop' WHERE source_question_no='12'"
            ),
            "deleted": lambda connection: connection.execute(
                "UPDATE candidate_review_drafts SET deleted_at='2026-07-15' WHERE source_question_no='12'"
            ),
            "missing": lambda connection: connection.execute(
                "DELETE FROM candidate_review_drafts WHERE source_question_no='12'"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                self.db_path.write_bytes(completed)
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    mutate(connection)
                before = self._business_snapshot()
                with self.assertRaisesRegex(FinalizationError, "completed|已完成|一致"):
                    finalize_review(self.db_path, self.private_root, 1, apply=True)
                self.assertEqual(before, self._business_snapshot())

    def test_missing_formal_question_fails_without_partial_changes(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DELETE FROM question_sources WHERE source_question_no='2'")
            connection.execute("DELETE FROM questions WHERE id=?", (self.ids["2"],))
        before = self._rows("SELECT source_question_no,status,approval_source,version FROM candidate_review_drafts ORDER BY id")
        with self.assertRaisesRegex(FinalizationError, "正式题不存在"):
            finalize_review(self.db_path, self.private_root, 1, apply=True)
        self.assertEqual(before, self._rows("SELECT source_question_no,status,approval_source,version FROM candidate_review_drafts ORDER BY id"))

    def test_transaction_rolls_back_all_business_writes_on_sync_failure(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_finalize BEFORE UPDATE OF stem_markdown ON questions
                   WHEN NEW.source_question_no='22' BEGIN SELECT RAISE(ABORT,'forced'); END"""
            )
        before = self._rows("SELECT source_question_no,status,approval_source,version FROM candidate_review_drafts ORDER BY id")
        with self.assertRaises(sqlite3.IntegrityError):
            finalize_review(self.db_path, self.private_root, 1, apply=True)
        self.assertEqual(before, self._rows("SELECT source_question_no,status,approval_source,version FROM candidate_review_drafts ORDER BY id"))
        self.assertEqual(0, self._rows("SELECT COUNT(*) FROM question_versions")[0][0])
        self.assertEqual(0, self._rows("SELECT COUNT(*) FROM question_reviews WHERE notes LIKE 'finalize_review:%'")[0][0])

    def test_real_foreign_key_violation_rolls_back_transaction_business_writes(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO question_related_knowledge_points VALUES(?,999999)",
                (999999,),
            )
        before = self._business_snapshot()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "外键检查失败"):
            finalize_review(self.db_path, self.private_root, 1, apply=True)

        self.assertEqual(before, self._business_snapshot())
        self.assertNotEqual([], self._rows("PRAGMA foreign_key_check"))

    def test_failure_after_job_status_update_rolls_back_all_business_writes(self):
        self._approve_q12_with_formal_question()
        before_job = self._rows(
            "SELECT status,error_message,updated_at FROM import_jobs WHERE id=1"
        )
        before_questions = self._rows(
            "SELECT id,stem_markdown,content_hash,updated_at FROM questions ORDER BY id"
        )
        before_drafts = self._rows(
            """SELECT id,status,approval_source,approval_evidence_json,version,updated_at
               FROM candidate_review_drafts ORDER BY id"""
        )

        with patch(
            "src.reviewing.finalize._foreign_key_violations",
            return_value=[("forced", 1, "parent", 0)],
            create=True,
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "外键检查失败"):
                finalize_review(self.db_path, self.private_root, 1, apply=True)

        self.assertEqual(before_job, self._rows(
            "SELECT status,error_message,updated_at FROM import_jobs WHERE id=1"
        ))
        self.assertEqual(before_questions, self._rows(
            "SELECT id,stem_markdown,content_hash,updated_at FROM questions ORDER BY id"
        ))
        self.assertEqual(before_drafts, self._rows(
            """SELECT id,status,approval_source,approval_evidence_json,version,updated_at
               FROM candidate_review_drafts ORDER BY id"""
        ))
        self.assertEqual(0, self._rows("SELECT COUNT(*) FROM question_versions")[0][0])
        self.assertEqual(0, self._rows(
            "SELECT COUNT(*) FROM question_reviews WHERE notes LIKE 'finalize_review:%'"
        )[0][0])
        self.assertEqual(
            1,
            len(list((self.db_path.parent / "backups").glob(
                "question-bank-before-finalize-*.db"
            ))),
            "事务外备份是保留的诊断工件，不属于数据库业务写入回滚范围",
        )


if __name__ == "__main__":
    unittest.main()
