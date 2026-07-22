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
from src.pipeline.automatic_import import (
    AutomaticImportOutcome,
    resume_interrupted_automatic_imports,
    run_automatic_import,
)
from src.processing.pdf_page_renderer import claim_render_job, run_claimed_render
from src.processing.question_splitter import (
    SAFE_CODEX_MISSING,
    CodexExecutionError,
    CodexRunResult,
    claim_split_job,
    run_claimed_split,
)
from src.web.app import PreviewUploadBodyLimitMiddleware, create_app


class OneQuestionSplitRunner:
    def __init__(self):
        self.calls = []

    def run(self, *, image_paths, prompt):
        self.calls.append((tuple(image_paths), prompt))
        job_id = int(re.search(r"import_job_id=(\d+)", prompt).group(1))
        return CodexRunResult(json.dumps({
            "version": 1,
            "import_job_id": job_id,
            "question_count": 1,
            "questions": [{
                "question_no": 1,
                "regions": [{
                    "page_number": 1,
                    "bbox_normalized": [0.05, 0.05, 0.95, 0.95],
                }],
                "warnings": [],
                "confidence": 0.9,
            }],
        }), "automatic-test-run")


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

    async def test_layout_start_chunked_form_without_content_length_is_limited(self):
        state, sent = await self.invoke(
            [], [b"a" * 8, b"b" * 8], limit=10, path="/imports/123/layout"
        )

        self.assertTrue(state["downstream_called"])
        self.assertFalse(state["parsed"])
        self.assertEqual(413, sent[0]["status"])


