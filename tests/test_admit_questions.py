import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from src.database.initialize import initialize_database
from src.importing.admit_questions import AdmissionError, admit_questions, assess_job
from tests.fixture_factory import create_import_job_fixture


ROOT = Path(__file__).resolve().parents[1]
class AdmitQuestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.job = create_import_job_fixture(self.private)
        self.db = self.private / "question-bank.db"
        initialize_database(self.db).close()
        with sqlite3.connect(self.db) as con:
            source = con.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'paper.pdf','raw_papers/TJ/2025/paper.pdf','TJ',2025,'YK',?)""",
                ("a" * 64, "测试卷"),
            ).lastrowid
            con.execute("INSERT INTO import_jobs(id,source_paper_id,page_start,page_end,status) VALUES(1,?,1,4,'needs_review')", (source,))

    def tearDown(self):
        self.temp.cleanup()

    def _json(self, name):
        path = self.job / name
        return path, json.loads(path.read_text(encoding="utf-8"))

    def _write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _mutate_audits(self, mutate):
        path, audit = self._json("ai_audit.json")
        mutate({item["source_question_no"]: item for item in audit["questions"]})
        audit["counts"] = {
            status: sum(item["audit_status"] == status for item in audit["questions"])
            for status in ("auto_pass", "disputed", "human_required")
        }
        self._write(path, audit)

    def _strict_auto_pass(self, item):
        item.update(
            audit_status="auto_pass",
            audit_confidence="high",
            issues=[],
            suggested_corrections=[],
        )

    def _answer_analysis_sha256(self, question):
        payload = {
            "source_question_no": question["source_question_no"],
            "answer_markdown": question.get("answer_markdown", ""),
            "analysis_markdown": question.get("analysis_markdown", ""),
            "subquestions": [
                {
                    "label": subquestion.get("label", ""),
                    "stem_markdown": subquestion.get("stem_markdown", ""),
                    "answer_markdown": subquestion.get("answer_markdown", ""),
                    "analysis_markdown": subquestion.get("analysis_markdown", ""),
                }
                for subquestion in question.get("subquestions", [])
            ],
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def test_assessment_finds_22_eligible_and_excludes_q12(self):
        report = assess_job(self.db, self.private, 1)
        self.assertEqual(22, len(report.eligible))
        self.assertEqual(["12"], [item.question_no for item in report.ineligible])
        self.assertIn("human_required", report.ineligible[0].reasons)

    def test_abnormal_question_number_is_dynamic_and_q12_can_become_eligible(self):
        def mutate(by_no):
            self._strict_auto_pass(by_no["12"])
            by_no["7"].update(
                audit_status="human_required",
                audit_confidence="medium",
                issues=["需要人工确认"],
                suggested_corrections=["核对原图"],
            )

        self._mutate_audits(mutate)
        report = assess_job(self.db, self.private, 1)
        self.assertIn("12", [item.question_no for item in report.eligible])
        self.assertEqual(["7"], [item.question_no for item in report.ineligible])
        admit_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as con:
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='12'"
            ).fetchone())
            self.assertIsNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='7'"
            ).fetchone())

    def test_strict_ai_gate_requires_each_of_all_four_signals(self):
        failures = (
            ("audit_status", "disputed"),
            ("audit_confidence", "medium"),
            ("issues", ["发现问题"]),
            ("suggested_corrections", ["需要修正"]),
        )
        for field, value in failures:
            with self.subTest(field=field):
                path, original = self._json("ai_audit.json")

                def mutate(by_no):
                    self._strict_auto_pass(by_no["12"])
                    by_no["5"][field] = value

                self._mutate_audits(mutate)
                report = assess_job(self.db, self.private, 1)
                self.assertEqual(["5"], [item.question_no for item in report.ineligible])
                self._write(path, original)

    def test_dynamic_question_count_is_admitted_without_batch_size_assumptions(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"] = candidate["questions"][:5]
        candidate["question_count"] = 5
        self._write(candidate_path, candidate)

        audit_path, audit = self._json("ai_audit.json")
        audit["questions"] = audit["questions"][:5]
        audit["question_count"] = 5
        audit["counts"] = {"auto_pass": 5, "disputed": 0, "human_required": 0}
        audit["random_sample_recommendation"]["question_nos"] = ["3"]
        self._write(audit_path, audit)

        crops_path, crops = self._json("question_crops.json")
        crops["questions"] = crops["questions"][:5]
        crops["question_count"] = 5
        self._write(crops_path, crops)

        figures_path, figures = self._json("figure_assets.json")
        figures["assets"] = [item for item in figures["assets"] if str(item["question_no"]) in {"1", "2", "3", "4", "5"}]
        self._write(figures_path, figures)

        result = admit_questions(self.db, self.private, 1)
        self.assertEqual((5, 0, 5, 0), (result.inserted, result.already_present, result.eligible, result.ineligible))

    def test_soft_deleted_candidate_is_ineligible_but_missing_draft_is_allowed(self):
        with sqlite3.connect(self.db) as con:
            con.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,deleted_at)
                   VALUES (1,'2',?,'{}','{}','2026-07-14T00:00:00+08:00')""",
                ("b" * 64,),
            )
        result = admit_questions(self.db, self.private, 1)
        self.assertEqual((21, 2), (result.eligible, result.ineligible))
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='2'"
            ).fetchone())
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM question_sources WHERE import_job_id=1 AND source_question_no='1'"
            ).fetchone())

    def test_no_eligible_questions_is_a_safe_idempotent_noop(self):
        def mutate(by_no):
            for item in by_no.values():
                item.update(
                    audit_status="human_required",
                    audit_confidence="medium",
                    issues=["需要人工确认"],
                    suggested_corrections=["核对原图"],
                )

        self._mutate_audits(mutate)
        first = admit_questions(self.db, self.private, 1)
        second = admit_questions(self.db, self.private, 1)
        self.assertEqual((0, 0, 0, 23), (first.inserted, first.already_present, first.eligible, first.ineligible))
        self.assertEqual(first, second)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_fixture_is_generated_inside_temp_root_without_repository_private_data(self):
        repository_private_processing = ROOT / "data" / "private" / "processing"
        self.assertTrue(self.job.resolve().is_relative_to(self.root.resolve()))
        self.assertFalse(self.job.resolve().is_relative_to(repository_private_processing.resolve()))
        self.assertEqual(23, len(list((self.job / "question_crops").glob("Q*.png"))))
        factory_source = Path(create_import_job_fixture.__code__.co_filename).read_text(encoding="utf-8")
        self.assertNotIn("data/private", factory_source)

    def test_fixture_metadata_and_required_semantics_are_self_consistent(self):
        candidate = json.loads((self.job / "candidate_questions.json").read_text(encoding="utf-8"))
        by_no = {int(item["source_question_no"]): item for item in candidate["questions"]}
        self.assertEqual(23, candidate["question_count"])
        self.assertTrue(all(option["content"] == "见原页选项图" for option in by_no[3]["options"]))
        self.assertEqual({3, 16}, {number for number, item in by_no.items() if item["figure_required"]})
        self.assertEqual(["（1）", "（2）", "（2）（i）", "（2）（ii）"], [x["label"] for x in by_no[22]["subquestions"]])
        self.assertEqual(["（1）", "（2）", "（2）①", "（2）②"], [x["label"] for x in by_no[23]["subquestions"]])

        render = json.loads((self.job / "render_manifest.json").read_text(encoding="utf-8"))
        crops = json.loads((self.job / "question_crops.json").read_text(encoding="utf-8"))
        figures = json.loads((self.job / "figure_assets.json").read_text(encoding="utf-8"))
        entries = [
            *[(entry, "pixel_width", "pixel_height") for entry in render["pages"]],
            *[(entry, "width", "height") for entry in crops["questions"]],
            *[(entry, "width", "height") for entry in figures["assets"]],
        ]
        for entry, width_key, height_key in entries:
            with self.subTest(path=entry["relative_path"] if "relative_path" in entry else entry["output_relative_path"]):
                relative = entry.get("relative_path", entry.get("output_relative_path"))
                path = self.job / relative
                self.assertEqual(path.stat().st_size, entry["byte_size"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                with Image.open(path) as image:
                    self.assertEqual("PNG", image.format)
                    self.assertEqual((entry[width_key], entry[height_key]), image.size)

    def test_admits_all_relations_without_answers_and_is_idempotent(self):
        first = admit_questions(self.db, self.private, 1)
        second = admit_questions(self.db, self.private, 1)
        self.assertEqual((22, 0), (first.inserted, first.already_present))
        self.assertEqual((0, 22), (second.inserted, second.already_present))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(22, con.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(22, con.execute("SELECT count(DISTINCT question_code) FROM questions").fetchone()[0])
            self.assertIsNone(con.execute("SELECT 1 FROM question_sources WHERE source_question_no='12'").fetchone())
            self.assertEqual(22, con.execute("SELECT count(*) FROM question_assets WHERE asset_kind='complete_question'").fetchone()[0])
            self.assertEqual(2, con.execute("SELECT count(*) FROM question_assets WHERE asset_kind='question_figure'").fetchone()[0])
            self.assertEqual(22, con.execute("SELECT count(*) FROM question_sources").fetchone()[0])
            self.assertEqual(22, con.execute("SELECT count(*) FROM question_reviews WHERE review_item='usability'").fetchone()[0])
            self.assertEqual((0, 22), con.execute("SELECT count(nullif(answer_markdown,'')), count(*) FROM questions WHERE answer_status='missing'").fetchone())
            self.assertEqual((0, 22), con.execute(
                "SELECT count(analysis_markdown),count(*) FROM questions WHERE analysis_review_status='not_applicable'"
            ).fetchone())
            self.assertEqual((0, 0), con.execute(
                "SELECT count(nullif(answer_markdown,'')),count(analysis_markdown) FROM subquestions WHERE answer_status='missing'"
            ).fetchone())

    def test_answer_without_dedicated_audit_status_is_unsafe(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"][0]["answer_markdown"] = "$x=1$"
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        question = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("answer_status_not_passed", question.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_status_not_passed"):
            admit_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_analysis_without_dedicated_audit_status_is_unsafe(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = candidate["questions"][0]
        question["analysis_markdown"] = "原卷解析。"
        self._write(candidate_path, candidate)
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_analysis_sha256=self._answer_analysis_sha256(question)
            )
        )

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("analysis_status_not_passed", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "analysis_status_not_passed"):
            admit_questions(self.db, self.private, 1)

    def test_answer_changed_after_audit_is_unsafe_due_to_hash_mismatch(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = candidate["questions"][0]
        question["answer_markdown"] = "$x=1$"
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_status="passed", answer_analysis_sha256=reviewed_hash
            )
        )
        question["answer_markdown"] = "$x=2$"
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "1")
        self.assertIn("answer_analysis_sha256_mismatch", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_subquestion_changed_after_audit_is_unsafe_due_to_hash_mismatch(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0]["answer_markdown"] = "$2$"
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["22"].update(
                answer_status="passed", answer_analysis_sha256=reviewed_hash
            )
        )
        question["subquestions"][0]["stem_markdown"] = "审核后被篡改的小问题干"
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_subquestion_reorder_after_audit_is_unsafe_due_to_hash_mismatch(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0]["answer_markdown"] = "$2$"
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["22"].update(
                answer_status="passed", answer_analysis_sha256=reviewed_hash
            )
        )
        question["subquestions"][0], question["subquestions"][1] = (
            question["subquestions"][1], question["subquestions"][0]
        )
        self._write(candidate_path, candidate)

        report = assess_job(self.db, self.private, 1)

        assessed = next(item for item in report.ineligible if item.question_no == "22")
        self.assertIn("answer_analysis_sha256_mismatch", assessed.reasons)
        with self.assertRaisesRegex(AdmissionError, "answer_analysis_sha256_mismatch"):
            admit_questions(self.db, self.private, 1)

    def test_admits_parent_answer_and_analysis_as_reviewed_content(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"][0].update(
            answer_markdown="$x=1$",
            analysis_markdown="由题意直接计算。",
        )
        self._write(candidate_path, candidate)
        reviewed_hash = self._answer_analysis_sha256(candidate["questions"][0])
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_status="passed",
                analysis_status="passed",
                answer_analysis_sha256=reviewed_hash,
            )
        )

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT answer_markdown,answer_status,answer_review_status,
                          analysis_markdown,analysis_review_status
                   FROM questions WHERE source_question_no='1'"""
            ).fetchone()
        self.assertEqual(
            ("$x=1$", "provided", "passed", "由题意直接计算。", "passed"),
            row,
        )
        with sqlite3.connect(self.db) as con:
            review_note = con.execute(
                """SELECT r.notes FROM question_reviews r JOIN questions q ON q.id=r.question_id
                   WHERE q.source_question_no='1' AND r.review_item='usability'"""
            ).fetchone()[0]
        self.assertIn("原卷答案已通过审核", review_note)

    def test_admits_subquestion_answer_and_analysis(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0].update(
            answer_markdown="$2$",
            analysis_markdown="先化简，再求值。",
        )
        self._write(candidate_path, candidate)
        reviewed_hash = self._answer_analysis_sha256(question)
        self._mutate_audits(
            lambda by_no: by_no["22"].update(
                answer_status="passed",
                analysis_status="passed",
                answer_analysis_sha256=reviewed_hash,
            )
        )

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            row = con.execute(
                """SELECT s.answer_markdown,s.answer_status,s.analysis_markdown
                   FROM subquestions s JOIN questions q ON q.id=s.question_id
                   WHERE q.source_question_no='22' AND s.display_order=1"""
            ).fetchone()
            review_note = con.execute(
                """SELECT r.notes FROM question_reviews r JOIN questions q ON q.id=r.question_id
                   WHERE q.source_question_no='22' AND r.review_item='usability'"""
            ).fetchone()[0]
            parent_statuses = con.execute(
                """SELECT q.answer_status,q.answer_review_status,q.analysis_review_status
                   FROM questions q WHERE q.source_question_no='22'"""
            ).fetchone()
        self.assertEqual(("$2$", "provided", "先化简，再求值。"), row)
        self.assertEqual(("missing", "not_applicable", "not_applicable"), parent_statuses)
        self.assertNotIn("未提供答案", review_note)
        self.assertIn("答案已通过审核", review_note)

    def test_non_string_answer_or_analysis_rejects_and_rolls_back_batch(self):
        candidate_path, original = self._json("candidate_questions.json")
        mutations = (
            lambda data: data["questions"][0].__setitem__("answer_markdown", 123),
            lambda data: data["questions"][0].__setitem__("analysis_markdown", None),
            lambda data: next(q for q in data["questions"] if q["source_question_no"] == "22")["subquestions"][0].__setitem__("answer_markdown", ["2"]),
            lambda data: next(q for q in data["questions"] if q["source_question_no"] == "22")["subquestions"][0].__setitem__("analysis_markdown", {"text": "解析"}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(original)
                mutate(changed)
                self._write(candidate_path, changed)
                try:
                    with self.assertRaises(AdmissionError):
                        admit_questions(self.db, self.private, 1)

                    with sqlite3.connect(self.db) as con:
                        self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])
                finally:
                    with sqlite3.connect(self.db) as con:
                        con.execute("DELETE FROM questions")
        self._write(candidate_path, original)

    def test_explicit_non_list_subquestions_raise_controlled_admission_error(self):
        candidate_path, original = self._json("candidate_questions.json")
        for invalid in (None, {}, "not-a-list"):
            with self.subTest(invalid=invalid):
                changed = copy.deepcopy(original)
                changed["questions"][0]["subquestions"] = invalid
                self._write(candidate_path, changed)

                with self.assertRaisesRegex(AdmissionError, "subquestions必须为列表"):
                    admit_questions(self.db, self.private, 1)

        self._write(candidate_path, original)

    def test_empty_answer_and_analysis_keep_missing_semantics(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        candidate["questions"][0].update(answer_markdown="", analysis_markdown="   ")
        question = next(q for q in candidate["questions"] if q["source_question_no"] == "22")
        question["subquestions"][0].update(answer_markdown="   ", analysis_markdown="")
        self._write(candidate_path, candidate)

        admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            parent = con.execute(
                """SELECT answer_markdown,answer_status,answer_review_status,
                          analysis_markdown,analysis_review_status
                   FROM questions WHERE source_question_no='1'"""
            ).fetchone()
            child = con.execute(
                """SELECT s.answer_markdown,s.answer_status,s.analysis_markdown
                   FROM subquestions s JOIN questions q ON q.id=s.question_id
                   WHERE q.source_question_no='22' AND s.display_order=1"""
            ).fetchone()
        self.assertEqual(("", "missing", "not_applicable", None, "not_applicable"), parent)
        self.assertEqual(("", "missing", None), child)

    def test_manifest_failures_abort_batch_without_partial_rows(self):
        mutations = [
            ("ai_audit.json", lambda d: d["questions"].pop()),
            ("question_crops.json", lambda d: d["questions"].pop()),
            ("question_crops.json", lambda d: d["questions"][0].__setitem__("sha256", "0" * 64)),
            ("figure_assets.json", lambda d: d["assets"][0].__setitem__("review_status", "pending_ai_review")),
            ("candidate_questions.json", lambda d: d["questions"][0].__setitem__("primary_knowledge_point_code", "missing.code")),
        ]
        for filename, mutate in mutations:
            with self.subTest(filename=filename, mutation=mutate):
                path, original = self._json(filename)
                changed = copy.deepcopy(original); mutate(changed); self._write(path, changed)
                with self.assertRaises(AdmissionError):
                    admit_questions(self.db, self.private, 1)
                with sqlite3.connect(self.db) as con:
                    self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])
                self._write(path, original)

    def test_transaction_rolls_back_on_insert_error(self):
        with mock.patch("src.importing.admit_questions._insert_one", side_effect=sqlite3.IntegrityError("boom")):
            with self.assertRaises(sqlite3.IntegrityError):
                admit_questions(self.db, self.private, 1)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])

    def test_later_sqlite_error_rolls_back_previously_inserted_answer(self):
        candidate_path, candidate = self._json("candidate_questions.json")
        first = candidate["questions"][0]
        first["answer_markdown"] = "$x=1$"
        candidate["questions"][1]["options"][1]["code"] = "A"
        self._write(candidate_path, candidate)
        self._mutate_audits(
            lambda by_no: by_no["1"].update(
                answer_status="passed",
                answer_analysis_sha256=self._answer_analysis_sha256(first),
            )
        )

        with self.assertRaises(sqlite3.IntegrityError):
            admit_questions(self.db, self.private, 1)

        with sqlite3.connect(self.db) as con:
            self.assertEqual(0, con.execute("SELECT count(*) FROM questions").fetchone()[0])
            self.assertEqual(0, con.execute("SELECT count(*) FROM question_options").fetchone()[0])

    def test_question_code_is_stable_and_paths_are_safe(self):
        report = admit_questions(self.db, self.private, 1)
        codes = report.question_codes
        self.assertEqual(codes, admit_questions(self.db, self.private, 1).question_codes)
        with sqlite3.connect(self.db) as con:
            paths = [row[0] for row in con.execute("SELECT relative_path FROM question_assets")]
        self.assertTrue(all(not p.startswith("/") and ".." not in Path(p).parts for p in paths))


if __name__ == "__main__":
    unittest.main()
