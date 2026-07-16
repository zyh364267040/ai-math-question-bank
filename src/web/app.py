"""FastAPI application for browsing papers and OCR review candidates."""

import json
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

from src.database import baskets as basket_store
from src.database.initialize import initialize_database
from src.importing.pdf_intake import PdfIntakeError, has_intake_receipt, intake_pdf
from src.importing.upload_confirmation import (
    MANIFEST_HMAC_KEY_FILENAME,
    MAX_UPLOAD_BYTES,
    UploadConfirmationError,
    discard_staged_upload,
    load_verified_upload,
    pending_upload_operation,
    stage_pdf_upload,
    validate_import_metadata,
)
from src.processing.pdf_page_renderer import (
    PageRenderError,
    claim_render_job,
    run_claimed_render,
)
from src.processing.page_layout_analyzer import (
    PageLayoutError,
    claim_layout_job,
    load_completed_layout,
    read_layout_overlay,
    run_claimed_layout,
)
from src.processing.question_splitter import (
    SAFE_CODEX_MISSING,
    SAFE_EXISTING_ERROR as SAFE_SPLIT_EXISTING_ERROR,
    SAFE_RENDER_REQUIRED,
    SAFE_SPLIT_ERROR,
    SAFE_WEEKLY_LOW,
    SAFE_WEEKLY_UNAVAILABLE,
    QuestionSplitError,
    claim_split_job,
    completed_split_result_valid,
    read_completed_split_image,
    run_claimed_split,
)
from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    validate_signed_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "private" / "question-bank.db"
DEFAULT_PRIVATE_ROOT = PROJECT_ROOT / "data" / "private"
WEB_ROOT = Path(__file__).resolve().parent
STATUS_NAMES = {
    "pending": "等待处理",
    "processing": "处理中",
    "needs_review": "待人工审核",
    "completed": "已完成",
    "failed": "处理失败",
}
RENDER_STATUS_NAMES = {
    "pending": "等待开始",
    "processing": "页面处理中",
    "completed": "页面处理完成",
    "failed": "页面处理失败",
}
LAYOUT_STATUS_NAMES = {
    "pending": "等待开始",
    "processing": "版面分析中",
    "completed": "版面分析完成",
    "failed": "版面分析失败",
}
SPLIT_STATUS_NAMES = {
    "pending": "等待调用",
    "processing": "Codex 正在自动切题",
    "completed": "自动切题完成",
    "failed": "自动切题失败",
}
SAFE_SPLIT_ERRORS = {SAFE_SPLIT_ERROR, SAFE_CODEX_MISSING}
SAFE_LAYOUT_ERRORS = {
    "页面分析输入校验失败，请重新处理页面后重试",
    "页面数量或分析结果超过安全处理限制",
    "版面分析失败，请重试",
    "现有版面分析结果校验失败，请点击重试",
}
SAFE_RENDER_ERRORS = {
    "归档 PDF 校验失败，请重新导入后重试",
    "确认页码范围无效，请重新导入后重试",
    "PDF 页数或页面尺寸超过安全处理限制",
    "页面处理失败，请重试",
    "现有页面结果校验失败，请点击重试",
}
AUDIT_STATUSES = ("auto_pass", "disputed", "human_required")
FILTER_STATUSES = (*AUDIT_STATUSES, "all")
REVIEW_ACTION_STATUS = {"save": "draft", "approve": "approved", "needs_fix": "needs_fix", "needs_recrop": "needs_recrop"}
REVIEW_STATUS_NAMES = {
    "pending": "等待审核", "draft": "草稿", "approved": "审核通过",
    "needs_fix": "需要修正", "needs_recrop": "需要重切",
}
DELETION_REASONS = {
    "unreadable": "看不清", "incomplete": "题目不完整", "duplicate": "重复题",
    "unneeded": "不需要", "other": "其他",
}
DELETE_FORM_FIELDS = {"csrf_token", "reason", "note", "confirmed", "next"}
RESTORE_FORM_FIELDS = {"csrf_token"}
CANDIDATE_DELETE_FORM_FIELDS = {"csrf_token", "version", "reason", "note", "confirmed"}
CANDIDATE_RESTORE_FORM_FIELDS = {"csrf_token", "version"}
MAX_DELETE_FORM_BYTES = 4_096
MAX_PREVIEW_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
MAX_RENDER_START_FORM_BYTES = 64 * 1024
REVIEW_FORM_FIELDS = {
    "csrf_token", "version", "action", "stem_markdown", "question_type_code",
    "primary_knowledge_point_code", "related_knowledge_point_codes", "review_notes",
    "option_source_index", "option_code", "option_content", "option_order",
    "subquestion_source_index", "subquestion_content", "subquestion_order",
    "options_present", "subquestions_present",
}
QUICK_REVIEW_FORM_FIELDS = {"csrf_token", "version", "action"}
INLINE_EDIT_FORM_FIELDS = {"csrf_token", "version", "field", "index", "value"}
QUICK_REVIEW_ACTION_STATUS = {
    "approve": "approved", "needs_fix": "needs_fix", "needs_recrop": "needs_recrop",
}
CHOICE_TYPES = {"single_choice", "multiple_choice"}
MAX_REVIEW_FORM_BYTES = 256_000
MAX_STRUCTURED_ITEMS = 100
MAX_ITEM_CONTENT_LENGTH = 10_000
MAX_INLINE_EDIT_FORM_BYTES = 32_000
INLINE_EDIT_FIELDS = {
    "stem_markdown": (None, None, 20_000),
    "option_content": ("options", "content", MAX_ITEM_CONTENT_LENGTH),
    "subquestion_content": ("subquestions", "stem_markdown", MAX_ITEM_CONTENT_LENGTH),
}
MAX_REVIEW_GUIDANCE_ITEMS = 8
MAX_REVIEW_GUIDANCE_ITEM_LENGTH = 180
OPTION_CODE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,15}\Z")
MAIN_SUBQUESTION_LABEL = r"(?:（(?P<full_main>\d+)）|\((?P<ascii_main>\d+)\))"
ROMAN_SUBQUESTION_LABEL = r"(?:（(?P<full_child>i{1,3}|iv|v|vi{0,3}|ix|x)）|\((?P<ascii_child>i{1,3}|iv|v|vi{0,3}|ix|x)\))"
CIRCLED_SUBQUESTION_LABEL = r"(?P<circled_child>[①②③④⑤⑥⑦⑧⑨⑩])"
CHILD_SUBQUESTION_LABEL = rf"(?:{ROMAN_SUBQUESTION_LABEL}|{CIRCLED_SUBQUESTION_LABEL})"
SUBQUESTION_LABEL_PATTERN = re.compile(
    rf"\A(?P<main>{MAIN_SUBQUESTION_LABEL})(?P<child>{CHILD_SUBQUESTION_LABEL})?\Z",
    re.IGNORECASE,
)
STORED_SUBQUESTION_PREFIX_PATTERN = re.compile(
    rf"\A\s*(?P<label>{MAIN_SUBQUESTION_LABEL}(?:{CHILD_SUBQUESTION_LABEL})?)\s*",
    re.IGNORECASE,
)
STORED_NONSTANDARD_PREFIX_PATTERN = re.compile(
    rf"\A\s*(?P<label>{MAIN_SUBQUESTION_LABEL}\s*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])\s*"
)
ROMAN_SUBQUESTION_ORDERS = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5,
    "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9, "⑩": 10,
}


class _PreviewRequestBodyTooLarge(Exception):
    pass


class PreviewUploadBodyLimitMiddleware:
    """Enforce route-specific import caps while ASGI body messages are received."""

    def __init__(self, app, max_body_bytes=MAX_PREVIEW_REQUEST_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        if path == "/imports/preview":
            request_limit = self.max_body_bytes
        elif re.fullmatch(r"/imports/[^/]+/(?:render|layout|split)", path):
            request_limit = min(self.max_body_bytes, MAX_RENDER_START_FORM_BYTES)
        else:
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            try:
                declared_length = int(content_lengths[-1])
            except ValueError:
                declared_length = 0
            if declared_length > request_limit:
                await self._send_413(send)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > request_limit:
                    raise _PreviewRequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _PreviewRequestBodyTooLarge:
            if not response_started:
                await self._send_413(send)

    @staticmethod
    async def _send_413(send):
        body = b"Request body too large"
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class AuditDataError(ValueError):
    """The second-pass audit cannot be safely matched to the candidates."""


def _group_labeled_subquestions(subquestions):
    """Preserve authoritative labels and group only an unambiguous whitelist.

    Candidate rows carry ``label`` separately.  Already-admitted rows predate a
    label column and store the label at the start of ``stem_markdown``; those
    known prefixes are separated here so presentation never numbers them twice.
    """
    flat_items = []
    parsed_labels = []
    if not isinstance(subquestions, (list, tuple)):
        subquestions = []
    for flat_index, source in enumerate(subquestions):
        item = dict(source) if isinstance(source, (dict, sqlite3.Row)) else {}
        stem = str(item.get("stem_markdown", ""))
        if "label" in item:
            raw_label = item.get("label")
            label = str(raw_label).strip() if raw_label is not None else ""
        else:
            prefix = STORED_SUBQUESTION_PREFIX_PATTERN.match(stem)
            if prefix is None:
                prefix = STORED_NONSTANDARD_PREFIX_PATTERN.match(stem)
            label = prefix.group("label").strip() if prefix else ""
            if prefix:
                stem = stem[prefix.end():].lstrip()
        match = SUBQUESTION_LABEL_PATTERN.fullmatch(label) if label else None
        main_number = int(match.group("full_main") or match.group("ascii_main")) if match else None
        child_value = (
            (match.group("full_child") or match.group("ascii_child") or match.group("circled_child")).lower()
            if match and match.group("child") else None
        )
        main_label = match.group("main") if match else ""
        child_label = match.group("child") if match and match.group("child") else ""
        display_label = label or f"第{flat_index + 1}项"
        title = (
            f"第{main_label}问{child_label}" if match
            else display_label
        )
        item.update({
            "flat_index": flat_index,
            "original_label": label,
            "display_label": display_label,
            "main_label": main_label,
            "child_label": child_label,
            "title": title,
            "stem_markdown": stem,
        })
        flat_items.append(item)
        parsed_labels.append((match, main_number, child_value))

    groups = []
    valid = bool(flat_items) and all(match is not None for match, _, _ in parsed_labels)
    expected_main = 1
    group_by_number = {}
    child_orders = {}
    if valid:
        for item, (_, main_number, child_value) in zip(flat_items, parsed_labels):
            group = group_by_number.get(main_number)
            if group is None:
                if main_number != expected_main:
                    valid = False
                    break
                group = {
                    "main_number": main_number,
                    "main_label": item["main_label"],
                    "parent": None,
                    "children": [],
                }
                groups.append(group)
                group_by_number[main_number] = group
                child_orders[main_number] = 0
                expected_main += 1
            elif main_number != expected_main - 1:
                valid = False
                break
            if child_value is None:
                if group["parent"] is not None or group["children"]:
                    valid = False
                    break
                group["parent"] = item
            else:
                child_order = ROMAN_SUBQUESTION_ORDERS.get(child_value)
                if child_order != child_orders[main_number] + 1:
                    valid = False
                    break
                child_orders[main_number] = child_order
                group["children"].append(item)

    if not valid:
        groups = []
    return {"groups": groups, "flat_items": flat_items, "needs_review": bool(flat_items) and not valid}


def _structured_rows(form, prefix, current_items, editable_key):
    """Validate parallel form arrays and merge edits into their current draft rows."""
    names = (f"{prefix}_source_index", f"{prefix}_content", f"{prefix}_order")
    source_indexes, contents, orders = (list(form.getlist(name)) for name in names)
    extra_values = []
    if prefix == "option":
        extra_values = list(form.getlist("option_code"))
    arrays = [source_indexes, contents, orders, extra_values] if prefix == "option" else [source_indexes, contents, orders]
    lengths = {len(values) for values in arrays}
    if len(lengths) != 1:
        raise ValueError("结构化字段数量不一致")
    count = lengths.pop()
    if count > MAX_STRUCTURED_ITEMS:
        raise ValueError("选项或小问数量过多")
    expected_orders = [str(index) for index in range(1, count + 1)]
    if orders != expected_orders:
        raise ValueError("顺序必须唯一且连续")
    if any(len(str(content)) > MAX_ITEM_CONTENT_LENGTH for content in contents):
        raise ValueError("选项或小问内容过长")

    used_indexes = set()
    rebuilt = []
    for position, raw_index in enumerate(source_indexes):
        if raw_index == "":
            item = {}
        else:
            try:
                source_index = int(raw_index)
            except (TypeError, ValueError) as error:
                raise ValueError("原始项目标识无效") from error
            if source_index < 0 or source_index >= len(current_items) or source_index in used_indexes:
                raise ValueError("原始项目标识无效")
            used_indexes.add(source_index)
            source_item = current_items[source_index]
            if not isinstance(source_item, dict):
                raise ValueError("候选题结构无效")
            item = dict(source_item)
        item[editable_key] = str(contents[position])
        if prefix == "option":
            code = str(extra_values[position]).strip()
            if not code or not OPTION_CODE_PATTERN.fullmatch(code):
                raise ValueError("选项标识格式无效")
            item["code"] = code
        rebuilt.append(item)
    if prefix == "option" and len({item["code"].casefold() for item in rebuilt}) != len(rebuilt):
        raise ValueError("选项标识不能重复")
    return rebuilt


def _connect(database_path):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _job_dir(private_root, job_id):
    return Path(private_root) / "processing" / f"import_job_{job_id}"


def _load_json(path, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"{label}缺失") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}损坏") from error


