import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.web.app import create_app


class UploadConfirmationWebTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "question-bank.db"
        self.private_root = self.root / "private"
        initialize_database(self.database_path).close()
        self.client = TestClient(
            create_app(
                database_path=self.database_path,
                private_root=self.private_root,
            )
        )

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def pdf_bytes(self, page_count=2):
        document = pymupdf.open()
        for _ in range(page_count):
            document.new_page()
        content = document.tobytes()
        document.close()
        return content

    def csrf(self):
        self.client.get("/imports/new")
        return self.client.cookies.get("basket_csrf")

    def database_counts(self):
        with sqlite3.connect(self.database_path) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("source_papers", "import_jobs")
            )

    def preview_pdf(self, filename="天津月考.pdf", page_count=2):
        response = self.client.post(
            "/imports/preview",
            data={"csrf_token": self.csrf()},
            files={"pdf_file": (filename, self.pdf_bytes(page_count), "application/pdf")},
        )
        token = re.search(r"/imports/([0-9a-f]{64})/confirm", response.text).group(1)
        return response, token

    def test_upload_page_has_multipart_form_and_safety_notice(self):
        response = self.client.get("/imports/new")

        self.assertEqual(200, response.status_code)
        self.assertIn('action="/imports/preview"', response.text)
        self.assertIn('enctype="multipart/form-data"', response.text)
        self.assertIn('type="file"', response.text)
        self.assertIn('name="csrf_token"', response.text)
        self.assertIn("上传预览不会入库", response.text)
        self.assertIn("点击确认后才创建任务", response.text)

    def test_papers_page_has_prominent_import_link(self):
        response = self.client.get("/papers")

        self.assertEqual(200, response.status_code)
        self.assertIn('class="button" href="/imports/new"', response.text)
        self.assertIn("导入新试卷", response.text)

    def test_preview_real_pdf_shows_details_without_creating_database_rows(self):
        content = self.pdf_bytes(page_count=2)

        with patch("src.web.app.intake_pdf") as intake:
            response = self.client.post(
                "/imports/preview",
                data={"csrf_token": self.csrf()},
                files={"pdf_file": ("天津月考.pdf", content, "application/pdf")},
            )
        intake.assert_not_called()

        self.assertEqual(200, response.status_code)
        self.assertIn("确认导入", response.text)
        self.assertIn("天津月考.pdf", response.text)
        self.assertIn("2 页", response.text)
        self.assertIn(f"{len(content)} 字节", response.text)
        self.assertIn(hashlib.sha256(content).hexdigest(), response.text)
        self.assertEqual((0, 0), self.database_counts())
        pending = list((self.private_root / "pending_uploads").iterdir())
        self.assertEqual(1, len(pending))
        manifest = json.loads((pending[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "token",
                "original_filename",
                "stored_filename",
                "size",
                "sha256",
                "page_count",
            },
            set(manifest),
        )
        self.assertEqual("天津月考.pdf", manifest["original_filename"])
        self.assertEqual(len(content), manifest["size"])
        self.assertEqual(2, manifest["page_count"])

    def test_confirm_with_csrf_creates_pending_job_and_removes_staging(self):
        _, token = self.preview_pdf(filename="和平区月考.pdf", page_count=2)

        response = self.client.post(
            f"/imports/{token}/confirm",
            data={
                "csrf_token": self.client.cookies.get("basket_csrf"),
                "paper_name": "和平区高一月考",
                "region_code": "TJ",
                "exam_year": "2026",
                "exam_type_code": "YK",
                "page_range": "1-2",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/papers", response.headers["location"])
        self.assertEqual((1, 1), self.database_counts())
        with sqlite3.connect(self.database_path) as connection:
            paper = connection.execute(
                "SELECT original_filename, paper_name FROM source_papers"
            ).fetchone()
            job = connection.execute(
                "SELECT page_start, page_end, status FROM import_jobs"
            ).fetchone()
        self.assertEqual(("和平区月考.pdf", "和平区高一月考"), paper)
        self.assertEqual((1, 2, "pending"), job)
        self.assertFalse((self.private_root / "pending_uploads" / token).exists())

    def test_cancel_removes_staging_without_creating_rows(self):
        _, token = self.preview_pdf()

        response = self.client.post(
            f"/imports/{token}/cancel",
            data={"csrf_token": self.client.cookies.get("basket_csrf")},
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/papers", response.headers["location"])
        self.assertEqual((0, 0), self.database_counts())
        self.assertFalse((self.private_root / "pending_uploads" / token).exists())

    def test_invalid_uploads_leave_no_staging_or_database_rows(self):
        valid_pdf = self.pdf_bytes(page_count=1)
        cases = (
            ("wrong.txt", valid_pdf),
            ("fake.pdf", b"not a pdf"),
            ("damaged.pdf", b"%PDF-1.7\nbroken"),
            ("empty.pdf", b""),
            ("oversized.pdf", b"%PDF-" + b"0" * (50 * 1024 * 1024)),
        )

        for filename, content in cases:
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/imports/preview",
                    data={"csrf_token": self.csrf()},
                    files={"pdf_file": (filename, content, "application/pdf")},
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual((0, 0), self.database_counts())
                pending_root = self.private_root / "pending_uploads"
                self.assertEqual([], list(pending_root.iterdir()) if pending_root.exists() else [])

    def test_all_import_writes_require_valid_csrf(self):
        content = self.pdf_bytes(page_count=1)
        preview = self.client.post(
            "/imports/preview",
            data={"csrf_token": "wrong"},
            files={"pdf_file": ("csrf.pdf", content, "application/pdf")},
        )
        self.assertEqual(403, preview.status_code)
        self.assertEqual((0, 0), self.database_counts())

        _, token = self.preview_pdf(page_count=1)
        confirm = self.client.post(
            f"/imports/{token}/confirm",
            data={
                "csrf_token": "wrong",
                "paper_name": "CSRF 测试",
                "region_code": "TJ",
                "exam_year": "",
                "exam_type_code": "YK",
                "page_range": "1",
            },
        )
        cancel = self.client.post(
            f"/imports/{token}/cancel", data={"csrf_token": "wrong"}
        )
        self.assertEqual(403, confirm.status_code)
        self.assertEqual(403, cancel.status_code)
        self.assertEqual((0, 0), self.database_counts())
        self.assertTrue((self.private_root / "pending_uploads" / token).is_dir())

    def test_invalid_tokens_tampering_and_repeated_confirm_are_safe(self):
        csrf = self.csrf()
        for token in ("not-a-token", "a" * 63, "g" * 64):
            response = self.client.post(
                f"/imports/{token}/confirm",
                data={"csrf_token": csrf},
            )
            self.assertEqual(400, response.status_code)
            self.assertNotIn(str(self.root), response.text)
        traversal = self.client.post(
            "/imports/%2e%2e%2f%2e%2e%2fetc/confirm",
            data={"csrf_token": csrf},
        )
        self.assertIn(traversal.status_code, (400, 404))
        self.assertEqual((0, 0), self.database_counts())

        _, file_token = self.preview_pdf(page_count=1)
        staged_file = next(
            path
            for path in (self.private_root / "pending_uploads" / file_token).iterdir()
            if path.suffix.lower() == ".pdf"
        )
        staged_file.write_bytes(staged_file.read_bytes() + b"tampered")
        response = self.client.post(
            f"/imports/{file_token}/confirm",
            data={
                "csrf_token": self.client.cookies.get("basket_csrf"),
                "paper_name": "被篡改文件",
                "region_code": "TJ",
                "exam_year": "",
                "exam_type_code": "YK",
                "page_range": "1",
            },
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual((0, 0), self.database_counts())

        _, manifest_token = self.preview_pdf(page_count=1)
        manifest_path = self.private_root / "pending_uploads" / manifest_token / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["page_count"] = "not-an-integer"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        response = self.client.post(
            f"/imports/{manifest_token}/confirm",
            data={"csrf_token": self.client.cookies.get("basket_csrf")},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual((0, 0), self.database_counts())

        _, repeated_token = self.preview_pdf(page_count=1)
        confirm_data = {
            "csrf_token": self.client.cookies.get("basket_csrf"),
            "paper_name": "只导入一次",
            "region_code": "TJ",
            "exam_year": "",
            "exam_type_code": "YK",
            "page_range": "1",
        }
        first = self.client.post(
            f"/imports/{repeated_token}/confirm", data=confirm_data, follow_redirects=False
        )
        second = self.client.post(
            f"/imports/{repeated_token}/confirm", data=confirm_data, follow_redirects=False
        )
        self.assertEqual(303, first.status_code)
        self.assertEqual(400, second.status_code)
        self.assertEqual((1, 1), self.database_counts())

    def test_invalid_metadata_keeps_confirmation_recoverable_without_import(self):
        invalid_values = (
            ("region_code", "XX"),
            ("exam_type_code", "XX"),
            ("exam_year", "1899"),
            ("exam_year", "二〇二六"),
            ("page_range", "2-1"),
            ("page_range", "1-3"),
        )
        for field, invalid in invalid_values:
            with self.subTest(field=field, invalid=invalid):
                _, token = self.preview_pdf(page_count=2)
                values = {
                    "csrf_token": self.client.cookies.get("basket_csrf"),
                    "paper_name": "可修正元数据",
                    "region_code": "TJ",
                    "exam_year": "2026",
                    "exam_type_code": "YK",
                    "page_range": "1-2",
                }
                values[field] = invalid
                response = self.client.post(f"/imports/{token}/confirm", data=values)
                self.assertEqual(400, response.status_code)
                self.assertIn("确认导入", response.text)
                self.assertIn('name="paper_name"', response.text)
                self.assertTrue((self.private_root / "pending_uploads" / token).is_dir())
                self.assertEqual((0, 0), self.database_counts())


if __name__ == "__main__":
    unittest.main()