class UploadConfirmationWebTestCases:
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
                auto_submit=lambda callback: None,
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
        self.assertIn("确认后才创建任务并自动处理页面与切题", response.text)

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

        with TestClient(create_app(
            self.database_path, self.private_root, auto_submit=lambda callback: None
        )) as restarted:
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
        with TestClient(create_app(
            self.database_path, self.private_root, auto_submit=lambda callback: None
        )) as client:
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
        with TestClient(create_app(
            self.database_path, self.private_root, auto_submit=lambda callback: None
        )) as client:
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
        self.assertRegex(response.headers["location"], r"\A/imports/\d+/split\Z")
        self.assertEqual((1, 1), self.database_counts())
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

    def test_one_confirm_returns_before_and_enqueues_render_then_split(self):
        queued = []
        events = []

        class SplitRunner:
            def run(inner_self, *, image_paths, prompt):
                events.append("codex")
                job_id = int(re.search(r"import_job_id=(\d+)", prompt).group(1))
                return CodexRunResult(json.dumps({
                    "version": 1,
                    "import_job_id": job_id,
                    "question_count": 1,
                    "questions": [{
                        "question_no": 1,
                        "regions": [{
                            "page_number": 1,
                            "bbox_normalized": [0.05, 0.05, 0.95, 0.95],
                        }],
                        "warnings": [],
                        "confidence": 0.9,
                    }],
                }), "auto-confirm-run")

        def render_worker(claim):
            events.append("render")
            return run_claimed_render(claim)

        def split_worker(claim):
            events.append("split")
            return run_claimed_split(claim)

        self.client.close()
        self.client = TestClient(create_app(
            self.database_path,
            self.private_root,
            split_runner=SplitRunner(),
            auto_submit=queued.append,
            render_worker=render_worker,
            split_worker=split_worker,
        ))
        _, token = self.preview_pdf(filename="自动流水线.pdf", page_count=1)

        response = self.client.post(
            f"/imports/{token}/confirm",
            data=self.confirm_values(),
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertRegex(response.headers["location"], r"\A/imports/\d+/split\Z")
        self.assertEqual([], events, "确认请求不应等待后台 runner")
        self.assertEqual(1, len(queued))
        job_id = int(response.headers["location"].split("/")[2])
        waiting = self.client.get(response.headers["location"])
        self.assertEqual(200, waiting.status_code)
        self.assertIn("后台自动处理", waiting.text)
        self.assertIn("页面处理中", waiting.text)

        queued.pop()()

        self.assertEqual(["render", "split", "codex"], events)
        completed = self.client.get(f"/imports/{job_id}/split")
        self.assertEqual(200, completed.status_code)
        self.assertIn("共切分 1 题", completed.text)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0])

    def test_automatic_render_failure_never_calls_split_and_is_retryable(self):
        queued = []
        split_calls = []

        def fail_render(claim):
            job_id = claim.job_id
            claim.close()
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """UPDATE import_page_render_runs
                       SET status='failed', error_message='页面处理失败，请重试'
                       WHERE import_job_id=?""",
                    (job_id,),
                )
            return None

        self.client.close()
        self.client = TestClient(create_app(
            self.database_path,
            self.private_root,
            split_runner=object(),
            auto_submit=queued.append,
            render_worker=fail_render,
            split_worker=lambda claim: split_calls.append(claim),
        ))
        _, token = self.preview_pdf(filename="渲染失败.pdf", page_count=1)
        response = self.client.post(
            f"/imports/{token}/confirm",
            data=self.confirm_values(),
            follow_redirects=False,
        )

        queued.pop()()

        self.assertEqual([], split_calls)
        failed = self.client.get(response.headers["location"])
        self.assertEqual(200, failed.status_code)
        self.assertIn("页面处理失败，请重试", failed.text)
        self.assertIn("重试页面处理", failed.text)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM import_question_split_runs"
            ).fetchone()[0])

    def test_missing_codex_after_render_has_safe_failed_state(self):
        queued = []

        class MissingCodexRunner:
            def run(inner_self, *, image_paths, prompt):
                raise FileNotFoundError(
                    f"Codex executable is missing under {self.root}"
                )

        self.client.close()
        self.client = TestClient(create_app(
            self.database_path,
            self.private_root,
            split_runner=MissingCodexRunner(),
            auto_submit=queued.append,
        ))
        _, token = self.preview_pdf(filename="缺少Codex.pdf", page_count=1)
        response = self.client.post(
            f"/imports/{token}/confirm",
            data=self.confirm_values(),
            follow_redirects=False,
        )

        queued.pop()()

        failed = self.client.get(response.headers["location"])
        self.assertEqual(200, failed.status_code)
        self.assertIn("自动切题失败", failed.text)
        self.assertIn("可手动重试 Codex 自动切题", failed.text)
        self.assertNotIn(str(self.root), failed.text)
        with sqlite3.connect(self.database_path) as connection:
            split_status, error_message = connection.execute(
                """SELECT status,error_message FROM import_question_split_runs
                   WHERE import_job_id=(
                       SELECT import_job_id FROM import_upload_receipts WHERE token=?
                   )""",
                (token,),
            ).fetchone()
        self.assertEqual("failed", split_status)
        self.assertEqual("Codex 自动切题失败，请重试", error_message)

    def test_claim_stage_missing_codex_is_failed_and_manually_retryable(self):
        queued = []
        self.client.close()
        self.client = TestClient(create_app(
            self.database_path,
            self.private_root,
            auto_submit=queued.append,
        ))
        _, token = self.preview_pdf(filename="领取阶段缺少Codex.pdf", page_count=1)
        response = self.client.post(
            f"/imports/{token}/confirm",
            data=self.confirm_values(),
            follow_redirects=False,
        )
        job_id = int(response.headers["location"].split("/")[2])

        with patch(
            "src.processing.question_splitter._resolve_codex_bin",
            side_effect=CodexExecutionError(SAFE_CODEX_MISSING),
        ):
            queued.pop()()

        failed = self.client.get(f"/imports/{job_id}/split")
        self.assertEqual(200, failed.status_code)
        self.assertIn(SAFE_CODEX_MISSING, failed.text)
        self.assertIn("重试调用 Codex 自动切题", failed.text)
        self.assertNotIn("即将开始", failed.text)
        self.assertNotIn('http-equiv="refresh"', failed.text)
        self.assertNotIn(str(self.root), failed.text)
        with sqlite3.connect(self.database_path) as connection:
            split_run = connection.execute(
                """SELECT status,error_message FROM import_question_split_runs
                   WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
            question_count = connection.execute(
                "SELECT COUNT(*) FROM questions"
            ).fetchone()[0]
        self.assertEqual(("failed", SAFE_CODEX_MISSING), split_run)
        self.assertEqual(0, question_count)

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
        queued = []
        runner = OneQuestionSplitRunner()
        from src.importing.upload_confirmation import validate_import_metadata as real_validate

        def synchronized_validate(form, page_count):
            result = real_validate(form, page_count)
            barrier.wait(timeout=5)
            return result

        def confirm():
            try:
                with TestClient(
                    create_app(
                        self.database_path,
                        self.private_root,
                        split_runner=runner,
                        auto_submit=queued.append,
                    )
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
        for callback in queued:
            callback()
        self.assertEqual(1, len(runner.calls))

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

    def test_repeat_confirm_after_staging_cleanup_redirects_same_job_once(self):
        queued = []
        runner = OneQuestionSplitRunner()
        self.client.close()
        self.client = TestClient(create_app(
            self.database_path,
            self.private_root,
            split_runner=runner,
            auto_submit=queued.append,
        ))
        _, token = self.preview_pdf(filename="清理后重复确认.pdf", page_count=1)
        values = self.confirm_values()

        first = self.client.post(
            f"/imports/{token}/confirm", data=values, follow_redirects=False
        )
        second = self.client.post(
            f"/imports/{token}/confirm", data=values, follow_redirects=False
        )

        self.assertEqual(303, first.status_code)
        self.assertEqual(303, second.status_code)
        self.assertEqual(first.headers["location"], second.headers["location"])
        self.assertEqual((1, 1), self.database_counts())
        for callback in queued:
            callback()
        self.assertEqual(1, len(runner.calls))

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
                with TestClient(create_app(
                    self.database_path,
                    self.private_root,
                    auto_submit=lambda callback: None,
                )) as client:
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
                with TestClient(create_app(
                    self.database_path,
                    self.private_root,
                    auto_submit=lambda callback: None,
                )) as client:
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


class AutomaticImportRestartRecoveryTests(unittest.TestCase):
    """Recover only persisted, user-authorized render-to-split work."""

    @staticmethod
    def pdf_bytes():
        document = pymupdf.open()
        document.new_page(width=72, height=72)
        content = document.tobytes()
        document.close()
        return content

    def confirm_one_import(self, database_path, private_root):
        queued = []
        client = TestClient(create_app(
            database_path,
            private_root,
            auto_submit=queued.append,
        ))
        client.get("/imports/new")
        csrf = client.cookies.get("basket_csrf")
        preview = client.post(
            "/imports/preview",
            data={"csrf_token": csrf},
            files={"pdf_file": ("restart.pdf", self.pdf_bytes(), "application/pdf")},
        )
        token = re.search(r"/imports/([0-9a-f]{64})/confirm", preview.text).group(1)
        response = client.post(
            f"/imports/{token}/confirm",
            data={
                "csrf_token": csrf,
                "paper_name": "服务重启恢复测试",
                "region_code": "TJ",
                "exam_year": "2026",
                "exam_type_code": "YK",
                "page_range": "1",
            },
            follow_redirects=False,
        )
        client.close()
        self.assertEqual(303, response.status_code)
        self.assertEqual(1, len(queued), "确认提交本身只应提交自动流水线")
        return int(response.headers["location"].split("/")[2])

    def test_startup_recovers_all_four_crash_windows_with_one_coordinator(self):
        for crash_window in (
            "after_confirmation",
            "during_render",
            "between_render_and_split",
            "during_split",
        ):
            with self.subTest(crash_window=crash_window), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                database_path = root / "question-bank.db"
                private_root = root / "private"
                initialize_database(database_path).close()
                job_id = self.confirm_one_import(database_path, private_root)

                if crash_window != "after_confirmation":
                    render_claim = claim_render_job(database_path, private_root, job_id)
                    self.assertIsNotNone(render_claim)
                    if crash_window == "during_render":
                        render_claim.close()
                    else:
                        self.assertIsNotNone(run_claimed_render(render_claim))
                        if crash_window == "during_split":
                            split_claim = claim_split_job(
                                database_path,
                                private_root,
                                job_id,
                                runner=OneQuestionSplitRunner(),
                            )
                            self.assertIsNotNone(split_claim)
                            split_claim.close()

                startup_callbacks = []
                runner = OneQuestionSplitRunner()
                with TestClient(create_app(
                    database_path,
                    private_root,
                    auto_submit=startup_callbacks.append,
                    split_runner=runner,
                )) as restarted:
                    self.assertEqual(200, restarted.get("/health").status_code)
                    self.assertEqual(
                        1,
                        len(startup_callbacks),
                        "startup 必须只提交一个串行恢复协调器",
                    )
                    coordinator = startup_callbacks[0]
                    coordinator()
                    coordinator()

                with sqlite3.connect(database_path) as connection:
                    statuses = connection.execute(
                        """SELECT r.status,s.status
                           FROM import_page_render_runs r
                           JOIN import_question_split_runs s
                             ON s.import_job_id=r.import_job_id
                           WHERE r.import_job_id=?""",
                        (job_id,),
                    ).fetchone()
                self.assertEqual(("completed", "completed"), statuses)
                self.assertEqual(1, len(runner.calls))

    def test_stale_processing_claim_initialization_failure_becomes_failed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            private_root = root / "private"
            initialize_database(database_path).close()
            job_id = self.confirm_one_import(database_path, private_root)
            self.assertIsNotNone(run_claimed_render(
                claim_render_job(database_path, private_root, job_id)
            ))
            stale_claim = claim_split_job(
                database_path,
                private_root,
                job_id,
                runner=OneQuestionSplitRunner(),
            )
            stale_claim.close()

            with patch(
                "src.processing.question_splitter._resolve_codex_bin",
                side_effect=CodexExecutionError(SAFE_CODEX_MISSING),
            ):
                outcome = run_automatic_import(database_path, private_root, job_id)

            self.assertEqual(AutomaticImportOutcome.FAILED, outcome)
            with sqlite3.connect(database_path) as connection:
                self.assertEqual(("failed", SAFE_CODEX_MISSING), connection.execute(
                    """SELECT status,error_message FROM import_question_split_runs
                       WHERE import_job_id=?""",
                    (job_id,),
                ).fetchone())

    def test_busy_global_render_lock_is_rescanned_without_restart(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            private_root = root / "private"
            initialize_database(database_path).close()
            blocker_id = self.confirm_one_import(database_path, private_root)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "DELETE FROM import_upload_receipts WHERE import_job_id=?",
                    (blocker_id,),
                )
            blocker = claim_render_job(database_path, private_root, blocker_id)
            remaining_ids = [
                self.confirm_one_import(database_path, private_root)
                for _ in range(2)
            ]
            runner = OneQuestionSplitRunner()
            sleep_calls = []

            self.assertEqual(
                AutomaticImportOutcome.BUSY,
                run_automatic_import(
                    database_path, private_root, remaining_ids[0], split_runner=runner
                ),
            )

            def release_after_first_busy(delay):
                sleep_calls.append(delay)
                blocker.close()

            resume_interrupted_automatic_imports(
                database_path,
                private_root,
                split_runner=runner,
                sleep=release_after_first_busy,
                backoff_seconds=0.25,
                max_rounds=3,
            )

            self.assertEqual([0.25], sleep_calls)
            with sqlite3.connect(database_path) as connection:
                statuses = connection.execute(
                    """SELECT r.import_job_id,r.status,s.status
                       FROM import_page_render_runs r
                       JOIN import_question_split_runs s
                         ON s.import_job_id=r.import_job_id
                       WHERE r.import_job_id IN (?,?) ORDER BY r.import_job_id""",
                    remaining_ids,
                ).fetchall()
            self.assertEqual(
                [(job_id, "completed", "completed") for job_id in remaining_ids],
                statuses,
            )
            self.assertEqual(2, len(runner.calls))

    def test_recovery_busy_backoff_does_not_spin(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            selected = self.make_selection_database(database_path)
            calls = []
            sleeps = []

            def always_busy(_database, _private, job_id, **_kwargs):
                calls.append(job_id)
                return AutomaticImportOutcome.BUSY

            resume_interrupted_automatic_imports(
                database_path,
                root / "private",
                automatic_import_runner=always_busy,
                sleep=sleeps.append,
                backoff_seconds=2.0,
                max_rounds=3,
            )

            self.assertEqual(selected * 3, calls)
            self.assertEqual([2.0, 2.0], sleeps)

    def test_active_job_completion_during_backoff_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            selected = self.make_selection_database(database_path)
            active_id = selected[-1]
            calls = []
            sleeps = []

            def active_then_completed(_database, _private, job_id, **_kwargs):
                calls.append(job_id)
                if job_id != active_id:
                    return AutomaticImportOutcome.NOOP
                with sqlite3.connect(database_path) as connection:
                    connection.execute(
                        """UPDATE import_question_split_runs
                           SET status='completed',question_count=1,processed_pages=1,
                               codex_run_id='active-worker',result_manifest_sha256=?,
                               render_manifest_sha256=?,source_pdf_sha256=?,
                               crop_manifest_sha256=?,crop_generation_id=?,
                               crop_manifest_signature=?,completed_at=CURRENT_TIMESTAMP
                           WHERE import_job_id=?""",
                        (
                            "c" * 64, "a" * 64, "b" * 64,
                            "d" * 64, "e" * 32, "f" * 64, job_id,
                        ),
                    )
                return AutomaticImportOutcome.BUSY

            resume_interrupted_automatic_imports(
                database_path,
                root / "private",
                automatic_import_runner=active_then_completed,
                sleep=sleeps.append,
                backoff_seconds=1.0,
                max_rounds=3,
            )

            self.assertEqual(1, calls.count(active_id))
            self.assertEqual([], sleeps)

    @staticmethod
    def add_job(connection, source_id, *, receipt=True, import_status="pending"):
        job_id = connection.execute(
            """INSERT INTO import_jobs
               (source_paper_id,page_start,page_end,status) VALUES (?,1,1,?)""",
            (source_id, import_status),
        ).lastrowid
        if receipt:
            connection.execute(
                """INSERT INTO import_upload_receipts
                   (token,source_paper_id,import_job_id) VALUES (?,?,?)""",
                (f"receipt-{job_id}", source_id, job_id),
            )
        return job_id

    @staticmethod
    def add_render(connection, job_id, status):
        values = (
            ("a" * 64, 10, "batch", "b" * 64)
            if status == "completed" else (None, None, None, None)
        )
        connection.execute(
            """INSERT INTO import_page_render_runs
               (import_job_id,status,dpi,total_pages,rendered_pages,
                manifest_sha256,manifest_byte_size,published_batch_id,source_pdf_sha256)
               VALUES (?,?,300,1,?,?,?,?,?)""",
            (job_id, status, 1 if status == "completed" else 0, *values),
        )

    @staticmethod
    def add_split(connection, job_id, status):
        if status == "completed":
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,question_count,processed_pages,codex_run_id,
                    result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                    crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                    completed_at)
                   VALUES (?,'completed',1,1,'run',?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (job_id, "c" * 64, "a" * 64, "b" * 64, "d" * 64, "e" * 32, "f" * 64),
            )
        else:
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,processed_pages) VALUES (?,?,0)""",
                (job_id, status),
            )

    def make_selection_database(self, database_path):
        initialize_database(database_path).close()
        with sqlite3.connect(database_path) as connection:
            source_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256,file_size,original_filename,stored_path,region_code,
                    exam_year,exam_type_code,paper_name)
                   VALUES (?,1,'scan.pdf','raw_papers/TJ/scan.pdf','TJ',2026,'YK','scan')""",
                ("0" * 64,),
            ).lastrowid
            selected = []
            selected.append(self.add_job(connection, source_id))
            job_id = self.add_job(connection, source_id)
            self.add_render(connection, job_id, "processing")
            selected.append(job_id)
            for split_status in (None, "pending", "processing"):
                job_id = self.add_job(connection, source_id)
                self.add_render(connection, job_id, "completed")
                if split_status is not None:
                    self.add_split(connection, job_id, split_status)
                selected.append(job_id)

            self.add_job(connection, source_id, receipt=False)
            job_id = self.add_job(connection, source_id)
            self.add_render(connection, job_id, "failed")
            for split_status in ("failed", "completed"):
                job_id = self.add_job(connection, source_id)
                self.add_render(connection, job_id, "completed")
                self.add_split(connection, job_id, split_status)
            for split_status in ("failed", "completed"):
                job_id = self.add_job(connection, source_id)
                self.add_render(connection, job_id, "processing")
                self.add_split(connection, job_id, split_status)
            self.add_job(connection, source_id, import_status="needs_review")
            job_id = self.add_job(connection, source_id, import_status="completed")
            self.add_render(connection, job_id, "processing")
        return selected

    def test_startup_selection_is_receipt_gated_and_serial_in_stable_id_order(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            expected = self.make_selection_database(database_path)
            callbacks = []
            events = []
            active = 0
            maximum_active = 0

            def fake_automatic_import(_database, _private, job_id, **_kwargs):
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                events.append(job_id)
                active -= 1

            with TestClient(create_app(
                database_path,
                root / "private",
                auto_submit=callbacks.append,
                automatic_import_runner=fake_automatic_import,
            )) as client:
                self.assertEqual(200, client.get("/health").status_code)
                self.assertEqual(1, len(callbacks))
                callbacks[0]()

            self.assertEqual(expected, events)
            self.assertEqual(1, maximum_active)

    def test_recovery_errors_are_contained_and_do_not_block_startup_or_leak_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            selected = self.make_selection_database(database_path)[:2]
            callbacks = []
            calls = []

            def failing_runner(_database, private_root, job_id, **_kwargs):
                calls.append(job_id)
                with sqlite3.connect(database_path) as connection:
                    connection.execute(
                        "UPDATE import_jobs SET status='failed' WHERE id=?",
                        (job_id,),
                    )
                raise OSError(f"private failure at {private_root}")

            with TestClient(create_app(
                database_path,
                root / "private-secret",
                auto_submit=callbacks.append,
                automatic_import_runner=failing_runner,
            )) as client:
                response = client.get("/health")
                self.assertEqual(200, response.status_code)
                self.assertNotIn(str(root), response.text)
                callbacks[0]()
                self.assertEqual(200, client.get("/health").status_code)

            self.assertEqual(selected, calls[:2])

            with patch(
                "src.pipeline.automatic_import.sqlite3.connect",
                side_effect=sqlite3.OperationalError(f"database at {root}"),
            ):
                callbacks[0]()

    def test_production_startup_migrates_before_submitting_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database_path = root / "question-bank.db"
            initialize_database(database_path).close()
            with sqlite3.connect(database_path) as connection:
                connection.execute("DROP TABLE import_upload_receipts")
            callbacks = []

            with TestClient(create_app(
                database_path,
                root / "private",
                auto_submit=callbacks.append,
                automatic_import_runner=lambda *_args, **_kwargs: None,
                _initialize_schema=False,
            )) as client:
                self.assertEqual(200, client.get("/health").status_code)
                self.assertEqual(1, len(callbacks))
                callbacks[0]()

            with sqlite3.connect(database_path) as connection:
                table = connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='import_upload_receipts'"""
                ).fetchone()
            self.assertEqual(("import_upload_receipts",), table)

class UploadConfirmationWebTests(UploadConfirmationWebTestCases, unittest.TestCase):
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
        self.assertEqual(303, second.status_code)
        self.assertEqual(first.headers["location"], second.headers["location"])
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
