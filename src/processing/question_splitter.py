"""User-authorized Codex question-boundary detection and crop publication."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import io
import json
import logging
import math
import os
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from src.processing.crop_review import CropReviewError, _load_current_crop_review_locked
from src.processing.crop_review_sheet import generate_crop_review_sheets
from src.processing.page_layout_analyzer import load_completed_layout
from src.processing.pdf_page_renderer import (
    _open_child_directory,
    _open_or_create_directory,
    _open_safe_directory,
    _read_regular_at,
)
from src.processing.question_crop import generate_question_crops_report
from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    load_hmac_key,
    locked_job,
    open_directory_at,
    read_file_at,
    write_file_at,
    validate_signed_manifest,
)


MAX_CODEX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CODEX_STDERR_BYTES = 512 * 1024
MAX_QUESTIONS = 200
MAX_REGIONS = 1_000
MAX_PROMPT_BYTES = 64 * 1024
MAX_WARNINGS_PER_QUESTION = 100
MAX_MASK_REGIONS_PER_QUESTION = 10
MAX_MASK_REASON_LENGTH = 200
CONTROLLED_MASK_REASONS = {"qr_code", "promotion_overlay"}
CODEX_TIMEOUT_SECONDS = 300
SAFE_SPLIT_ERROR = "Codex 自动切题失败，请重试"
SAFE_CODEX_MISSING = "未配置 Codex：请设置 CODEX_BIN 或安装 Codex CLI"
SAFE_RENDER_REQUIRED = "页面处理完成后才能调用 Codex 自动切题"
SAFE_EXISTING_ERROR = "现有切题结果校验失败，请点击重试"

LOGGER = logging.getLogger(__name__)


class QuestionSplitError(ValueError):
    """A fixed, user-safe split failure."""


class CodexExecutionError(QuestionSplitError):
    """The isolated Codex subprocess did not produce a bounded final message."""


class SplitCommitUncertain(QuestionSplitError):
    """The durable publish witness must be reconciled by the next claim."""


def _close_split_locks(artifact_context, job_stream, global_stream):
    try:
        if artifact_context is not None:
            artifact_context.__exit__(None, None, None)
    finally:
        # Acquired global -> per-job -> artifacts; release in reverse order.
        for stream in (job_stream, global_stream):
            if stream is None:
                continue
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass


@dataclass(frozen=True)
class CodexRunResult:
    final_message: str
    run_id: str


@dataclass
class SplitClaim:
    database_path: Path
    private_root: Path
    job_id: int
    runner: Any
    replace_invalid: bool
    review_feedback: tuple[dict[str, Any], ...]
    frozen_regions: tuple[dict[str, Any], ...]
    lock_stream: Any
    global_lock_stream: Any
    artifact_lock_context: Any
    artifact_lock: Any

    def close(self):
        artifact_context = self.artifact_lock_context
        self.artifact_lock_context = self.artifact_lock = None
        job_stream = self.lock_stream
        global_stream = self.global_lock_stream
        self.lock_stream = self.global_lock_stream = None
        _close_split_locks(artifact_context, job_stream, global_stream)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_codex_bin():
    configured = os.environ.get("CODEX_BIN")
    candidates: list[str | None] = [
        configured, str(Path.home() / ".local/bin/codex"), shutil.which("codex")
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            details = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(details.st_mode) and os.access(path, os.X_OK):
            return path.resolve()
    raise CodexExecutionError(SAFE_CODEX_MISSING)


def _terminate_process_group(process, grace_seconds=1.0):
    """Bounded TERM/KILL cleanup for Codex and every tool process it spawned."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


class CodexCliRunner:
    """Run Codex read-only with only manifest-approved page images attached."""

    def __init__(self, executable=None, timeout=CODEX_TIMEOUT_SECONDS,
                 max_output_bytes=MAX_CODEX_OUTPUT_BYTES,
                 max_stderr_bytes=MAX_CODEX_STDERR_BYTES):
        self.executable = Path(executable).resolve() if executable else _resolve_codex_bin()
        details = self.executable.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise CodexExecutionError(SAFE_CODEX_MISSING)
        self.executable_identity = (
            details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns,
        )
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.max_stderr_bytes = max_stderr_bytes

    def run(self, *, image_paths, prompt):
        try:
            details = self.executable.stat()
        except OSError as error:
            raise CodexExecutionError(SAFE_CODEX_MISSING) from error
        identity = (
            details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
            or identity != self.executable_identity
        ):
            raise CodexExecutionError(SAFE_CODEX_MISSING)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise CodexExecutionError(SAFE_SPLIT_ERROR)
        with tempfile.TemporaryDirectory(prefix="question-split-codex-") as temporary:
            root = Path(temporary)
            last_message = root / "last-message.json"
            output_schema = root / "output-schema.json"
            output_schema.write_text(json.dumps(_codex_output_schema()), encoding="utf-8")
            command = [
                str(self.executable), "exec", "--sandbox", "read-only",
                "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--disable", "shell_tool", "--disable", "unified_exec",
                "--disable", "shell_snapshot", "--skip-git-repo-check",
                "--color", "never", "--cd", str(root),
                "--output-schema", str(output_schema),
                "--output-last-message", str(last_message), "--image",
                *(str(Path(path).resolve()) for path in image_paths), "--", prompt,
            ]
            environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
            for name in (
                "HOME", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            ):
                if os.environ.get(name):
                    environment[name] = os.environ[name]
            process = subprocess.Popen(
                command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                start_new_session=True,
            )
            try:
                try:
                    stdout, stderr = _bounded_communicate(
                        process, self.timeout,
                        self.max_output_bytes, self.max_stderr_bytes,
                    )
                except Exception:
                    _terminate_process_group(process)
                    raise
            finally:
                process.stdout.close()
                process.stderr.close()
            if process.returncode != 0:
                raise CodexExecutionError(SAFE_SPLIT_ERROR)
            try:
                content = last_message.read_bytes()
            except OSError as error:
                raise CodexExecutionError(SAFE_SPLIT_ERROR) from error
            if not content or len(content) > self.max_output_bytes:
                raise CodexExecutionError(SAFE_SPLIT_ERROR)
            try:
                final = content.decode("utf-8")
            except UnicodeError as error:
                raise CodexExecutionError(SAFE_SPLIT_ERROR) from error
            run_id = "codex-" + hashlib.sha256(stdout + stderr + content).hexdigest()[:24]
            return CodexRunResult(final, run_id)


