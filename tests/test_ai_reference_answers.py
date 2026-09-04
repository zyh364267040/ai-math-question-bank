import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.importing.admit_questions import admit_questions
from src.processing.ai_reference_answers import (
    AiReferenceAnswerError,
    import_ai_reference_answers,
)
from src.web.app import _valid_ai_reference_answer, create_app
from tests.fixture_factory import (
    anchor_synthetic_candidate_audit,
    anchor_synthetic_figure_reviews,
    create_import_job_fixture,
)


class AiReferenceAnswerImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prototype = tempfile.TemporaryDirectory()
        root = Path(cls.prototype.name)
        cls.prototype_private = root / "private"
        job_dir = create_import_job_fixture(cls.prototype_private)
        cls.prototype_db = cls.prototype_private / "question-bank.db"
        initialize_database(cls.prototype_db).close()
        with sqlite3.connect(cls.prototype_db) as connection:
            paper_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'synthetic.pdf','raw_papers/TJ/2025/synthetic.pdf',
                           'TJ',2025,'YK','合成测试卷')""",
                ("b" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) "
                "VALUES(1,?,1,4,'needs_review')",
                (paper_id,),
            )
        anchor_synthetic_candidate_audit(cls.prototype_db, job_dir)
        anchor_synthetic_figure_reviews(cls.prototype_db, cls.prototype_private)
        admit_questions(cls.prototype_db, cls.prototype_private, 1)
        with sqlite3.connect(cls.prototype_db) as connection:
            connection.execute(
                "UPDATE import_answer_sources SET source_answer_state='source_has_no_answer',"
                "answer_page_start=NULL,answer_page_end=NULL,render_manifest_sha256=NULL "
                "WHERE import_job_id=1"
            )

    @classmethod
    def tearDownClass(cls):
        cls.prototype.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "question-bank.db"
        shutil.copy2(self.prototype_db, self.db)
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT q.question_code,q.content_hash
                   FROM questions q JOIN question_sources qs ON qs.question_id=q.id
                   WHERE qs.source_question_no IN ('1','2','3','4','5','6','7','8','21','22')
                   ORDER BY q.id"""
            ).fetchall()
            self.formal_suborders = {
                row[0]: [item[0] for item in connection.execute(
                    "SELECT display_order FROM subquestions s JOIN questions q "
                    "ON q.id=s.question_id WHERE q.question_code=? ORDER BY display_order",
                    (row[0],),
                )]
                for row in rows
            }
        self.source = {
            "schema_version": 1,
            "questions": [
                {"question_code": row[0], "question_content_hash": row[1]}
                for row in rows
            ],
        }
        generated = []
        independent = []
        review = []
        for index, item in enumerate(self.source["questions"]):
            code = item["question_code"]
            subs = [
                {
                    "display_order": order,
                    "answer_markdown": f"生成小问答案 {index}-{order}",
                    "analysis_markdown": f"生成小问解析 {index}-{order}",
                }
                for order in self.formal_suborders[code]
            ]
            generated.append({
                **item,
                "answer_markdown": f"生成答案 {index} <script>bad()</script>",
                "analysis_markdown": f"生成解析 {index}",
                "subquestions": subs,
            })
            independent.append({
                **item,
                "answer_markdown": f"独立答案 {index}",
                "analysis_markdown": f"独立解析 {index}",
                "subquestions": [dict(sub) for sub in subs],
            })
            review.append({
                **item,
                "decision": "unresolved" if index == 9 else "passed",
                "notes": "合成复核备注",
                "answer_markdown": "" if index == 9 else f"最终答案 {index} <em>reviewed</em>",
                "analysis_markdown": "" if index == 9 else f"最终解析 {index}",
                "subquestions": [] if index == 9 else [
                    {
                        "display_order": order,
                        "answer_markdown": f"最终小问答案 {index}-{order}",
                        "analysis_markdown": f"最终小问解析 {index}-{order}",
                    }
                    for order in self.formal_suborders[code]
                ],
            })
        self.generator = {"schema_version": 1, "model": "generator-test", "questions": generated}
        self.independent = {"schema_version": 1, "model": "solver-test", "questions": independent}
        self.final_review = {"schema_version": 1, "model": "reviewer-test", "questions": review}
        self.paths = self._write_payloads()

    def tearDown(self):
        self.temp.cleanup()

    def _write_payloads(self):
        paths = []
        for name, payload in (
            ("source", self.source), ("generator", self.generator),
            ("independent", self.independent), ("final-review", self.final_review),
        ):
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            paths.append(path)
        return paths

    def _run(self, *, apply=False):
        self.paths = self._write_payloads()
        return import_ai_reference_answers(self.db, *self.paths, apply=apply)

    def _count(self):
        with sqlite3.connect(self.db) as connection:
            return connection.execute("SELECT COUNT(*) FROM ai_reference_answers").fetchone()[0]

    def test_schema_is_relational_constrained_and_migration_idempotent(self):
        initialize_database(self.db).close()
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("ai_reference_answers", tables)
            self.assertIn("ai_reference_subquestion_answers", tables)
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(ai_reference_answers)"
            )}
            self.assertTrue({
                "question_id", "question_content_hash", "answer_markdown",
                "analysis_markdown", "generator_model", "independent_model",
                "final_review_model", "review_decision", "review_notes",
                "source_sha256", "generator_sha256", "independent_sha256",
                "final_review_sha256", "created_at",
            }.issubset(columns))
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(ai_reference_answers)"
            ).fetchall()
            self.assertTrue(any(row[2] == "questions" for row in foreign_keys))
            triggers = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )}
            self.assertTrue({
                "ai_reference_subquestion_validate_insert",
                "ai_reference_subquestion_validate_update",
            }.issubset(triggers))

    def test_subquestion_binding_constraints_reject_real_insert_and_updates(self):
        self._run(apply=True)
        with sqlite3.connect(self.db) as connection:
            source_question_id = connection.execute(
                "SELECT id FROM questions WHERE question_code=?",
                (self.source["questions"][0]["question_code"],),
            ).fetchone()[0]
            source_answer_id = connection.execute(
                "SELECT id FROM ai_reference_answers WHERE question_id=?",
                (source_question_id,),
            ).fetchone()[0]
            solution_answer_id, solution_subquestion_id, child_id = connection.execute(
                """SELECT a.id,s.id,asa.id
                   FROM ai_reference_answers a
                   JOIN subquestions s ON s.question_id=a.question_id
                   JOIN ai_reference_subquestion_answers asa
                     ON asa.ai_reference_answer_id=a.id
                    AND asa.subquestion_id=s.id
                   ORDER BY asa.display_order LIMIT 1"""
            ).fetchone()
            foreign_subquestion_id = connection.execute(
                """INSERT INTO subquestions
                   (question_id,display_order,stem_markdown)
                   VALUES(?,1,'跨题测试小问')""",
                (source_question_id,),
            ).lastrowid

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "AI reference subquestion question mismatch",
            ):
                connection.execute(
                    """INSERT INTO ai_reference_subquestion_answers
                       (ai_reference_answer_id,subquestion_id,display_order,
                        answer_markdown,analysis_markdown)
                       VALUES(?,?,1,'错误答案','错误解析')""",
                    (source_answer_id, solution_subquestion_id),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "AI reference subquestion display_order mismatch",
            ):
                connection.execute(
                    """INSERT INTO ai_reference_subquestion_answers
                       (ai_reference_answer_id,subquestion_id,display_order,
                        answer_markdown,analysis_markdown)
                       VALUES(?,?,2,'错误答案','错误解析')""",
                    (source_answer_id, foreign_subquestion_id),
                )
            for column, value in (
                ("subquestion_id", foreign_subquestion_id),
                ("ai_reference_answer_id", source_answer_id),
            ):
                with self.subTest(update_column=column), self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "AI reference subquestion question mismatch",
                ):
                    connection.execute(
                        f"UPDATE ai_reference_subquestion_answers SET {column}=? WHERE id=?",
                        (value, child_id),
                    )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "AI reference subquestion display_order mismatch",
            ):
                connection.execute(
                    "UPDATE ai_reference_subquestion_answers SET display_order=99 WHERE id=?",
                    (child_id,),
                )

            self.assertEqual(
                (solution_answer_id, solution_subquestion_id, 1),
                connection.execute(
                    """SELECT ai_reference_answer_id,subquestion_id,display_order
                       FROM ai_reference_subquestion_answers WHERE id=?""",
                    (child_id,),
                ).fetchone(),
            )

    def test_dry_run_then_apply_nine_skip_one_and_repeat_is_unchanged(self):
        dry_run = self._run()
        self.assertEqual({"inserted": 9, "unchanged": 0, "skipped": 1, "applied": False}, dry_run)
        self.assertEqual(0, self._count())
        official_before = self._official_snapshot()
        applied = self._run(apply=True)
        self.assertEqual({"inserted": 9, "unchanged": 0, "skipped": 1, "applied": True}, applied)
        self.assertEqual(9, self._count())
        self.assertEqual(official_before, self._official_snapshot())
        repeated = self._run(apply=True)
        self.assertEqual({"inserted": 0, "unchanged": 9, "skipped": 1, "applied": True}, repeated)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(3, connection.execute(
                "SELECT COUNT(*) FROM ai_reference_subquestion_answers"
            ).fetchone()[0])

    def test_accepts_one_and_three_question_batches_for_dry_run_and_apply(self):
        originals = tuple(copy.deepcopy(payload) for payload in (
            self.source, self.generator, self.independent, self.final_review,
        ))
        for question_count in (1, 3):
            with self.subTest(question_count=question_count):
                (
                    self.source, self.generator, self.independent,
                    self.final_review,
                ) = tuple(copy.deepcopy(payload) for payload in originals)
                db_copy = self.root / f"batch-{question_count}.db"
                shutil.copy2(self.db, db_copy)
                for payload in (
                    self.source, self.generator, self.independent,
                    self.final_review,
                ):
                    payload["questions"] = payload["questions"][:question_count]
                paths = self._write_payloads()

                dry_run = import_ai_reference_answers(db_copy, *paths)
                self.assertEqual({
                    "inserted": question_count,
                    "unchanged": 0,
                    "skipped": 0,
                    "applied": False,
                }, dry_run)
                with sqlite3.connect(db_copy) as connection:
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM ai_reference_answers"
                    ).fetchone()[0])

                applied = import_ai_reference_answers(
                    db_copy, *paths, apply=True
                )
                self.assertEqual({
                    "inserted": question_count,
                    "unchanged": 0,
                    "skipped": 0,
                    "applied": True,
                }, applied)
                with sqlite3.connect(db_copy) as connection:
                    self.assertEqual(question_count, connection.execute(
                        "SELECT COUNT(*) FROM ai_reference_answers"
                    ).fetchone()[0])

    def test_rejects_zero_and_eleven_question_batches_without_writes(self):
        originals = tuple(copy.deepcopy(payload) for payload in (
            self.source, self.generator, self.independent, self.final_review,
        ))
        for question_count in (0, 11):
            with self.subTest(question_count=question_count):
                (
                    self.source, self.generator, self.independent,
                    self.final_review,
                ) = tuple(copy.deepcopy(payload) for payload in originals)
                for payload in (
                    self.source, self.generator, self.independent,
                    self.final_review,
                ):
                    questions = payload["questions"]
                    payload["questions"] = (
                        [] if question_count == 0
                        else questions + [copy.deepcopy(questions[0])]
                    )
                with self.assertRaisesRegex(
                    AiReferenceAnswerError, "between 1 and 10"
                ):
                    self._run(apply=True)
                self.assertEqual(0, self._count())

    def _official_snapshot(self):
        with sqlite3.connect(self.db) as connection:
            return (
                connection.execute(
                    "SELECT id,answer_markdown,analysis_markdown,answer_status FROM questions ORDER BY id"
                ).fetchall(),
                connection.execute(
                    "SELECT id,answer_markdown,analysis_markdown,answer_status FROM subquestions ORDER BY id"
                ).fetchall(),
                connection.execute(
                    "SELECT import_job_id,source_answer_state FROM import_answer_sources ORDER BY import_job_id"
                ).fetchall(),
            )

    def test_rejects_every_source_gate_and_content_hash_mismatch(self):
        code = self.source["questions"][0]["question_code"]
        cases = (
            ("UPDATE questions SET deleted_at='2026-01-01' WHERE question_code=?", "deleted"),
            ("DELETE FROM question_sources WHERE question_id=(SELECT id FROM questions WHERE question_code=?)", "formal"),
            ("UPDATE import_answer_sources SET source_answer_state='source_answer_linked',answer_page_start=1,answer_page_end=1,render_manifest_sha256='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE import_job_id=1", "source_has_no_answer"),
            ("UPDATE import_answer_sources SET source_answer_state='source_has_answer_unprocessed',answer_page_start=1,answer_page_end=1,render_manifest_sha256='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE import_job_id=1", "source_has_no_answer"),
            ("DELETE FROM import_answer_sources WHERE import_job_id=1", "source_has_no_answer"),
        )
        for sql, marker in cases:
            with self.subTest(marker=marker):
                db_copy = self.root / f"gate-{marker}-{len(list(self.root.glob('gate-*')))}.db"
                shutil.copy2(self.db, db_copy)
                with sqlite3.connect(db_copy) as connection:
                    connection.execute(sql, (code,) if "?" in sql else ())
                with self.assertRaisesRegex(AiReferenceAnswerError, marker):
                    import_ai_reference_answers(db_copy, *self.paths, apply=True)
        for payload in (self.source, self.generator, self.independent, self.final_review):
            payload["questions"][0]["question_content_hash"] = "f" * 64
        with self.assertRaisesRegex(AiReferenceAnswerError, "content_hash"):
            self._run(apply=True)

    def test_strict_coverage_order_shape_lengths_and_subquestions(self):
        mutations = (
            (lambda: self.source["questions"].pop(), "coverage"),
            (lambda: self.generator["questions"].pop(), "coverage"),
            (lambda: self.independent["questions"].reverse(), "coverage"),
            (lambda: self.final_review["questions"].reverse(), "order"),
            (lambda: self.generator["questions"][0].update({
                "question_code": self.source["questions"][1]["question_code"],
            }), "coverage"),
            (lambda: self.independent["questions"][0].update({
                "question_content_hash": "f" * 64,
            }), "coverage"),
            (lambda: self.generator.update({"unexpected": True}), "unknown"),
            (lambda: self.generator["questions"][0].update({"answer_markdown": ""}), "non-empty"),
            (lambda: self.generator["questions"][0].update({"answer_markdown": "x" * 50001}), "length"),
            (lambda: self.generator["questions"][-1]["subquestions"].pop(), "subquestion"),
            (lambda: self.final_review["questions"][0].pop("answer_markdown"), "missing"),
            (lambda: self.final_review["questions"][0].update({"answer_markdown": ""}), "non-empty"),
            (lambda: self.final_review["questions"][0].update({"analysis_markdown": "   "}), "non-empty"),
            (lambda: self.final_review["questions"][0]["subquestions"].append({
                "display_order": 99,
                "answer_markdown": "额外最终小问答案",
                "analysis_markdown": "额外最终小问解析",
            }), "subquestion"),
            (lambda: self.final_review["questions"][-1].update({"answer_markdown": "不应存在"}), "empty"),
            (lambda: self.final_review["questions"][-1].update({"analysis_markdown": "不应存在"}), "empty"),
            (lambda: self.final_review["questions"][-1]["subquestions"].append({
                "display_order": 1,
                "answer_markdown": "不应存在",
                "analysis_markdown": "不应存在",
            }), "empty"),
        )
        for mutate, marker in mutations:
            with self.subTest(marker=marker):
                originals = tuple(copy.deepcopy(value) for value in (
                    self.source, self.generator, self.independent, self.final_review
                ))
                mutate()
                with self.assertRaisesRegex(AiReferenceAnswerError, marker):
                    self._run(apply=True)
                self.assertEqual(0, self._count())
                self.source, self.generator, self.independent, self.final_review = originals

    def test_nonpassed_records_never_write_and_invalid_decision_rejects(self):
        self.final_review["questions"][0]["decision"] = "failed"
        self.final_review["questions"][0].update(
            answer_markdown="", analysis_markdown="", subquestions=[]
        )
        result = self._run(apply=True)
        self.assertEqual(8, result["inserted"])
        self.assertEqual(2, result["skipped"])
        with sqlite3.connect(self.db) as connection:
            code = self.source["questions"][0]["question_code"]
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM ai_reference_answers a JOIN questions q ON q.id=a.question_id "
                "WHERE q.question_code=?", (code,)
            ).fetchone())
        self.final_review["questions"][0]["decision"] = "approved"
        with self.assertRaisesRegex(AiReferenceAnswerError, "decision"):
            self._run(apply=True)

    def test_repeat_cannot_silently_demote_the_only_stored_answer(self):
        for item in self.final_review["questions"][1:]:
            item.update(
                decision="unresolved", answer_markdown="",
                analysis_markdown="", subquestions=[],
            )
        self._run(apply=True)
        self.assertEqual(1, self._count())
        self.final_review["questions"][0].update(
            decision="failed", answer_markdown="",
            analysis_markdown="", subquestions=[],
        )
        with self.assertRaisesRegex(AiReferenceAnswerError, "conflict"):
            self._run(apply=True)
        self.assertEqual(1, self._count())

    def test_only_final_review_content_is_persisted_and_rendered(self):
        self.generator["questions"][0].update(
            answer_markdown="错误生成答案-禁止展示",
            analysis_markdown="错误生成解析-禁止展示",
        )
        self.independent["questions"][0].update(
            answer_markdown="独立证据答案-禁止展示",
            analysis_markdown="独立证据解析-禁止展示",
        )
        self.final_review["questions"][0].update(
            answer_markdown="复核纠正后的最终答案",
            analysis_markdown="复核纠正后的最终解析",
        )
        self._run(apply=True)
        code = self.source["questions"][0]["question_code"]
        with sqlite3.connect(self.db) as connection:
            stored = connection.execute(
                """SELECT a.answer_markdown,a.analysis_markdown
                   FROM ai_reference_answers a JOIN questions q ON q.id=a.question_id
                   WHERE q.question_code=?""",
                (code,),
            ).fetchone()
        self.assertEqual(
            ("复核纠正后的最终答案", "复核纠正后的最终解析"), stored
        )
        private = self.root / "web-private"
        private.mkdir()
        with TestClient(create_app(self.db, private)) as client:
            page = client.get(f"/questions/{code}").text
        self.assertIn("复核纠正后的最终答案", page)
        self.assertIn("复核纠正后的最终解析", page)
        self.assertNotIn("错误生成答案-禁止展示", page)
        self.assertNotIn("错误生成解析-禁止展示", page)
        self.assertNotIn("独立证据答案-禁止展示", page)
        self.assertNotIn("独立证据解析-禁止展示", page)

    def test_conflict_rolls_back_whole_batch_for_content_or_evidence_change(self):
        self._run(apply=True)
        with sqlite3.connect(self.db) as connection:
            before = connection.execute(
                "SELECT question_id,answer_markdown,source_sha256,generator_sha256 FROM ai_reference_answers ORDER BY question_id"
            ).fetchall()
        self.final_review["questions"][0]["answer_markdown"] = "不同的最终答案"
        with self.assertRaisesRegex(AiReferenceAnswerError, "conflict"):
            self._run(apply=True)
        with sqlite3.connect(self.db) as connection:
            after = connection.execute(
                "SELECT question_id,answer_markdown,source_sha256,generator_sha256 FROM ai_reference_answers ORDER BY question_id"
            ).fetchall()
        self.assertEqual(before, after)

        self.final_review["questions"][0]["answer_markdown"] = "最终答案 0 <em>reviewed</em>"
        self.independent["questions"][0]["analysis_markdown"] = "证据变化"
        with self.assertRaisesRegex(AiReferenceAnswerError, "conflict"):
            self._run(apply=True)
        self.assertEqual(9, self._count())

    def test_each_evidence_file_hash_change_conflicts_without_semantic_change(self):
        self._run(apply=True)
        for index, name in enumerate((
            "source", "generator", "independent", "final_review"
        )):
            with self.subTest(evidence=name):
                db_copy = self.root / f"evidence-{index}.db"
                shutil.copy2(self.db, db_copy)
                paths = self._write_payloads()
                original = paths[index].read_bytes()
                paths[index].write_bytes(original + b"\n")
                with self.assertRaisesRegex(AiReferenceAnswerError, "conflict"):
                    import_ai_reference_answers(db_copy, *paths, apply=True)
                with sqlite3.connect(db_copy) as connection:
                    self.assertEqual(9, connection.execute(
                        "SELECT COUNT(*) FROM ai_reference_answers"
                    ).fetchone()[0])

    def test_existing_subquestion_content_or_coverage_drift_is_conflict(self):
        self._run(apply=True)
        with sqlite3.connect(self.db) as connection:
            row_id = connection.execute(
                "SELECT id FROM ai_reference_subquestion_answers LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE ai_reference_subquestion_answers "
                "SET answer_markdown='被篡改的小问答案' WHERE id=?", (row_id,)
            )
        with self.assertRaisesRegex(AiReferenceAnswerError, "conflict"):
            self._run(apply=True)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("被篡改的小问答案", connection.execute(
                "SELECT answer_markdown FROM ai_reference_subquestion_answers WHERE id=?",
                (row_id,),
            ).fetchone()[0])

    def test_sha256_is_recomputed_from_all_four_file_bytes(self):
        result = self._run(apply=True)
        self.assertEqual(9, result["inserted"])
        expected = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths]
        with sqlite3.connect(self.db) as connection:
            actual = connection.execute(
                """SELECT source_sha256,generator_sha256,independent_sha256,
                          final_review_sha256 FROM ai_reference_answers LIMIT 1"""
            ).fetchone()
        self.assertEqual(tuple(expected), actual)

    def test_rejects_same_generator_and_independent_model_without_writes(self):
        self.independent["model"] = self.generator["model"]
        with self.assertRaisesRegex(
            AiReferenceAnswerError, "generator and independent models"
        ):
            self._run(apply=True)
        self.assertEqual(0, self._count())

    def test_rejects_model_identifiers_with_surrounding_whitespace_without_writes(self):
        for payload_name, value in (
            ("generator", " generator-test"),
            ("independent", "solver-test "),
            ("final_review", " reviewer-test "),
        ):
            with self.subTest(payload=payload_name):
                payload = getattr(self, payload_name)
                original = payload["model"]
                payload["model"] = value
                try:
                    with self.assertRaisesRegex(AiReferenceAnswerError, "model"):
                        self._run(apply=True)
                    self.assertEqual(0, self._count())
                finally:
                    payload["model"] = original
                    with sqlite3.connect(self.db) as connection:
                        connection.execute("DELETE FROM ai_reference_subquestion_answers")
                        connection.execute("DELETE FROM ai_reference_answers")

    def test_same_model_with_whitespace_cannot_bypass_independence_gate(self):
        self.independent["model"] = f" {self.generator['model']} "
        with self.assertRaisesRegex(AiReferenceAnswerError, "model"):
            self._run(apply=True)
        self.assertEqual(0, self._count())

    def test_schema_version_requires_exact_json_integer_one_without_writes(self):
        for payload_name in ("source", "generator", "independent", "final_review"):
            for invalid in (True, 1.0):
                with self.subTest(payload=payload_name, invalid=invalid):
                    payload = getattr(self, payload_name)
                    payload["schema_version"] = invalid
                    try:
                        with self.assertRaisesRegex(
                            AiReferenceAnswerError, "schema_version"
                        ):
                            self._run(apply=True)
                        self.assertEqual(0, self._count())
                    finally:
                        payload["schema_version"] = 1
                        with sqlite3.connect(self.db) as connection:
                            connection.execute("DELETE FROM ai_reference_subquestion_answers")
                            connection.execute("DELETE FROM ai_reference_answers")

    def test_rejects_duplicate_input_file_sha256_without_writes(self):
        paths = self._write_payloads()
        with self.assertRaisesRegex(
            AiReferenceAnswerError, "input files have duplicate SHA-256"
        ):
            import_ai_reference_answers(
                self.db, paths[0], paths[1], paths[1], paths[3], apply=True
            )
        self.assertEqual(0, self._count())

    def test_final_review_may_use_the_independent_model(self):
        self.final_review["model"] = self.independent["model"]
        result = self._run(apply=True)
        self.assertEqual(9, result["inserted"])
        self.assertEqual(9, self._count())

    def test_cli_defaults_to_dry_run_and_requires_explicit_apply(self):
        command = [
            sys.executable, "scripts/import_ai_reference_answers.py",
            "--database", str(self.db), *(str(path) for path in self.paths),
        ]
        dry_run = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[1], text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(0, dry_run.returncode, dry_run.stderr)
        self.assertIn("DRY RUN", dry_run.stdout)
        self.assertEqual(0, self._count())
        applied = subprocess.run(
            [*command, "--apply"], cwd=Path(__file__).resolve().parents[1],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("APPLIED", applied.stdout)
        self.assertEqual(9, self._count())

    def test_dry_run_does_not_create_a_missing_database_file(self):
        missing = self.root / "missing.db"
        with self.assertRaisesRegex(AiReferenceAnswerError, "database"):
            import_ai_reference_answers(missing, *self.paths)
        self.assertFalse(missing.exists())


class AiReferenceAnswerWebTests(unittest.TestCase):
    _write_payloads = AiReferenceAnswerImportTests._write_payloads
    _run = AiReferenceAnswerImportTests._run

    @classmethod
    def setUpClass(cls):
        AiReferenceAnswerImportTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        AiReferenceAnswerImportTests.tearDownClass()

    def setUp(self):
        self.prototype_db = AiReferenceAnswerImportTests.prototype_db
        AiReferenceAnswerImportTests.setUp(self)
        self._run(apply=True)
        self.private = self.root / "private"
        self.private.mkdir()
        self.client = TestClient(create_app(self.db, self.private))

    def tearDown(self):
        self.client.close()
        AiReferenceAnswerImportTests.tearDown(self)

    def _code(self, source_number):
        with sqlite3.connect(self.db) as connection:
            return connection.execute(
                """SELECT q.question_code FROM questions q
                   JOIN question_sources qs ON qs.question_id=q.id
                   WHERE qs.source_question_no=?""",
                (str(source_number),),
            ).fetchone()[0]

    def _csrf(self, path="/questions"):
        import re
        match = re.search(
            r'name="csrf_token" value="([^"]+)"', self.client.get(path).text
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def _post(self, path, data=None, *, json_response=False):
        values = dict(data or {})
        values["csrf_token"] = self._csrf()
        headers = {"Accept": "application/json"} if json_response else {}
        return self.client.post(
            path, data=values, headers=headers, follow_redirects=False
        )

    def _assert_ai_hidden_after_drift(self, code, *, answer_marker="最终答案 0",
                                      detail_status=200, basket_available=True):
        listing = self.client.get("/questions").text
        if code in listing:
            offset = listing.index(code)
            card = listing[
                listing.rfind("<article", 0, offset):listing.find(
                    "</article>", offset
                )
            ]
            self.assertNotIn("有AI参考答案", card)

        detail = self.client.get(f"/questions/{code}")
        self.assertEqual(detail_status, detail.status_code)
        self.assertNotIn("<h2>AI参考答案</h2>", detail.text)
        self.assertNotIn(answer_marker, detail.text)

        preview = self._post(
            "/basket/preview", {"include_ai_answers": "on"},
            json_response=True,
        )
        export = self._post(
            "/basket/export", {"include_ai_answers": "on"}
        )
        if basket_available:
            self.assertEqual(200, preview.status_code)
            self.assertNotIn("AI参考答案", preview.json()["html"])
            self.assertNotIn(answer_marker, preview.json()["html"])
            self.assertEqual(303, export.status_code)
            markdown = self._last_export_text()
            self.assertNotIn("AI参考答案", markdown)
            self.assertNotIn(answer_marker, markdown)
        else:
            self.assertEqual(400, preview.status_code)
            self.assertEqual(400, export.status_code)

    def test_detail_shows_approved_block_exact_title_disclaimer_and_escaped_html(self):
        detail = self.client.get(f"/questions/{self._code(1)}")
        self.assertEqual(200, detail.status_code)
        self.assertIn("<h2>AI参考答案</h2>", detail.text)
        self.assertNotIn("AI参考答案（非官方）", detail.text)
        self.assertIn("AI生成并经复核，不是原卷官方答案", detail.text)
        self.assertIn("最终答案 0 &lt;em&gt;reviewed&lt;/em&gt;", detail.text)
        self.assertNotIn("<em>reviewed</em>", detail.text)
        self.assertNotIn("生成答案 0", detail.text)
        self.assertNotIn("独立答案 0", detail.text)

    def test_detail_hides_empty_block_and_solution_uses_bound_display_order(self):
        unresolved = self.client.get(f"/questions/{self._code(22)}").text
        self.assertNotIn("AI参考答案", unresolved)
        solution = self.client.get(f"/questions/{self._code(21)}").text
        self.assertIn("<h2>AI参考答案</h2>", solution)
        for order in (1, 2, 3):
            self.assertIn(f"最终小问答案 8-{order}", solution)
            self.assertIn(f"最终小问解析 8-{order}", solution)
        self.assertLess(
            solution.index("最终小问答案 8-1"),
            solution.index("最终小问答案 8-3"),
        )

    def test_authoritative_nested_labels_survive_detail_preview_and_markdown(self):
        code = self._code(21)
        expected_labels = ("（1）", "（2）（i）", "（2）（ii）")
        with sqlite3.connect(self.db) as connection:
            question_id = connection.execute(
                "SELECT id FROM questions WHERE question_code=?", (code,)
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT id FROM subquestions WHERE question_id=? ORDER BY display_order",
                (question_id,),
            ).fetchall()
            for row, label in zip(rows, expected_labels):
                connection.execute(
                    "UPDATE subquestions SET stem_markdown=? WHERE id=?",
                    (f"{label} 权威小问题干", row[0]),
                )

        detail = self.client.get(f"/questions/{code}")
        self.assertEqual(200, detail.status_code)
        self._post(f"/basket/add/{code}")
        preview = self._post(
            "/basket/preview", {"include_ai_answers": "on"},
            json_response=True,
        )
        self.assertEqual(200, preview.status_code)
        exported = self._post(
            "/basket/export", {"include_ai_answers": "on"}
        )
        self.assertEqual(303, exported.status_code)

        detail_ai = detail.text.split(
            '<section class="ai-reference-answer">', 1
        )[1].split("</section>", 1)[0]
        preview_ai = preview.json()["html"].split(
            '<section class="preview-ai-reference">', 1
        )[1].split("</section>", 1)[0]
        markdown_ai = self._last_export_text().split("### AI参考答案", 1)[1]
        for surface in (detail_ai, preview_ai, markdown_ai):
            for label in expected_labels:
                self.assertIn(label, surface)
            for synthetic in ("第1小问", "第2小问", "第3小问"):
                self.assertNotIn(synthetic, surface)

    def test_listing_marker_is_independent_of_official_answer_status(self):
        listing = self.client.get("/questions").text
        approved_code = self._code(1)
        unresolved_code = self._code(22)
        approved_at = listing.index(approved_code)
        approved_card = listing[listing.rfind("<article", 0, approved_at):listing.find("</article>", approved_at)]
        unresolved_at = listing.index(unresolved_code)
        unresolved_card = listing[listing.rfind("<article", 0, unresolved_at):listing.find("</article>", unresolved_at)]
        self.assertIn("有AI参考答案", approved_card)
        self.assertIn("原卷未提供答案", approved_card)
        self.assertNotIn("原卷答案已审核", approved_card)
        self.assertNotIn("有AI参考答案", unresolved_card)

    def test_source_answer_state_drift_hides_ai_on_every_read_surface(self):
        code = self._code(1)
        self._post(f"/basket/add/{code}")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE import_answer_sources "
                "SET source_answer_state='source_has_answer_unprocessed',"
                "answer_page_start=1,answer_page_end=1,"
                "render_manifest_sha256=? WHERE import_job_id=1",
                ("a" * 64,),
            )
        self._assert_ai_hidden_after_drift(code)

    def test_content_hash_drift_hides_ai_on_every_read_surface(self):
        code = self._code(1)
        self._post(f"/basket/add/{code}")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE questions SET content_hash=? WHERE question_code=?",
                ("f" * 64, code),
            )
        self._assert_ai_hidden_after_drift(code)

    def _drop_ai_subquestion_validation_triggers(self, connection):
        connection.execute(
            "DROP TRIGGER IF EXISTS ai_reference_subquestion_validate_insert"
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS ai_reference_subquestion_validate_update"
        )

    def _solution_binding(self, connection):
        code = self._code(21)
        answer_id = connection.execute(
            """SELECT a.id FROM ai_reference_answers a
               JOIN questions q ON q.id=a.question_id
               WHERE q.question_code=?""",
            (code,),
        ).fetchone()[0]
        children = connection.execute(
            """SELECT asa.id,asa.subquestion_id,asa.display_order
               FROM ai_reference_subquestion_answers asa
               WHERE asa.ai_reference_answer_id=? ORDER BY asa.display_order""",
            (answer_id,),
        ).fetchall()
        return code, answer_id, children

    def test_ai_answer_reads_subquestions_from_one_stable_snapshot(self):
        with sqlite3.connect(self.db) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
            code, _answer_id, children = self._solution_binding(setup)
            question_id = setup.execute(
                "SELECT id FROM questions WHERE question_code=?", (code,)
            ).fetchone()[0]
        reader = sqlite3.connect(self.db)
        reader.row_factory = sqlite3.Row
        writer = sqlite3.connect(self.db)
        mutation_fired = False

        class MutatingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.qualification_read = False

            def execute(self, sql, parameters=()):
                nonlocal mutation_fired
                if self.qualification_read and "SELECT s.display_order" in sql:
                    writer.execute(
                        "DELETE FROM ai_reference_subquestion_answers WHERE id=?",
                        (children[0][0],),
                    )
                    writer.commit()
                    self.qualification_read = False
                    mutation_fired = True
                cursor = self.connection.execute(sql, parameters)
                if "SELECT ai.*" in sql:
                    self.qualification_read = True
                return cursor

            def __getattr__(self, name):
                return getattr(self.connection, name)

        try:
            answer = _valid_ai_reference_answer(
                MutatingConnection(reader), question_id
            )
        finally:
            reader.close()
            writer.close()

        returned_count = (
            None
            if answer is None
            else len(answer["subquestion_display"]["flat_items"])
        )
        self.assertTrue(mutation_fired)
        self.assertIsNotNone(answer, "parent_returned=False")
        self.assertEqual(3, returned_count, f"returned_ai_subquestions={returned_count}/3")

    def test_ai_answer_read_exception_does_not_leave_own_transaction(self):
        code = self._code(21)
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            question_id = connection.execute(
                "SELECT id FROM questions WHERE question_code=?", (code,)
            ).fetchone()[0]

            class FailingConnection:
                def __init__(self, wrapped):
                    self.wrapped = wrapped

                def execute(self, sql, parameters=()):
                    if "SELECT s.display_order" in sql:
                        raise sqlite3.OperationalError("synthetic read failure")
                    return self.wrapped.execute(sql, parameters)

                def __getattr__(self, name):
                    return getattr(self.wrapped, name)

            with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic"):
                _valid_ai_reference_answer(
                    FailingConnection(connection), question_id
                )
            self.assertFalse(connection.in_transaction)

    def test_ai_answer_read_preserves_callers_existing_transaction(self):
        code = self._code(21)
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            question_id = connection.execute(
                "SELECT id FROM questions WHERE question_code=?", (code,)
            ).fetchone()[0]
            original_name = connection.execute(
                "SELECT paper_name FROM source_papers LIMIT 1"
            ).fetchone()[0]
            connection.execute("BEGIN")
            connection.execute(
                "UPDATE source_papers SET paper_name='transaction sentinel'"
            )

            answer = _valid_ai_reference_answer(connection, question_id)

            self.assertIsNotNone(answer)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(
                "transaction sentinel",
                connection.execute(
                    "SELECT paper_name FROM source_papers LIMIT 1"
                ).fetchone()[0],
            )
            connection.rollback()
        with sqlite3.connect(self.db) as verification:
            self.assertEqual(
                original_name,
                verification.execute(
                    "SELECT paper_name FROM source_papers LIMIT 1"
                ).fetchone()[0],
            )

    def test_cross_question_subquestion_damage_hides_whole_ai_answer_everywhere(self):
        with sqlite3.connect(self.db) as connection:
            self._drop_ai_subquestion_validation_triggers(connection)
            code, _answer_id, children = self._solution_binding(connection)
            other_question_id = connection.execute(
                "SELECT id FROM questions WHERE question_code=?",
                (self._code(1),),
            ).fetchone()[0]
            foreign_subquestion_id = connection.execute(
                """INSERT INTO subquestions
                   (question_id,display_order,stem_markdown)
                   VALUES(?,1,'另一题的小问')""",
                (other_question_id,),
            ).lastrowid
            connection.execute(
                """UPDATE ai_reference_subquestion_answers
                   SET subquestion_id=? WHERE id=?""",
                (foreign_subquestion_id, children[0][0]),
            )
        self._post(f"/basket/add/{code}")
        self._assert_ai_hidden_after_drift(code, answer_marker="最终答案 8")

    def test_missing_subquestion_damage_hides_whole_ai_answer_everywhere(self):
        with sqlite3.connect(self.db) as connection:
            code, _answer_id, children = self._solution_binding(connection)
            connection.execute(
                "DELETE FROM ai_reference_subquestion_answers WHERE id=?",
                (children[0][0],),
            )
        self._post(f"/basket/add/{code}")
        self._assert_ai_hidden_after_drift(code, answer_marker="最终答案 8")

    def test_extra_subquestion_damage_hides_whole_ai_answer_everywhere(self):
        with sqlite3.connect(self.db) as connection:
            self._drop_ai_subquestion_validation_triggers(connection)
            code, answer_id, _children = self._solution_binding(connection)
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO ai_reference_subquestion_answers
                   (ai_reference_answer_id,subquestion_id,display_order,
                    answer_markdown,analysis_markdown)
                   VALUES(?,999999999,99,'额外答案','额外解析')""",
                (answer_id,),
            )
        self._post(f"/basket/add/{code}")
        self._assert_ai_hidden_after_drift(code, answer_marker="最终答案 8")

    def test_display_order_damage_hides_whole_ai_answer_everywhere(self):
        with sqlite3.connect(self.db) as connection:
            self._drop_ai_subquestion_validation_triggers(connection)
            code, _answer_id, children = self._solution_binding(connection)
            connection.execute(
                """UPDATE ai_reference_subquestion_answers
                   SET display_order=99 WHERE id=?""",
                (children[0][0],),
            )
        self._post(f"/basket/add/{code}")
        self._assert_ai_hidden_after_drift(code, answer_marker="最终答案 8")

    def test_missing_formal_source_hides_ai_on_every_read_surface(self):
        code = self._code(1)
        self._post(f"/basket/add/{code}")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "DELETE FROM question_sources WHERE question_id="
                "(SELECT id FROM questions WHERE question_code=?)",
                (code,),
            )
        self._assert_ai_hidden_after_drift(
            code, detail_status=404, basket_available=False
        )

    def test_soft_deleted_question_is_410_and_cannot_leak_from_basket(self):
        code = self._code(1)
        self._post(f"/basket/add/{code}")
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE questions SET deleted_at='2026-09-04' "
                "WHERE question_code=?", (code,)
            )
        self._assert_ai_hidden_after_drift(
            code, detail_status=410, basket_available=False
        )

    def test_preview_ai_switch_is_independent_escaped_and_has_no_side_effect(self):
        for number in (1, 21, 22):
            self._post(f"/basket/add/{self._code(number)}")
        with sqlite3.connect(self.db) as connection:
            before = connection.execute("SELECT COUNT(*) FROM basket_exports").fetchone()[0]
        ordinary = self._post("/basket/preview", {}, json_response=True).json()["html"]
        self.assertNotIn("AI参考答案", ordinary)
        self.assertNotIn("最终答案", ordinary)
        ai = self._post(
            "/basket/preview", {"include_ai_answers": "on"}, json_response=True
        ).json()["html"]
        self.assertIn("AI参考答案", ai)
        self.assertIn("AI生成并经复核，不是原卷官方答案", ai)
        self.assertIn("最终答案 0 &lt;em&gt;reviewed&lt;/em&gt;", ai)
        self.assertNotIn("<em>reviewed</em>", ai)
        self.assertIn("最终小问答案 8-1", ai)
        self.assertNotIn("最终答案 9", ai)
        self.assertNotIn("生成答案 0", ai)
        self.assertNotIn("独立答案 0", ai)
        self.assertNotIn("原卷未提供答案", ai)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(before, connection.execute(
                "SELECT COUNT(*) FROM basket_exports"
            ).fetchone()[0])

    def test_markdown_ai_switch_and_no_switch_no_leakage(self):
        self._post(f"/basket/add/{self._code(1)}")
        without = self._post("/basket/export")
        self.assertEqual(303, without.status_code)
        without_text = self._last_export_text()
        self.assertNotIn("AI参考答案", without_text)
        self.assertNotIn("最终答案", without_text)

        with_ai = self._post(
            "/basket/export", {"include_ai_answers": "on"}
        )
        self.assertEqual(303, with_ai.status_code)
        text = self._last_export_text()
        self.assertIn("### AI参考答案", text)
        self.assertIn("AI生成并经复核，不是原卷官方答案", text)
        self.assertIn("最终答案 0 &lt;em&gt;reviewed&lt;/em&gt;", text)
        self.assertNotIn("<em>reviewed</em>", text)
        self.assertNotIn("生成答案 0", text)
        self.assertNotIn("独立答案 0", text)
        self.assertNotIn("原卷未提供答案", text)

    def test_markdown_export_escapes_parent_and_subquestion_ai_html(self):
        self.client.close()
        self.final_review["questions"][0].update(
            answer_markdown="<script>parent-answer()</script> **保留粗体**",
            analysis_markdown="<iframe>parent-analysis</iframe> $x < y$",
        )
        self.final_review["questions"][8]["subquestions"][0].update(
            answer_markdown="<svg onload=bad()>sub-answer</svg> `保留代码`",
            analysis_markdown="<img src=x onerror=bad()> $a < b$",
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM ai_reference_subquestion_answers")
            connection.execute("DELETE FROM ai_reference_answers")
        self._run(apply=True)
        self.client = TestClient(create_app(self.db, self.private))
        for number in (1, 21):
            self._post(f"/basket/add/{self._code(number)}")

        response = self._post(
            "/basket/export", {"include_ai_answers": "on"}
        )
        self.assertEqual(303, response.status_code)
        text = self._last_export_text()
        for raw_tag in ("<script>", "</script>", "<iframe>", "</iframe>",
                        "<svg onload=bad()>", "</svg>",
                        "<img src=x onerror=bad()>"):
            self.assertNotIn(raw_tag, text)
        self.assertIn("&lt;script&gt;parent-answer()&lt;/script&gt; **保留粗体**", text)
        self.assertIn("&lt;iframe&gt;parent-analysis&lt;/iframe&gt; $x &lt; y$", text)
        self.assertIn(
            "&lt;svg onload=bad()&gt;sub-answer&lt;/svg&gt; `保留代码`", text
        )
        self.assertIn("&lt;img src=x onerror=bad()&gt; $a &lt; b$", text)

    def _last_export_text(self):
        with sqlite3.connect(self.db) as connection:
            relative = connection.execute(
                "SELECT output_path FROM basket_exports ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        return (self.private / relative).read_text(encoding="utf-8")

    def test_basket_form_has_separate_unchecked_ai_option(self):
        self._post(f"/basket/add/{self._code(1)}")
        page = self.client.get("/basket").text
        self.assertRegex(page, r'name="include_ai_answers"(?! checked)')
        self.assertIn("AI参考答案</label>", page)
        self.assertRegex(page, r'name="include_answers"(?! checked)')


if __name__ == "__main__":
    unittest.main()
