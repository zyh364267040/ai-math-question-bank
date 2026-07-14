import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


from src.database.initialize import initialize_database
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
        self._seed_database()
        self._write_audit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_database(self):
        with sqlite3.connect(self.db_path) as connection:
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
            drafts = {
                "1": ("approved", self._edited("1", "人工修改题干", primary="01.01.01")),
                "2": ("pending", self._edited("2", "AI 修改题干", primary="01.01.01")),
                "12": ("pending", self._edited("12", "证据不足", primary="01.01.01")),
                "22": ("approved", self._edited("22", "复杂题干", primary="08.04.04", complex=True)),
            }
            for number, (status, edited) in drafts.items():
                payload = json.dumps(edited, ensure_ascii=False)
                connection.execute(
                    """INSERT INTO candidate_review_drafts
                       (import_job_id,source_question_no,source_candidate_sha256,
                        source_snapshot_json,edited_json,status,version,reviewed_at)
                       VALUES(?,?,?, ?,?,?,3,?)""",
                    (job_id, number, "b" * 64, payload, payload, status,
                     "2026-07-14T10:00:00+08:00" if status == "approved" else None),
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
        }
        if complex:
            result["subquestions"] = [
                {"label": "（1）", "stem_markdown": "第一问"},
                {"label": "（2）", "stem_markdown": "公共条件"},
                {"label": "（2）①", "stem_markdown": "第一小问"},
                {"label": "（2）②", "stem_markdown": "第二小问"},
            ]
        return result

    def _write_audit(self):
        job_dir = self.private_root / "processing" / "import_job_1"
        job_dir.mkdir(parents=True)
        audits = []
        for number in ("1", "2", "12", "22"):
            audits.append({
                "source_question_no": number,
                "audit_status": "human_required" if number == "12" else "auto_pass",
                "audit_confidence": "high",
                "issues": ["证据不足"] if number == "12" else [],
                "suggested_corrections": [],
            })
        (job_dir / "ai_audit.json").write_text(
            json.dumps({"import_job_id": 1, "question_count": 4, "questions": audits}),
            encoding="utf-8",
        )

    def _rows(self, sql, parameters=()):
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(sql, parameters).fetchall()

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
        self.assertEqual([], self._rows("PRAGMA foreign_key_check"))

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
        first = finalize_review(self.db_path, self.private_root, 1, apply=True)
        versions = self._rows("SELECT id,question_id,version_no,snapshot_json FROM question_versions")
        reviews = self._rows("SELECT id,question_id,notes FROM question_reviews")
        draft_versions = self._rows("SELECT source_question_no,version FROM candidate_review_drafts ORDER BY id")
        second = finalize_review(self.db_path, self.private_root, 1, apply=True)
        self.assertEqual(0, second.changed_questions)
        self.assertEqual(versions, self._rows("SELECT id,question_id,version_no,snapshot_json FROM question_versions"))
        self.assertEqual(reviews, self._rows("SELECT id,question_id,notes FROM question_reviews"))
        self.assertEqual(draft_versions, self._rows("SELECT source_question_no,version FROM candidate_review_drafts ORDER BY id"))
        self.assertNotEqual(first.backup_path, second.backup_path)

    def test_missing_formal_question_fails_without_partial_changes(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DELETE FROM candidate_review_drafts WHERE source_question_no='12'")
            connection.execute("DELETE FROM question_sources WHERE source_question_no='2'")
            connection.execute("DELETE FROM questions WHERE id=?", (self.ids["2"],))
        before = self._rows("SELECT source_question_no,status,approval_source,version FROM candidate_review_drafts ORDER BY id")
        with self.assertRaisesRegex(FinalizationError, "正式题不存在"):
            finalize_review(self.db_path, self.private_root, 1, apply=True)
        self.assertEqual(before, self._rows("SELECT source_question_no,status,approval_source,version FROM candidate_review_drafts ORDER BY id"))

    def test_transaction_rolls_back_everything_on_sync_failure(self):
        with sqlite3.connect(self.db_path) as connection:
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


if __name__ == "__main__":
    unittest.main()