def _codex_output_schema():
    coordinate = {"type": "number", "minimum": 0, "maximum": 1}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["version", "import_job_id", "question_count", "questions"],
        "properties": {
            "version": {"type": "integer", "const": 1},
            "import_job_id": {"type": "integer", "minimum": 1},
            "question_count": {"type": "integer", "minimum": 1, "maximum": MAX_QUESTIONS},
            "questions": {
                "type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": [
                        "question_no", "regions", "warnings", "confidence",
                        "mask_regions_normalized",
                    ],
                    "properties": {
                        "question_no": {"type": "integer", "minimum": 1},
                        "regions": {
                            "type": "array", "minItems": 1, "maxItems": MAX_REGIONS,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["page_number", "bbox_normalized"],
                                "properties": {
                                    "page_number": {"type": "integer", "minimum": 1},
                                    "bbox_normalized": {
                                        "type": "array", "minItems": 4, "maxItems": 4,
                                        "items": coordinate,
                                    },
                                },
                            },
                        },
                        "warnings": {
                            "type": "array", "maxItems": MAX_WARNINGS_PER_QUESTION,
                            "items": {"type": "string", "maxLength": 500}
                        },
                        "confidence": {
                            "type": "string", "enum": ["low", "medium", "high"]
                        },
                        "mask_regions_normalized": {
                            "type": "array", "maxItems": MAX_MASK_REGIONS_PER_QUESTION,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["bbox_normalized", "reason"],
                                "properties": {
                                    "bbox_normalized": {
                                        "type": "array", "minItems": 4, "maxItems": 4,
                                        "items": coordinate,
                                    },
                                    "reason": {
                                        "type": "string", "minLength": 1,
                                        "maxLength": MAX_MASK_REASON_LENGTH,
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def _bounded_communicate(process, timeout, stdout_limit, stderr_limit):
    selector = selectors.DefaultSelector()
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    limits = {process.stdout: stdout_limit, process.stderr: stderr_limit}
    for stream in buffers:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = __import__("time").monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - __import__("time").monotonic()
            if remaining <= 0:
                raise CodexExecutionError(SAFE_SPLIT_ERROR)
            for key, _ in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.fileobj].extend(chunk)
                if len(buffers[key.fileobj]) > limits[key.fileobj]:
                    raise CodexExecutionError(SAFE_SPLIT_ERROR)
        remaining = deadline - __import__("time").monotonic()
        if remaining <= 0:
            raise CodexExecutionError(SAFE_SPLIT_ERROR)
        process.wait(timeout=remaining)
        return bytes(buffers[process.stdout]), bytes(buffers[process.stderr])
    except subprocess.TimeoutExpired as error:
        raise CodexExecutionError(SAFE_SPLIT_ERROR) from error
    finally:
        selector.close()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _canonical_mask_reason(value):
    if not isinstance(value, str):
        raise TypeError
    stripped = value.strip()
    for reason in CONTROLLED_MASK_REASONS:
        if stripped == reason or stripped.startswith(reason + ":") or stripped.startswith(reason + "："):
            return reason
    raise TypeError


def parse_codex_question_plan(raw, job_id, page_sizes):
    """Strictly decode the only accepted Codex response and derive pixel boxes."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CODEX_OUTPUT_BYTES:
        raise QuestionSplitError(SAFE_SPLIT_ERROR)
    try:
        decoder = json.JSONDecoder()
        payload, end = decoder.raw_decode(raw)
        if raw[end:].strip() or not isinstance(payload, dict):
            raise TypeError
        if set(payload) != {"version", "import_job_id", "question_count", "questions"}:
            raise TypeError
        if (
            type(payload["version"]) is not int or payload["version"] != 1
            or type(payload["import_job_id"]) is not int
            or payload["import_job_id"] != job_id
        ):
            raise TypeError
        count = payload["question_count"]
        questions = payload["questions"]
        if (type(count) is not int or not 1 <= count <= MAX_QUESTIONS
                or not isinstance(questions, list) or len(questions) != count):
            raise TypeError
        normalized = []
        total_regions = 0
        for expected, question in enumerate(questions, 1):
            question_keys = {
                "question_no", "regions", "warnings", "confidence",
            }
            if (
                not isinstance(question, dict)
                or set(question) not in {
                    frozenset(question_keys),
                    frozenset((*question_keys, "mask_regions_normalized")),
                }
                or type(question["question_no"]) is not int
                or question["question_no"] != expected
            ):
                raise TypeError
            regions = question["regions"]
            warnings = question.get("warnings", [])
            confidence = question.get("confidence")
            if (not isinstance(regions, list) or not regions
                    or not isinstance(warnings, list)
                    or len(warnings) > MAX_WARNINGS_PER_QUESTION
                    or not all(isinstance(item, str) and len(item) <= 500 for item in warnings)
                    or not (
                        (_number(confidence) and 0 <= confidence <= 1)
                        or confidence in {"low", "medium", "high"}
                    )):
                raise TypeError
            total_regions += len(regions)
            if total_regions > MAX_REGIONS:
                raise TypeError
            converted = []
            for region in regions:
                if not isinstance(region, dict) or set(region) != {
                    "page_number", "bbox_normalized"
                }:
                    raise TypeError
                page = region["page_number"]
                box = region["bbox_normalized"]
                if type(page) is not int or page not in page_sizes or (
                    not isinstance(box, list) or len(box) != 4
                    or not all(_number(item) for item in box)
                ):
                    raise TypeError
                left, top, right, bottom = box
                if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                    raise TypeError
                width, height = page_sizes[page]
                pixels = [
                    math.floor(left * width), math.floor(top * height),
                    math.ceil(right * width), math.ceil(bottom * height),
                ]
                if not (0 <= pixels[0] < pixels[2] <= width
                        and 0 <= pixels[1] < pixels[3] <= height):
                    raise TypeError
                converted.append({
                    "page_number": page, "bbox_normalized": list(box),
                    "bbox": pixels,
                })
            raw_masks = question.get("mask_regions_normalized", [])
            if (
                not isinstance(raw_masks, list)
                or len(raw_masks) > MAX_MASK_REGIONS_PER_QUESTION
            ):
                raise TypeError
            normalized_masks = []
            pixel_masks = []
            seen_masks = set()
            for mask in raw_masks:
                if (
                    not isinstance(mask, dict)
                    or set(mask) != {"bbox_normalized", "reason"}
                ):
                    raise TypeError
                box = mask["bbox_normalized"]
                reason = mask["reason"]
                if (
                    not isinstance(box, list) or len(box) != 4
                    or not all(_number(value) for value in box)
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or len(reason) > MAX_MASK_REASON_LENGTH
                ):
                    raise TypeError
                reason = _canonical_mask_reason(reason)
                left, top, right, bottom = box
                if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                    raise TypeError
                identity = tuple(box)
                if identity in seen_masks:
                    raise TypeError
                seen_masks.add(identity)
                containing = [
                    region for region in converted
                    if (
                        region["bbox_normalized"][0] <= left
                        and region["bbox_normalized"][1] <= top
                        and right <= region["bbox_normalized"][2]
                        and bottom <= region["bbox_normalized"][3]
                    )
                ]
                containing_pages = {region["page_number"] for region in containing}
                if len(containing) != 1 or len(containing_pages) != 1:
                    raise TypeError
                page = next(iter(containing_pages))
                width, height = page_sizes[page]
                pixels = [
                    math.floor(left * width), math.floor(top * height),
                    math.ceil(right * width), math.ceil(bottom * height),
                ]
                if not any(
                    region["page_number"] == page
                    and region["bbox"][0] <= pixels[0]
                    and region["bbox"][1] <= pixels[1]
                    and pixels[2] <= region["bbox"][2]
                    and pixels[3] <= region["bbox"][3]
                    for region in containing
                ):
                    raise TypeError
                normalized_masks.append({
                    "bbox_normalized": list(box), "reason": reason,
                })
                pixel_masks.append({
                    "page_number": page, "bbox": pixels, "reason": reason,
                })
            item = {
                "question_no": expected, "regions": converted,
                "warnings": list(warnings),
                "mask_regions_normalized": normalized_masks,
                "mask_regions": pixel_masks,
            }
            if confidence is not None:
                item["confidence"] = confidence
            normalized.append(item)
        return {
            "version": 1, "import_job_id": job_id,
            "question_count": count, "questions": normalized,
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise QuestionSplitError(SAFE_SPLIT_ERROR) from error


def _read_job_regular(private_root, job_id, name, *, max_bytes):
    root_fd = processing_fd = job_fd = None
    try:
        root_fd = _open_safe_directory(Path(private_root))
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        return _read_regular_at(job_fd, name, max_bytes=max_bytes)
    finally:
        for descriptor in (job_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _load_render(database_path, private_root, job_id):
    """Load one DB-anchored render generation through pinned descriptors."""
    root_fd = processing_fd = job_fd = pages_fd = None
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """SELECT r.status,r.dpi,r.total_pages,r.rendered_pages,
                          r.manifest_sha256,r.manifest_byte_size,
                          r.published_batch_id,r.source_pdf_sha256,p.sha256
                   FROM import_page_render_runs r
                   JOIN import_jobs j ON j.id=r.import_job_id
                   JOIN source_papers p ON p.id=j.source_paper_id
                   WHERE r.import_job_id=? AND j.status='pending'""",
                (job_id,),
            ).fetchone()
        if (
            row is None or row[0] != "completed" or row[1] != 300
            or type(row[2]) is not int or row[2] < 1 or row[3] != row[2]
            or not isinstance(row[4], str) or len(row[4]) != 64
            or type(row[5]) is not int or row[5] < 1
            or not isinstance(row[6], str) or not row[6]
            or row[7] != row[8]
        ):
            raise TypeError
        root_fd = _open_safe_directory(Path(private_root))
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        raw = _read_regular_at(
            job_fd, "render_manifest.json", max_bytes=10 * 1024 * 1024,
            expected_size=row[5],
        )
        if hashlib.sha256(raw).hexdigest() != row[4]:
            raise TypeError
        manifest = json.loads(raw.decode("utf-8"))
        expected_keys = {
            "version", "import_job_id", "dpi", "source_pdf_sha256",
            "source_page_count", "page_start", "page_end", "page_count", "pages",
        }
        pages = manifest.get("pages")
        if (
            not isinstance(manifest, dict) or set(manifest) != expected_keys
            or manifest.get("version") != 1 or manifest.get("import_job_id") != job_id
            or manifest.get("dpi") != 300
            or manifest.get("source_pdf_sha256") != row[8]
            or manifest.get("page_count") != row[2]
            or not isinstance(pages, list) or len(pages) != row[2]
        ):
            raise TypeError
        pages_fd = _open_child_directory(job_fd, "pages")
        result = []
        seen = set()
        total_bytes = 0
        for entry in pages:
            if not isinstance(entry, dict) or set(entry) != {
                "page_number", "relative_path", "pixel_width", "pixel_height",
                "byte_size", "sha256",
            }:
                raise TypeError
            number = entry["page_number"]
            relative = entry["relative_path"]
            name = f"page_{number:03d}.png" if type(number) is int else ""
            if (
                type(number) is not int or number in seen or number < 1
                or relative != f"pages/{name}"
                or type(entry["pixel_width"]) is not int or entry["pixel_width"] < 1
                or type(entry["pixel_height"]) is not int or entry["pixel_height"] < 1
                or type(entry["byte_size"]) is not int or entry["byte_size"] < 1
                or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64
            ):
                raise TypeError
            content = _read_regular_at(
                pages_fd, name, max_bytes=200 * 1024 * 1024,
                expected_size=entry["byte_size"],
            )
            total_bytes += len(content)
            if total_bytes > 1024 * 1024 * 1024:
                raise TypeError
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise TypeError
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if image.format != "PNG" or image.size != (
                    entry["pixel_width"], entry["pixel_height"]
                ):
                    raise TypeError
            seen.add(number)
            result.append((number, content, (entry["pixel_width"], entry["pixel_height"])))
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError,
            UnidentifiedImageError, sqlite3.Error) as error:
        raise QuestionSplitError(SAFE_RENDER_REQUIRED) from error
    finally:
        for descriptor in (pages_fd, job_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _prompt(job_id, pages, layout, review_feedback=(), frozen_regions=()):
    hint = json.dumps(layout, ensure_ascii=False, separators=(",", ":")) if layout else "null"
    feedback = json.dumps(
        list(review_feedback), ensure_ascii=False, separators=(",", ":")
    )
    recrop_numbers = ",".join(
        str(item["question_no"]) for item in review_feedback
    )
    frozen_numbers = ",".join(
        str(item["question_no"]) for item in frozen_regions
    )
    recrop_instruction = (
        f"冻结题号={frozen_numbers or '无'}；冻结题不得改动；"
        f"重点只修needs_recrop题号={recrop_numbers or '无'}；"
        if review_feedback else ""
    )
    return (
        "只分析随本请求附加的原卷页面图片，不读取或修改任何文件。原图是最终依据。"
        "只识别正式试卷中的连续规范题号；试卷后的答案和解析页不作为新题。"
        "每道题的region必须完整包含题号、公共条件、题干、公式、选项、小问和必要配图；"
        "顶部宁可保留少量空白，也不能截断属于本题的条件。"
        "同页相邻题应在两题之间的空白处分界，上一题不得包含下一题题号或文字，下一题也不得缺少开头。"
        "跨页题或跨栏题使用多个region并按阅读顺序排列，不得把多个页面粗暴合并为无关大框。"
        "版面提示仅作弱参考；如提示与图片冲突，必须以原图逐题核对结果为准。"
        "只输出一个JSON对象，禁止Markdown围栏和解释文字。结构必须严格为："
        '{"version":1,"import_job_id":整数,"question_count":整数,"questions":['
        '{"question_no":连续整数,"regions":[{"page_number":整数,'
        '"bbox_normalized":[left,top,right,bottom]}],"warnings":[],"confidence":low、medium或high,'
        '"mask_regions_normalized":[{"bbox_normalized":[left,top,right,bottom],'
        '"reason":"原因"}]}]}。每题必须输出mask_regions_normalized数组，'
        "没有遮罩时输出[]；"
        "遮罩仅限经独立确认不覆盖试题内容的二维码、水印、群组或答案获取宣传层；"
        "绝不能用于隐藏题干、选项、答案、解析，也不能用于掩盖相邻题边界错误；"
        f"。import_job_id={job_id}；允许页码={','.join(str(x[0]) for x in pages)}；"
        f"可选版面提示={hint}；"
        "以下审核反馈仅在非空时是当前数据库锚定且验签通过的重切依据；"
        "逐题修正其中标记为needs_recrop的边界，同时仍须复核所有题的连续性；"
        f"{recrop_instruction}"
        f"可信审核反馈={feedback}"
    )


def _prepare_locks(private_root, job_id):
    root_fd = processing_fd = locks_fd = None
    streams = []
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_or_create_directory(root_fd, "processing")
        locks_fd = _open_or_create_directory(processing_fd, ".split_locks")
        for name in ("global.lock", f"import_job_{job_id}.lock"):
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, 0o600, dir_fd=locks_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(fd)
                raise OSError
            streams.append(os.fdopen(fd, "a+b"))
        return tuple(streams)
    except Exception:
        for stream in streams:
            stream.close()
        raise
    finally:
        for fd in (locks_fd, processing_fd, root_fd):
            if fd is not None:
                os.close(fd)


def _completed_result_valid(
    private_root, job_id, question_count, expected_digest,
    crop_manifest_digest=None, crop_generation_id=None, crop_manifest_signature=None,
):
    root_fd = processing_fd = job_fd = crops_fd = None
    try:
        if (
            type(question_count) is not int or question_count < 1
            or not isinstance(expected_digest, str) or len(expected_digest) != 64
            or not isinstance(crop_manifest_digest, str) or len(crop_manifest_digest) != 64
            or not isinstance(crop_generation_id, str) or len(crop_generation_id) != 32
            or not isinstance(crop_manifest_signature, str)
            or len(crop_manifest_signature) != 64
        ):
            return False
        root_fd = _open_safe_directory(Path(private_root))
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        regions = _read_regular_at(
            job_fd, "question_regions.json", max_bytes=MAX_CODEX_OUTPUT_BYTES
        )
        if hashlib.sha256(regions).hexdigest() != expected_digest:
            return False
        decoded = json.loads(regions.decode("utf-8"))
        if (not isinstance(decoded, dict) or decoded.get("import_job_id") != job_id
                or decoded.get("question_count") != question_count):
            return False
        manifest_bytes = _read_regular_at(
            job_fd, "question_crops.json", max_bytes=16 * 1024 * 1024
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != crop_manifest_digest:
            return False
        manifest = validate_signed_manifest(
            json.loads(manifest_bytes.decode("utf-8")),
            load_hmac_key(Path(private_root) / "processing" / f"import_job_{job_id}"),
            expected_job_id=job_id,
            expected_question_nos=list(range(1, question_count + 1)),
        )
        if (
            manifest["generation_id"] != crop_generation_id
            or manifest["signature"] != crop_manifest_signature
        ):
            return False
        crops_fd = _open_child_directory(job_fd, "question_crops")
        for entry in manifest["questions"]:
            expected_relative = f"question_crops/Q{entry['question_no']:03d}.png"
            if entry["output_relative_path"] != expected_relative:
                return False
            content = _read_regular_at(
                crops_fd, f"Q{entry['question_no']:03d}.png",
                max_bytes=64 * 1024 * 1024, expected_size=entry["byte_size"],
            )
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                return False
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if image.format != "PNG" or image.size != (entry["width"], entry["height"]):
                    return False
        return True
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError,
            UnidentifiedImageError, SecureCropArtifactError):
        return False
    finally:
        for descriptor in (crops_fd, job_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def completed_split_result_valid(
    private_root, job_id, question_count, expected_digest,
    crop_manifest_digest, crop_generation_id, crop_manifest_signature,
):
    """Public read-only verification gate for Web status and image serving."""
    return _completed_result_valid(
        private_root, job_id, question_count, expected_digest,
        crop_manifest_digest, crop_generation_id, crop_manifest_signature,
    )


def read_completed_split_image(
    private_root, job_id, question_count, crop_manifest_digest,
    crop_generation_id, crop_manifest_signature, question_no,
):
    """Validate the anchored crop manifest, then read only the requested PNG."""
    if (
        type(question_count) is not int or type(question_no) is not int
        or not 1 <= question_no <= question_count
        or not isinstance(crop_manifest_digest, str) or len(crop_manifest_digest) != 64
        or not isinstance(crop_generation_id, str) or len(crop_generation_id) != 32
        or not isinstance(crop_manifest_signature, str)
        or len(crop_manifest_signature) != 64
    ):
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    root_fd = processing_fd = job_fd = crops_fd = None
    try:
        root_fd = _open_safe_directory(Path(private_root))
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        manifest_bytes = _read_regular_at(
            job_fd, "question_crops.json", max_bytes=16 * 1024 * 1024
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != crop_manifest_digest:
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        manifest = validate_signed_manifest(
            json.loads(manifest_bytes.decode("utf-8")),
            load_hmac_key(Path(private_root) / "processing" / f"import_job_{job_id}"),
            expected_job_id=job_id,
            expected_question_nos=list(range(1, question_count + 1)),
        )
        if (
            manifest["generation_id"] != crop_generation_id
            or manifest["signature"] != crop_manifest_signature
        ):
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        entry = manifest["questions"][question_no - 1]
        expected_relative = f"question_crops/Q{question_no:03d}.png"
        if entry["question_no"] != question_no or entry["output_relative_path"] != expected_relative:
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        crops_fd = _open_child_directory(job_fd, "question_crops")
        content = _read_regular_at(
            crops_fd, f"Q{question_no:03d}.png", max_bytes=64 * 1024 * 1024,
            expected_size=entry["byte_size"],
        )
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or image.size != (entry["width"], entry["height"]):
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
        return content
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError,
            UnidentifiedImageError, SecureCropArtifactError) as error:
        raise QuestionSplitError(SAFE_EXISTING_ERROR) from error
    finally:
        for descriptor in (crops_fd, job_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def record_split_claim_failure(database_path, private_root, job_id, error):
    """Fail an inactive split claim while proving no worker owns its locks."""
    if type(job_id) is not int or job_id < 1:
        return False
    message = str(error)
    if message not in {SAFE_CODEX_MISSING, SAFE_SPLIT_ERROR}:
        message = SAFE_SPLIT_ERROR
    global_stream = job_stream = None
    try:
        global_stream, job_stream = _prepare_locks(Path(private_root), job_id)
        try:
            fcntl.flock(global_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(job_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        now = _now()
        with closing(sqlite3.connect(Path(database_path), timeout=10)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT j.status,r.status,s.status
                   FROM import_jobs j
                   LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                   LEFT JOIN import_question_split_runs s ON s.import_job_id=j.id
                   WHERE j.id=?""",
                (job_id,),
            ).fetchone()
            if (
                current is None or current[0] != "pending"
                or current[1] != "completed" or current[2] == "completed"
            ):
                connection.rollback()
                return False
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,processed_pages,error_message,updated_at)
                   VALUES (?,'failed',0,?,?)
                   ON CONFLICT(import_job_id) DO UPDATE SET
                     status='failed',error_message=excluded.error_message,
                     updated_at=excluded.updated_at
                   WHERE import_question_split_runs.status
                         IN ('pending','processing','failed')""",
                (job_id, message, now),
            )
            connection.commit()
            return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        for stream in (job_stream, global_stream):
            if stream is None:
                continue
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass


def _retained_review_context(current, review, private_root, job_id):
    retained_valid = bool(
        current[2] in {"completed", "failed", "processing"}
        and current[9] == current[5] and current[10] == current[8]
        and _completed_result_valid(
            private_root, job_id, current[3], current[4],
            current[11], current[12], current[13],
        )
    )
    review_matches = bool(
        retained_valid
        and review
        and review["input_generation_id"] == current[12]
        and review["output_manifest_sha256"] == current[11]
        and review["output_manifest_signature"] == current[13]
    )
    feedback = tuple(
        {
            "question_no": item["question_no"],
            "status": item["status"],
            "warnings": list(item["warnings"]),
        }
        for item in review["questions"]
        if item["status"] == "needs_recrop"
    ) if review_matches else ()
    passed_numbers = tuple(
        item["question_no"]
        for item in review["questions"]
        if item["status"] == "ai_review_passed"
    ) if review_matches else ()
    return retained_valid, review_matches, feedback, passed_numbers


def _load_trusted_frozen_regions(
    artifact_lock, job_id, question_count, expected_digest, passed_numbers, page_sizes,
    crop_manifest_digest, crop_generation_id, crop_manifest_signature,
):
    """Read passed regions only from the DB-anchored current split artifact."""
    try:
        snapshot = read_file_at(
            artifact_lock.descriptor,
            "question_regions.json",
            max_bytes=MAX_CODEX_OUTPUT_BYTES,
        )
        if snapshot.sha256 != expected_digest:
            raise TypeError
        stored = json.loads(snapshot.data.decode("utf-8"))
        if (
            not isinstance(stored, dict)
            or set(stored) != {
                "version", "import_job_id", "question_count", "questions",
            }
            or stored.get("version") != 1
            or stored.get("import_job_id") != job_id
            or stored.get("question_count") != question_count
            or not isinstance(stored.get("questions"), list)
        ):
            raise TypeError
        codex_questions = []
        for question in stored["questions"]:
            base_keys = {
                "question_no", "regions", "warnings", "confidence",
            }
            mask_keys = {"mask_regions_normalized", "mask_regions"}
            if (
                not isinstance(question, dict)
                or set(question) not in {
                    frozenset(base_keys), frozenset((*base_keys, *mask_keys)),
                }
                or not isinstance(question["regions"], list)
            ):
                raise TypeError
            regions = []
            for region in question["regions"]:
                if (
                    not isinstance(region, dict)
                    or set(region) != {
                        "page_number", "bbox_normalized", "bbox",
                    }
                ):
                    raise TypeError
                regions.append({
                    "page_number": region["page_number"],
                    "bbox_normalized": region["bbox_normalized"],
                })
            codex_questions.append({
                "question_no": question["question_no"],
                "regions": regions,
                "warnings": question["warnings"],
                "confidence": question["confidence"],
                "mask_regions_normalized": question.get(
                    "mask_regions_normalized", []
                ),
            })
        normalized = parse_codex_question_plan(json.dumps({
            "version": stored["version"],
            "import_job_id": stored["import_job_id"],
            "question_count": stored["question_count"],
            "questions": codex_questions,
        }), job_id, page_sizes)
        canonical_stored = json.loads(json.dumps(stored))
        for question in canonical_stored["questions"]:
            question.setdefault("mask_regions_normalized", [])
            question.setdefault("mask_regions", [])
            for mask in question["mask_regions_normalized"]:
                mask["reason"] = _canonical_mask_reason(mask.get("reason"))
            for mask in question["mask_regions"]:
                mask["reason"] = _canonical_mask_reason(mask.get("reason"))
        if normalized != canonical_stored:
            raise TypeError
        expected_numbers = tuple(range(1, question_count + 1))
        if (
            tuple(item["question_no"] for item in normalized["questions"])
            != expected_numbers
            or tuple(sorted(passed_numbers)) != tuple(passed_numbers)
            or any(number not in expected_numbers for number in passed_numbers)
        ):
            raise TypeError
        passed = set(passed_numbers)
        manifest_snapshot = read_file_at(
            artifact_lock.descriptor,
            "question_crops.json",
            max_bytes=16 * 1024 * 1024,
        )
        if manifest_snapshot.sha256 != crop_manifest_digest:
            raise TypeError
        manifest = validate_signed_manifest(
            json.loads(manifest_snapshot.data.decode("utf-8")),
            load_hmac_key(artifact_lock.path),
            expected_job_id=job_id,
            expected_question_nos=list(expected_numbers),
        )
        if (
            manifest["generation_id"] != crop_generation_id
            or manifest["signature"] != crop_manifest_signature
        ):
            raise TypeError
        manifest_by_number = {
            item["question_no"]: item for item in manifest["questions"]
        }
        for question in normalized["questions"]:
            if question["question_no"] not in passed:
                continue
            crop = manifest_by_number[question["question_no"]]
            if (
                crop["review_status"] != "ai_review_passed"
                or crop["regions"] != [
                    {
                        "page_number": region["page_number"],
                        "bbox": region["bbox"],
                    }
                    for region in question["regions"]
                ]
                or crop.get("mask_regions_normalized", [])
                != question["mask_regions_normalized"]
                or crop.get("mask_regions", []) != question["mask_regions"]
            ):
                raise TypeError
        frozen = tuple(
            json.loads(json.dumps(item))
            for item in normalized["questions"]
            if item["question_no"] in passed
        )
        if len(frozen) != len(passed_numbers):
            raise TypeError
        return frozen
    except (
        OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError,
        QuestionSplitError, SecureCropArtifactError,
    ) as error:
        raise QuestionSplitError(SAFE_EXISTING_ERROR) from error


def _merge_frozen_regions(plan, review_feedback, frozen_regions):
    if not review_feedback:
        if frozen_regions:
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        return plan
    recrop_numbers = tuple(item["question_no"] for item in review_feedback)
    frozen_numbers = tuple(item["question_no"] for item in frozen_regions)
    expected_numbers = tuple(range(1, plan["question_count"] + 1))
    if (
        len(set(recrop_numbers)) != len(recrop_numbers)
        or len(set(frozen_numbers)) != len(frozen_numbers)
        or set(recrop_numbers).intersection(frozen_numbers)
        or tuple(sorted((*recrop_numbers, *frozen_numbers))) != expected_numbers
    ):
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    merged = json.loads(json.dumps(plan))
    for frozen in frozen_regions:
        number = frozen["question_no"]
        if number != merged["questions"][number - 1]["question_no"]:
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        merged["questions"][number - 1] = json.loads(json.dumps(frozen))
    return merged


def claim_split_job(database_path, private_root, job_id, runner=None):
    if type(job_id) is not int or job_id < 1:
        raise QuestionSplitError("切题任务参数无效")
    database_path, private_root = Path(database_path), Path(private_root)
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            """SELECT j.status,r.status,s.status,s.question_count,
                      s.result_manifest_sha256,r.manifest_sha256,
                      r.manifest_byte_size,r.published_batch_id,r.source_pdf_sha256,
                      s.render_manifest_sha256,s.source_pdf_sha256,
                      s.crop_manifest_sha256,s.crop_generation_id,s.crop_manifest_signature
               FROM import_jobs j
               LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
               LEFT JOIN import_question_split_runs s ON s.import_job_id=j.id
               WHERE j.id=?""", (job_id,)
        ).fetchone()
    if row is None:
        raise QuestionSplitError("未找到导入任务")
    if row[0] != "pending" or row[1] != "completed" or any(
        value is None for value in row[5:9]
    ):
        raise QuestionSplitError(SAFE_RENDER_REQUIRED)
    global_stream = job_stream = artifact_context = artifact_lock = None
    try:
        # Canonical acquisition order: global split, per-job split, artifacts.
        # Review writers acquire only artifacts, so no reverse edge can deadlock.
        global_stream, job_stream = _prepare_locks(private_root, job_id)
        try:
            fcntl.flock(global_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(job_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            for stream in (job_stream, global_stream):
                if stream:
                    stream.close()
            return None
        artifact_context = locked_job(
            private_root / "processing" / f"import_job_{job_id}"
        )
        artifact_lock = artifact_context.__enter__()
        _recover_split_transaction(database_path, private_root, job_id)
        try:
            review = _load_current_crop_review_locked(
                database_path, artifact_lock, job_id, recover=True,
            )
        except (CropReviewError, OSError, SecureCropArtifactError):
            review = None
        feedback: tuple[dict[str, Any], ...] = ()
        frozen_regions: tuple[dict[str, Any], ...] = ()
        page_sizes = None
        now = _now()
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT j.status,r.status,s.status,s.question_count,
                          s.result_manifest_sha256,r.manifest_sha256,
                          r.manifest_byte_size,r.published_batch_id,r.source_pdf_sha256,
                      s.render_manifest_sha256,s.source_pdf_sha256,
                      s.crop_manifest_sha256,s.crop_generation_id,s.crop_manifest_signature
                   FROM import_jobs j
                   LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                   LEFT JOIN import_question_split_runs s ON s.import_job_id=j.id
                   WHERE j.id=?""", (job_id,)
            ).fetchone()
            if (
                current is None or current[0] != "pending" or current[1] != "completed"
                or any(value is None for value in current[5:9])
            ):
                raise QuestionSplitError(SAFE_RENDER_REQUIRED)
            retained_valid, review_matches, feedback, passed_numbers = (
                _retained_review_context(
                    current, review, private_root, job_id,
                )
            )
            if current[2] == "completed" and retained_valid and not feedback:
                connection.rollback()
                closing_context = artifact_context
                closing_job_stream = job_stream
                closing_global_stream = global_stream
                artifact_context = artifact_lock = None
                job_stream = global_stream = None
                _close_split_locks(
                    closing_context, closing_job_stream, closing_global_stream,
                )
                return None
            if review_matches and feedback:
                pages = _load_render(database_path, private_root, job_id)
                page_sizes = {item[0]: item[2] for item in pages}
                frozen_regions = _load_trusted_frozen_regions(
                    artifact_lock, job_id, current[3], current[4],
                    passed_numbers, page_sizes, current[11], current[12], current[13],
                )
            connection.rollback()
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT j.status,r.status,s.status,s.question_count,
                          s.result_manifest_sha256,r.manifest_sha256,
                          r.manifest_byte_size,r.published_batch_id,r.source_pdf_sha256,
                          s.render_manifest_sha256,s.source_pdf_sha256,
                          s.crop_manifest_sha256,s.crop_generation_id,s.crop_manifest_signature
                   FROM import_jobs j
                   LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                   LEFT JOIN import_question_split_runs s ON s.import_job_id=j.id
                   WHERE j.id=?""", (job_id,)
            ).fetchone()
            if (
                current is None or current[0] != "pending" or current[1] != "completed"
                or any(value is None for value in current[5:9])
            ):
                raise QuestionSplitError(SAFE_RENDER_REQUIRED)
            retained_valid, review_matches, feedback, passed_numbers = (
                _retained_review_context(
                    current, review, private_root, job_id,
                )
            )
            if current[2] == "completed" and retained_valid and not feedback:
                connection.rollback()
                closing_context = artifact_context
                closing_job_stream = job_stream
                closing_global_stream = global_stream
                artifact_context = artifact_lock = None
                job_stream = global_stream = None
                _close_split_locks(
                    closing_context, closing_job_stream, closing_global_stream,
                )
                return None
            if review_matches and feedback:
                if page_sizes is None:
                    pages = _load_render(database_path, private_root, job_id)
                    page_sizes = {item[0]: item[2] for item in pages}
                frozen_regions = _load_trusted_frozen_regions(
                    artifact_lock, job_id, current[3], current[4],
                    passed_numbers, page_sizes, current[11], current[12], current[13],
                )
            else:
                frozen_regions = ()
            if runner is None:
                runner = CodexCliRunner()
            connection.execute(
                """INSERT INTO import_question_split_runs
                   (import_job_id,status,processed_pages,started_at,updated_at)
                   VALUES (?,'processing',0,?,?)
                   ON CONFLICT(import_job_id) DO UPDATE SET
                     status='processing',error_message=NULL,started_at=excluded.started_at,
                     completed_at=NULL,updated_at=excluded.updated_at""",
                (job_id, now, now),
            )
            connection.commit()
        return SplitClaim(
            database_path, private_root, job_id, runner,
            current[2] == "completed" or retained_valid,
            feedback, frozen_regions, job_stream, global_stream,
            artifact_context, artifact_lock,
        )
    except Exception:
        _close_split_locks(artifact_context, job_stream, global_stream)
        raise


_FORMAL_OUTPUTS = (
    "question_regions.json", "question_crops", "question_crops.json",
    "crop_ai_review.json", "review",
)
_SPLIT_BACKUP_NAME = ".split-backup-current"
_SPLIT_JOURNAL_NAME = ".split-publish-journal.json"


def _backup_identity(details):
    return (
        details.st_dev, details.st_ino, stat.S_IFMT(details.st_mode),
        details.st_nlink, details.st_size, details.st_mtime_ns, details.st_ctime_ns,
    )


def _entry_details(parent_fd, name):
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _entry_still_bound(parent_fd, name, expected):
    try:
        return _backup_identity(_entry_details(parent_fd, name)) == expected
    except FileNotFoundError:
        return False


def _remove_tree_at(parent_fd, name):
    """Remove one controlled entry without ever following a replaced link."""
    try:
        details = _entry_details(parent_fd, name)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(details.st_mode):
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            if _backup_identity(os.fstat(descriptor)) != _backup_identity(details):
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
            with os.scandir(descriptor) as entries:
                children = [entry.name for entry in entries]
            for child in children:
                _remove_tree_at(descriptor, child)
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _copy_bound_entry(
    source_parent_fd, source_name, target_parent_fd, target_name, budget,
):
    """Copy from pinned descriptors and reject every entry identity race."""
    before = _entry_details(source_parent_fd, source_name)
    expected = _backup_identity(before)
    budget[0] += 1
    if budget[0] > budget[1] or stat.S_ISLNK(before.st_mode):
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    try:
        if stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1:
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
            source_fd = os.open(
                source_name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_parent_fd,
            )
            target_fd = None
            try:
                if _backup_identity(os.fstat(source_fd)) != expected:
                    raise QuestionSplitError(SAFE_EXISTING_ERROR)
                target_fd = os.open(
                    target_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    stat.S_IMODE(before.st_mode) & 0o700,
                    dir_fd=target_parent_fd,
                )
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_fd, view)
                        view = view[written:]
                os.fsync(target_fd)
                if (
                    _backup_identity(os.fstat(source_fd)) != expected
                    or not _entry_still_bound(source_parent_fd, source_name, expected)
                ):
                    raise QuestionSplitError(SAFE_EXISTING_ERROR)
            finally:
                if target_fd is not None:
                    os.close(target_fd)
                os.close(source_fd)
            return
        if not stat.S_ISDIR(before.st_mode):
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        source_fd = os.open(source_name, flags, dir_fd=source_parent_fd)
        target_fd = None
        try:
            if _backup_identity(os.fstat(source_fd)) != expected:
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
            os.mkdir(target_name, stat.S_IMODE(before.st_mode) & 0o700, dir_fd=target_parent_fd)
            target_fd = os.open(target_name, flags, dir_fd=target_parent_fd)
            with os.scandir(source_fd) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                _copy_bound_entry(source_fd, name, target_fd, name, budget)
            os.fsync(target_fd)
            if (
                _backup_identity(os.fstat(source_fd)) != expected
                or not _entry_still_bound(source_parent_fd, source_name, expected)
            ):
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)
    except Exception as error:
        try:
            _remove_tree_at(target_parent_fd, target_name)
        except (OSError, QuestionSplitError):
            pass
        if isinstance(error, QuestionSplitError):
            raise
        raise QuestionSplitError(SAFE_EXISTING_ERROR) from error


def _journal_signature(key, payload):
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _fsync_backup_tree(root):
    directories = []
    for current, _, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in files:
            path = directory / name
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _split_anchors(database_path, job_id):
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            """SELECT question_count,result_manifest_sha256,crop_manifest_sha256,
                      crop_generation_id,crop_manifest_signature
               FROM import_question_split_runs WHERE import_job_id=?""", (job_id,)
        ).fetchone()
    return tuple(row) if row is not None else (None, None, None, None, None)


def _authoritative_split_commit_state(
    database_path, private_root, job_id, old_anchors, new_anchors,
):
    """Classify a throwing commit through a fresh SQLite connection and artifacts."""
    try:
        current = _split_anchors(database_path, job_id)
    except sqlite3.Error:
        return "unknown"
    if current == tuple(new_anchors):
        try:
            valid = _completed_result_valid(
                private_root, job_id, new_anchors[0], new_anchors[1],
                new_anchors[2], new_anchors[3], new_anchors[4],
            )
        except (OSError, QuestionSplitError, SecureCropArtifactError):
            return "unknown"
        return "committed" if valid else "unknown"
    if current == tuple(old_anchors):
        return "uncommitted"
    return "unknown"


def _write_split_journal(job_dir, payload):
    unsigned = {key: payload[key] for key in (
        "version", "saved_outputs", "old_anchors", "new_anchors",
    )}
    signed = {
        **unsigned,
        "signature": _journal_signature(load_hmac_key(job_dir), unsigned),
    }
    _atomic_json(job_dir / _SPLIT_JOURNAL_NAME, signed)


def _valid_split_anchors(value, *, allow_none):
    if value is None:
        return allow_none
    if not isinstance(value, list) or len(value) != 5:
        return False
    count, result_digest, crop_digest, generation, signature = value
    if all(item is None for item in value):
        return True
    return bool(
        type(count) is int and 1 <= count <= MAX_QUESTIONS
        and all(isinstance(item, str) for item in value[1:])
        and len(result_digest) == 64 and len(crop_digest) == 64
        and len(generation) == 32 and len(signature) == 64
    )


def _read_split_journal(job_dir):
    journal = job_dir / _SPLIT_JOURNAL_NAME
    details = journal.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > 64 * 1024:
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QuestionSplitError(SAFE_EXISTING_ERROR) from error
    if not isinstance(payload, dict) or set(payload) != {
        "version", "saved_outputs", "old_anchors", "new_anchors", "signature",
    }:
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    unsigned = {key: payload[key] for key in (
        "version", "saved_outputs", "old_anchors", "new_anchors",
    )}
    if (
        payload["version"] != 2 or not isinstance(payload["saved_outputs"], list)
        or any(not isinstance(name, str) for name in payload["saved_outputs"])
        or len(set(payload["saved_outputs"])) != len(payload["saved_outputs"])
        or any(name not in _FORMAL_OUTPUTS for name in payload["saved_outputs"])
        or not _valid_split_anchors(payload["old_anchors"], allow_none=False)
        or not _valid_split_anchors(payload["new_anchors"], allow_none=True)
        or not isinstance(payload["signature"], str)
        or not hmac.compare_digest(
            payload["signature"], _journal_signature(load_hmac_key(job_dir), unsigned)
        )
    ):
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    return payload


def _snapshot_outputs(job_dir, database_path, job_id):
    backup = job_dir / _SPLIT_BACKUP_NAME
    flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    job_fd = os.open(job_dir, flags)
    backup_fd = None
    try:
        try:
            _entry_details(job_fd, _SPLIT_JOURNAL_NAME)
        except FileNotFoundError:
            pass
        else:
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        try:
            _entry_details(job_fd, _SPLIT_BACKUP_NAME)
        except FileNotFoundError:
            pass
        else:
            _remove_tree_at(job_fd, _SPLIT_BACKUP_NAME)
        os.mkdir(_SPLIT_BACKUP_NAME, 0o700, dir_fd=job_fd)
        backup_fd = os.open(_SPLIT_BACKUP_NAME, flags, dir_fd=job_fd)
        saved = []
        budget = [0, 5_000]
        for name in _FORMAL_OUTPUTS:
            try:
                _entry_details(job_fd, name)
            except FileNotFoundError:
                continue
            _copy_bound_entry(job_fd, name, backup_fd, name, budget)
            saved.append(name)
        os.fsync(backup_fd)
        os.close(backup_fd)
        backup_fd = None
        _write_split_journal(job_dir, {
            "version": 2,
            "saved_outputs": saved,
            "old_anchors": list(_split_anchors(database_path, job_id)),
            "new_anchors": None,
        })
        return backup
    except Exception:
        try:
            _remove_tree_at(job_fd, _SPLIT_BACKUP_NAME)
        except (OSError, QuestionSplitError):
            pass
        try:
            os.unlink(_SPLIT_JOURNAL_NAME, dir_fd=job_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(job_fd)


def _record_new_split_anchors(job_dir, new_anchors):
    payload = _read_split_journal(job_dir)
    if payload["new_anchors"] is not None:
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    _write_split_journal(job_dir, {
        "version": 2,
        "saved_outputs": payload["saved_outputs"],
        "old_anchors": payload["old_anchors"],
        "new_anchors": list(new_anchors),
    })


def _restore_outputs(job_dir, backup):
    """Idempotently restore from retained copies; backup survives interrupted recovery."""
    if Path(backup) != Path(job_dir) / _SPLIT_BACKUP_NAME:
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    payload = _read_split_journal(job_dir)
    saved_names = set(payload["saved_outputs"])
    flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    job_fd = os.open(job_dir, flags)
    backup_fd = None
    stages = []
    try:
        backup_fd = os.open(_SPLIT_BACKUP_NAME, flags, dir_fd=job_fd)
        budget = [0, 5_000]
        for name in _FORMAL_OUTPUTS:
            stage = f".split-restore-{name}"
            _remove_tree_at(job_fd, stage)
            if name in saved_names:
                _copy_bound_entry(backup_fd, name, job_fd, stage, budget)
                stages.append((name, stage))
            else:
                try:
                    _entry_details(backup_fd, name)
                except FileNotFoundError:
                    pass
                else:
                    raise QuestionSplitError(SAFE_EXISTING_ERROR)
        for name in _FORMAL_OUTPUTS:
            _remove_tree_at(job_fd, name)
        for name, stage in stages:
            os.replace(stage, name, src_dir_fd=job_fd, dst_dir_fd=job_fd)
        os.fsync(job_fd)
        os.unlink(_SPLIT_JOURNAL_NAME, dir_fd=job_fd)
        os.close(backup_fd)
        backup_fd = None
        _remove_tree_at(job_fd, _SPLIT_BACKUP_NAME)
        os.fsync(job_fd)
    except Exception:
        for _, stage in stages:
            try:
                _remove_tree_at(job_fd, stage)
            except (OSError, QuestionSplitError):
                pass
        raise
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(job_fd)


def _recover_split_transaction(database_path, private_root, job_id):
    """Finish a DB-committed generation or restore a pre-commit publication."""
    job_dir = Path(private_root) / "processing" / f"import_job_{job_id}"
    journal = job_dir / _SPLIT_JOURNAL_NAME
    backup = job_dir / _SPLIT_BACKUP_NAME
    if not journal.exists():
        if backup.exists() and backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        return False
    payload = _read_split_journal(job_dir)
    current_anchors = _split_anchors(database_path, job_id)
    new_anchors = (
        tuple(payload["new_anchors"])
        if payload["new_anchors"] is not None else None
    )
    if new_anchors is not None and current_anchors == new_anchors:
        if not _completed_result_valid(
            private_root, job_id, new_anchors[0], new_anchors[1],
            new_anchors[2], new_anchors[3], new_anchors[4],
        ):
            raise QuestionSplitError(SAFE_EXISTING_ERROR)
        if backup.exists() or backup.is_symlink():
            if not backup.is_dir() or backup.is_symlink():
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
            shutil.rmtree(backup)
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        return True
    if not backup.is_dir() or backup.is_symlink():
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    if current_anchors != tuple(payload["old_anchors"]):
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    _restore_outputs(job_dir, backup)
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            """SELECT question_count,result_manifest_sha256,processed_pages,codex_run_id,
                      crop_manifest_sha256,crop_generation_id,crop_manifest_signature
               FROM import_question_split_runs WHERE import_job_id=?""", (job_id,)
        ).fetchone()
        if row and _completed_result_valid(
            private_root, job_id, row[0], row[1], row[4], row[5], row[6]
        ):
            connection.execute(
                """UPDATE import_question_split_runs SET status='completed',
                   error_message=NULL,completed_at=COALESCE(completed_at,?),updated_at=?
                   WHERE import_job_id=?""", (_now(), _now(), job_id)
            )
        else:
            connection.execute(
                """UPDATE import_question_split_runs SET status='failed',
                   question_count=NULL,processed_pages=0,error_message=?,codex_run_id=NULL,
                   result_manifest_sha256=NULL,render_manifest_sha256=NULL,
                   source_pdf_sha256=NULL,crop_manifest_sha256=NULL,
                   crop_generation_id=NULL,crop_manifest_signature=NULL,
                   completed_at=NULL,updated_at=?
                   WHERE import_job_id=?""", (SAFE_SPLIT_ERROR, _now(), job_id)
            )
        connection.commit()
    return True


def _remove_formal_output(job_dir, name):
    target = job_dir / name
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()


def _freeze_passed_crop_reviews(database_path, artifact_lock, job_id, recrop_numbers):
    """Archive the exact signed review batch and anchor each untouched passed crop."""
    review = _load_current_crop_review_locked(
        database_path, artifact_lock, job_id, recover=True,
    )
    manifest_snapshot = read_file_at(
        artifact_lock.descriptor, "question_crops.json", max_bytes=16 * 1024 * 1024,
    )
    manifest = validate_signed_manifest(
        json.loads(manifest_snapshot.data), load_hmac_key(artifact_lock.path),
        expected_job_id=job_id,
    )
    evidence_snapshot = read_file_at(
        artifact_lock.descriptor, "crop_ai_review.json", max_bytes=4 * 1024 * 1024,
    )
    passed = {
        item["question_no"] for item in review["questions"]
        if item["status"] == "ai_review_passed"
    }
    if passed.intersection(recrop_numbers):
        raise QuestionSplitError(SAFE_EXISTING_ERROR)
    try:
        os.mkdir("frozen_crop_reviews", 0o700, dir_fd=artifact_lock.descriptor)
    except FileExistsError:
        pass
    archive_fd = open_directory_at(artifact_lock.descriptor, "frozen_crop_reviews")
    archive_name = f"evidence_{evidence_snapshot.sha256}.json"
    try:
        try:
            write_file_at(archive_fd, archive_name, evidence_snapshot.data)
        except FileExistsError:
            existing = read_file_at(
                archive_fd, archive_name, max_bytes=4 * 1024 * 1024,
            )
            if existing.sha256 != evidence_snapshot.sha256:
                raise QuestionSplitError(SAFE_EXISTING_ERROR)
    finally:
        os.close(archive_fd)
    entries = {item["question_no"]: item for item in manifest["questions"]}
    now = _now()
    with closing(sqlite3.connect(database_path)) as connection:
        for number in sorted(passed):
            entry = entries[number]
            entry_digest = hashlib.sha256(json.dumps(
                entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            connection.execute(
                """INSERT INTO import_frozen_crop_reviews
                   (import_job_id,question_no,crop_generation_id,crop_sha256,
                    manifest_entry_sha256,source_manifest_sha256,
                    source_manifest_signature,review_evidence_sha256,
                    review_evidence_signature,evidence_relative_path,reviewer,frozen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(import_job_id,question_no,crop_generation_id) DO NOTHING""",
                (job_id, number, manifest["generation_id"], entry["sha256"],
                 entry_digest, manifest_snapshot.sha256, manifest["signature"],
                 evidence_snapshot.sha256, review["signature"],
                 f"frozen_crop_reviews/{archive_name}", review["reviewer_run_id"], now),
            )
        connection.commit()


def _atomic_json(path, value):
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return content
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mark_failed(database_path, job_id):
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                """UPDATE import_question_split_runs
                   SET status='failed',error_message=?,completed_at=NULL,updated_at=?
                   WHERE import_job_id=?""", (SAFE_SPLIT_ERROR, _now(), job_id)
            )
            connection.commit()
    except sqlite3.Error:
        pass


def split_import_job(
    database_path, private_root, job_id, runner, *, replace_invalid=False,
    review_feedback=(), frozen_regions=(), artifact_lock=None,
):
    job_dir = Path(private_root) / "processing" / f"import_job_{job_id}"
    pages = _load_render(database_path, private_root, job_id)
    layout = None
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            status = connection.execute(
                "SELECT status FROM import_layout_analysis_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
        if status == ("completed",):
            loaded = load_completed_layout(database_path, private_root, job_id)
            layout = {"pages": loaded.get("pages", []), "questions": loaded.get("questions", [])}
    except (sqlite3.Error, ValueError):
        layout = None
    with tempfile.TemporaryDirectory(prefix="question-split-input-") as snapshot_dir:
        snapshot_root = Path(snapshot_dir)
        image_paths = []
        for number, content, _ in pages:
            path = snapshot_root / f"page_{number:03d}.png"
            with path.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            image_paths.append(path)
        response = runner.run(
            image_paths=image_paths,
            prompt=_prompt(
                job_id, pages, layout, review_feedback, frozen_regions,
            ),
        )
    if not isinstance(response, CodexRunResult):
        raise QuestionSplitError(SAFE_SPLIT_ERROR)
    plan = parse_codex_question_plan(
        response.final_message, job_id, {item[0]: item[2] for item in pages}
    )
    plan = _merge_frozen_regions(plan, review_feedback, frozen_regions)
    recrop_numbers = tuple(
        item["question_no"] for item in review_feedback
    )
    if replace_invalid and recrop_numbers:
        _freeze_passed_crop_reviews(
            database_path, artifact_lock, job_id, set(recrop_numbers),
        )
    backup = _snapshot_outputs(job_dir, database_path, job_id)
    database_committed = False
    commit_uncertain = False
    try:
        if replace_invalid and not recrop_numbers:
            for name in (
                "question_crops", "question_crops.json", "crop_ai_review.json", "review",
            ):
                _remove_formal_output(job_dir, name)
        plan_content = _atomic_json(job_dir / "question_regions.json", plan)
        crop_questions = [{
            "question_no": item["question_no"],
            "regions": [{"page_number": region["page_number"], "bbox": region["bbox"]}
                        for region in item["regions"]],
            "mask_regions_normalized": item["mask_regions_normalized"],
            "mask_regions": item["mask_regions"],
            "warnings": item.get("warnings", []),
        } for item in plan["questions"]]
        report = generate_question_crops_report(
            job_dir=job_dir, questions=crop_questions,
            expected_question_nos=list(range(1, plan["question_count"] + 1)),
            source_page_bytes={number: content for number, content, _ in pages},
            job_lock=artifact_lock,
            force_recrop_question_nos=recrop_numbers,
        )
        generate_crop_review_sheets(
            job_dir=job_dir, recropped_question_nos=report.recropped_question_nos,
            job_lock=artifact_lock,
        )
        crop_manifest_content = _read_job_regular(
            private_root, job_id, "question_crops.json", max_bytes=16 * 1024 * 1024
        )
        if json.loads(crop_manifest_content.decode("utf-8")) != report.manifest:
            raise QuestionSplitError(SAFE_SPLIT_ERROR)
        digest = hashlib.sha256(plan_content).hexdigest()
        crop_digest = hashlib.sha256(crop_manifest_content).hexdigest()
        _record_new_split_anchors(job_dir, (
            plan["question_count"], digest, crop_digest,
            report.generation_id, report.manifest["signature"],
        ))
        now = _now()
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            render_anchor = connection.execute(
                """SELECT manifest_sha256,source_pdf_sha256,total_pages
                   FROM import_page_render_runs WHERE import_job_id=? AND status='completed'""",
                (job_id,),
            ).fetchone()
            if (
                render_anchor is None or any(value is None for value in render_anchor[:2])
                or render_anchor[2] != len(pages)
            ):
                raise QuestionSplitError(SAFE_RENDER_REQUIRED)
            connection.execute(
                """UPDATE import_question_split_runs SET status='completed',
                   question_count=?,processed_pages=?,error_message=NULL,codex_run_id=?,
                   result_manifest_sha256=?,render_manifest_sha256=?,source_pdf_sha256=?,
                   crop_manifest_sha256=?,crop_generation_id=?,crop_manifest_signature=?,
                   completed_at=?,updated_at=? WHERE import_job_id=?""",
                (
                    plan["question_count"], len(pages), response.run_id, digest,
                    render_anchor[0], render_anchor[1], crop_digest,
                    report.generation_id, report.manifest["signature"], now, now, job_id,
                ),
            )
            try:
                connection.commit()
                database_committed = True
            except Exception:
                journal = _read_split_journal(job_dir)
                new_anchors = tuple(journal["new_anchors"] or ())
                old_anchors = tuple(journal["old_anchors"])
                state = _authoritative_split_commit_state(
                    database_path, private_root, job_id, old_anchors, new_anchors,
                )
                if state == "committed":
                    database_committed = True
                elif state == "unknown":
                    commit_uncertain = True
                    raise SplitCommitUncertain(SAFE_SPLIT_ERROR)
                else:
                    raise
        try:
            shutil.rmtree(backup)
        except FileNotFoundError:
            pass
        try:
            (job_dir / _SPLIT_JOURNAL_NAME).unlink()
        except FileNotFoundError:
            pass
        return plan
    except Exception:
        if not database_committed and not commit_uncertain:
            _restore_outputs(job_dir, backup)
            raise
        if commit_uncertain:
            raise
        LOGGER.warning(
            "deferred cleanup for committed question split job %s",
            job_id,
            exc_info=True,
        )
        return plan


def run_claimed_split(claim):
    if not isinstance(claim, SplitClaim) or claim.lock_stream is None:
        raise QuestionSplitError("切题任务无效")
    try:
        try:
            return split_import_job(
                claim.database_path, claim.private_root, claim.job_id, claim.runner,
                replace_invalid=claim.replace_invalid,
                review_feedback=claim.review_feedback,
                frozen_regions=claim.frozen_regions,
                artifact_lock=claim.artifact_lock,
            )
        except SplitCommitUncertain:
            LOGGER.exception(
                "question split commit outcome is uncertain for import job %s",
                claim.job_id,
            )
            return None
        except Exception:
            LOGGER.exception("question split failed for import job %s", claim.job_id)
            _mark_failed(claim.database_path, claim.job_id)
            return None
    finally:
        claim.close()
