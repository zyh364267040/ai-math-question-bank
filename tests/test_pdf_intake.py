import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import initialize_database
from src.importing.pdf_intake import PdfIntakeError, intake_pdf


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class PdfIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "question-bank.db"
        self.storage_root = self.root / "private"
        initialize_database(self.db_path).close()
        self.pdf_path = self.root / "user supplied name.pdf"
        self.pdf_path.write_bytes(MINIMAL_PDF)

    def tearDown(self):
        self.temp_dir.cleanup()

    def intake(self, path=None, **overrides):
        arguments = {
            "pdf_path": path or self.pdf_path,
            "region_code": "TJ",
            "exam_year": 2025,
            "exam_type_code": "GK",
            "paper_name": "2025 年天津高考数学",
            "page_range": "1-6",
            "database_path": self.db_path,
            "private_storage_root": self.storage_root,
        }
        arguments.update(overrides)
        return intake_pdf(**arguments)

    def rows(self, table):
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(f"SELECT * FROM {table}").fetchall()

    def test_first_import_creates_archive_source_and_job(self):
        result = self.intake()
        stored = self.storage_root / result["stored_path"]
        self.assertTrue(stored.is_file())
        self.assertEqual(MINIMAL_PDF, stored.read_bytes())
        self.assertFalse(result["deduplicated"])
        self.assertEqual("pending", result["status"])
        self.assertEqual(64, len(result["sha256"]))
        self.assertEqual(1, len(self.rows("source_papers")))
        self.assertEqual(1, len(self.rows("import_jobs")))

    def test_duplicate_reuses_source_file_but_creates_another_job(self):
        first = self.intake()
        second = self.intake(page_range="3")
        self.assertEqual(first["source_paper_id"], second["source_paper_id"])
        self.assertNotEqual(first["import_job_id"], second["import_job_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(1, len(self.rows("source_papers")))
        self.assertEqual(2, len(self.rows("import_jobs")))
        self.assertEqual(1, len(list(self.storage_root.rglob("*.pdf"))))

    def test_same_idempotency_key_returns_original_job(self):
        first = self.intake(idempotency_key="upload-token-1")
        second = self.intake(page_range="3", idempotency_key="upload-token-1")

        self.assertEqual(first["source_paper_id"], second["source_paper_id"])
        self.assertEqual(first["import_job_id"], second["import_job_id"])
        self.assertEqual(1, len(self.rows("source_papers")))
        self.assertEqual(1, len(self.rows("import_jobs")))

    def test_different_idempotency_keys_keep_existing_duplicate_semantics(self):
        first = self.intake(idempotency_key="upload-token-1")
        second = self.intake(page_range="3", idempotency_key="upload-token-2")

        self.assertEqual(first["source_paper_id"], second["source_paper_id"])
        self.assertNotEqual(first["import_job_id"], second["import_job_id"])
        self.assertEqual(2, len(self.rows("import_jobs")))

    def test_duplicate_with_missing_archive_is_rejected_without_new_job(self):
        first = self.intake()
        (self.storage_root / first["stored_path"]).unlink()
        with self.assertRaisesRegex(PdfIntakeError, "归档文件"):
            self.intake(page_range="3")
        self.assertEqual(1, len(self.rows("source_papers")))
        self.assertEqual(1, len(self.rows("import_jobs")))

    def test_non_pdf_extension_fake_pdf_and_empty_file_are_rejected(self):
        cases = {
            "wrong.txt": MINIMAL_PDF,
            "fake.pdf": b"not a pdf",
            "empty.pdf": b"",
        }
        for filename, content in cases.items():
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_bytes(content)
                with self.assertRaises(PdfIntakeError):
                    self.intake(path)
        self.assertEqual([], self.rows("source_papers"))

    def test_missing_directory_and_non_regular_file_are_rejected(self):
        for path in (self.root / "missing.pdf", self.root / "folder.pdf"):
            if path.name == "folder.pdf":
                path.mkdir()
            with self.subTest(path=path):
                with self.assertRaises(PdfIntakeError):
                    self.intake(path)

    def test_year_is_optional_but_invalid_years_are_rejected(self):
        result = self.intake(exam_year=None)
        self.assertIn("unknown", result["stored_path"])
        for year in (1899, 10000, "2025"):
            with self.subTest(year=year):
                with self.assertRaises(PdfIntakeError):
                    self.intake(exam_year=year)

    def test_invalid_page_ranges_are_rejected(self):
        for value in ("0", "0-2", "6-1", "1-2-3", "1 - 2", "abc", 3):
            with self.subTest(value=value):
                with self.assertRaises(PdfIntakeError):
                    self.intake(page_range=value)

    def test_unknown_region_and_exam_type_are_rejected(self):
        for overrides in ({"region_code": "XX"}, {"exam_type_code": "UNKNOWN"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(PdfIntakeError):
                    self.intake(**overrides)
        self.assertEqual([], self.rows("source_papers"))

    def test_stored_path_is_relative_safe_and_stays_inside_storage_root(self):
        result = self.intake(
            paper_name="../../escape",
            region_code="TJ",
            exam_type_code="GK",
        )
        stored_path = Path(result["stored_path"])
        self.assertFalse(stored_path.is_absolute())
        self.assertNotIn("..", stored_path.parts)
        destination = (self.storage_root / stored_path).resolve()
        self.assertTrue(destination.is_relative_to(self.storage_root.resolve()))
        self.assertIn("2025-TJ-GK-", destination.name)

    def test_copy_interruption_leaves_no_final_file_or_database_rows(self):
        with mock.patch(
            "src.importing.pdf_intake._copy_and_hash",
            side_effect=OSError("simulated interruption"),
        ):
            with self.assertRaises(PdfIntakeError):
                self.intake()
        self.assertEqual([], self.rows("source_papers"))
        self.assertEqual([], self.rows("import_jobs"))
        self.assertEqual([], list(self.storage_root.rglob("*.pdf")))

    def test_hash_mismatch_leaves_no_final_file_or_database_rows(self):
        with mock.patch(
            "src.importing.pdf_intake._copy_and_hash",
            return_value="0" * 64,
        ):
            with self.assertRaisesRegex(PdfIntakeError, "哈希"):
                self.intake()
        self.assertEqual([], self.rows("source_papers"))
        self.assertEqual([], self.rows("import_jobs"))
        self.assertEqual([], list(self.storage_root.rglob("*.pdf")))

    def test_database_constraints_reject_invalid_status_and_absolute_path(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO source_papers
                       (sha256, file_size, original_filename, stored_path,
                        region_code, exam_type_code, paper_name)
                       VALUES (?, 1, 'a.pdf', '/absolute/a.pdf', 'TJ', 'GK', 'x')""",
                    ("a" * 64,),
                )
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256, file_size, original_filename, stored_path,
                    region_code, exam_type_code, paper_name)
                   VALUES (?, 1, 'a.pdf', 'raw_papers/TJ/unknown/a.pdf', 'TJ', 'GK', 'x')""",
                ("b" * 64,),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO import_jobs (source_paper_id, status) VALUES (?, 'queued')",
                    (source_id,),
                )

    def test_cli_outputs_valid_json(self):
        command = [
            sys.executable,
            "-m",
            "src.importing.pdf_intake",
            str(self.pdf_path),
            "--region", "TJ",
            "--year", "2025",
            "--exam-type", "GK",
            "--paper-name", "2025 年天津高考数学",
            "--pages", "1-6",
            "--database", str(self.db_path),
            "--private-storage-root", str(self.storage_root),
        ]
        completed = subprocess.run(
            command, cwd=PROJECT_ROOT, capture_output=True, text=True
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            {"source_paper_id", "import_job_id", "stored_path", "sha256",
             "deduplicated", "status"},
            set(result),
        )


if __name__ == "__main__":
    unittest.main()
