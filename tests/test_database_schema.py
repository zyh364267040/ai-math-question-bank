import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import initialize_database


CORE_TABLES = {
    "questions",
    "question_options",
    "subquestions",
    "question_figures",
    "knowledge_points",
    "question_related_knowledge_points",
    "tag_definitions",
    "question_tags",
    "question_reviews",
    "question_usage_records",
    "duplicate_groups",
    "question_versions",
    "regions",
    "exam_types",
    "question_types",
    "review_statuses",
    "usability_statuses",
    "difficulty_levels",
    "source_papers",
    "import_jobs",
    "import_upload_receipts",
    "import_page_render_runs",
}


class DatabaseSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "question-bank.db"
        self.connection = initialize_database(self.db_path)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def insert_knowledge_point(self, code="08.06.02"):
        self.connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_points
                (code, name, level, system_version)
            VALUES (?, '分离参数', 3, 'v1')
            """,
            (code,),
        )
        return self.connection.execute(
            "SELECT id FROM knowledge_points WHERE code = ?", (code,)
        ).fetchone()[0]

    def insert_question(self, code="TJ-2025-GK-MATH-001", **overrides):
        knowledge_point_id = overrides.pop(
            "primary_knowledge_point_id", self.insert_knowledge_point()
        )
        values = {
            "question_code": code,
            "stem_markdown": "已知函数 $f(x)$。",
            "answer_markdown": "$x=1$",
            "region_code": "TJ",
            "exam_type_code": "GK",
            "question_type_code": "solution",
            "difficulty_level": 3,
            "primary_knowledge_point_id": knowledge_point_id,
            "ocr_review_status": "pending",
            "formula_review_status": "pending",
            "figure_review_status": "not_applicable",
            "answer_review_status": "pending",
            "analysis_review_status": "not_applicable",
            "tag_review_status": "pending",
            "usability_status": "draft",
            "content_hash": f"hash-{code}",
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = self.connection.execute(
            f"INSERT INTO questions ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return cursor.lastrowid, knowledge_point_id

    def test_all_core_tables_exist(self):
        actual = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(CORE_TABLES <= actual, CORE_TABLES - actual)

    def test_foreign_keys_are_enabled(self):
        self.assertEqual(
            1, self.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )

    def test_seed_dictionaries_exist_and_are_idempotent(self):
        expected_counts = {
            "regions": 1,
            "exam_types": 11,
            "question_types": 4,
            "review_statuses": 4,
            "usability_statuses": 5,
            "difficulty_levels": 5,
            "tag_definitions": 48,
        }
        before = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in expected_counts
        }
        initialize_database(self.db_path).close()
        after = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in expected_counts
        }
        self.assertEqual(expected_counts, before)
        self.assertEqual(before, after)
        categories = {
            row[0]
            for row in self.connection.execute(
                "SELECT DISTINCT category FROM tag_definitions"
            )
        }
        self.assertEqual({"task", "method", "error", "scenario"}, categories)

    def test_question_code_is_unique(self):
        self.insert_question()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_question(
                primary_knowledge_point_id=self.insert_knowledge_point("08.06.03")
            )

    def test_invalid_question_type_difficulty_and_review_status_are_rejected(self):
        invalid_values = (
            {"question_type_code": "essay"},
            {"difficulty_level": 6},
            {"answer_review_status": "approved"},
        )
        for index, overrides in enumerate(invalid_values, start=1):
            with self.subTest(overrides=overrides):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_question(
                        code=f"TJ-2025-GK-MATH-00{index}",
                        primary_knowledge_point_id=self.insert_knowledge_point(
                            f"08.06.{index + 10}"
                        ),
                        **overrides,
                    )

    def test_related_knowledge_point_and_usage_record_link_to_question(self):
        question_id, _ = self.insert_question()
        related_id = self.insert_knowledge_point("08.03.02")
        self.connection.execute(
            """
            INSERT INTO question_related_knowledge_points
                (question_id, knowledge_point_id)
            VALUES (?, ?)
            """,
            (question_id, related_id),
        )
        self.connection.execute(
            """
            INSERT INTO question_usage_records
                (question_id, used_at, context_type, context_name)
            VALUES (?, '2026-07-11T10:00:00+08:00', 'lesson', '导数专题课')
            """,
            (question_id,),
        )
        self.assertEqual(
            (question_id, related_id),
            self.connection.execute(
                """SELECT question_id, knowledge_point_id
                   FROM question_related_knowledge_points"""
            ).fetchone(),
        )
        self.assertEqual(
            question_id,
            self.connection.execute(
                "SELECT question_id FROM question_usage_records"
            ).fetchone()[0],
        )

    def test_referenced_knowledge_point_cannot_be_deleted(self):
        _, knowledge_point_id = self.insert_question()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM knowledge_points WHERE id = ?", (knowledge_point_id,)
            )

    def test_initializer_cli_is_safe_to_repeat(self):
        command = [
            sys.executable,
            str(PROJECT_ROOT / "src/database/initialize.py"),
            str(self.db_path),
        ]
        first = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
        second = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)

    def test_existing_database_gets_upload_receipts_without_data_loss(self):
        question_id, _ = self.insert_question()
        source_id = self.connection.execute(
            """INSERT INTO source_papers
               (sha256, file_size, original_filename, stored_path,
                region_code, exam_type_code, paper_name)
               VALUES (?, 123, 'existing.pdf', 'raw_papers/TJ/unknown/existing.pdf',
                       'TJ', 'GK', '既有试卷')""",
            ("e" * 64,),
        ).lastrowid
        job_id = self.connection.execute(
            "INSERT INTO import_jobs (source_paper_id, status) VALUES (?, 'pending')",
            (source_id,),
        ).lastrowid
        self.connection.execute("DROP TABLE import_upload_receipts")
        self.connection.commit()

        initialize_database(self.db_path).close()

        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("import_upload_receipts", tables)
        self.assertEqual(
            (source_id, "既有试卷"),
            self.connection.execute(
                "SELECT id, paper_name FROM source_papers WHERE id = ?", (source_id,)
            ).fetchone(),
        )
        self.assertEqual(
            (job_id, source_id, "pending"),
            self.connection.execute(
                "SELECT id, source_paper_id, status FROM import_jobs WHERE id = ?",
                (job_id,),
            ).fetchone(),
        )
        self.assertEqual(
            (question_id, "已知函数 $f(x)$。"),
            self.connection.execute(
                "SELECT id, stem_markdown FROM questions WHERE id = ?", (question_id,)
            ).fetchone(),
        )

        self.connection.execute(
            """INSERT INTO import_upload_receipts
               (token, source_paper_id, import_job_id) VALUES ('old-token', ?, ?)""",
            (source_id, job_id),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO import_upload_receipts
                   (token, source_paper_id, import_job_id) VALUES ('other-token', ?, ?)""",
                (source_id, job_id),
            )

    def test_existing_database_gets_render_runs_without_data_loss(self):
        question_id, _ = self.insert_question()
        source_id = self.connection.execute(
            """INSERT INTO source_papers
               (sha256, file_size, original_filename, stored_path,
                region_code, exam_type_code, paper_name)
               VALUES (?, 123, 'render-existing.pdf',
                       'raw_papers/TJ/unknown/render-existing.pdf',
                       'TJ', 'GK', '渲染迁移试卷')""",
            ("d" * 64,),
        ).lastrowid
        job_id = self.connection.execute(
            "INSERT INTO import_jobs (source_paper_id, status) VALUES (?, 'pending')",
            (source_id,),
        ).lastrowid
        self.connection.execute("DROP TABLE IF EXISTS import_page_render_runs")
        self.connection.commit()

        initialize_database(self.db_path).close()

        self.assertEqual(
            (source_id, "渲染迁移试卷"),
            self.connection.execute(
                "SELECT id, paper_name FROM source_papers WHERE id = ?", (source_id,)
            ).fetchone(),
        )
        self.assertEqual(
            (job_id, source_id, "pending"),
            self.connection.execute(
                "SELECT id, source_paper_id, status FROM import_jobs WHERE id = ?",
                (job_id,),
            ).fetchone(),
        )
        self.assertEqual(
            question_id,
            self.connection.execute(
                "SELECT id FROM questions WHERE id = ?", (question_id,)
            ).fetchone()[0],
        )
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(import_page_render_runs)"
            )
        }
        self.assertTrue(
            {
                "import_job_id", "status", "dpi", "total_pages",
                "rendered_pages", "error_message", "started_at",
                "completed_at", "updated_at",
            }
            <= columns
        )

    def test_soft_delete_columns_are_migrated_idempotently_without_data_loss(self):
        question_id, _ = self.insert_question()
        self.connection.execute(
            "INSERT INTO candidate_review_drafts "
            "(import_job_id,source_question_no,source_candidate_sha256,source_snapshot_json,edited_json) "
            "SELECT id,'22',?,'{}','{}' FROM import_jobs LIMIT 0",
            ("a" * 64,),
        )
        self.connection.commit()

        initialize_database(self.db_path).close()
        initialize_database(self.db_path).close()

        with sqlite3.connect(self.db_path) as connection:
            question_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(questions)")
            }
            draft_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(candidate_review_drafts)"
                )
            }
            expected = {"deleted_at", "deletion_reason", "deletion_note"}
            self.assertTrue(expected <= question_columns)
            self.assertTrue(expected <= draft_columns)
            self.assertEqual(
                (question_id, "已知函数 $f(x)$。"),
                connection.execute(
                    "SELECT id,stem_markdown FROM questions WHERE id=?", (question_id,)
                ).fetchone(),
            )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_approval_source_is_constrained_and_migrated(self):
        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(candidate_review_drafts)"
            )
        }
        self.assertIn("approval_source", columns)
        self.assertIn("approval_evidence_json", columns)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO candidate_review_drafts
                   (import_job_id,source_question_no,source_candidate_sha256,
                    source_snapshot_json,edited_json,approval_source)
                   VALUES(1,'x',?,'{}','{}','robot')""",
                ("a" * 64,),
            )


if __name__ == "__main__":
    unittest.main()