def _validate_audit_payload(audit, job_id, candidate_questions):
    """Validate already-decoded audit data without reopening its source file."""
    try:
        entries = audit["questions"]
        counts = audit["counts"]
        if not isinstance(audit, dict) or not isinstance(entries, list) or not isinstance(counts, dict):
            raise AuditDataError
        candidate_nos = [question["source_question_no"] for question in candidate_questions]
        if (
            audit.get("import_job_id") != job_id
            or audit.get("question_count") != len(candidate_questions)
            or len(entries) != len(candidate_questions)
            or len(candidate_nos) != len(set(candidate_nos))
        ):
            raise AuditDataError

        entries_by_no = {}
        calculated = {status: 0 for status in AUDIT_STATUSES}
        for entry in entries:
            if not isinstance(entry, dict):
                raise AuditDataError
            number = entry.get("source_question_no")
            status = entry.get("audit_status")
            issues = entry.get("issues")
            corrections = entry.get("suggested_corrections")
            evidence_page = entry.get("evidence_page")
            if (
                not isinstance(number, str)
                or number in entries_by_no
                or status not in AUDIT_STATUSES
                or not isinstance(issues, list)
                or not all(isinstance(text, str) for text in issues)
                or not isinstance(corrections, list)
                or not all(isinstance(text, str) for text in corrections)
                or not isinstance(evidence_page, int)
                or isinstance(evidence_page, bool)
                or evidence_page < 1
            ):
                raise AuditDataError
            entries_by_no[number] = entry
            calculated[status] += 1
        if set(entries_by_no) != set(candidate_nos):
            raise AuditDataError
        if set(counts) != set(AUDIT_STATUSES):
            raise AuditDataError
        if any(not isinstance(counts[key], int) or isinstance(counts[key], bool) for key in counts):
            raise AuditDataError
        if counts != calculated or sum(counts.values()) != len(candidate_questions):
            raise AuditDataError

        recommendation = audit.get("random_sample_recommendation")
        if recommendation is not None:
            if not isinstance(recommendation, dict):
                raise AuditDataError
            question_nos = recommendation.get("question_nos")
            reason = recommendation.get("reason")
            if (
                not isinstance(question_nos, list)
                or not isinstance(reason, str)
                or any(str(number) not in entries_by_no for number in question_nos)
            ):
                raise AuditDataError
        return audit, entries_by_no
    except (ValueError, KeyError, TypeError) as error:
        raise AuditDataError("AI复核数据损坏或不完整") from error


def _load_valid_audit(path, job_id, candidate_questions):
    """Load one fixed audit file and reject the entire audit on any mismatch."""
    try:
        audit = _load_json(path, "AI复核数据")
    except ValueError as error:
        raise AuditDataError("AI复核数据损坏或不完整") from error
    return _validate_audit_payload(audit, job_id, candidate_questions)


def _load_figure_assets(job_dir):
    """Return only safe PNG entries from the independent private asset manifest."""
    path = job_dir / "figure_assets.json"
    if not path.is_file():
        return []
    try:
        payload = _load_json(path, "配图清单")
        entries = payload["assets"]
        if not isinstance(entries, list):
            return []
    except (ValueError, KeyError, TypeError):
        return []
    safe = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative = entry.get("output_relative_path")
        question_no = entry.get("question_no")
        kind = entry.get("kind")
        if not isinstance(relative, str) or not isinstance(question_no, str):
            continue
        candidate = PurePosixPath(relative)
        if (candidate.is_absolute() or ".." in candidate.parts or "\\" in relative
                or candidate.suffix.lower() != ".png" or candidate.parts[0] not in {"assets", "review"}
                or not question_no.isdigit() or kind not in {"question_figure", "review_evidence"}):
            continue
        safe.append(entry)
    return safe


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _review_source_texts(value):
    """Yield only scalar review hints; nested raw structures are never rendered."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item


def _review_action(text, source="general"):
    """Translate a technical hint into one concise Chinese review action."""
    compact = re.sub(r"\s+", " ", str(text)).strip()
    folded = compact.casefold()
    if source == "crop" or any(word in folded for word in ("裁图", "裁切", "crop")):
        return "检查裁图是否缺边、混入相邻题或遗漏题图"
    if any(word in folded for word in ("小问", "子问", "subquestion")):
        return "检查小问数量、顺序和编号是否完整"
    if any(word in folded for word in ("图像选项", "配图", "题图", "图片", "图形", "image option")):
        return "检查题干与配图是否完整、方向和选项对应是否正确"
    if any(word in folded for word in ("知识点", "题型", "knowledge", "question type")):
        return "确认题型和主、关联知识点是否准确"
    if any(word in folded for word in ("ocr", "latex", "公式", "符号", "识别")):
        return "对照原题检查文字、符号和 LaTeX 公式"
    if not compact:
        return None
    prefix = "按建议复核：" if source == "correction" else "核对并处理："
    available = MAX_REVIEW_GUIDANCE_ITEM_LENGTH - len(prefix) - 1
    shortened = compact[:available] + ("…" if len(compact) > available else "")
    return prefix + shortened


def _review_guidance(question, edited, crop, figures, audit):
    """Build the server-rendered, question-type-specific review checklist."""
    basic_items = ["对照原题检查题干文字、数字、符号和 LaTeX 公式"]
    question_type = edited.get("question_type_code")
    if question_type in CHOICE_TYPES:
        basic_items.append("核对选项数量、顺序和内容")
    elif question_type == "fill_blank":
        basic_items.append("检查填空位置和题干完整性")
    elif question_type == "solution":
        basic_items.append("检查小问数量、顺序、编号和公式")
    basic_items.extend((
        "确认题型和主、关联知识点是否准确",
        "核对来源页码是否准确",
    ))
    options = edited.get("options", [])
    image_options = bool(options) and all(
        isinstance(item, dict) and str(item.get("content", "")).strip() == "见原页选项图"
        for item in options
    )
    requires_figure = bool(question.get("figure_required")) or image_options
    priority_items = []

    def add(action):
        if action and action not in priority_items and len(priority_items) < MAX_REVIEW_GUIDANCE_ITEMS:
            priority_items.append(action[:MAX_REVIEW_GUIDANCE_ITEM_LENGTH])

    if question_type == "solution" and _group_labeled_subquestions(
        edited.get("subquestions", [])
    )["needs_review"]:
        add("请核对小问层级标签")

    for field in ("warnings", "review_notes"):
        for text in _review_source_texts(question.get(field)):
            add(_review_action(text))
    needs_review = question.get("needs_review")
    if needs_review:
        texts = list(_review_source_texts(needs_review))
        if texts:
            for text in texts:
                add(_review_action(text))
        else:
            add("按候选题的人工复核标记，对照原题逐项确认")
    confidence = question.get("confidence")
    if confidence not in (None, "", "high"):
        confidence_name = {"medium": "中", "low": "低"}.get(str(confidence).casefold(), "异常")
        add(f"候选识别置信度为{confidence_name}，请重点对照原题核查")
    if crop:
        for text in _review_source_texts(crop.get("warnings")):
            add(_review_action(text, "crop"))
    if audit:
        for text in _review_source_texts(audit.get("issues")):
            add(_review_action(text, "issue"))
        for text in _review_source_texts(audit.get("suggested_corrections")):
            add(_review_action(text, "correction"))
        audit_confidence = audit.get("audit_confidence")
        if audit_confidence not in (None, "", "high"):
            add("AI 复核置信度不足，请结合原题证据人工确认")
        if audit.get("audit_status") != "auto_pass" and not priority_items:
            add("AI 复核要求人工确认，请对照原题逐项核查")
    if requires_figure:
        basic_items.append("检查配图完整性及其与题图对应关系")
        available = bool(crop) if image_options else bool(figures)
        pending = (
            crop.get("review_status") != "ai_review_passed"
            if image_options and crop
            else any(item.get("review_status") != "ai_review_passed" for item in figures)
        )
        if not available:
            add("必要配图缺失，请检查题干与配图是否完整、方向和选项对应是否正确")
        elif pending:
            add("必要配图审核尚未通过，请检查题干与配图是否完整、方向和选项对应是否正确")
    return {
        "focus": bool(priority_items),
        "priority_items": priority_items,
        "basic_items": basic_items,
    }


def _initialize_candidate_drafts(connection, job_id, candidate_path, questions):
    digest = _file_sha256(candidate_path)
    for question in questions:
        number = str(question.get("source_question_no", "")).strip()
        if not number or len(number) > 50:
            raise ValueError("候选题号无效")
        snapshot = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
        connection.execute(
            """INSERT OR IGNORE INTO candidate_review_drafts
               (import_job_id,source_question_no,source_candidate_sha256,source_snapshot_json,edited_json)
               VALUES(?,?,?,?,?)""", (job_id, number, digest, snapshot, snapshot)
        )


def _validate_review_approval(edited, original, valid_types, valid_points, crop, figures):
    """Apply the same approval gate to full-form and status-only review actions."""
    if not isinstance(edited, dict) or not isinstance(original, dict):
        raise ValueError("候选题结构无效")
    stem = edited.get("stem_markdown")
    if not isinstance(stem, str) or not stem.strip():
        raise ValueError("审核通过前题干不能为空")
    type_code = edited.get("question_type_code")
    primary = edited.get("primary_knowledge_point_code")
    if type_code not in valid_types or primary not in valid_points:
        raise ValueError("题型或知识点无效")
    options = edited.get("options", [])
    if not isinstance(options, list):
        raise ValueError("候选题结构无效")
    if type_code in CHOICE_TYPES:
        if len(options) < 2:
            raise ValueError("单选或多选题至少需要两个选项")
        codes = []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("content"), str):
                raise ValueError("选择题选项结构无效")
            code = option.get("code")
            if not isinstance(code, str) or not OPTION_CODE_PATTERN.fullmatch(code.strip()):
                raise ValueError("选择题选项结构无效")
            codes.append(code.strip().casefold())
        if len(codes) != len(set(codes)):
            raise ValueError("选项标识不能重复")
    image_options = bool(options) and all(
        isinstance(option, dict) and str(option.get("content", "")).strip() == "见原页选项图"
        for option in options
    )
    if image_options and not crop:
        raise ValueError("图像选项题缺少必要图片")
    if original.get("figure_required") and not figures:
        raise ValueError("需要图形题缺少必要配图")


def _review_error(request, templates, message, status_code):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return _error(request, templates, message, status_code)


def _load_question_crops(
    job_dir, job_id, expected_question_nos=None, *, require_signature=False
):
    """Load an all-or-nothing, file-verified complete-question crop manifest."""
    path = job_dir / "question_crops.json"
    if not path.is_file():
        return []
    try:
        payload = _load_json(path, "单题原图清单")
        if require_signature:
            payload = validate_signed_manifest(
                payload, _read_existing_crop_key(job_dir), expected_job_id=job_id,
                expected_question_nos=(
                    sorted(int(number) for number in expected_question_nos)
                    if expected_question_nos is not None else None
                ),
            )
        entries = payload["questions"]
        if (not isinstance(payload, dict) or payload.get("import_job_id") != job_id
                or not isinstance(entries, list) or payload.get("question_count") != len(entries)):
            return []
        numbers = [entry.get("question_no") for entry in entries if isinstance(entry, dict)]
        if (len(numbers) != len(entries) or any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in numbers)
                or len(numbers) != len(set(numbers))):
            return []
        expected = sorted(int(number) for number in expected_question_nos) if expected_question_nos is not None else sorted(numbers)
        if sorted(numbers) != expected:
            return []
        safe = []
        for entry in entries:
            number = entry["question_no"]
            relative = entry.get("output_relative_path")
            expected_relative = f"question_crops/Q{number:03d}.png"
            candidate = PurePosixPath(relative) if isinstance(relative, str) else None
            if (candidate is None or candidate.is_absolute() or ".." in candidate.parts
                    or "\\" in relative or candidate.as_posix() != expected_relative
                    or entry.get("crop_status") != "generated"
                    or entry.get("review_status") not in {"pending_ai_review", "ai_review_passed"}
                    or not isinstance(entry.get("warnings"), list)):
                return []
            target = (job_dir / expected_relative).resolve()
            if not target.is_relative_to(job_dir.resolve()) or not target.is_file():
                return []
            if (entry.get("byte_size") != target.stat().st_size
                    or entry.get("sha256") != _file_sha256(target)):
                return []
            with Image.open(target) as image:
                if (image.format != "PNG" or image.size != (entry.get("width"), entry.get("height"))
                        or image.width < 1 or image.height < 1):
                    return []
            safe.append(entry)
        return safe
    except (ValueError, KeyError, TypeError, OSError, UnidentifiedImageError,
            SecureCropArtifactError):
        return []


def _read_existing_crop_key(job_dir):
    """Read the existing signing key without allowing a GET path to create it."""
    key_path = Path(job_dir).parent.parent / MANIFEST_HMAC_KEY_FILENAME
    descriptor = None
    try:
        descriptor = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600
                or not 32 <= details.st_size <= 4096):
            raise OSError
        content = os.read(descriptor, 4097)
        if len(content) != details.st_size:
            raise OSError
        return content
    except OSError as error:
        raise SecureCropArtifactError("裁图签名密钥不可用") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _get_job(connection, job_id):
    return connection.execute(
        """SELECT j.id, j.page_start, j.page_end, j.status, j.created_at,
                  s.id AS source_paper_id, s.paper_name, s.original_filename,
                  s.exam_year, r.name AS region_name, e.name AS exam_type_name
           FROM import_jobs j
           JOIN source_papers s ON s.id = j.source_paper_id
           JOIN regions r ON r.code = s.region_code
           JOIN exam_types e ON e.code = s.exam_type_code
           WHERE j.id = ?""",
        (job_id,),
    ).fetchone()


def _basket_count(connection):
    return connection.execute(
        """SELECT COUNT(*) FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id
           JOIN questions q ON q.id=bi.question_id
           WHERE b.basket_key='default' AND q.deleted_at IS NULL"""
    ).fetchone()[0]


def _required_question_content(question, options, assets):
    """Select only the images needed to solve a formal question.

    The admission pipeline records candidate ``figure_required`` as a passed
    figure review.  Complete-question crops exist for every imported question,
    so asset presence alone must never define this semantic.
    """
    registered = [dict(asset) for asset in assets]
    raw_options = [dict(option) for option in options]
    placeholder_options = bool(raw_options) and all(
        str(option.get("content_markdown", "")).strip() == "见原页选项图"
        for option in raw_options
    )
    display_options = [] if placeholder_options else raw_options
    required = question.get("figure_review_status") == "passed"
    image_options = (
        required
        and question.get("question_type_code") in CHOICE_TYPES
        and not display_options
    )
    wanted_kind = "complete_question" if image_options else "question_figure"
    candidates = sorted(
        (asset for asset in registered if required and asset.get("asset_kind") == wanted_kind),
        key=lambda asset: (asset.get("display_order", 0), asset.get("id", 0)),
    )
    selected, seen = [], set()
    for asset in candidates:
        relative_path = asset.get("relative_path")
        if relative_path in seen:
            continue
        seen.add(relative_path)
        selected.append(asset)
        if image_options:
            break
    return {
        "has_required_image": required,
        "image_options": image_options,
        "display_options": display_options,
        "display_assets": selected,
        "required_image_missing": required and not selected,
    }


def _basket_questions(connection):
    rows = connection.execute(
        """SELECT q.*,bi.position,qt.name AS question_type_name,kp.name AS primary_knowledge_name,
                  s.paper_name AS source_paper_name,s.exam_year,qs.import_job_id,qs.source_question_no,
                  (q.figure_review_status='passed') AS has_figure
           FROM baskets b JOIN basket_items bi ON bi.basket_id=b.id JOIN questions q ON q.id=bi.question_id
           JOIN question_types qt ON qt.code=q.question_type_code
           JOIN knowledge_points kp ON kp.id=q.primary_knowledge_point_id
           JOIN question_sources qs ON qs.question_id=q.id JOIN source_papers s ON s.id=qs.source_paper_id
           WHERE b.basket_key='default' AND q.deleted_at IS NULL ORDER BY bi.position"""
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["options"] = connection.execute(
            "SELECT option_code,content_markdown FROM question_options WHERE question_id=? ORDER BY display_order", (row["id"],)
        ).fetchall()
        item["subquestions"] = connection.execute(
            "SELECT * FROM subquestions WHERE question_id=? ORDER BY display_order", (row["id"],)
        ).fetchall()
        item["subquestion_display"] = _group_labeled_subquestions(item["subquestions"])
        item["assets"] = connection.execute(
            "SELECT * FROM question_assets WHERE question_id=? ORDER BY asset_kind,display_order", (row["id"],)
        ).fetchall()
        result.append(item)
    return result


def _verified_asset_path(private_root, asset):
    job_dir = _job_dir(private_root, asset["import_job_id"])
    manifest_name = "question_crops.json" if asset["asset_kind"] == "complete_question" else "figure_assets.json"
    payload = _load_json(job_dir / manifest_name, "图片清单")
    if not isinstance(payload, dict):
        raise ValueError("图片清单验证失败")
    entries = payload.get("questions", []) if asset["asset_kind"] == "complete_question" else payload.get("assets", [])
    if not isinstance(entries, list):
        raise ValueError("图片清单验证失败")
    manifest = next((x for x in entries if isinstance(x, dict) and x.get("output_relative_path") == asset["relative_path"]), None)
    if (manifest is None or manifest.get("review_status") != "ai_review_passed"
            or any(manifest.get(key) != asset[key] for key in ("width", "height", "byte_size", "sha256"))):
        raise ValueError("图片清单验证失败")
    relative = str(asset["relative_path"])
    relative_path = PurePosixPath(relative)
    target = (job_dir / relative_path).resolve()
    if (relative_path.is_absolute() or ".." in relative_path.parts or "\\" in relative
            or not target.is_relative_to(job_dir.resolve()) or not target.is_file()
            or target.suffix.lower() != ".png" or _file_sha256(target) != asset["sha256"]
            or target.stat().st_size != asset["byte_size"]):
        raise ValueError("图片文件验证失败")
    with Image.open(target) as image:
        if image.format != "PNG" or image.size != (asset["width"], asset["height"]):
            raise ValueError("图片格式验证失败")
    return target


EXPORT_OPTION_NAMES = (
    "include_source", "include_knowledge", "include_answers", "include_analysis",
)
LEGACY_IGNORED_EXPORT_OPTIONS = {"include_images"}


def _parse_export_options(form):
    unknown = set(form.keys()) - {"csrf_token", *EXPORT_OPTION_NAMES, *LEGACY_IGNORED_EXPORT_OPTIONS}
    invalid_values = [key for key in EXPORT_OPTION_NAMES if key in form and form.get(key) != "on"]
    if unknown or invalid_values:
        raise ValueError("无效的导出选项")
    return {key: key in form for key in EXPORT_OPTION_NAMES}


def _exercise_questions(questions, options):
    """Build the shared, inert content model used by preview and Markdown export."""
    result = []
    for number, question in enumerate(questions, 1):
        item = dict(question)
        item["number"] = number
        content = _required_question_content(
            item, question["options"], question["assets"]
        )
        item.update(content)
        item["image_placeholders"] = content["image_options"]
        item["answer_display"] = (
            question["answer_markdown"] if question["answer_status"] == "provided"
            else "原卷未提供答案"
        )
        item["analysis_display"] = question["analysis_markdown"] or "原卷未提供解析"
        for asset in item["display_assets"]:
            asset["preview_url"] = (
                f"/question-assets/{quote(question['question_code'], safe='')}/"
                f"{quote(asset['relative_path'], safe='/')}"
            )
        result.append(item)
    return result


def _export_markdown(private_root, questions, options, destination):
    lines = ["# 数学练习", ""]
    assets_dir = destination / "assets"
    for question in _exercise_questions(questions, options):
        number = question["number"]
        lines += [f"## {number}.", "", question["stem_markdown"], ""]
        for option in question["display_options"]:
            lines += [f"{option['option_code']}. {option['content_markdown']}", ""]
        subquestion_display = question["subquestion_display"]
        if subquestion_display["groups"]:
            for group in subquestion_display["groups"]:
                parent = group["parent"]
                lines += [
                    f"{group['main_label']}{parent['stem_markdown'] if parent else ''}", ""
                ]
                for child in group["children"]:
                    lines += [f"　　{child['child_label']}{child['stem_markdown']}", ""]
        else:
            for sub in subquestion_display["flat_items"]:
                lines += [f"{sub['display_label']}{sub['stem_markdown']}", ""]
        if question["display_assets"]:
            for index, asset in enumerate(question["display_assets"], 1):
                source = _verified_asset_path(private_root, asset)
                assets_dir.mkdir(exist_ok=True)
                filename = f"{number:03d}_{index:02d}_{asset['asset_kind']}.png"
                shutil.copyfile(source, assets_dir / filename)
                lines += [f"![第{number}题图片](assets/{filename})", ""]
        if options["include_source"]:
            lines += [f"来源：{question['source_paper_name']} · {question['exam_year'] or '年份未知'} · 原题号 {question['source_question_no']}", ""]
        if options["include_knowledge"]:
            lines += [f"知识点：{question['primary_knowledge_name']}", ""]
        if options["include_answers"]:
            lines += [f"**答案：** {question['answer_display']}", ""]
        if options["include_analysis"]:
            lines += [f"**解析：** {question['analysis_display']}", ""]
    markdown = "\n".join(lines).rstrip() + "\n"
    target = destination / "练习.md"
    temporary = destination / ".练习.md.tmp"
    temporary.write_text(markdown, encoding="utf-8")
    os.replace(temporary, target)
    return target


def _error(request, templates, message, status_code):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"message": message, "status_code": status_code},
        status_code=status_code,
    )


def _wants_json(request):
    return "application/json" in request.headers.get("accept", "").lower()


def _safe_basket_next(value, question_code, default="/questions"):
    """Allow only local basket, listing, or the question being changed."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return default
    decoded = unquote(str(value))
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        return default
    parsed = urlsplit(str(value))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return default
    if parsed.path == "/basket" or parsed.path == "/questions" or parsed.path == f"/questions/{question_code}":
        return str(value)
    return default


