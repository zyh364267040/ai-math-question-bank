import hashlib
import json
import re
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from src.database.initialize import initialize_database
from src.web.app import PreviewUploadBodyLimitMiddleware, create_app


class PreviewUploadBodyLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, headers, chunks, limit=10, path="/imports/preview"):
        state = {"downstream_called": False, "parsed": False, "receive_calls": 0}
        messages = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]

        async def downstream(scope, receive, send):
            state["downstream_called"] = True
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
            state["parsed"] = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            state["receive_calls"] += 1
            return messages.pop(0)

        sent = []

        async def send(message):
            sent.append(message)

        middleware = PreviewUploadBodyLimitMiddleware(downstream, max_body_bytes=limit)
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": headers,
            },
            receive,
            send,
        )
        return state, sent

    async def test_large_content_length_is_rejected_without_calling_downstream(self):
        state, sent = await self.invoke([(b"content-length", b"11")], [b"ignored"])

        self.assertFalse(state["downstream_called"])
        self.assertEqual(0, state["receive_calls"])
        self.assertEqual(413, sent[0]["status"])

    async def test_missing_or_forged_small_content_length_is_stream_counted(self):
        for headers in ([], [(b"content-length", b"5")]):
            with self.subTest(headers=headers):
                state, sent = await self.invoke(headers, [b"123456", b"78901"])
                self.assertTrue(state["downstream_called"])
                self.assertFalse(state["parsed"])
                self.assertEqual(413, sent[0]["status"])
                body = b"".join(
                    message.get("body", b"")
                    for message in sent
                    if message["type"] == "http.response.body"
                )
                self.assertNotIn(str(Path.home()).encode(), body)

    async def test_render_start_form_has_a_small_streaming_request_limit(self):
        state, sent = await self.invoke(
            [], [b"123456", b"78901"], limit=10, path="/imports/123/render"
        )

        self.assertTrue(state["downstream_called"])
        self.assertFalse(state["parsed"])
        self.assertEqual(413, sent[0]["status"])


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
                "signature",
            },
            set(manifest),
        )
        self.assertEqual("天津月考.pdf", manifest["original_filename"])
        self.assertEqual(len(content), manifest["size"])
        self.assertEqual(2, manifest["page_count"])
        self.assertRegex(manifest["signature"], r"\A[0-9a-f]{64}\Z")

    def confirm_values(self, csrf=None, **overrides):
        values = {
            "csrf_token": csrf or self.client.cookies.get("basket_csrf"),
            "paper_name": "真实性测试",
            "region_code": "TJ",
            "exam_year": "2026",
            "exam_type_code": "YK",
            "page_range": "1",
        }
        values.update(overrides)
        return values

    def test_manifest_key_is_persistent_private_and_survives_app_restart(self):
        _, token = self.preview_pdf(filename="重启确认.pdf", page_count=1)
        key_path = self.private_root / ".upload_manifest_hmac.key"

        self.assertTrue(key_path.is_file())
        self.assertFalse(key_path.is_symlink())
        self.assertGreaterEqual(len(key_path.read_bytes()), 32)
        self.assertEqual(0o600, stat.S_IMODE(key_path.stat().st_mode))
        original_key = key_path.read_bytes()

        with TestClient(create_app(self.database_path, self.private_root)) as restarted:
            restarted.cookies.set("basket_csrf", self.client.cookies.get("basket_csrf"))
            response = restarted.post(
                f"/imports/{token}/confirm",
                data=self.confirm_values(),
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertEqual(original_key, key_path.read_bytes())
        self.assertEqual((1, 1), self.database_counts())

    def test_consistent_manifest_and_pdf_tampering_is_rejected(self):
        _, token = self.preview_pdf(filename="原始名称.pdf", page_count=1)
        directory = self.private_root / "pending_uploads" / token
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_pdf = directory / manifest["stored_filename"]
        replacement = self.pdf_bytes(page_count=2)
        new_pdf = directory / "攻击者改名.pdf"
        old_pdf.unlink()
        new_pdf.write_bytes(replacement)
        manifest.update(
            {
                "original_filename": new_pdf.name,
                "stored_filename": new_pdf.name,
                "size": len(replacement),
                "sha256": hashlib.sha256(replacement).hexdigest(),
                "page_count": 2,
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        response = self.client.post(
            f"/imports/{token}/confirm",
            data=self.confirm_values(page_range="1-2"),
            follow_redirects=False,
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual((0, 0), self.database_counts())

    def test_manifest_field_changes_and_field_set_changes_are_rejected(self):
        mutations = (
            lambda value: value.__setitem__("original_filename", "changed.pdf"),
            lambda value: value.__setitem__("size", value["size"] + 1),
            lambda value: value.__setitem__("sha256", "0" * 64),
            lambda value: value.__setitem__("page_count", value["page_count"] + 1),
            lambda value: value.__setitem__("unexpected", "field"),
            lambda value: value.pop("page_count"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                _, token = self.preview_pdf(page_count=1)
                manifest_path = (
                    self.private_root / "pending_uploads" / token / "manifest.json"
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                response = self.client.post(
                    f"/imports/{token}/confirm",
                    data=self.confirm_values(),
                    follow_redirects=False,
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual((0, 0), self.database_counts())

    def test_manifest_and_pdf_symbolic_links_are_rejected_without_database_writes(self):
        for target_kind in ("manifest", "pdf"):
            with self.subTest(target_kind=target_kind):
                _, token = self.preview_pdf(page_count=1)
                directory = self.private_root / "pending_uploads" / token
                manifest_path = directory / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                target = (
                    manifest_path
                    if target_kind == "manifest"
                    else directory / manifest["stored_filename"]
                )
                external = self.root / f"external-{target_kind}"
                external.write_bytes(target.read_bytes())
                target.unlink()
                target.symlink_to(external)

                response = self.client.post(
                    f"/imports/{token}/confirm",
                    data=self.confirm_values(),
                    follow_redirects=False,
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual((0, 0), self.database_counts())

    def test_hmac_key_and_pending_directory_symbolic_links_are_rejected(self):
        self.client.close()
        external_key = self.root / "external-key"
        external_key.write_bytes(b"k" * 32)
        self.private_root.mkdir(parents=True, exist_ok=True)
        (self.private_root / ".upload_manifest_hmac.key").symlink_to(external_key)
        with TestClient(create_app(self.database_path, self.private_root)) as client:
            csrf = client.get("/imports/new").cookies.get("basket_csrf") or client.cookies.get(
                "basket_csrf"
            )
            response = client.post(
                "/imports/preview",
                data={"csrf_token": csrf},
                files={"pdf_file": ("key-link.pdf", self.pdf_bytes(1), "application/pdf")},
            )
        self.assertEqual(400, response.status_code)
        self.assertEqual(b"k" * 32, external_key.read_bytes())
        self.assertEqual((0, 0), self.database_counts())

        (self.private_root / ".upload_manifest_hmac.key").unlink()
        external_pending = self.root / "external-pending"
        external_pending.mkdir()
        (self.private_root / "pending_uploads").symlink_to(external_pending, target_is_directory=True)
        with TestClient(create_app(self.database_path, self.private_root)) as client:
            csrf = client.get("/imports/new").cookies.get("basket_csrf") or client.cookies.get(
                "basket_csrf"
            )
            response = client.post(
                "/imports/preview",
                data={"csrf_token": csrf},
                files={"pdf_file": ("dir-link.pdf", self.pdf_bytes(1), "application/pdf")},
            )
        self.assertEqual(400, response.status_code)
        self.assertEqual([], list(external_pending.iterdir()))
        self.assertEqual((0, 0), self.database_counts())

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
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM import_page_render_runs"
                ).fetchone()[0],
            )
        self.assertFalse((self.private_root / "processing").exists())
        self.assertFalse((self.private_root / "pending_uploads" / token).exists())

    def test_concurrent_confirm_requests_create_at_most_one_job(self):
        _, token = self.preview_pdf(filename="并发确认.pdf", page_count=2)
        csrf = self.client.cookies.get("basket_csrf")
        values = {
            "csrf_token": csrf,
            "paper_name": "并发确认",
            "region_code": "TJ",
            "exam_year": "2026",
            "exam_type_code": "YK",
            "page_range": "1-2",
        }
        barrier = threading.Barrier(2)
        responses = []
        errors = []
        from src.importing.upload_confirmation import validate_import_metadata as real_validate

        def synchronized_validate(form, page_count):
            result = real_validate(form, page_count)
            barrier.wait(timeout=5)
            return result

        def confirm():
            try:
                with TestClient(
                    create_app(self.database_path, self.private_root)
                ) as client:
                    client.cookies.set("basket_csrf", csrf)
                    responses.append(
                        client.post(
                            f"/imports/{token}/confirm",
                            data=values,
                            follow_redirects=False,
                        )
                    )
            except BaseException as error:  # surfaced below with both thread results
                errors.append(error)

        with patch("src.web.app.validate_import_metadata", side_effect=synchronized_validate):
            threads = [threading.Thread(target=confirm) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(2, len(responses))
        self.assertTrue(all(response.status_code in (303, 409) for response in responses))
        self.assertEqual((1, 1), self.database_counts())

    def test_retry_after_commit_before_staging_cleanup_is_idempotent(self):
        _, token = self.preview_pdf(filename="提交后重试.pdf", page_count=1)
        values = {
            "csrf_token": self.client.cookies.get("basket_csrf"),
            "paper_name": "提交后重试",
            "region_code": "TJ",
            "exam_year": "2026",
            "exam_type_code": "YK",
            "page_range": "1",
        }
        with patch("src.web.app.discard_staged_upload"):
            first = self.client.post(
                f"/imports/{token}/confirm", data=values, follow_redirects=False
            )
            second = self.client.post(
                f"/imports/{token}/confirm", data=values, follow_redirects=False
            )

        self.assertEqual(303, first.status_code)
        self.assertEqual(303, second.status_code)
        self.assertEqual((1, 1), self.database_counts())

    def test_confirm_cancel_race_has_only_one_successful_operation(self):
        _, token = self.preview_pdf(filename="确认取消竞争.pdf", page_count=1)
        csrf = self.client.cookies.get("basket_csrf")
        committed = threading.Event()
        cancel_finished = threading.Event()
        responses = {}
        errors = []
        from src.importing.pdf_intake import intake_pdf as real_intake

        def intake_then_pause(*args, **kwargs):
            result = real_intake(*args, **kwargs)
            committed.set()
            cancel_finished.wait(timeout=0.5)
            return result

        def confirm():
            try:
                with TestClient(create_app(self.database_path, self.private_root)) as client:
                    client.cookies.set("basket_csrf", csrf)
                    responses["confirm"] = client.post(
                        f"/imports/{token}/confirm",
                        data={
                            "csrf_token": csrf,
                            "paper_name": "确认取消竞争",
                            "region_code": "TJ",
                            "exam_year": "2026",
                            "exam_type_code": "YK",
                            "page_range": "1",
                        },
                        follow_redirects=False,
                    )
            except BaseException as error:
                errors.append(error)

        def cancel():
            try:
                self.assertTrue(committed.wait(timeout=5))
                with TestClient(create_app(self.database_path, self.private_root)) as client:
                    client.cookies.set("basket_csrf", csrf)
                    responses["cancel"] = client.post(
                        f"/imports/{token}/cancel",
                        data={"csrf_token": csrf},
                        follow_redirects=False,
                    )
            except BaseException as error:
                errors.append(error)
            finally:
                cancel_finished.set()

        with patch("src.web.app.intake_pdf", side_effect=intake_then_pause):
            threads = [
                threading.Thread(target=confirm),
                threading.Thread(target=cancel),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual({"confirm", "cancel"}, set(responses))
        successes = [name for name, response in responses.items() if response.status_code == 303]
        self.assertEqual(1, len(successes), responses)
        if responses["cancel"].status_code == 303:
            self.assertEqual((0, 0), self.database_counts())
        self.assertNotIn(500, [response.status_code for response in responses.values()])

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

    def test_entry_body_limit_rejects_before_staging_or_database_writes(self):
        with patch("src.web.app.stage_pdf_upload") as stage:
            with TestClient(
                create_app(
                    self.database_path,
                    self.private_root,
                    preview_request_max_bytes=256,
                )
            ) as client:
                response = client.post(
                    "/imports/preview",
                    data={"csrf_token": "x" * 64},
                    files={"pdf_file": ("large.pdf", b"%PDF-" + b"x" * 512)},
                )

        self.assertEqual(413, response.status_code)
        stage.assert_not_called()
        self.assertEqual((0, 0), self.database_counts())
        pending_root = self.private_root / "pending_uploads"
        self.assertFalse(pending_root.exists())

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