def create_app(
    database_path=DEFAULT_DATABASE_PATH,
    private_root=DEFAULT_PRIVATE_ROOT,
    preview_request_max_bytes=MAX_PREVIEW_REQUEST_BYTES,
    split_runner=None,
    weekly_checker=None,
    _initialize_schema=True,
):
    database_path = Path(database_path)
    private_root = Path(private_root)
    if _initialize_schema:
        initialize_database(database_path).close()
    application = FastAPI(title="AI 数学题库", docs_url=None, redoc_url=None)
    application.state.database_path = database_path
    application.state.private_root = private_root
    application.state.split_runner = split_runner
    application.state.weekly_checker = weekly_checker
    if not _initialize_schema:
        @application.on_event("startup")
        def initialize_schema_on_server_start():
            """Migrate the production database before serving any request."""
            initialize_database(database_path).close()
    def common_context(request):
        try:
            with _connect(database_path) as connection:
                count = _basket_count(connection)
        except sqlite3.Error:
            count = 0
        return {"basket_count": count, "csrf_token": request.state.csrf_token}
    templates = Jinja2Templates(directory=WEB_ROOT / "templates", context_processors=[common_context])
    application.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")

    @application.middleware("http")
    async def identify_application(request: Request, call_next):
        cookie_token = request.cookies.get("basket_csrf")
        token = cookie_token if cookie_token and len(cookie_token) == 64 else secrets.token_hex(32)
        request.state.csrf_token = token
        response = await call_next(request)
        response.headers["X-AI-Math-Question-Bank"] = "1"
        if (
            request.method == "GET"
            and re.fullmatch(r"/imports/[^/]+/layout", request.url.path)
        ):
            response.headers["Cache-Control"] = "no-store"
        if cookie_token != token:
            response.set_cookie("basket_csrf", token, httponly=True, samesite="strict", secure=False)
        return response

    application.add_middleware(
        PreviewUploadBodyLimitMiddleware,
        max_body_bytes=preview_request_max_bytes,
    )

    async def require_csrf(request):
        form = await request.form()
        supplied = form.get("csrf_token", "")
        cookie = request.cookies.get("basket_csrf", "")
        if not supplied or not cookie or not hmac.compare_digest(str(supplied), cookie):
            for _, value in form.multi_items():
                if hasattr(value, "filename") and hasattr(value, "close"):
                    await value.close()
            return None
        return form

    @application.get("/health")
    def health():
        return JSONResponse({"status": "ok"})

    @application.get("/", response_class=HTMLResponse)
    def home(request: Request):
        try:
            with _connect(database_path) as connection:
                stats = {
                    "papers": connection.execute("SELECT COUNT(*) FROM source_papers").fetchone()[0],
                    "jobs": connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0],
                    "review_jobs": connection.execute(
                        "SELECT COUNT(*) FROM import_jobs WHERE status = 'needs_review'"
                    ).fetchone()[0],
                    "questions": connection.execute("SELECT COUNT(*) FROM questions WHERE deleted_at IS NULL").fetchone()[0],
                    "basket": _basket_count(connection),
                }
        except sqlite3.Error:
            return _error(request, templates, "题库数据库暂时无法读取", 500)
        return templates.TemplateResponse(
            request=request, name="home.html", context={"stats": stats}
        )

    @application.get("/papers", response_class=HTMLResponse)
    def papers(request: Request):
        try:
            with _connect(database_path) as connection:
                rows = connection.execute(
                    """SELECT j.id, j.page_start, j.page_end, j.status,
                              s.paper_name, s.original_filename, s.exam_year,
                              r.name AS region_name, e.name AS exam_type_name,
                              pr.status AS render_status,
                              pr.rendered_pages, pr.total_pages, pr.dpi,
                              la.status AS layout_status,
                              la.analyzed_pages, la.detected_questions,
                              qs.status AS split_status,
                              qs.question_count AS split_question_count,
                              (SELECT COUNT(*) FROM candidate_review_drafts d
                               WHERE d.import_job_id=j.id AND d.deleted_at IS NOT NULL) AS deleted_count
                       FROM import_jobs j
                       JOIN source_papers s ON s.id = j.source_paper_id
                       JOIN regions r ON r.code = s.region_code
                       JOIN exam_types e ON e.code = s.exam_type_code
                       LEFT JOIN import_page_render_runs pr ON pr.import_job_id = j.id
                       LEFT JOIN import_layout_analysis_runs la ON la.import_job_id = j.id
                       LEFT JOIN import_question_split_runs qs ON qs.import_job_id = j.id
                       ORDER BY j.created_at DESC, j.id DESC"""
                ).fetchall()
        except sqlite3.Error:
            return _error(request, templates, "试卷数据暂时无法读取", 500)

        jobs = []
        for row in rows:
            item = dict(row)
            item["status_name"] = STATUS_NAMES.get(item["status"], item["status"])
            candidate_path = _job_dir(private_root, item["id"]) / "candidate_questions.json"
            item["has_candidates"] = candidate_path.is_file()
            item["candidate_count"] = None
            item["audit"] = None
            item["audit_error"] = False
            if item["has_candidates"]:
                try:
                    data = _load_json(candidate_path, "候选题数据")
                    candidate_questions = data.get("questions", [])
                    item["candidate_count"] = len(candidate_questions)
                    item["first_question_no"] = str(candidate_questions[0].get("source_question_no", "1")) if candidate_questions else "1"
                    audit_path = _job_dir(private_root, item["id"]) / "ai_audit.json"
                    if audit_path.is_file():
                        try:
                            item["audit"], _ = _load_valid_audit(
                                audit_path, item["id"], candidate_questions
                            )
                        except AuditDataError:
                            item["audit_error"] = True
                except (ValueError, AttributeError):
                    item["candidate_count"] = "数据异常"
            jobs.append(item)
        return templates.TemplateResponse(
            request=request, name="papers.html", context={"jobs": jobs}
        )

    @application.post("/imports/{job_id}/render")
    async def start_page_render(
        request: Request, job_id: int, background_tasks: BackgroundTasks
    ):
        """Claim one user-authorized render and enqueue at most one worker."""
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) != {"csrf_token"}:
            return _error(request, templates, "页面处理请求参数无效", 400)
        try:
            claim = claim_render_job(database_path, private_root, job_id)
        except PageRenderError as error:
            message = str(error)
            if message == "未找到导入任务":
                return _error(request, templates, message, 404)
            if message == "该历史任务不能启动页面处理":
                return _error(request, templates, message, 409)
            return _error(request, templates, "页面处理任务暂时无法启动", 500)
        if claim is not None:
            background_tasks.add_task(run_claimed_render, claim)
        return RedirectResponse(
            f"/imports/{job_id}/processing", status_code=303
        )

    @application.get("/imports/{job_id}/processing", response_class=HTMLResponse)
    def page_render_status(request: Request, job_id: int):
        """Show safe render progress and explicit retry/validation controls."""
        try:
            with _connect(database_path) as connection:
                row = connection.execute(
                    """SELECT j.id, j.status AS import_status, j.page_start, j.page_end,
                              s.paper_name, s.original_filename,
                              r.status, r.dpi, r.total_pages, r.rendered_pages,
                              r.error_message
                       FROM import_jobs j
                       JOIN source_papers s ON s.id=j.source_paper_id
                       LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                       WHERE j.id=?""",
                    (job_id,),
                ).fetchone()
        except sqlite3.Error:
            return _error(request, templates, "页面处理状态暂时无法读取", 500)
        if row is None:
            return _error(request, templates, "未找到导入任务", 404)
        if row["status"] is None and row["import_status"] != "pending":
            return _error(request, templates, "该历史任务没有页面处理记录", 409)
        run = dict(row)
        run["status"] = run["status"] or "pending"
        run["dpi"] = run["dpi"] or 300
        run["rendered_pages"] = run["rendered_pages"] or 0
        if run["total_pages"] is None and run["page_start"] is not None:
            run["total_pages"] = run["page_end"] - run["page_start"] + 1
        run["status_name"] = RENDER_STATUS_NAMES[run["status"]]
        if run["error_message"] not in SAFE_RENDER_ERRORS:
            run["error_message"] = "页面处理失败，请重试" if run["status"] == "failed" else None
        return templates.TemplateResponse(
            request=request,
            name="import_processing.html",
            context={"run": run},
        )

    @application.post("/imports/{job_id}/layout")
    async def start_layout_analysis(
        request: Request, job_id: int, background_tasks: BackgroundTasks
    ):
        """Start analysis only after this explicit CSRF-protected request."""
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if (
            set(form.keys()) != {"csrf_token"}
            or len(list(form.multi_items())) != 1
        ):
            return _error(request, templates, "版面分析请求参数无效", 400)
        try:
            claim = claim_layout_job(database_path, private_root, job_id)
        except PageLayoutError as error:
            if str(error) == "未找到导入任务":
                return _error(request, templates, str(error), 404)
            if str(error) == "页面处理完成后才能开始版面分析":
                return _error(request, templates, str(error), 409)
            return _error(request, templates, "版面分析任务暂时无法启动", 500)
        if claim is not None:
            background_tasks.add_task(run_claimed_layout, claim)
        return RedirectResponse(f"/imports/{job_id}/layout", status_code=303)

    @application.get("/imports/{job_id}/layout", response_class=HTMLResponse)
    def layout_analysis_status(request: Request, job_id: int):
        """Show progress without ever implicitly starting analysis."""
        try:
            with _connect(database_path) as connection:
                row = connection.execute(
                    """SELECT j.id,j.status AS import_status,j.page_start,j.page_end,
                              s.paper_name,s.original_filename,
                              pr.status AS render_status,
                              pr.total_pages AS render_total_pages,
                              la.status,la.total_pages,la.analyzed_pages,
                              la.detected_questions,la.error_message
                       FROM import_jobs j
                       JOIN source_papers s ON s.id=j.source_paper_id
                       LEFT JOIN import_page_render_runs pr ON pr.import_job_id=j.id
                       LEFT JOIN import_layout_analysis_runs la ON la.import_job_id=j.id
                       WHERE j.id=?""", (job_id,)
                ).fetchone()
        except sqlite3.Error:
            return _error(request, templates, "版面分析状态暂时无法读取", 500)
        if row is None:
            return _error(request, templates, "未找到导入任务", 404)
        if row["import_status"] != "pending" or row["render_status"] != "completed":
            return _error(request, templates, "页面处理完成后才能查看版面分析", 409)
        run = dict(row)
        run["status"] = run["status"] or "pending"
        run["analyzed_pages"] = run["analyzed_pages"] or 0
        if run["total_pages"] is None:
            if run["render_total_pages"] is not None:
                run["total_pages"] = run["render_total_pages"]
            elif run["page_start"] is not None and run["page_end"] is not None:
                run["total_pages"] = run["page_end"] - run["page_start"] + 1
        run["detected_questions"] = run["detected_questions"] or 0
        run["status_name"] = LAYOUT_STATUS_NAMES[run["status"]]
        if run["error_message"] not in SAFE_LAYOUT_ERRORS:
            run["error_message"] = "版面分析失败，请重试" if run["status"] == "failed" else None
        manifest = None
        if run["status"] == "completed":
            try:
                manifest = load_completed_layout(database_path, private_root, job_id)
            except PageLayoutError:
                run["status"] = "failed"
                run["status_name"] = "结果校验失败"
                run["error_message"] = "现有版面分析结果校验失败，请点击重试"
        return templates.TemplateResponse(
            request=request, name="import_layout.html", context={"run": run, "manifest": manifest}
        )

    @application.get("/imports/{job_id}/layout-overlays/{page_number}.png")
    def layout_overlay(job_id: int, page_number: int):
        try:
            content = read_layout_overlay(
                database_path, private_root, job_id, page_number
            )
        except PageLayoutError as error:
            status = 404 if str(error) in {"未找到导入任务", "未找到版面预览"} else 409
            return Response(
                content=("未找到版面预览" if status == 404 else "版面预览校验失败"),
                status_code=status,
                media_type="text/plain; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return Response(
            content=content, media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @application.post("/imports/{job_id}/split")
    async def start_question_split(
        request: Request, job_id: int, background_tasks: BackgroundTasks
    ):
        """Only an explicit, CSRF-protected POST may start Codex."""
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) != {"csrf_token"} or len(list(form.multi_items())) != 1:
            return _error(request, templates, "自动切题请求参数无效", 400)
        try:
            claim = claim_split_job(
                database_path, private_root, job_id,
                runner=application.state.split_runner,
                weekly_checker=application.state.weekly_checker,
            )
        except QuestionSplitError as error:
            message = str(error)
            if message == "未找到导入任务":
                return _error(request, templates, message, 404)
            if message in {SAFE_RENDER_REQUIRED, SAFE_CODEX_MISSING}:
                return _error(request, templates, message, 409)
            if message in {SAFE_WEEKLY_LOW, SAFE_WEEKLY_UNAVAILABLE}:
                return _error(request, templates, message, 409)
            return _error(request, templates, "自动切题任务暂时无法启动", 500)
        if claim is not None:
            background_tasks.add_task(run_claimed_split, claim)
        return RedirectResponse(f"/imports/{job_id}/split", status_code=303)

    @application.get("/imports/{job_id}/split", response_class=HTMLResponse)
    def question_split_status(request: Request, job_id: int):
        """Read-only status and verified crop thumbnails; never starts Codex."""
        try:
            with _connect(database_path) as connection:
                row = connection.execute(
                    """SELECT j.id,j.status AS import_status,s.paper_name,
                              s.original_filename,r.status AS render_status,
                              q.status,q.question_count,q.processed_pages,
                              q.error_message,q.codex_run_id,q.updated_at,
                              q.result_manifest_sha256,q.crop_manifest_sha256,
                              q.crop_generation_id,q.crop_manifest_signature,
                              q.render_manifest_sha256 AS split_render_sha256,
                              q.source_pdf_sha256 AS split_source_sha256,
                              r.manifest_sha256 AS current_render_sha256,
                              r.source_pdf_sha256 AS current_source_sha256
                       FROM import_jobs j JOIN source_papers s ON s.id=j.source_paper_id
                       LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                       LEFT JOIN import_question_split_runs q ON q.import_job_id=j.id
                       WHERE j.id=?""", (job_id,)
                ).fetchone()
        except sqlite3.Error:
            return _error(request, templates, "自动切题状态暂时无法读取", 500)
        if row is None:
            return _error(request, templates, "未找到导入任务", 404)
        if row["import_status"] != "pending" or row["render_status"] != "completed":
            return _error(request, templates, SAFE_RENDER_REQUIRED, 409)
        run = dict(row)
        run["status"] = run["status"] or "pending"
        run["processed_pages"] = run["processed_pages"] or 0
        run["status_name"] = SPLIT_STATUS_NAMES[run["status"]]
        if run["error_message"] not in SAFE_SPLIT_ERRORS:
            run["error_message"] = SAFE_SPLIT_ERROR if run["status"] == "failed" else None
        crops = []
        retained_valid = bool(
            run["question_count"]
            and run["split_render_sha256"] == run["current_render_sha256"]
            and run["split_source_sha256"] == run["current_source_sha256"]
            and completed_split_result_valid(
                private_root, job_id, run["question_count"],
                run["result_manifest_sha256"], run["crop_manifest_sha256"],
                run["crop_generation_id"], run["crop_manifest_signature"],
            )
        )
        if retained_valid:
            crops = _load_question_crops(
                _job_dir(private_root, job_id), job_id,
                range(1, run["question_count"] + 1),
                require_signature=True,
            )
        elif run["status"] == "completed":
            run["status"] = "failed"
            run["status_name"] = "结果校验失败"
            run["error_message"] = SAFE_SPLIT_EXISTING_ERROR
        return templates.TemplateResponse(
            request=request, name="import_split.html",
            context={"run": run, "crops": crops},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/imports/{job_id}/split-images/{question_no}.png")
    def question_split_image(job_id: int, question_no: int):
        try:
            with _connect(database_path) as connection:
                row = connection.execute(
                    """SELECT question_count,crop_manifest_sha256,
                              crop_generation_id,crop_manifest_signature
                       FROM import_question_split_runs WHERE import_job_id=?""", (job_id,)
                ).fetchone()
            if row is None:
                raise ValueError
            content = read_completed_split_image(
                private_root, job_id, row[0], row[1], row[2], row[3], question_no
            )
        except (sqlite3.Error, OSError, ValueError, StopIteration, TypeError,
                QuestionSplitError):
            return Response(
                "未找到已登记的单题图片", status_code=404,
                media_type="text/plain; charset=utf-8",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        return Response(
            content, media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @application.get("/imports/new", response_class=HTMLResponse)
    def new_import(request: Request):
        return templates.TemplateResponse(request=request, name="import_upload.html")

    @application.post("/imports/preview", response_class=HTMLResponse)
    async def preview_import(request: Request):
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        upload = form.get("pdf_file")
        if upload is None or not hasattr(upload, "read"):
            return templates.TemplateResponse(
                request=request,
                name="import_upload.html",
                context={"error": "请选择 PDF 文件"},
                status_code=400,
            )
        try:
            try:
                manifest = await stage_pdf_upload(upload, private_root)
            except UploadConfirmationError as error:
                return templates.TemplateResponse(
                    request=request,
                    name="import_upload.html",
                    context={"error": str(error)},
                    status_code=400,
                )
        finally:
            await upload.close()
        try:
            with _connect(database_path) as connection:
                regions = connection.execute(
                    "SELECT code, name FROM regions WHERE is_active = 1 ORDER BY code"
                ).fetchall()
                exam_types = connection.execute(
                    "SELECT code, name FROM exam_types WHERE is_active = 1 ORDER BY code"
                ).fetchall()
        except sqlite3.Error:
            discard_staged_upload(private_root, manifest.token)
            return _error(request, templates, "基础数据暂时无法读取", 500)
        return templates.TemplateResponse(
            request=request,
            name="import_confirm.html",
            context={
                "manifest": manifest,
                "regions": regions,
                "exam_types": exam_types,
                "paper_name": Path(manifest.original_filename).stem,
                "page_range": f"1-{manifest.page_count}",
            },
        )

    @application.post("/imports/{token}/confirm", response_class=HTMLResponse)
    async def confirm_import(request: Request, token: str):
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        try:
            manifest, stored_path = load_verified_upload(private_root, token)
            metadata = validate_import_metadata(form, manifest.page_count)
            with pending_upload_operation(private_root, token):
                try:
                    manifest, stored_path = load_verified_upload(private_root, token)
                except UploadConfirmationError:
                    if has_intake_receipt(database_path, token):
                        return RedirectResponse("/papers", status_code=303)
                    raise
                intake_pdf(
                    pdf_path=stored_path,
                    region_code=metadata.region_code,
                    exam_year=metadata.exam_year,
                    exam_type_code=metadata.exam_type_code,
                    paper_name=metadata.paper_name,
                    page_range=metadata.page_range,
                    database_path=database_path,
                    private_storage_root=private_root,
                    idempotency_key=token,
                )
                discard_staged_upload(private_root, token)
        except (UploadConfirmationError, PdfIntakeError) as error:
            error_message = str(error)
            if isinstance(error, PdfIntakeError) and error_message.startswith(
                "PDF 导入失败："
            ):
                error_message = "PDF 导入失败，请检查信息后重试"
            try:
                with _connect(database_path) as connection:
                    regions = connection.execute(
                        "SELECT code, name FROM regions WHERE is_active = 1 ORDER BY code"
                    ).fetchall()
                    exam_types = connection.execute(
                        "SELECT code, name FROM exam_types WHERE is_active = 1 ORDER BY code"
                    ).fetchall()
            except sqlite3.Error:
                return _error(request, templates, "基础数据暂时无法读取", 500)
            context = {
                "error": error_message,
                "manifest": locals().get("manifest"),
                "regions": regions,
                "exam_types": exam_types,
                "paper_name": str(form.get("paper_name", "")),
                "exam_year": str(form.get("exam_year", "")),
                "region_code": str(form.get("region_code", "")),
                "exam_type_code": str(form.get("exam_type_code", "")),
                "page_range": str(form.get("page_range", "")),
            }
            if context["manifest"] is None:
                return _error(request, templates, str(error), 400)
            return templates.TemplateResponse(
                request=request,
                name="import_confirm.html",
                context=context,
                status_code=400,
            )
        return RedirectResponse("/papers", status_code=303)

    @application.post("/imports/{token}/cancel", response_class=HTMLResponse)
    async def cancel_import(request: Request, token: str):
        if await require_csrf(request) is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        try:
            with pending_upload_operation(private_root, token):
                load_verified_upload(private_root, token)
                discard_staged_upload(private_root, token)
        except UploadConfirmationError as error:
            return _error(request, templates, str(error), 400)
        return RedirectResponse("/papers", status_code=303)

    def workbench_data(request, job_id, question_no):
        try:
            with _connect(database_path) as connection:
                job = _get_job(connection, job_id)
                if job is None:
                    return None, _error(request, templates, "未找到导入任务", 404)
                candidate_path = _job_dir(private_root, job_id) / "candidate_questions.json"
                candidate = _load_json(candidate_path, "候选题数据")
                questions = candidate.get("questions")
                if not isinstance(questions, list) or not questions:
                    raise ValueError("候选题数据损坏")
                numbers = [str(q.get("source_question_no")) for q in questions]
                if question_no not in numbers:
                    return None, _error(request, templates, "未找到候选题", 404)
                drafts = connection.execute("SELECT * FROM candidate_review_drafts WHERE import_job_id=? ORDER BY id", (job_id,)).fetchall()
                draft_by_no = {row["source_question_no"]: dict(row) for row in drafts}
                digest = _file_sha256(candidate_path)
                for question in questions:
                    number = str(question.get("source_question_no", ""))
                    snapshot = json.dumps(question, ensure_ascii=False, separators=(",", ":"))
                    draft_by_no.setdefault(number, {"source_question_no": number, "source_candidate_sha256": digest,
                        "source_snapshot_json": snapshot, "edited_json": snapshot, "status": "pending",
                        "review_notes": "", "version": 1, "updated_at": None, "reviewed_at": None,
                        "approval_source": None, "approval_evidence_json": None})
                types = [dict(row) for row in connection.execute("SELECT code,name FROM question_types WHERE is_active=1 ORDER BY rowid")]
                points = [dict(row) for row in connection.execute("SELECT code,name FROM knowledge_points WHERE is_active=1 ORDER BY sort_order,id")]
        except (sqlite3.Error, ValueError, OSError, KeyError, TypeError):
            return None, _error(request, templates, "候选审核数据损坏或暂时无法读取", 500)
        job_dir = _job_dir(private_root, job_id)
        crops = {str(x["question_no"]): x for x in _load_question_crops(job_dir, job_id, numbers)}
        figures = [x for x in _load_figure_assets(job_dir) if x["kind"] == "question_figure"]
        audit_by_no = {}
        try:
            _, audit_by_no = _load_valid_audit(job_dir / "ai_audit.json", job_id, questions)
        except (AuditDataError, ValueError):
            pass
        manifest = _load_json(job_dir / "render_manifest.json", "页面清单")
        question = questions[numbers.index(question_no)]
        draft = draft_by_no[question_no]
        edited = json.loads(draft["edited_json"])
        subquestion_display = _group_labeled_subquestions(edited.get("subquestions", []))
        type_names = {item["code"]: item["name"] for item in types}
        point_names = {item["code"]: item["name"] for item in points}
        options = edited.get("options", [])
        image_options = bool(options) and all(
            isinstance(item, dict) and str(item.get("content", "")).strip() == "见原页选项图"
            for item in options
        )
        display = {
            "type_name": type_names.get(edited.get("question_type_code"), edited.get("question_type_code", "")),
            "primary_name": point_names.get(edited.get("primary_knowledge_point_code"), edited.get("primary_knowledge_point_code", "")),
            "related_names": [point_names.get(code, code) for code in edited.get("related_knowledge_point_codes", [])],
            "source_pages": edited.get("source_pages", []),
            "status_name": REVIEW_STATUS_NAMES.get(draft["status"], draft["status"]),
            "approval_source_name": {
                "human": "人工审核", "ai_second_pass": "AI二审",
            }.get(draft.get("approval_source")),
            "image_options": image_options,
        }
        figures_by_no = {
            number: [item for item in figures if item["question_no"] == number]
            for number in numbers
        }
        guidance_by_no = {}
        for number, source_question in zip(numbers, questions):
            question_edited = json.loads(draft_by_no[number]["edited_json"])
            guidance_by_no[number] = _review_guidance(
                source_question, question_edited, crops.get(number), figures_by_no[number],
                audit_by_no.get(number),
            )
        question_figures = figures_by_no[question_no]
        review_guidance = guidance_by_no[question_no]
        active_numbers = [n for n in numbers if draft_by_no[n].get("deleted_at") is None]
        active_drafts = [draft_by_no[n] for n in active_numbers]
        navigation = [{"number": n, "status": draft_by_no[n]["status"], "focus": guidance_by_no[n]["focus"]} for n in active_numbers]
        counts = {key: sum(d["status"] == key for d in active_drafts) for key in REVIEW_ACTION_STATUS.values()}
        progress = {"total": len(active_numbers), "deleted": len(numbers) - len(active_numbers),
                    "approved": counts["approved"], "needs_fix": counts["needs_fix"], "needs_recrop": counts["needs_recrop"],
                    "pending": len(active_numbers)-counts["approved"]-counts["needs_fix"]-counts["needs_recrop"],
                    "human_approved": sum(d["status"] == "approved" and d.get("approval_source") == "human" for d in active_drafts),
                    "ai_approved": sum(d["status"] == "approved" and d.get("approval_source") == "ai_second_pass" for d in active_drafts)}
        page_number = (question.get("source_pages") or [None])[0]
        source_page = next((p for p in manifest.get("pages", []) if p.get("page_number") == page_number), None)
        active_index = active_numbers.index(question_no) if question_no in active_numbers else None
        return {"job": dict(job), "number": question_no, "draft": draft, "edited": edited, "display": display,
                "subquestion_display": subquestion_display,
                "question_types": types, "knowledge_points": points, "navigation": navigation,
                "progress": progress, "crop": crops.get(question_no), "figures": question_figures,
                "review_guidance": review_guidance,
                "source_page": source_page, "focus": guidance_by_no[question_no]["focus"],
                "is_deleted": draft.get("deleted_at") is not None, "reason_names": DELETION_REASONS,
                "previous": active_numbers[active_index-1] if active_index is not None and active_index else None,
                "next": active_numbers[active_index+1] if active_index is not None and active_index+1 < len(active_numbers) else None}, None

    @application.get("/reviews/{job_id}/questions/{question_no}", response_class=HTMLResponse)
    def review_workbench(request: Request, job_id: int, question_no: str, saved: int = 0, quick: str = ""):
        context, error = workbench_data(request, job_id, question_no)
        if error: return error
        if context["is_deleted"]:
            return templates.TemplateResponse(
                request=request, name="deleted_candidate_status.html", context=context,
                status_code=410,
            )
        context["saved"] = saved == 1
        context["quick_feedback"] = {
            "approved_previous": "上一题已审核通过",
            "approved_last": "本题已审核通过，已是最后一题",
            "needs_fix": "已标记为需要修正，可直接在识别结果中修改",
            "needs_recrop": "已标记为需要重切，可继续检查裁图",
        }.get(quick)
        return templates.TemplateResponse(request=request, name="review_workbench.html", context=context)

    @application.get("/reviews/{job_id}/deleted", response_class=HTMLResponse)
    def deleted_candidate_questions(request: Request, job_id: int):
        try:
            with _connect(database_path) as connection:
                job = _get_job(connection, job_id)
                if job is None:
                    return _error(request, templates, "未找到导入任务", 404)
                rows = connection.execute(
                    """SELECT source_question_no,status,version,deleted_at,deletion_reason,deletion_note
                       FROM candidate_review_drafts
                       WHERE import_job_id=? AND deleted_at IS NOT NULL
                       ORDER BY CAST(source_question_no AS INTEGER),id""",
                    (job_id,),
                ).fetchall()
        except sqlite3.Error:
            return _error(request, templates, "已删除候选题暂时无法读取", 500)
        return templates.TemplateResponse(
            request=request, name="deleted_candidates.html",
            context={"job": dict(job), "questions": [dict(row) for row in rows],
                     "reason_names": DELETION_REASONS},
        )

    @application.post("/reviews/{job_id}/questions/{question_no}/delete")
    async def delete_candidate_question(request: Request, job_id: int, question_no: str):
        try:
            if int(request.headers.get("content-length", "0")) > MAX_DELETE_FORM_BYTES:
                return _error(request, templates, "删除表单过大", 413)
        except ValueError:
            return _error(request, templates, "请求长度无效", 400)
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) - CANDIDATE_DELETE_FORM_FIELDS:
            return _error(request, templates, "包含未知字段", 400)
        reason, note = str(form.get("reason", "")), str(form.get("note", ""))
        raw_version = str(form.get("version", ""))
        if reason not in DELETION_REASONS:
            return _error(request, templates, "删除原因无效", 400)
        if form.get("confirmed") != "yes":
            return _error(request, templates, "请先勾选删除确认", 400)
        if len(note) > 500 or not re.fullmatch(r"[1-9][0-9]*", raw_version):
            return _error(request, templates, "删除备注或版本号无效", 400)
        context, error = workbench_data(request, job_id, question_no)
        if error:
            return error
        version = int(raw_version)
        try:
            with _connect(database_path) as connection:
                with connection:
                    candidate_path = _job_dir(private_root, job_id) / "candidate_questions.json"
                    candidate_questions = _load_json(candidate_path, "候选题数据")["questions"]
                    _initialize_candidate_drafts(connection, job_id, candidate_path, candidate_questions)
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    cursor = connection.execute(
                        """UPDATE candidate_review_drafts
                           SET deleted_at=?,deletion_reason=?,deletion_note=?,version=version+1,updated_at=?
                           WHERE import_job_id=? AND source_question_no=? AND version=? AND deleted_at IS NULL""",
                        (now, reason, note or None, now, job_id, question_no, version),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("conflict")
                    active = [row[0] for row in connection.execute(
                        """SELECT source_question_no FROM candidate_review_drafts
                           WHERE import_job_id=? AND deleted_at IS NULL
                           ORDER BY CAST(source_question_no AS INTEGER),id""", (job_id,)
                    )]
        except RuntimeError:
            return _error(request, templates, "该题已被其他操作更新，请刷新后重试", 409)
        except sqlite3.Error:
            return _error(request, templates, "删除失败，草稿未发生变化", 500)
        following = next((number for number in active if int(number) > int(question_no)), None)
        destination_number = following or (active[0] if active else None)
        destination = (
            f"/reviews/{job_id}/questions/{quote(destination_number, safe='')}?deleted=1"
            if destination_number else f"/reviews/{job_id}/deleted?deleted=1"
        )
        return RedirectResponse(destination, status_code=303)

    @application.post("/reviews/{job_id}/questions/{question_no}/restore")
    async def restore_candidate_question(request: Request, job_id: int, question_no: str):
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) - CANDIDATE_RESTORE_FORM_FIELDS:
            return _error(request, templates, "包含未知字段", 400)
        raw_version = str(form.get("version", ""))
        if not re.fullmatch(r"[1-9][0-9]*", raw_version):
            return _error(request, templates, "版本号无效", 400)
        context, error = workbench_data(request, job_id, question_no)
        if error:
            return error
        try:
            with _connect(database_path) as connection:
                with connection:
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    cursor = connection.execute(
                        """UPDATE candidate_review_drafts SET deleted_at=NULL,version=version+1,updated_at=?
                           WHERE import_job_id=? AND source_question_no=? AND version=? AND deleted_at IS NOT NULL""",
                        (now, job_id, question_no, int(raw_version)),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("conflict")
        except RuntimeError:
            return _error(request, templates, "该题已被其他操作更新，请刷新后重试", 409)
        except sqlite3.Error:
            return _error(request, templates, "恢复失败，草稿未发生变化", 500)
        return RedirectResponse(
            f"/reviews/{job_id}/questions/{quote(question_no, safe='')}?restored=1", status_code=303
        )

    @application.post("/reviews/{job_id}/questions/{question_no}")
    async def update_review_workbench(request: Request, job_id: int, question_no: str):
        try:
            if int(request.headers.get("content-length", "0")) > MAX_REVIEW_FORM_BYTES:
                return _review_error(request, templates, "提交表单过大", 413)
        except ValueError:
            return _review_error(request, templates, "请求长度无效", 400)
        form = await require_csrf(request)
        if form is None: return _review_error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) - REVIEW_FORM_FIELDS:
            return _review_error(request, templates, "包含未知字段", 400)
        context, error = workbench_data(request, job_id, question_no)
        if error: return error
        if context["is_deleted"]:
            return _review_error(request, templates, "本题已删除，请先恢复", 410)
        try:
            action = str(form.get("action", "")); version = int(form.get("version", ""))
            if action not in REVIEW_ACTION_STATUS: raise ValueError("无效的审核操作")
            stem = str(form.get("stem_markdown", "")); notes = str(form.get("review_notes", ""))
            if len(stem) > 20000 or len(notes) > 5000: raise ValueError("输入内容过长")
            type_code = str(form.get("question_type_code", "")); primary = str(form.get("primary_knowledge_point_code", ""))
            valid_types = {x["code"] for x in context["question_types"]}; valid_points = {x["code"] for x in context["knowledge_points"]}
            if type_code not in valid_types or primary not in valid_points: raise ValueError("题型或知识点无效")
            current_edited = json.loads(context["draft"]["edited_json"])
            current_options = current_edited.get("options", [])
            current_subs = current_edited.get("subquestions", [])
            if not isinstance(current_options, list) or not isinstance(current_subs, list):
                raise ValueError("候选题结构无效")
            option_fields_present = any(name in form for name in (
                "options_present", "option_source_index", "option_code", "option_content", "option_order"
            ))
            subquestion_fields_present = any(name in form for name in (
                "subquestions_present", "subquestion_source_index", "subquestion_content", "subquestion_order"
            ))
            if "options_present" in form and list(form.getlist("options_present")) != ["1"]:
                raise ValueError("选项结构标记无效")
            if "subquestions_present" in form and list(form.getlist("subquestions_present")) != ["1"]:
                raise ValueError("小问结构标记无效")
            options = (
                _structured_rows(form, "option", current_options, "content")
                if option_fields_present else current_options
            )
            subs = (
                _structured_rows(form, "subquestion", current_subs, "stem_markdown")
                if subquestion_fields_present else current_subs
            )
            if type_code in CHOICE_TYPES and len(options) < 2:
                raise ValueError("单选或多选题至少需要两个选项")
            if type_code == "fill_blank" and option_fields_present:
                submitted_sources = list(form.getlist("option_source_index"))
                expected_sources = {str(index) for index in range(len(current_options))}
                if len(submitted_sources) != len(current_options) or set(submitted_sources) != expected_sources:
                    raise ValueError("填空题不允许新增或删除选项")
            related = list(dict.fromkeys(str(x) for x in form.getlist("related_knowledge_point_codes")))
            if any(x not in valid_points for x in related): raise ValueError("关联知识点无效")
            related = [x for x in related if x != primary]
            edited = current_edited
            edited.update({"stem_markdown": stem, "question_type_code": type_code, "primary_knowledge_point_code": primary,
                           "related_knowledge_point_codes": related, "options": options, "subquestions": subs})
            if action == "approve":
                _validate_review_approval(
                    edited, json.loads(context["draft"]["source_snapshot_json"]),
                    valid_types, valid_points, context["crop"], context["figures"],
                )
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with _connect(database_path) as connection:
                with connection:
                    candidate_path = _job_dir(private_root, job_id) / "candidate_questions.json"
                    candidate_questions = _load_json(candidate_path, "候选题数据")["questions"]
                    _initialize_candidate_drafts(connection, job_id, candidate_path, candidate_questions)
                    approval_source = "human" if action == "approve" else None
                    approval_evidence = json.dumps(
                        {"method": "workbench", "reviewed_at": now}, separators=(",", ":")
                    ) if approval_source else None
                    cursor = connection.execute("""UPDATE candidate_review_drafts SET edited_json=?,status=?,review_notes=?,version=version+1,updated_at=?,reviewed_at=?,approval_source=?,approval_evidence_json=? WHERE import_job_id=? AND source_question_no=? AND version=? AND deleted_at IS NULL""",
                        (json.dumps(edited, ensure_ascii=False, separators=(",", ":")), REVIEW_ACTION_STATUS[action], notes, now, now if action == "approve" else None, approval_source, approval_evidence, job_id, question_no, version))
                    if cursor.rowcount != 1: raise RuntimeError("conflict")
        except RuntimeError:
            return _review_error(request, templates, "该题已被其他操作更新，请刷新后重试", 409)
        except sqlite3.Error:
            return _review_error(request, templates, "保存失败，草稿未发生变化，请稍后重试", 500)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _review_error(request, templates, str(exc) or "提交内容无效", 400)
        return RedirectResponse(f"/reviews/{job_id}/questions/{quote(question_no, safe='')}?saved=1", status_code=303)

    @application.post("/reviews/{job_id}/questions/{question_no}/inline-edit")
    async def inline_edit_review_question(request: Request, job_id: int, question_no: str):
        def inline_error(message, status_code):
            return JSONResponse({"ok": False, "error": message}, status_code=status_code)

        try:
            if int(request.headers.get("content-length", "0")) > MAX_INLINE_EDIT_FORM_BYTES:
                return inline_error("提交表单过大", 413)
        except ValueError:
            return inline_error("请求长度无效", 400)
        form = await require_csrf(request)
        if form is None:
            return inline_error("CSRF 校验失败", 403)
        if set(form.keys()) - INLINE_EDIT_FORM_FIELDS:
            return inline_error("包含未知字段", 400)
        if any(len(form.getlist(name)) != 1 for name in ("csrf_token", "version", "field", "value")):
            return inline_error("字段缺失或重复", 400)

        context, error = workbench_data(request, job_id, question_no)
        if error:
            return inline_error("未找到导入任务或候选题" if error.status_code == 404 else "候选审核数据损坏或暂时无法读取", error.status_code)
        if context["is_deleted"]:
            return inline_error("本题已删除，请先恢复", 410)
        try:
            raw_version = str(form.get("version", ""))
            if not re.fullmatch(r"[1-9][0-9]*", raw_version):
                raise ValueError("版本号无效")
            version = int(raw_version)
            field = str(form.get("field", ""))
            if field not in INLINE_EDIT_FIELDS:
                raise ValueError("不支持的原位编辑字段")
            collection_name, item_key, max_length = INLINE_EDIT_FIELDS[field]
            value = str(form.get("value", ""))
            if len(value) > max_length:
                raise ValueError("输入内容过长")

            index = None
            index_values = form.getlist("index")
            if collection_name is None:
                if index_values:
                    raise ValueError("该字段不允许提交索引")
            else:
                if len(index_values) != 1 or not re.fullmatch(r"0|[1-9][0-9]*", str(index_values[0])):
                    raise ValueError("项目索引无效")
                index = int(index_values[0])

            candidate_path = _job_dir(private_root, job_id) / "candidate_questions.json"
            candidate_questions = _load_json(candidate_path, "候选题数据")["questions"]
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            was_approved = False
            with _connect(database_path) as connection:
                with connection:
                    _initialize_candidate_drafts(connection, job_id, candidate_path, candidate_questions)
                    row = connection.execute(
                        "SELECT edited_json,status FROM candidate_review_drafts "
                        "WHERE import_job_id=? AND source_question_no=?",
                        (job_id, question_no),
                    ).fetchone()
                    if row is None:
                        raise ValueError("未找到候选题")
                    edited = json.loads(row["edited_json"])
                    if not isinstance(edited, dict):
                        raise ValueError("候选题结构无效")
                    if collection_name is None:
                        edited[field] = value
                    else:
                        items = edited.get(collection_name)
                        if not isinstance(items, list) or index >= len(items):
                            raise ValueError("项目索引越界")
                        item = items[index]
                        if not isinstance(item, dict):
                            raise ValueError("候选题结构无效")
                        item[item_key] = value
                    was_approved = row["status"] == "approved"
                    cursor = connection.execute(
                        """UPDATE candidate_review_drafts
                           SET edited_json=?,status='draft',version=version+1,updated_at=?,reviewed_at=NULL,
                               approval_source=NULL,approval_evidence_json=NULL
                           WHERE import_job_id=? AND source_question_no=? AND version=? AND deleted_at IS NULL""",
                        (json.dumps(edited, ensure_ascii=False, separators=(",", ":")), now,
                         job_id, question_no, version),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("conflict")
        except RuntimeError:
            return inline_error("内容已被其他操作更新，请刷新", 409)
        except sqlite3.Error:
            return inline_error("保存失败，草稿未发生变化，请重试", 500)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, OSError) as exc:
            return inline_error(str(exc) or "原位编辑内容无效", 400)

        message = "内容已修改，需要重新审核" if was_approved else "保存成功"
        return JSONResponse({
            "ok": True, "field": field, "index": index, "value": value,
            "version": version + 1, "status": "draft",
            "status_name": REVIEW_STATUS_NAMES["draft"], "message": message,
        })

    @application.post("/reviews/{job_id}/questions/{question_no}/quick-status")
    async def update_quick_review_status(request: Request, job_id: int, question_no: str):
        try:
            if int(request.headers.get("content-length", "0")) > MAX_REVIEW_FORM_BYTES:
                return _review_error(request, templates, "提交表单过大", 413)
        except ValueError:
            return _review_error(request, templates, "请求长度无效", 400)
        form = await require_csrf(request)
        if form is None:
            return _review_error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) - QUICK_REVIEW_FORM_FIELDS:
            return _review_error(request, templates, "包含未知字段", 400)
        context, error = workbench_data(request, job_id, question_no)
        if error:
            return error
        if context["is_deleted"]:
            return _review_error(request, templates, "本题已删除，请先恢复", 410)
        try:
            action = str(form.get("action", ""))
            if action not in QUICK_REVIEW_ACTION_STATUS:
                raise ValueError("无效的快速审核操作")
            version = int(form.get("version", ""))
            if version < 1:
                raise ValueError("版本号无效")
            if action == "approve":
                _validate_review_approval(
                    context["edited"], json.loads(context["draft"]["source_snapshot_json"]),
                    {item["code"] for item in context["question_types"]},
                    {item["code"] for item in context["knowledge_points"]},
                    context["crop"], context["figures"],
                )
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with _connect(database_path) as connection:
                with connection:
                    candidate_path = _job_dir(private_root, job_id) / "candidate_questions.json"
                    candidate_questions = _load_json(candidate_path, "候选题数据")["questions"]
                    _initialize_candidate_drafts(connection, job_id, candidate_path, candidate_questions)
                    approval_source = "human" if action == "approve" else None
                    approval_evidence = json.dumps(
                        {"method": "workbench_quick", "reviewed_at": now}, separators=(",", ":")
                    ) if approval_source else None
                    cursor = connection.execute(
                        """UPDATE candidate_review_drafts
                           SET status=?,version=version+1,reviewed_at=?,approval_source=?,
                               approval_evidence_json=?,updated_at=?
                           WHERE import_job_id=? AND source_question_no=? AND version=? AND deleted_at IS NULL""",
                        (QUICK_REVIEW_ACTION_STATUS[action], now if action == "approve" else None,
                         approval_source, approval_evidence, now, job_id, question_no, version),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("conflict")
        except RuntimeError:
            return _review_error(request, templates, "该题已被其他操作更新，请刷新后重试", 409)
        except sqlite3.Error:
            return _review_error(request, templates, "快速审核失败，草稿未发生变化，请稍后重试", 500)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return _review_error(request, templates, str(exc) or "快速审核内容无效", 400)

        if action == "approve" and context["next"]:
            destination = f"/reviews/{job_id}/questions/{quote(context['next'], safe='')}?quick=approved_previous"
        elif action == "approve":
            destination = f"/reviews/{job_id}/questions/{quote(question_no, safe='')}?quick=approved_last"
        else:
            destination = f"/reviews/{job_id}/questions/{quote(question_no, safe='')}?quick={action}"
        return RedirectResponse(destination, status_code=303)

    @application.get("/questions", response_class=HTMLResponse)
    def questions(request: Request, knowledge: str | None = None,
                  question_type: str | None = None, source: str | None = None,
                  has_figure: str | None = None):
        try:
            with _connect(database_path) as connection:
                if knowledge is not None and connection.execute(
                    "SELECT 1 FROM knowledge_points WHERE code=? AND is_active=1", (knowledge,)
                ).fetchone() is None:
                    return _error(request, templates, "无效的知识点筛选条件", 400)
                if question_type is not None and connection.execute(
                    "SELECT 1 FROM question_types WHERE code=? AND is_active=1", (question_type,)
                ).fetchone() is None:
                    return _error(request, templates, "无效的题型筛选条件", 400)
                if has_figure not in (None, "true", "false"):
                    return _error(request, templates, "无效的图片筛选条件", 400)
                if source is not None and (len(source) > 100 or any(ord(char) < 32 for char in source)):
                    return _error(request, templates, "无效的来源筛选条件", 400)
                clauses, params = ["q.deleted_at IS NULL"], []
                if knowledge:
                    clauses.append("(kp.code=? OR EXISTS (SELECT 1 FROM question_related_knowledge_points qr JOIN knowledge_points kr ON kr.id=qr.knowledge_point_id WHERE qr.question_id=q.id AND kr.code=?))")
                    params.extend((knowledge, knowledge))
                if question_type:
                    clauses.append("q.question_type_code=?"); params.append(question_type)
                if source is not None:
                    clauses.append("s.paper_name LIKE ? ESCAPE '\\'")
                    escaped = source.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    params.append(f"%{escaped}%")
                if has_figure:
                    predicate = "q.figure_review_status='passed'"
                    clauses.append(predicate if has_figure == "true" else f"NOT {predicate}")
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = connection.execute(
                    """SELECT q.id,q.question_code,q.stem_markdown,q.source_question_no,q.answer_status,q.question_type_code,q.figure_review_status,
                              qt.name AS question_type_name,kp.name AS primary_knowledge_name,kp.code AS primary_knowledge_code,
                              s.paper_name,s.exam_year,
                              (q.figure_review_status='passed') AS has_figure,
                              EXISTS(SELECT 1 FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE bi.question_id=q.id AND b.basket_key='default') AS in_basket
                       FROM questions q JOIN question_types qt ON qt.code=q.question_type_code
                       JOIN knowledge_points kp ON kp.id=q.primary_knowledge_point_id
                       JOIN question_sources qs ON qs.question_id=q.id JOIN source_papers s ON s.id=qs.source_paper_id""" + where +
                    " ORDER BY s.exam_year DESC, qs.import_job_id DESC, CAST(qs.source_question_no AS INTEGER)", params
                ).fetchall()
                questions = [dict(row) for row in rows]
                options_by_question = {question["id"]: [] for question in questions}
                subquestions_by_question = {question["id"]: [] for question in questions}
                assets_by_question = {question["id"]: [] for question in questions}
                if options_by_question:
                    placeholders = ",".join("?" for _ in options_by_question)
                    option_rows = connection.execute(
                        f"""SELECT question_id,option_code,content_markdown
                            FROM question_options WHERE question_id IN ({placeholders})
                            ORDER BY question_id,display_order""",
                        tuple(options_by_question),
                    ).fetchall()
                    for option in option_rows:
                        options_by_question[option["question_id"]].append(dict(option))
                    subquestion_rows = connection.execute(
                        f"""SELECT question_id,display_order,stem_markdown
                            FROM subquestions WHERE question_id IN ({placeholders})
                            ORDER BY question_id,display_order""",
                        tuple(subquestions_by_question),
                    ).fetchall()
                    for subquestion in subquestion_rows:
                        subquestions_by_question[subquestion["question_id"]].append(dict(subquestion))
                    asset_rows = connection.execute(
                        f"""SELECT * FROM question_assets
                            WHERE question_id IN ({placeholders})
                            ORDER BY question_id,display_order,id""",
                        tuple(assets_by_question),
                    ).fetchall()
                    for asset in asset_rows:
                        assets_by_question[asset["question_id"]].append(dict(asset))
                for question in questions:
                    options = options_by_question[question["id"]]
                    content = _required_question_content(
                        question, options, assets_by_question[question["id"]]
                    )
                    question.update(content)
                    question["options"] = content["display_options"]
                    question["subquestion_display"] = _group_labeled_subquestions(
                        subquestions_by_question[question["id"]]
                    )
                    verified_assets = []
                    if content["display_assets"]:
                        try:
                            for index, asset in enumerate(content["display_assets"], 1):
                                _verified_asset_path(private_root, asset)
                                asset["url"] = (
                                    f"/question-assets/{quote(question['question_code'], safe='')}/"
                                    f"{quote(asset['relative_path'], safe='/')}"
                                )
                                asset["alt"] = (
                                    f"第 {question['source_question_no']} 题图像选项完整题图"
                                    if content["image_options"] else
                                    f"第 {question['source_question_no']} 题必要配图 {index}"
                                )
                                verified_assets.append(asset)
                        except (ValueError, OSError, UnidentifiedImageError):
                            verified_assets = []
                    question["display_assets"] = verified_assets
                    question["required_image_unavailable"] = (
                        content["has_required_image"] and not verified_assets
                    )
                filters = {
                    "knowledge": connection.execute(
                        """SELECT kp.code,kp.name FROM knowledge_points kp
                           WHERE kp.is_active=1 AND (
                               EXISTS (SELECT 1 FROM questions q WHERE q.primary_knowledge_point_id=kp.id AND q.deleted_at IS NULL)
                               OR EXISTS (SELECT 1 FROM question_related_knowledge_points qr
                                          JOIN questions q ON q.id=qr.question_id
                                          WHERE qr.knowledge_point_id=kp.id AND q.deleted_at IS NULL)
                           ) ORDER BY kp.code"""
                    ).fetchall(),
                    "types": connection.execute("SELECT code,name FROM question_types WHERE is_active=1 ORDER BY rowid").fetchall(),
                }
        except sqlite3.Error:
            return _error(request, templates, "题库数据暂时无法读取", 500)
        return templates.TemplateResponse(request=request, name="questions.html", context={
            "questions": questions, "filters": filters,
            "active": {"knowledge": knowledge, "question_type": question_type, "source": source or "", "has_figure": has_figure or ""},
        })

    @application.get("/questions/deleted", response_class=HTMLResponse)
    def deleted_questions(request: Request, already_active: int = 0):
        try:
            with _connect(database_path) as connection:
                rows = connection.execute(
                    """SELECT q.question_code,q.source_question_no,q.deleted_at,
                              q.deletion_reason,q.deletion_note,s.paper_name
                       FROM questions q
                       LEFT JOIN question_sources qs ON qs.question_id=q.id
                       LEFT JOIN source_papers s ON s.id=qs.source_paper_id
                       WHERE q.deleted_at IS NOT NULL
                       ORDER BY q.deleted_at DESC,q.id DESC"""
                ).fetchall()
        except sqlite3.Error:
            return _error(request, templates, "已删除题目暂时无法读取", 500)
        return templates.TemplateResponse(
            request=request, name="deleted_questions.html",
            context={"questions": [dict(row) for row in rows],
                     "reason_names": DELETION_REASONS,
                     "already_active": already_active == 1},
        )

    @application.post("/questions/{question_code}/delete")
    async def delete_question(request: Request, question_code: str):
        try:
            if int(request.headers.get("content-length", "0")) > MAX_DELETE_FORM_BYTES:
                return _error(request, templates, "删除表单过大", 413)
        except ValueError:
            return _error(request, templates, "请求长度无效", 400)
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) - DELETE_FORM_FIELDS:
            return _error(request, templates, "包含未知字段", 400)
        reason = str(form.get("reason", ""))
        note = str(form.get("note", ""))
        if reason not in DELETION_REASONS:
            return _error(request, templates, "删除原因无效", 400)
        if form.get("confirmed") != "yes":
            return _error(request, templates, "请先勾选删除确认", 400)
        if len(note) > 500:
            return _error(request, templates, "删除备注不能超过500字", 400)
        try:
            with _connect(database_path) as connection:
                with connection:
                    row = connection.execute(
                        "SELECT id,deleted_at FROM questions WHERE question_code=?",
                        (question_code,),
                    ).fetchone()
                    if row is None:
                        return _error(request, templates, "未找到正式题目", 404)
                    if row["deleted_at"] is not None:
                        return RedirectResponse("/questions/deleted?already_deleted=1", status_code=303)
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    connection.execute(
                        """UPDATE questions SET deleted_at=?,deletion_reason=?,deletion_note=?,updated_at=?
                           WHERE id=? AND deleted_at IS NULL""",
                        (now, reason, note or None, now, row["id"]),
                    )
                    basket_ids = [item[0] for item in connection.execute(
                        "SELECT DISTINCT basket_id FROM basket_items WHERE question_id=?", (row["id"],)
                    )]
                    connection.execute("DELETE FROM basket_items WHERE question_id=?", (row["id"],))
                    for basket_id in basket_ids:
                        basket_store._compact(connection, basket_id)
        except sqlite3.Error:
            return _error(request, templates, "删除失败，题目未发生变化", 500)
        destination = "/basket" if form.get("next") == "/basket" else "/questions"
        return RedirectResponse(destination + "?deleted=1", status_code=303)

    @application.post("/questions/{question_code}/restore")
    async def restore_question(request: Request, question_code: str):
        form = await require_csrf(request)
        if form is None:
            return _error(request, templates, "CSRF 校验失败", 403)
        if set(form.keys()) - RESTORE_FORM_FIELDS:
            return _error(request, templates, "包含未知字段", 400)
        try:
            with _connect(database_path) as connection:
                with connection:
                    row = connection.execute(
                        "SELECT id,deleted_at FROM questions WHERE question_code=?", (question_code,)
                    ).fetchone()
                    if row is None:
                        return _error(request, templates, "未找到正式题目", 404)
                    if row["deleted_at"] is None:
                        return RedirectResponse("/questions/deleted?already_active=1", status_code=303)
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    connection.execute(
                        "UPDATE questions SET deleted_at=NULL,updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
        except sqlite3.Error:
            return _error(request, templates, "恢复失败，题目未发生变化", 500)
        return RedirectResponse("/questions?restored=1", status_code=303)

    @application.get("/questions/{question_code}", response_class=HTMLResponse)
    def question_detail(request: Request, question_code: str):
        try:
            with _connect(database_path) as connection:
                row = connection.execute(
                    """SELECT q.*,qt.name AS question_type_name,kp.name AS primary_knowledge_name,kp.code AS primary_knowledge_code,
                              s.paper_name AS source_paper_name,s.exam_year,qs.import_job_id,qs.source_pages_json
                       FROM questions q JOIN question_types qt ON qt.code=q.question_type_code
                       JOIN knowledge_points kp ON kp.id=q.primary_knowledge_point_id
                       JOIN question_sources qs ON qs.question_id=q.id JOIN source_papers s ON s.id=qs.source_paper_id
                       WHERE q.question_code=?""", (question_code,)
                ).fetchone()
                if row is None: return _error(request, templates, "未找到正式题目", 404)
                if row["deleted_at"] is not None:
                    return _error(request, templates, "题目已删除，可前往恢复", 410)
                options = connection.execute("SELECT * FROM question_options WHERE question_id=? ORDER BY display_order", (row["id"],)).fetchall()
                subs = connection.execute("SELECT * FROM subquestions WHERE question_id=? ORDER BY display_order", (row["id"],)).fetchall()
                related = connection.execute("""SELECT k.code,k.name FROM question_related_knowledge_points r JOIN knowledge_points k ON k.id=r.knowledge_point_id WHERE r.question_id=? ORDER BY k.sort_order,k.id""", (row["id"],)).fetchall()
                assets = connection.execute("SELECT * FROM question_assets WHERE question_id=? ORDER BY asset_kind,display_order", (row["id"],)).fetchall()
                review = connection.execute("SELECT * FROM question_reviews WHERE question_id=? AND review_item='usability' ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
                in_basket = connection.execute("SELECT 1 FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.basket_key='default' AND bi.question_id=?", (row["id"],)).fetchone() is not None
        except sqlite3.Error:
            return _error(request, templates, "题目数据暂时无法读取", 500)
        return templates.TemplateResponse(request=request, name="question_detail.html", context={
            "question": dict(row), "options": options, "subquestions": subs,
            "subquestion_display": _group_labeled_subquestions(subs),
            "related": related, "assets": assets, "review": review,
            "in_basket": in_basket,
        })

    @application.get("/basket", response_class=HTMLResponse)
    def basket(request: Request):
        with _connect(database_path) as connection:
            questions = _basket_questions(connection)
        return templates.TemplateResponse(request=request, name="basket.html", context={"questions": questions})

    async def basket_action(request, question_code, action, in_basket, default_next="/questions"):
        form = await require_csrf(request)
        if form is None:
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": "CSRF 校验失败"}, status_code=403)
            return _error(request, templates, "CSRF 校验失败", 403)
        with _connect(database_path) as connection:
            question = connection.execute(
                "SELECT deleted_at FROM questions WHERE question_code=?", (question_code,)
            ).fetchone()
            if question is None:
                if _wants_json(request):
                    return JSONResponse({"ok": False, "error": "未找到正式题目"}, status_code=404)
                return _error(request, templates, "未找到正式题目", 404)
            if question["deleted_at"] is not None:
                if _wants_json(request):
                    return JSONResponse({"ok": False, "error": "题目已删除，请先恢复"}, status_code=410)
                return _error(request, templates, "题目已删除，请先恢复", 410)
            action(connection, question_code)
            count = _basket_count(connection)
        if _wants_json(request):
            return JSONResponse({
                "ok": True, "in_basket": in_basket, "basket_count": count,
                "label": "移出选题篮" if in_basket else "加入选题篮",
            })
        return RedirectResponse(_safe_basket_next(form.get("next"), question_code, default_next), status_code=303)

    @application.post("/basket/add/{question_code}")
    async def basket_add(request: Request, question_code: str):
        return await basket_action(request, question_code, basket_store.add, True)

    @application.post("/basket/remove/{question_code}")
    async def basket_remove(request: Request, question_code: str):
        return await basket_action(request, question_code, basket_store.remove, False)

    @application.post("/basket/move-up/{question_code}")
    async def basket_up(request: Request, question_code: str):
        return await basket_action(request, question_code, lambda con, code: basket_store.move(con, code, "up"), True, "/basket")

    @application.post("/basket/move-down/{question_code}")
    async def basket_down(request: Request, question_code: str):
        return await basket_action(request, question_code, lambda con, code: basket_store.move(con, code, "down"), True, "/basket")

    @application.post("/basket/clear")
    async def basket_clear(request: Request):
        if await require_csrf(request) is None: return _error(request, templates, "CSRF 校验失败", 403)
        with _connect(database_path) as connection: basket_store.clear(connection)
        return RedirectResponse("/basket", status_code=303)

    @application.post("/basket/preview")
    async def basket_preview(request: Request):
        form = await require_csrf(request)
        if form is None:
            return JSONResponse({"ok": False, "error": "CSRF 校验失败"}, status_code=403)
        try:
            options = _parse_export_options(form)
        except ValueError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
        with _connect(database_path) as connection:
            questions = _basket_questions(connection)
        if not questions:
            return JSONResponse({"ok": False, "error": "空选题篮不能预览"}, status_code=400)
        content = _exercise_questions(questions, options)
        html = templates.env.get_template("basket_preview.html").render(
            questions=content, options=options, csrf_token=request.state.csrf_token
        )
        return JSONResponse({"ok": True, "html": html, "question_count": len(content)})

    @application.post("/basket/export")
    async def basket_export(request: Request):
        form = await require_csrf(request)
        if form is None: return _error(request, templates, "CSRF 校验失败", 403)
        try:
            options = _parse_export_options(form)
        except ValueError as error:
            return _error(request, templates, str(error), 400)
        with _connect(database_path) as connection:
            questions = _basket_questions(connection)
            if not questions: return _error(request, templates, "空选题篮不能导出", 400)
            basket_id = connection.execute("SELECT id FROM baskets WHERE basket_key='default'").fetchone()[0]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        final_dir = private_root / "exports" / stamp
        staging = Path(tempfile.mkdtemp(prefix="basket-export-", dir=private_root))
        try:
            target = _export_markdown(private_root, questions, options, staging)
            digest = _file_sha256(target)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_dir)
            relative = final_dir.relative_to(private_root).joinpath("练习.md").as_posix()
            with _connect(database_path) as connection:
                with connection:
                    export_id = connection.execute("INSERT INTO basket_exports(basket_id,question_count,options_json,output_path,sha256) VALUES(?,?,?,?,?)", (basket_id,len(questions),json.dumps(options,ensure_ascii=False,sort_keys=True),relative,digest)).lastrowid
        except (OSError, ValueError, sqlite3.Error, UnidentifiedImageError):
            shutil.rmtree(staging, ignore_errors=True); shutil.rmtree(final_dir, ignore_errors=True)
            return _error(request, templates, "导出失败，未留下半成品", 500)
        return RedirectResponse(f"/basket/exports/{export_id}", status_code=303)

    @application.get("/basket/exports/{export_id}", response_class=HTMLResponse)
    def export_success(request: Request, export_id: int):
        with _connect(database_path) as connection:
            row = connection.execute("SELECT * FROM basket_exports WHERE id=?", (export_id,)).fetchone()
        if row is None: return _error(request, templates, "未找到导出记录", 404)
        return templates.TemplateResponse(request=request, name="export_success.html", context={"export": dict(row), "absolute_path": str(private_root / row["output_path"])})

    @application.get("/basket/exports/{export_id}/download")
    def export_download(request: Request, export_id: int):
        with _connect(database_path) as connection:
            row = connection.execute("SELECT output_path,sha256 FROM basket_exports WHERE id=?", (export_id,)).fetchone()
        if row is None: return _error(request, templates, "未找到导出记录", 404)
        rel = PurePosixPath(row["output_path"]); target = (private_root / rel).resolve()
        exports_root = (private_root / "exports").resolve()
        if (rel.is_absolute() or ".." in rel.parts or "\\" in row["output_path"] or rel.name != "练习.md"
                or not target.is_relative_to(exports_root) or not target.is_file() or _file_sha256(target) != row["sha256"]):
            return _error(request, templates, "导出文件验证失败", 404)
        return FileResponse(target, media_type="text/markdown; charset=utf-8", filename="练习.md")

    @application.get("/question-assets/{question_code}/{relative_path:path}")
    def question_asset(request: Request, question_code: str, relative_path: str):
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts or "\\" in relative_path or path.suffix.lower() != ".png":
            return _error(request, templates, "不允许访问该文件", 403)
        try:
            with _connect(database_path) as connection:
                asset = connection.execute(
                    """SELECT a.*,q.question_code FROM question_assets a JOIN questions q ON q.id=a.question_id
                       WHERE q.question_code=? AND q.deleted_at IS NULL AND a.relative_path=?""", (question_code, path.as_posix())
                ).fetchone()
                if asset is None: return _error(request, templates, "不允许访问该文件", 403)
            target = _verified_asset_path(private_root, asset)
        except (sqlite3.Error, ValueError, OSError, UnidentifiedImageError):
            return _error(request, templates, "图片暂时无法读取", 404)
        return FileResponse(target, media_type="image/png")

    @application.get("/review/{job_id}", response_class=HTMLResponse)
    def review(request: Request, job_id: int, status: str | None = None):
        if status is not None and status not in FILTER_STATUSES:
            return _error(request, templates, "无效的AI审核筛选条件", 400)
        try:
            with _connect(database_path) as connection:
                job = _get_job(connection, job_id)
                if job is None:
                    return _error(request, templates, "未找到导入任务", 404)
                knowledge = {
                    row["code"]: row["name"]
                    for row in connection.execute("SELECT code, name FROM knowledge_points")
                }
                draft_rows = connection.execute(
                    "SELECT source_question_no,version,deleted_at FROM candidate_review_drafts WHERE import_job_id=?",
                    (job_id,),
                ).fetchall()
                draft_by_no = {str(row["source_question_no"]): dict(row) for row in draft_rows}
        except sqlite3.Error:
            return _error(request, templates, "题库数据库暂时无法读取", 500)

        job_dir = _job_dir(private_root, job_id)
        try:
            candidate = _load_json(job_dir / "candidate_questions.json", "候选题数据")
            manifest = _load_json(job_dir / "render_manifest.json", "页面清单")
            questions = candidate["questions"]
            pages = manifest["pages"]
            if not isinstance(questions, list) or not isinstance(pages, list):
                raise ValueError("审核数据损坏")
        except (ValueError, KeyError, TypeError):
            return _error(request, templates, "候选题数据损坏或缺失，请重新生成后再查看", 500)

        for question in questions:
            number = str(question.get("source_question_no", ""))
            draft = draft_by_no.get(number)
            question["review_version"] = draft["version"] if draft else 1
            question["is_deleted"] = bool(draft and draft.get("deleted_at"))
            primary_code = question.get("primary_knowledge_point_code", "")
            question["primary_knowledge_name"] = knowledge.get(primary_code, primary_code)
            question["related_knowledge_names"] = [
                knowledge.get(code, code)
                for code in question.get("related_knowledge_point_codes", [])
            ]
            question["figure_assets"] = []
            question["question_crop"] = None
            question["subquestion_display"] = _group_labeled_subquestions(
                question.get("subquestions", [])
            )
        figure_assets = _load_figure_assets(job_dir)
        questions_by_no = {str(question.get("source_question_no")): question for question in questions}
        for asset in figure_assets:
            if asset["question_no"] in questions_by_no:
                questions_by_no[asset["question_no"]]["figure_assets"].append(asset)
        question_crops = _load_question_crops(job_dir, job_id, questions_by_no.keys())
        for crop in question_crops:
            questions_by_no[str(crop["question_no"])]["question_crop"] = crop
        audit = None
        audit_by_no = {}
        audit_error = False
        audit_path = job_dir / "ai_audit.json"
        if audit_path.is_file():
            try:
                audit, audit_by_no = _load_valid_audit(audit_path, job_id, questions)
            except AuditDataError:
                audit_error = True

        active_status = "all"
        active_questions = [question for question in questions if not question["is_deleted"]]
        displayed_questions = active_questions
        sample_questions = []
        if audit is not None:
            active_status = status or "human_required"
            for question in questions:
                question["ai_audit"] = audit_by_no[question["source_question_no"]]
            if active_status != "all":
                displayed_questions = [
                    question for question in active_questions
                    if question["ai_audit"]["audit_status"] == active_status
                ]
            recommendation = audit.get("random_sample_recommendation")
            if recommendation:
                for number in recommendation["question_nos"]:
                    number = str(number)
                    sample_questions.append({
                        "number": number,
                        "filter": (
                            "auto_pass"
                            if audit_by_no[number]["audit_status"] == "auto_pass"
                            else "all"
                        ),
                    })
        medium = sum(q.get("confidence") == "medium" for q in active_questions)
        high = sum(q.get("confidence") == "high" for q in active_questions)
        figures = sum(bool(q.get("figure_required")) for q in active_questions)
        focus = sum(bool(q.get("review_notes")) or q.get("confidence") != "high" for q in active_questions)
        summary = {"total": len(active_questions), "deleted": len(questions) - len(active_questions), "high": high, "medium": medium, "figures": figures, "focus": focus}
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context={
                "job": dict(job), "candidate": candidate,
                "questions": displayed_questions, "all_questions": questions,
                "pages": pages, "summary": summary, "audit": audit,
                "audit_error": audit_error, "active_status": active_status,
                "sample_questions": sample_questions,
            },
        )

    @application.get("/private-pages/{job_id}/{relative_path:path}")
    def private_page(request: Request, job_id: int, relative_path: str):
        path = PurePosixPath(relative_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in relative_path
            or path.suffix.lower() != ".png"
        ):
            return _error(request, templates, "不允许访问该文件", 403)
        try:
            with _connect(database_path) as connection:
                if _get_job(connection, job_id) is None:
                    return _error(request, templates, "未找到导入任务", 404)
            job_dir = _job_dir(private_root, job_id)
            manifest = _load_json(job_dir / "render_manifest.json", "页面清单")
            allowed = {
                item.get("relative_path") for item in manifest.get("pages", [])
                if isinstance(item, dict)
            }
            allowed.update(item["output_relative_path"] for item in _load_figure_assets(job_dir))
            allowed.update(item["output_relative_path"] for item in _load_question_crops(job_dir, job_id))
        except (sqlite3.Error, ValueError, AttributeError):
            return _error(request, templates, "页面图片暂时无法读取", 404)
        normalized = path.as_posix()
        if normalized not in allowed:
            return _error(request, templates, "不允许访问该文件", 403)
        target = (job_dir / normalized).resolve()
        if not target.is_relative_to(job_dir.resolve()) or not target.is_file():
            return _error(request, templates, "页面图片不存在", 404)
        return FileResponse(target, media_type="image/png")

    return application


app = create_app(_initialize_schema=False)
