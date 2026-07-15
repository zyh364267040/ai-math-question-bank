"""Conservative, user-started layout and question-boundary candidate analysis."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pymupdf
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from src.processing.pdf_page_renderer import (
    MAX_PAGE_PIXELS,
    MAX_SOURCE_BYTES,
    _open_child_directory,
    _open_or_create_directory,
    _open_safe_directory,
    _read_regular_at,
    _remove_tree_at,
    _write_new_regular_at,
)


MAX_ANALYSIS_PAGES = 200
MAX_DOWNSCALED_PIXELS_PER_PAGE = 2_000_000
MAX_TOTAL_ANALYSIS_PIXELS = 200_000_000
MAX_COLUMNS = 3
MAX_QUESTIONS = 200
MAX_LAYOUT_OUTPUT_BYTES = 1024 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MANIFEST_VERSION = 1
ALGORITHM_VERSION = "projection-text-v1"

SAFE_INPUT_ERROR = "页面分析输入校验失败，请重新处理页面后重试"
SAFE_LIMIT_ERROR = "页面数量或分析结果超过安全处理限制"
SAFE_ANALYSIS_ERROR = "版面分析失败，请重试"
SAFE_EXISTING_ERROR = "现有版面分析结果校验失败，请点击重试"
NO_TEXT_WARNING = "无可用文本层，未生成题号边界候选，请人工复核"
NO_ANCHOR_WARNING = "未发现可靠主题号，未生成题号边界候选，请人工复核"


class PageLayoutError(ValueError):
    """A layout failure containing only a fixed user-safe summary."""


@dataclass
class LayoutClaim:
    database_path: Path
    private_root: Path
    job_id: int
    lock_stream: object
    global_lock_stream: object

    def close(self):
        streams = (self.lock_stream, self.global_lock_stream)
        self.lock_stream = self.global_lock_stream = None
        for stream in streams:
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


def _prepare_locks(private_root: Path, job_id: int):
    root_fd = processing_fd = locks_fd = None
    streams = []
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_or_create_directory(root_fd, "processing")
        locks_fd = _open_or_create_directory(processing_fd, ".layout_locks")
        for name in ("global.lock", f"import_job_{job_id}.lock"):
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, 0o600, dir_fd=locks_fd)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                os.close(descriptor)
                raise OSError("unsafe lock")
            streams.append(os.fdopen(descriptor, "a+b"))
        return tuple(streams)
    except Exception:
        for stream in streams:
            stream.close()
        raise
    finally:
        _close_descriptors(locks_fd, processing_fd, root_fd)


def _eligible_job(database_path: Path, job_id: int):
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                """SELECT j.status,r.status FROM import_jobs j
                   LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                   WHERE j.id=?""",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as error:
        raise PageLayoutError("版面分析任务暂时无法启动") from error
    if row is None:
        raise PageLayoutError("未找到导入任务")
    if row != ("pending", "completed"):
        raise PageLayoutError("页面处理完成后才能开始版面分析")


def claim_layout_job(database_path, private_root, job_id: int) -> LayoutClaim | None:
    """Claim one explicit analysis start; a busy global analyzer returns ``None``."""
    if type(job_id) is not int or job_id <= 0:
        raise PageLayoutError("版面分析参数无效")
    database_path, private_root = Path(database_path), Path(private_root)
    _eligible_job(database_path, job_id)
    global_stream = job_stream = None
    try:
        global_stream, job_stream = _prepare_locks(private_root, job_id)
        try:
            fcntl.flock(global_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(job_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            for stream in (job_stream, global_stream):
                if stream is not None:
                    stream.close()
            return None
        try:
            _recover_layout_publication(database_path, private_root, job_id)
        except PageLayoutError as error:
            raise PageLayoutError("版面分析任务暂时无法启动") from error
        with sqlite3.connect(database_path, timeout=10) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT j.status,r.status FROM import_jobs j
                   LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
                   WHERE j.id=?""", (job_id,)
            ).fetchone()
            if row != ("pending", "completed"):
                raise PageLayoutError("页面处理完成后才能开始版面分析")
            now = _now()
            connection.execute(
                """INSERT INTO import_layout_analysis_runs
                   (import_job_id,status,analyzed_pages,detected_questions,
                    started_at,updated_at) VALUES (?,'processing',0,0,?,?)
                   ON CONFLICT(import_job_id) DO UPDATE SET status='processing',
                    total_pages=CASE WHEN manifest_sha256 IS NULL
                        THEN NULL ELSE total_pages END,
                    analyzed_pages=CASE WHEN manifest_sha256 IS NULL
                        THEN 0 ELSE analyzed_pages END,
                    detected_questions=CASE WHEN manifest_sha256 IS NULL
                        THEN 0 ELSE detected_questions END,
                    error_message=NULL,started_at=excluded.started_at,
                    completed_at=NULL,updated_at=excluded.updated_at""",
                (job_id, now, now),
            )
        return LayoutClaim(
            database_path, private_root, job_id, job_stream, global_stream
        )
    except PageLayoutError:
        for stream in (job_stream, global_stream):
            if stream is not None:
                stream.close()
        raise
    except (OSError, sqlite3.Error) as error:
        for stream in (job_stream, global_stream):
            if stream is not None:
                stream.close()
        _mark_failed(database_path, job_id, SAFE_ANALYSIS_ERROR)
        raise PageLayoutError("版面分析任务暂时无法启动") from error


def run_claimed_layout(claim: LayoutClaim):
    """Run one worker under its cross-process claim and always release it."""
    if not isinstance(claim, LayoutClaim) or claim.lock_stream is None:
        raise PageLayoutError("版面分析任务无效")
    try:
        try:
            return analyze_page_layout(
                claim.database_path, claim.private_root, claim.job_id
            )
        except PageLayoutError:
            return None
    finally:
        claim.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _available_bytes(descriptor: int) -> int:
    details = os.fstatvfs(descriptor)
    return details.f_bavail * details.f_frsize


def _check_output_budget(overlay_bytes: int, manifest_bytes: int = 0) -> None:
    if (
        type(overlay_bytes) is not int
        or type(manifest_bytes) is not int
        or overlay_bytes < 0
        or manifest_bytes < 0
        or overlay_bytes + manifest_bytes > MAX_LAYOUT_OUTPUT_BYTES
    ):
        raise PageLayoutError(SAFE_LIMIT_ERROR)


def _check_remaining_disk(descriptor: int, committed_bytes: int) -> None:
    _check_output_budget(committed_bytes)
    remaining_budget = MAX_LAYOUT_OUTPUT_BYTES - committed_bytes
    if _available_bytes(descriptor) < MIN_FREE_BYTES + remaining_budget:
        raise PageLayoutError(SAFE_LIMIT_ERROR)


def _close_descriptors(*descriptors: int | None) -> None:
    for descriptor in descriptors:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_source_pdf(private_root: Path, stored_path: str, expected_size: int) -> bytes:
    if (
        not isinstance(stored_path, str)
        or "\\" in stored_path
        or ".." in stored_path
        or PurePosixPath(stored_path).is_absolute()
        or type(expected_size) is not int
        or not 0 < expected_size <= MAX_SOURCE_BYTES
    ):
        raise PageLayoutError(SAFE_INPUT_ERROR)
    relative = PurePosixPath(stored_path)
    if not relative.parts or relative.parts[0] != "raw_papers":
        raise PageLayoutError(SAFE_INPUT_ERROR)
    descriptors = []
    file_fd = None
    try:
        parent = _open_safe_directory(private_root)
        descriptors.append(parent)
        for part in relative.parts[:-1]:
            parent = _open_child_directory(parent, part)
            descriptors.append(parent)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(relative.parts[-1], flags, dir_fd=parent)
        details = os.fstat(file_fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size != expected_size
        ):
            raise PageLayoutError(SAFE_INPUT_ERROR)
        chunks, received = [], 0
        while received < expected_size:
            chunk = os.read(file_fd, min(1024 * 1024, expected_size - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        if received != expected_size or os.read(file_fd, 1):
            raise PageLayoutError(SAFE_INPUT_ERROR)
        content = b"".join(chunks)
        if not content.startswith(b"%PDF"):
            raise PageLayoutError(SAFE_INPUT_ERROR)
        return content
    except PageLayoutError:
        raise
    except OSError as error:
        raise PageLayoutError(SAFE_INPUT_ERROR) from error
    finally:
        _close_descriptors(file_fd, *reversed(descriptors))


def _verify_png(content: bytes, width: int, height: int) -> Image.Image:
    try:
        with Image.open(io.BytesIO(content)) as opened:
            if opened.format != "PNG" or opened.size != (width, height):
                raise PageLayoutError(SAFE_INPUT_ERROR)
            opened.load()
            return opened.convert("RGB")
    except PageLayoutError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise PageLayoutError(SAFE_INPUT_ERROR) from error


def _validate_png_without_rgb(content: bytes, width: int, height: int) -> None:
    """Validate a source PNG without retaining a decoded RGB pixel buffer."""
    try:
        with Image.open(io.BytesIO(content)) as opened:
            if opened.format != "PNG" or opened.size != (width, height):
                raise PageLayoutError(SAFE_INPUT_ERROR)
            opened.verify()
    except PageLayoutError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise PageLayoutError(SAFE_INPUT_ERROR) from error


def _load_inputs(
    database_path: Path, private_root: Path, job_id: int, *, decode_images: bool = False
):
    """Load only the DB-authorized PDF and render whitelist through pinned fds."""
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        job = connection.execute(
            """SELECT j.id,j.status AS import_status,j.page_start,j.page_end,
                      p.stored_path,p.sha256,p.file_size,r.status AS render_status
               FROM import_jobs j
               JOIN source_papers p ON p.id=j.source_paper_id
               JOIN import_page_render_runs r ON r.import_job_id=j.id
               WHERE j.id=?""",
            (job_id,),
        ).fetchone()
    if job is None:
        raise PageLayoutError("未找到导入任务")
    if job["import_status"] != "pending" or job["render_status"] != "completed":
        raise PageLayoutError("页面处理完成后才能开始版面分析")
    content = _read_source_pdf(private_root, job["stored_path"], job["file_size"])
    if _sha256(content) != job["sha256"]:
        raise PageLayoutError(SAFE_INPUT_ERROR)
    try:
        with pymupdf.open(stream=content, filetype="pdf") as source_document:
            actual_source_pages = source_document.page_count
    except Exception as error:
        raise PageLayoutError(SAFE_INPUT_ERROR) from error
    if actual_source_pages <= 0:
        raise PageLayoutError(SAFE_INPUT_ERROR)

    root_fd = processing_fd = job_fd = pages_fd = None
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        render_details = os.stat(
            "render_manifest.json", dir_fd=job_fd, follow_symlinks=False
        )
        if not stat.S_ISREG(render_details.st_mode) or render_details.st_nlink != 1:
            raise PageLayoutError(SAFE_INPUT_ERROR)
        render_bytes = _read_regular_at(
            job_fd, "render_manifest.json", max_bytes=MAX_MANIFEST_BYTES
        )
        render = json.loads(render_bytes.decode("utf-8"))
        if not isinstance(render, dict):
            raise PageLayoutError(SAFE_INPUT_ERROR)
        page_entries = render.get("pages")
        start = job["page_start"] or 1
        end = job["page_end"] or render.get("source_page_count")
        valid_range = (
            type(start) is int
            and type(end) is int
            and 1 <= start <= end <= actual_source_pages
            and end - start + 1 <= MAX_ANALYSIS_PAGES
        )
        expected_numbers = list(range(start, end + 1)) if valid_range else []
        if (
            render.get("version") != 1
            or render.get("import_job_id") != job_id
            or render.get("source_pdf_sha256") != job["sha256"]
            or render.get("source_page_count") != actual_source_pages
            or render.get("page_start") != start
            or render.get("page_end") != end
            or render.get("page_count") != len(expected_numbers)
            or not isinstance(page_entries, list)
            or len(page_entries) != len(expected_numbers)
            or len(page_entries) > MAX_ANALYSIS_PAGES
            or start < 1
            or not isinstance(end, int)
            or end > actual_source_pages
        ):
            raise PageLayoutError(SAFE_INPUT_ERROR)
        pages_fd = _open_child_directory(job_fd, "pages")
        expected_names: set[str] = set()
        statted = []
        cumulative = 0
        total_pixels = 0
        for entry, number in zip(page_entries, expected_numbers):
            name = f"page_{number:03d}.png"
            expected_names.add(name)
            if not isinstance(entry, dict) or any(
                (
                    entry.get("page_number") != number,
                    entry.get("relative_path") != f"pages/{name}",
                    type(entry.get("pixel_width")) is not int,
                    type(entry.get("pixel_height")) is not int,
                    type(entry.get("byte_size")) is not int,
                    not isinstance(entry.get("sha256"), str),
                )
            ):
                raise PageLayoutError(SAFE_INPUT_ERROR)
            if entry["pixel_width"] <= 0 or entry["pixel_height"] <= 0:
                raise PageLayoutError(SAFE_INPUT_ERROR)
            page_pixels = entry["pixel_width"] * entry["pixel_height"]
            if page_pixels > MAX_PAGE_PIXELS:
                raise PageLayoutError(SAFE_LIMIT_ERROR)
            total_pixels += page_pixels
            if total_pixels > MAX_TOTAL_ANALYSIS_PIXELS:
                raise PageLayoutError(SAFE_LIMIT_ERROR)
            details = os.stat(name, dir_fd=pages_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_size != entry["byte_size"]
                or details.st_size <= 0
            ):
                raise PageLayoutError(SAFE_INPUT_ERROR)
            cumulative += details.st_size
            if cumulative > MAX_LAYOUT_OUTPUT_BYTES:
                raise PageLayoutError(SAFE_LIMIT_ERROR)
            statted.append((entry, name, details.st_size))
        if set(os.listdir(pages_fd)) != expected_names:
            raise PageLayoutError(SAFE_INPUT_ERROR)
        loaded = []
        for entry, name, size in statted:
            page_bytes = _read_regular_at(
                pages_fd, name, max_bytes=200 * 1024 * 1024, expected_size=size
            )
            if _sha256(page_bytes) != entry["sha256"]:
                raise PageLayoutError(SAFE_INPUT_ERROR)
            if decode_images:
                image = _verify_png(
                    page_bytes, entry["pixel_width"], entry["pixel_height"]
                )
            else:
                _validate_png_without_rgb(
                    page_bytes, entry["pixel_width"], entry["pixel_height"]
                )
                image = None
            loaded.append((entry, image))
        return dict(job), content, render, render_bytes, loaded
    except PageLayoutError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
        ValueError,
        OverflowError,
        AttributeError,
    ) as error:
        raise PageLayoutError(SAFE_INPUT_ERROR) from error
    finally:
        _close_descriptors(pages_fd, job_fd, processing_fd, root_fd)


def _load_valid_existing(
    private_root: Path,
    job_id: int,
    source_digest: str,
    render_digest: str,
    render_pages: list[dict],
    manifest_digest: str,
    manifest_byte_size: int,
    published_batch_id: str,
    detected_questions: int,
    *,
    target_page_number: int | None = None,
) -> dict:
    """Validate the complete published overlay batch before reading image bytes."""
    root_fd = processing_fd = job_fd = result_fd = overlays_fd = None
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        result_fd = _open_child_directory(job_fd, "layout_result")
        content = _read_regular_at(
            result_fd,
            "layout_manifest.json",
            max_bytes=MAX_MANIFEST_BYTES,
            expected_size=manifest_byte_size,
        )
        if _sha256(content) != manifest_digest:
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        manifest = json.loads(content.decode("utf-8"))
        if not isinstance(manifest, dict) or any((
            manifest.get("version") != MANIFEST_VERSION,
            manifest.get("algorithm_version") != ALGORITHM_VERSION,
            manifest.get("import_job_id") != job_id,
            manifest.get("source_pdf_sha256") != source_digest,
            manifest.get("render_manifest_sha256") != render_digest,
            manifest.get("published_batch_id") != published_batch_id,
            manifest.get("page_count") != len(render_pages),
        )):
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        pages = manifest.get("pages")
        questions = manifest.get("questions")
        if (
            not isinstance(pages, list)
            or len(pages) != len(render_pages)
            or not isinstance(questions, list)
            or manifest.get("question_count") != len(questions)
            or manifest.get("question_count") != detected_questions
            or len(questions) > MAX_QUESTIONS
            or not _valid_warnings(manifest.get("warnings"))
        ):
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        dimensions = {
            entry["page_number"]: (entry["pixel_width"], entry["pixel_height"])
            for entry in render_pages
        }
        page_order = {
            entry["page_number"]: index for index, entry in enumerate(render_pages)
        }
        overlays_fd = _open_child_directory(result_fd, "overlays")
        expected_names, statted, total = set(), [], len(content)
        for page, rendered in zip(pages, render_pages):
            number = rendered["page_number"]
            width, height = dimensions[number]
            overlay = page.get("overlay") if isinstance(page, dict) else None
            name = f"page_{number:03d}.png"
            if (
                not isinstance(page, dict)
                or page.get("page_number") != number
                or page.get("pixel_width") != width
                or page.get("pixel_height") != height
                or type(page.get("column_count")) is not int
                or not 1 <= page["column_count"] <= MAX_COLUMNS
                or not isinstance(page.get("columns"), list)
                or len(page["columns"]) != page["column_count"]
                or type(page.get("text_layer_available")) is not bool
                or page.get("confidence") not in {"high", "medium", "low"}
                or not _valid_warnings(page.get("warnings"))
                or not isinstance(overlay, dict)
                or overlay.get("relative_path") != f"overlays/{name}"
                or overlay.get("pixel_width") != width
                or overlay.get("pixel_height") != height
                or type(overlay.get("byte_size")) is not int
                or not isinstance(overlay.get("sha256"), str)
            ):
                raise PageLayoutError(SAFE_EXISTING_ERROR)
            previous_right = None
            for bbox in page["columns"]:
                if (
                    not _valid_bbox(bbox, width, height)
                    or (previous_right is not None and bbox[0] < previous_right)
                ):
                    raise PageLayoutError(SAFE_EXISTING_ERROR)
                previous_right = bbox[2]
            if overlay["byte_size"] <= 0:
                raise PageLayoutError(SAFE_EXISTING_ERROR)
            total += overlay["byte_size"]
            if total > MAX_LAYOUT_OUTPUT_BYTES:
                raise PageLayoutError(SAFE_EXISTING_ERROR)
            expected_names.add(name)
            if target_page_number is None or number == target_page_number:
                statted.append(
                    (number, name, overlay, width, height, overlay["byte_size"])
                )
        if target_page_number is None and set(os.listdir(overlays_fd)) != expected_names:
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        for question in questions:
            if (
                not isinstance(question, dict)
                or re.fullmatch(r"[1-9]\d{0,2}", question.get("question_no", "")) is None
                or question.get("confidence") not in {"high", "medium", "low"}
                or not _valid_warnings(question.get("warnings"))
                or not isinstance(question.get("regions"), list)
                or not question["regions"]
            ):
                raise PageLayoutError(SAFE_EXISTING_ERROR)
            previous_key = None
            for region in question["regions"]:
                if not isinstance(region, dict) or region.get("page_number") not in dimensions:
                    raise PageLayoutError(SAFE_EXISTING_ERROR)
                width, height = dimensions[region["page_number"]]
                if not _valid_bbox(region.get("bbox"), width, height):
                    raise PageLayoutError(SAFE_EXISTING_ERROR)
                region_page = next(
                    item for item in pages
                    if item["page_number"] == region["page_number"]
                )
                containing_columns = [
                    column_index
                    for column_index, column in enumerate(region_page["columns"])
                    if (
                        column[0] <= region["bbox"][0] < region["bbox"][2] <= column[2]
                        and column[1] <= region["bbox"][1] < region["bbox"][3] <= column[3]
                    )
                ]
                if len(containing_columns) != 1:
                    raise PageLayoutError(SAFE_EXISTING_ERROR)
                key = (
                    page_order[region["page_number"]],
                    containing_columns[0],
                    region["bbox"][1],
                )
                if previous_key is not None and key < previous_key:
                    raise PageLayoutError(SAFE_EXISTING_ERROR)
                previous_key = key
        if (
            target_page_number is not None
            and target_page_number not in dimensions
        ):
            raise PageLayoutError("未找到版面预览")
        target_bytes = None
        for number, name, overlay, width, height, size in statted:
            image_bytes = _read_regular_at(
                overlays_fd, name, max_bytes=200 * 1024 * 1024, expected_size=size
            )
            if _sha256(image_bytes) != overlay["sha256"]:
                raise PageLayoutError(SAFE_EXISTING_ERROR)
            _verify_png(image_bytes, width, height)
            if target_page_number is not None:
                target_bytes = image_bytes
        if target_page_number is not None:
            return manifest, target_bytes
        return manifest
    except PageLayoutError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
        ValueError,
        OverflowError,
        AttributeError,
    ) as error:
        raise PageLayoutError(SAFE_EXISTING_ERROR) from error
    finally:
        _close_descriptors(overlays_fd, result_fd, job_fd, processing_fd, root_fd)


def _valid_bbox(value, width: int, height: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(item) is int for item in value)
        and 0 <= value[0] < value[2] <= width
        and 0 <= value[1] < value[3] <= height
    )


def _valid_warnings(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_QUESTIONS
        and all(isinstance(item, str) and len(item) <= 500 for item in value)
    )


def _read_render_page(private_root: Path, job_id: int, entry: dict) -> Image.Image:
    """Read one whitelisted source page and return its verified RGB image."""
    root_fd = processing_fd = job_fd = pages_fd = None
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        pages_fd = _open_child_directory(job_fd, "pages")
        name = f"page_{entry['page_number']:03d}.png"
        content = _read_regular_at(
            pages_fd, name, max_bytes=200 * 1024 * 1024,
            expected_size=entry["byte_size"],
        )
        if _sha256(content) != entry["sha256"]:
            raise PageLayoutError(SAFE_INPUT_ERROR)
        return _verify_png(content, entry["pixel_width"], entry["pixel_height"])
    except PageLayoutError:
        raise
    except (OSError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise PageLayoutError(SAFE_INPUT_ERROR) from error
    finally:
        _close_descriptors(pages_fd, job_fd, processing_fd, root_fd)


def _ink_projection_columns(image: Image.Image) -> list[list[int]]:
    """Find at most two wide, internal, nearly ink-free vertical gutters."""
    width, height = image.size
    scale = min(1.0, 1000 / width, (MAX_DOWNSCALED_PIXELS_PER_PAGE / (width * height)) ** 0.5)
    sample_width = max(1, int(width * scale))
    sample_height = max(1, int(height * scale))
    gray = image.convert("L")
    if (sample_width, sample_height) != image.size:
        gray = gray.resize((sample_width, sample_height), Image.Resampling.BILINEAR)
    mask = gray.point(lambda value: 255 if value < 210 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return [[0, 0, width, height]]
    left, top, right, bottom = bbox
    content_width = max(1, right - left)
    content_height = max(1, bottom - top)
    pixels = mask.load()
    projection = [
        sum(1 for y in range(top, bottom) if pixels[x, y])
        for x in range(left, right)
    ]
    low_limit = max(1, int(content_height * 0.01))
    minimum_gutter = max(4, int(sample_width * 0.025))
    candidates = []
    run_start = None
    for offset, value in enumerate(projection + [low_limit + 1]):
        if value <= low_limit and run_start is None:
            run_start = offset
        elif value > low_limit and run_start is not None:
            run_end = offset
            center = left + (run_start + run_end) // 2
            if (
                run_end - run_start >= minimum_gutter
                and left + content_width * 0.15 < center < right - content_width * 0.15
            ):
                candidates.append((run_end - run_start, center))
            run_start = None
    candidates.sort(reverse=True)
    selected = sorted(center for _, center in candidates[: MAX_COLUMNS - 1])
    if len(selected) == 2:
        fractions = [(center - left) / content_width for center in selected]
        if not (0.20 <= fractions[0] <= 0.45 and 0.55 <= fractions[1] <= 0.80):
            selected = selected[:1]
    boundaries = [left, *selected, right]
    result = []
    for start, end in zip(boundaries, boundaries[1:]):
        result.append([
            max(0, round(start / scale)), max(0, round(top / scale)),
            min(width, round(end / scale)), min(height, round(bottom / scale)),
        ])
    return result


MAIN_NUMBER = re.compile(r"^(?P<number>[1-9]\d{0,2})(?:[.．、]|题)(?!\d)")


def _page_words(document, page_number: int):
    try:
        return document.load_page(page_number - 1).get_text("words", sort=True)
    except Exception:
        return []


def _anchors_for_page(document, entry: dict, columns: list[list[int]]):
    page = document.load_page(entry["page_number"] - 1)
    scale_x = entry["pixel_width"] / page.rect.width
    scale_y = entry["pixel_height"] / page.rect.height
    words = _page_words(document, entry["page_number"])
    anchors = []
    for word in words:
        match = MAIN_NUMBER.match(str(word[4]).strip())
        if not match:
            continue
        px = round(word[0] * scale_x)
        py = round(word[1] * scale_y)
        for column_index, column in enumerate(columns):
            column_width = column[2] - column[0]
            if column[0] - 5 <= px <= column[0] + max(30, column_width * 0.16):
                anchors.append({
                    "question_no": match.group("number"),
                    "page_number": entry["page_number"],
                    "column_index": column_index,
                    "x": max(column[0], px),
                    "y": max(column[1], py),
                })
                break
    anchors.sort(key=lambda item: (item["column_index"], item["y"], item["x"]))
    return bool(words), anchors


def _build_questions(page_results: list[dict], all_anchors: list[dict]):
    if len(all_anchors) > MAX_QUESTIONS:
        raise PageLayoutError(SAFE_LIMIT_ERROR)
    slots = [
        (page["page_number"], index, column)
        for page in page_results
        for index, column in enumerate(page["columns"])
    ]
    slot_index = {(number, column): index for index, (number, column, _) in enumerate(slots)}
    ordered = sorted(
        all_anchors,
        key=lambda item: (slot_index[(item["page_number"], item["column_index"])], item["y"]),
    )
    questions = []
    seen: set[int] = set()
    previous = None
    for index, anchor in enumerate(ordered):
        number = int(anchor["question_no"])
        warnings = []
        confidence = "medium"
        if number in seen:
            warnings.append("题号重复，请人工复核")
            confidence = "low"
        if previous is not None and number <= previous:
            warnings.append("题号倒序，请人工复核")
            confidence = "low"
        elif previous is not None and number != previous + 1:
            warnings.append("题号跳号，请人工复核")
            confidence = "medium"
        seen.add(number)
        previous = number
        next_anchor = ordered[index + 1] if index + 1 < len(ordered) else None
        start_slot = slot_index[(anchor["page_number"], anchor["column_index"])]
        end_slot = (
            slot_index[(next_anchor["page_number"], next_anchor["column_index"])]
            if next_anchor else len(slots) - 1
        )
        regions = []
        for current in range(start_slot, end_slot + 1):
            page_number, column_index, column = slots[current]
            top = anchor["y"] if current == start_slot else column[1]
            bottom = (
                next_anchor["y"] if next_anchor and current == end_slot else column[3]
            )
            if bottom > top:
                regions.append({
                    "page_number": page_number,
                    "bbox": [column[0], int(top), column[2], int(bottom)],
                })
        if not regions:
            raise PageLayoutError(SAFE_INPUT_ERROR)
        questions.append({
            "question_no": anchor["question_no"],
            "regions": regions,
            "confidence": confidence,
            "warnings": warnings,
        })
    return questions


@dataclass
class _LayoutWorkspace:
    root_fd: int
    processing_fd: int
    job_fd: int
    job_name: str
    device: int
    inode: int
    batch_name: str | None = None
    batch_fd: int | None = None
    overlays_fd: int | None = None
    batch_id: str | None = None

    @classmethod
    def open(cls, private_root: Path, job_id: int):
        root_fd = processing_fd = job_fd = None
        try:
            root_fd = _open_safe_directory(private_root)
            processing_fd = _open_or_create_directory(root_fd, "processing")
            job_name = f"import_job_{job_id}"
            job_fd = _open_child_directory(processing_fd, job_name)
            for name, expected in (("layout_result", stat.S_ISDIR),):
                try:
                    formal = os.stat(name, dir_fd=job_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(formal.st_mode) or not expected(formal.st_mode):
                    raise OSError("unsafe formal output")
                if stat.S_ISREG(formal.st_mode) and formal.st_nlink != 1:
                    raise OSError("unsafe formal output")
            details = os.fstat(job_fd)
            workspace = cls(root_fd, processing_fd, job_fd, job_name, details.st_dev, details.st_ino)
            workspace.assert_attached()
            root_fd = processing_fd = job_fd = None
            return workspace
        finally:
            _close_descriptors(job_fd, processing_fd, root_fd)

    def assert_attached(self):
        current = os.stat(self.job_name, dir_fd=self.processing_fd, follow_symlinks=False)
        pinned = os.fstat(self.job_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (self.device, self.inode)
            or (pinned.st_dev, pinned.st_ino) != (self.device, self.inode)
        ):
            raise OSError("job directory replaced")

    def create_batch(self):
        self.batch_id = secrets.token_hex(16)
        self.batch_name = f".layout_batch.{self.batch_id}"
        os.mkdir(self.batch_name, mode=0o700, dir_fd=self.job_fd)
        self.batch_fd = _open_child_directory(self.job_fd, self.batch_name)
        os.mkdir("overlays", mode=0o700, dir_fd=self.batch_fd)
        self.overlays_fd = _open_child_directory(self.batch_fd, "overlays")
        os.fsync(self.batch_fd)
        os.fsync(self.job_fd)

    def close(self):
        _close_descriptors(self.overlays_fd, self.batch_fd)
        self.overlays_fd = self.batch_fd = None
        if self.batch_name:
            try:
                _remove_tree_at(self.job_fd, self.batch_name)
                os.fsync(self.job_fd)
            except OSError:
                pass
        _close_descriptors(self.job_fd, self.processing_fd, self.root_fd)
        self.job_fd = self.processing_fd = self.root_fd = None


@dataclass
class _Publish:
    workspace: _LayoutWorkspace
    backup_name: str
    installed: bool = False


def _exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


_CONTROLLED_BATCH = re.compile(r"^\.layout_batch\.([0-9a-f]{16,64})$")
_CONTROLLED_BACKUP = re.compile(r"^\.layout_backup\.([0-9a-f]{16,64})$")


def _valid_sha256(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _directory_matches_anchor(job_fd: int, name: str, anchor: dict) -> bool:
    directory_fd = overlays_fd = None
    try:
        directory_fd = _open_child_directory(job_fd, name)
        if set(os.listdir(directory_fd)) != {"layout_manifest.json", "overlays"}:
            return False
        content = _read_regular_at(
            directory_fd,
            "layout_manifest.json",
            max_bytes=MAX_MANIFEST_BYTES,
            expected_size=anchor["manifest_byte_size"],
        )
        if _sha256(content) != anchor["manifest_sha256"]:
            return False
        manifest = json.loads(content.decode("utf-8"))
        if not isinstance(manifest, dict) or any(
            (
                manifest.get("version") != MANIFEST_VERSION,
                manifest.get("algorithm_version") != ALGORITHM_VERSION,
                manifest.get("import_job_id") != anchor["job_id"],
                manifest.get("published_batch_id") != anchor["published_batch_id"],
                manifest.get("source_pdf_sha256") != anchor["source_pdf_sha256"],
                manifest.get("render_manifest_sha256")
                != anchor["render_manifest_sha256"],
                manifest.get("page_count") != anchor["total_pages"],
                manifest.get("question_count") != anchor["detected_questions"],
            )
        ):
            return False
        pages = manifest.get("pages")
        questions = manifest.get("questions")
        page_count = manifest.get("page_count")
        question_count = manifest.get("question_count")
        if (
            type(page_count) is not int
            or not 0 < page_count <= MAX_ANALYSIS_PAGES
            or not isinstance(pages, list)
            or len(pages) != page_count
            or type(question_count) is not int
            or not 0 <= question_count <= MAX_QUESTIONS
            or not isinstance(questions, list)
            or len(questions) != question_count
            or not _valid_warnings(manifest.get("warnings"))
        ):
            return False
        overlays_fd = _open_child_directory(directory_fd, "overlays")
        expected_names = set()
        dimensions = {}
        page_order = {}
        total_bytes = len(content)
        total_pixels = 0
        for page in pages:
            if not isinstance(page, dict):
                return False
            number = page.get("page_number")
            width = page.get("pixel_width")
            height = page.get("pixel_height")
            overlay = page.get("overlay")
            if (
                type(number) is not int
                or number <= 0
                or number in dimensions
                or type(width) is not int
                or type(height) is not int
                or width <= 0
                or height <= 0
                or type(page.get("column_count")) is not int
                or not 1 <= page["column_count"] <= MAX_COLUMNS
                or not isinstance(page.get("columns"), list)
                or len(page["columns"]) != page["column_count"]
                or type(page.get("text_layer_available")) is not bool
                or page.get("confidence") not in {"high", "medium", "low"}
                or not _valid_warnings(page.get("warnings"))
                or not isinstance(overlay, dict)
            ):
                return False
            pixels = width * height
            total_pixels += pixels
            if (
                pixels > MAX_PAGE_PIXELS
                or total_pixels > MAX_TOTAL_ANALYSIS_PIXELS
            ):
                return False
            if any(not _valid_bbox(bbox, width, height) for bbox in page["columns"]):
                return False
            previous_right = None
            for bbox in page["columns"]:
                if previous_right is not None and bbox[0] < previous_right:
                    return False
                previous_right = bbox[2]
            filename = f"page_{number:03d}.png"
            byte_size = overlay.get("byte_size")
            if (
                overlay.get("relative_path") != f"overlays/{filename}"
                or overlay.get("pixel_width") != width
                or overlay.get("pixel_height") != height
                or type(byte_size) is not int
                or not 0 < byte_size <= 200 * 1024 * 1024
                or not _valid_sha256(overlay.get("sha256"))
            ):
                return False
            total_bytes += byte_size
            if total_bytes > MAX_LAYOUT_OUTPUT_BYTES:
                return False
            image_bytes = _read_regular_at(
                overlays_fd,
                filename,
                max_bytes=200 * 1024 * 1024,
                expected_size=byte_size,
            )
            if _sha256(image_bytes) != overlay["sha256"]:
                return False
            _validate_png_without_rgb(image_bytes, width, height)
            expected_names.add(filename)
            page_order[number] = len(page_order)
            dimensions[number] = (width, height, page["columns"])
        if set(os.listdir(overlays_fd)) != expected_names:
            return False
        for question in questions:
            if (
                not isinstance(question, dict)
                or re.fullmatch(
                    r"[1-9]\d{0,2}", question.get("question_no", "")
                )
                is None
                or question.get("confidence") not in {"high", "medium", "low"}
                or not _valid_warnings(question.get("warnings"))
                or not isinstance(question.get("regions"), list)
                or not question["regions"]
            ):
                return False
            previous_key = None
            for region in question["regions"]:
                if not isinstance(region, dict):
                    return False
                page_number = region.get("page_number")
                page_details = dimensions.get(page_number)
                if page_details is None:
                    return False
                width, height, columns = page_details
                bbox = region.get("bbox")
                if not _valid_bbox(bbox, width, height):
                    return False
                containing_columns = [
                    index
                    for index, column in enumerate(columns)
                    if (
                        column[0] <= bbox[0] < bbox[2] <= column[2]
                        and column[1] <= bbox[1] < bbox[3] <= column[3]
                    )
                ]
                if len(containing_columns) != 1:
                    return False
                key = (page_order[page_number], containing_columns[0], bbox[1])
                if previous_key is not None and key < previous_key:
                    return False
                previous_key = key
        return True
    except (
        OSError,
        PageLayoutError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
        ValueError,
        OverflowError,
        AttributeError,
    ):
        return False
    finally:
        _close_descriptors(overlays_fd, directory_fd)


def _recover_layout_publication(
    database_path: Path, private_root: Path, job_id: int
) -> None:
    """Deterministically reconcile controlled publication remnants with DB anchors."""
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                """SELECT status,manifest_sha256,manifest_byte_size,
                          published_batch_id,total_pages,analyzed_pages,
                          detected_questions,source_pdf_sha256,
                          render_manifest_sha256
                   FROM import_layout_analysis_runs WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as error:
        raise PageLayoutError(SAFE_ANALYSIS_ERROR) from error
    workspace = None
    try:
        workspace = _LayoutWorkspace.open(private_root, job_id)
        fd = workspace.job_fd
        names = os.listdir(fd)
        batches = sorted(name for name in names if _CONTROLLED_BATCH.fullmatch(name))
        backups = sorted(name for name in names if _CONTROLLED_BACKUP.fullmatch(name))
        anchored = (
            row is not None
            and isinstance(row[1], str)
            and type(row[2]) is int
            and row[2] > 0
            and isinstance(row[3], str)
            and type(row[4]) is int
            and 0 < row[4] <= MAX_ANALYSIS_PAGES
            and row[5] == row[4]
            and type(row[6]) is int
            and 0 <= row[6] <= MAX_QUESTIONS
            and _valid_sha256(row[7])
            and _valid_sha256(row[8])
        )
        anchor = (
            {
                "job_id": job_id,
                "manifest_sha256": row[1],
                "manifest_byte_size": row[2],
                "published_batch_id": row[3],
                "total_pages": row[4],
                "detected_questions": row[6],
                "source_pdf_sha256": row[7],
                "render_manifest_sha256": row[8],
            }
            if anchored
            else None
        )
        formal_matches = anchored and _directory_matches_anchor(
            fd, "layout_result", anchor
        )
        if anchored and formal_matches:
            for name in (*batches, *backups):
                _remove_tree_at(fd, name)
            os.fsync(fd)
            return
        matching_backup = next(
            (
                name
                for name in backups
                if anchored and _directory_matches_anchor(fd, name, anchor)
            ),
            None,
        )
        if matching_backup is not None:
            _remove_tree_at(fd, "layout_result")
            os.replace(
                matching_backup, "layout_result", src_dir_fd=fd, dst_dir_fd=fd
            )
            os.fsync(fd)
            for name in (*batches, *backups):
                if name != matching_backup:
                    _remove_tree_at(fd, name)
            os.fsync(fd)
            return
        if anchored and backups:
            raise PageLayoutError(SAFE_ANALYSIS_ERROR)
        _remove_tree_at(fd, "layout_result")
        for name in (*batches, *backups):
            _remove_tree_at(fd, name)
        os.fsync(fd)
    except PageLayoutError:
        raise
    except OSError as error:
        raise PageLayoutError(SAFE_ANALYSIS_ERROR) from error
    finally:
        if workspace is not None:
            workspace.close()


def _rollback(handle: _Publish):
    fd = handle.workspace.job_fd
    if handle.installed:
        _remove_tree_at(fd, "layout_result")
    if _exists(fd, handle.backup_name):
        os.replace(
            handle.backup_name, "layout_result", src_dir_fd=fd, dst_dir_fd=fd
        )
    os.fsync(fd)


def _rollback_with_retry(handle: _Publish) -> None:
    last_error = None
    for _ in range(2):
        try:
            _rollback(handle)
            return
        except OSError as error:
            last_error = error
    raise last_error


def _publish(workspace: _LayoutWorkspace) -> _Publish:
    suffix = workspace.batch_id or secrets.token_hex(8)
    handle = _Publish(workspace, f".layout_backup.{suffix}")
    fd = workspace.job_fd
    try:
        workspace.assert_attached()
        os.fsync(fd)
        if _exists(fd, "layout_result"):
            os.replace(
                "layout_result", handle.backup_name, src_dir_fd=fd, dst_dir_fd=fd
            )
            os.fsync(fd)
        workspace.assert_attached()
        os.replace(
            workspace.batch_name, "layout_result", src_dir_fd=fd, dst_dir_fd=fd
        )
        handle.installed = True
        os.fsync(fd)
        return handle
    except OSError as error:
        try:
            _rollback_with_retry(handle)
        except OSError as rollback_error:
            raise PageLayoutError(SAFE_ANALYSIS_ERROR) from rollback_error
        raise PageLayoutError(SAFE_ANALYSIS_ERROR) from error


def _finalize(handle: _Publish):
    _remove_tree_at(handle.workspace.job_fd, handle.backup_name)
    os.fsync(handle.workspace.job_fd)


def _mark_failed(database_path: Path, job_id: int, message: str):
    try:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """UPDATE import_layout_analysis_runs SET status='failed',
                   error_message=?,completed_at=NULL,updated_at=? WHERE import_job_id=?""",
                (message, _now(), job_id),
            )
    except sqlite3.Error:
        pass


def _complete_run(
    database_path: Path,
    job_id: int,
    page_count: int,
    question_count: int,
    manifest_bytes: bytes,
    batch_id: str,
    source_digest: str,
    render_digest: str,
) -> None:
    """Commit completion and immutable output anchors in one transaction."""
    with sqlite3.connect(database_path) as connection:
        now = _now()
        result = connection.execute(
            """UPDATE import_layout_analysis_runs SET status='completed',
               analyzed_pages=?,detected_questions=?,error_message=NULL,
               manifest_sha256=?,manifest_byte_size=?,published_batch_id=?,
               source_pdf_sha256=?,render_manifest_sha256=?,
               completed_at=?,updated_at=?
               WHERE import_job_id=? AND status='processing'""",
            (
                page_count,
                question_count,
                _sha256(manifest_bytes),
                len(manifest_bytes),
                batch_id,
                source_digest,
                render_digest,
                now,
                now,
                job_id,
            ),
        )
        if result.rowcount != 1:
            raise PageLayoutError(SAFE_ANALYSIS_ERROR)


def _load_completed_reference(database_path: Path, private_root: Path, job_id: int):
    """Read DB and render-manifest metadata without opening every rendered page."""
    try:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT j.id,j.status AS import_status,j.page_start,j.page_end,
                          p.sha256,r.status AS render_status,
                          la.status AS layout_status,la.total_pages,
                          la.analyzed_pages,la.detected_questions,
                          la.manifest_sha256,la.manifest_byte_size,
                          la.published_batch_id,la.source_pdf_sha256,
                          la.render_manifest_sha256
                   FROM import_jobs j
                   JOIN source_papers p ON p.id=j.source_paper_id
                   JOIN import_page_render_runs r ON r.import_job_id=j.id
                   JOIN import_layout_analysis_runs la ON la.import_job_id=j.id
                   WHERE j.id=?""",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as error:
        raise PageLayoutError(SAFE_EXISTING_ERROR) from error
    if row is None:
        raise PageLayoutError("未找到导入任务")
    if (
        row["import_status"] != "pending"
        or row["render_status"] != "completed"
        or row["layout_status"] != "completed"
        or row["source_pdf_sha256"] != row["sha256"]
        or not isinstance(row["manifest_sha256"], str)
        or type(row["manifest_byte_size"]) is not int
        or row["manifest_byte_size"] <= 0
        or not isinstance(row["published_batch_id"], str)
        or not isinstance(row["render_manifest_sha256"], str)
    ):
        raise PageLayoutError("版面分析尚未完成")

    root_fd = processing_fd = job_fd = None
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        details = os.stat(
            "render_manifest.json", dir_fd=job_fd, follow_symlinks=False
        )
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        render_bytes = _read_regular_at(
            job_fd, "render_manifest.json", max_bytes=MAX_MANIFEST_BYTES
        )
        if _sha256(render_bytes) != row["render_manifest_sha256"]:
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        render = json.loads(render_bytes.decode("utf-8"))
        if not isinstance(render, dict):
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        pages = render.get("pages")
        start = row["page_start"] or 1
        end = row["page_end"] or render.get("source_page_count")
        valid_range = (
            type(start) is int
            and type(end) is int
            and 1 <= start <= end
            and end - start + 1 <= MAX_ANALYSIS_PAGES
        )
        expected_numbers = list(range(start, end + 1)) if valid_range else []
        if (
            render.get("version") != 1
            or render.get("import_job_id") != job_id
            or render.get("source_pdf_sha256") != row["sha256"]
            or type(render.get("source_page_count")) is not int
            or render["source_page_count"] < end
            or render.get("page_start") != start
            or render.get("page_end") != end
            or render.get("page_count") != len(expected_numbers)
            or not expected_numbers
            or len(expected_numbers) > MAX_ANALYSIS_PAGES
            or not isinstance(pages, list)
            or len(pages) != len(expected_numbers)
            or row["total_pages"] != len(expected_numbers)
            or row["analyzed_pages"] != len(expected_numbers)
        ):
            raise PageLayoutError(SAFE_EXISTING_ERROR)
        total_pixels = 0
        for entry, number in zip(pages, expected_numbers):
            if (
                not isinstance(entry, dict)
                or entry.get("page_number") != number
                or entry.get("relative_path") != f"pages/page_{number:03d}.png"
                or type(entry.get("pixel_width")) is not int
                or type(entry.get("pixel_height")) is not int
                or entry["pixel_width"] <= 0
                or entry["pixel_height"] <= 0
                or type(entry.get("byte_size")) is not int
                or entry["byte_size"] <= 0
                or not isinstance(entry.get("sha256"), str)
            ):
                raise PageLayoutError(SAFE_EXISTING_ERROR)
            page_pixels = entry["pixel_width"] * entry["pixel_height"]
            total_pixels += page_pixels
            if (
                page_pixels > MAX_PAGE_PIXELS
                or total_pixels > MAX_TOTAL_ANALYSIS_PIXELS
            ):
                raise PageLayoutError(SAFE_EXISTING_ERROR)
        return dict(row), render, render_bytes
    except PageLayoutError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
        ValueError,
        OverflowError,
        AttributeError,
    ) as error:
        raise PageLayoutError(SAFE_EXISTING_ERROR) from error
    finally:
        _close_descriptors(job_fd, processing_fd, root_fd)


def load_completed_layout(database_path, private_root, job_id: int) -> dict:
    """Read and fully verify one completed review manifest without starting work."""
    if type(job_id) is not int or job_id <= 0:
        raise PageLayoutError("版面分析参数无效")
    database_path, private_root = Path(database_path), Path(private_root)
    reference, anchored_render, anchored_render_bytes = _load_completed_reference(
        database_path, private_root, job_id
    )
    job, _, render, render_bytes, _ = _load_inputs(
        database_path, private_root, job_id, decode_images=False
    )
    if render != anchored_render or render_bytes != anchored_render_bytes:
        raise PageLayoutError(SAFE_EXISTING_ERROR)
    return _load_valid_existing(
        private_root,
        job_id,
        job["sha256"],
        _sha256(render_bytes),
        render["pages"],
        reference["manifest_sha256"],
        reference["manifest_byte_size"],
        reference["published_batch_id"],
        reference["detected_questions"],
    )


def read_layout_overlay(database_path, private_root, job_id: int, page_number: int) -> bytes:
    """Return one manifest-whitelisted overlay through a no-follow descriptor."""
    if type(page_number) is not int or page_number <= 0:
        raise PageLayoutError("未找到版面预览")
    database_path, private_root = Path(database_path), Path(private_root)
    job, render, render_bytes = _load_completed_reference(
        database_path, private_root, job_id
    )
    target_entry = next(
        (entry for entry in render["pages"] if entry["page_number"] == page_number),
        None,
    )
    if target_entry is None:
        raise PageLayoutError("未找到版面预览")
    source_image = _read_render_page(private_root, job_id, target_entry)
    source_image.close()
    manifest, content = _load_valid_existing(
        private_root,
        job_id,
        job["sha256"],
        _sha256(render_bytes),
        render["pages"],
        job["manifest_sha256"],
        job["manifest_byte_size"],
        job["published_batch_id"],
        job["detected_questions"],
        target_page_number=page_number,
    )
    page = next(
        (item for item in manifest["pages"] if item["page_number"] == page_number),
        None,
    )
    if page is None:
        raise PageLayoutError("未找到版面预览")
    return content


def analyze_page_layout(database_path, private_root, job_id: int):
    """Analyze one eligible render and atomically publish review-only candidates."""
    if type(job_id) is not int or job_id <= 0:
        raise PageLayoutError("版面分析参数无效")
    database_path, private_root = Path(database_path), Path(private_root)
    workspace = document = None
    try:
        job, source, render, render_bytes, loaded = _load_inputs(
            database_path, private_root, job_id, decode_images=False
        )
        with sqlite3.connect(database_path) as connection:
            existing_status = connection.execute(
                "SELECT status FROM import_layout_analysis_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
        if existing_status is not None and existing_status[0] == "completed":
            return load_completed_layout(database_path, private_root, job_id)
        with sqlite3.connect(database_path) as connection:
            now = _now()
            connection.execute(
                """INSERT INTO import_layout_analysis_runs
                   (import_job_id,status,total_pages,analyzed_pages,detected_questions,
                    started_at,updated_at) VALUES (?,'processing',?,0,0,?,?)
                   ON CONFLICT(import_job_id) DO UPDATE SET status='processing',
                    total_pages=CASE WHEN manifest_sha256 IS NULL
                        THEN excluded.total_pages ELSE total_pages END,
                    analyzed_pages=CASE WHEN manifest_sha256 IS NULL
                        THEN 0 ELSE analyzed_pages END,
                    detected_questions=CASE WHEN manifest_sha256 IS NULL
                        THEN 0 ELSE detected_questions END,
                    error_message=NULL,
                    started_at=excluded.started_at,completed_at=NULL,
                    updated_at=excluded.updated_at""",
                (job_id, len(loaded), now, now),
            )
        document = pymupdf.open(stream=source, filetype="pdf")
        workspace = _LayoutWorkspace.open(private_root, job_id)
        workspace.create_batch()
        _check_remaining_disk(workspace.job_fd, 0)
        page_results, anchors, manifest_warnings = [], [], []
        total_overlay_bytes = 0
        for analyzed, (entry, _) in enumerate(loaded, start=1):
            image = _read_render_page(private_root, job_id, entry)
            try:
                columns = _ink_projection_columns(image)
            finally:
                image.close()
            text_available, page_anchors = _anchors_for_page(document, entry, columns)
            warnings = []
            if not text_available:
                warnings.append(NO_TEXT_WARNING)
            elif not page_anchors:
                warnings.append(NO_ANCHOR_WARNING)
            anchors.extend(page_anchors)
            page_result = {
                "page_number": entry["page_number"],
                "pixel_width": entry["pixel_width"],
                "pixel_height": entry["pixel_height"],
                "column_count": len(columns),
                "columns": columns,
                "text_layer_available": text_available,
                "confidence": "medium" if text_available else "low",
                "warnings": warnings,
            }
            page_results.append(page_result)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """UPDATE import_layout_analysis_runs SET
                       analyzed_pages=CASE WHEN manifest_sha256 IS NULL
                           THEN ? ELSE analyzed_pages END,updated_at=?
                       """
                    "WHERE import_job_id=? AND status='processing'",
                    (analyzed, _now(), job_id),
                )
        questions = _build_questions(page_results, anchors)
        if not questions:
            manifest_warnings.append(
                NO_TEXT_WARNING if not any(p["text_layer_available"] for p in page_results)
                else NO_ANCHOR_WARNING
            )
        for page_result, (entry, _) in zip(page_results, loaded):
            image = _read_render_page(private_root, job_id, entry)
            try:
                drawing = ImageDraw.Draw(image)
                label_size = max(18, image.width // 80)
                label_font = ImageFont.load_default(size=label_size)
                for index, bbox in enumerate(page_result["columns"], start=1):
                    drawing.rectangle(
                        bbox,
                        outline=(0, 120, 255),
                        width=max(3, image.width // 500),
                    )
                    drawing.text(
                        (bbox[0] + 6, bbox[1] + 6),
                        f"C{index}",
                        fill=(0, 80, 220),
                        font=label_font,
                        stroke_width=2,
                        stroke_fill="white",
                    )
                for question in questions:
                    for region in question["regions"]:
                        if region["page_number"] == page_result["page_number"]:
                            drawing.rectangle(
                                region["bbox"],
                                outline=(255, 50, 30),
                                width=max(3, image.width // 400),
                            )
                            drawing.text(
                                (
                                    region["bbox"][0] + 6,
                                    region["bbox"][1] + label_size + 10,
                                ),
                                f"Q{question['question_no']}",
                                fill=(220, 20, 20),
                                font=label_font,
                                stroke_width=2,
                                stroke_fill="white",
                            )
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                overlay = buffer.getvalue()
            finally:
                image.close()
            total_overlay_bytes += len(overlay)
            _check_output_budget(total_overlay_bytes)
            _check_remaining_disk(
                workspace.job_fd, total_overlay_bytes - len(overlay)
            )
            name = f"page_{page_result['page_number']:03d}.png"
            _write_new_regular_at(workspace.overlays_fd, name, overlay)
            page_result["overlay"] = {
                "relative_path": f"overlays/{name}",
                "pixel_width": page_result["pixel_width"],
                "pixel_height": page_result["pixel_height"],
                "byte_size": len(overlay),
                "sha256": _sha256(overlay),
            }
        manifest = {
            "version": MANIFEST_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "import_job_id": job_id,
            "source_pdf_sha256": job["sha256"],
            "render_manifest_sha256": _sha256(render_bytes),
            "published_batch_id": workspace.batch_id,
            "page_count": len(page_results),
            "pages": page_results,
            "question_count": len(questions),
            "questions": questions,
            "warnings": manifest_warnings,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise PageLayoutError(SAFE_LIMIT_ERROR)
        _check_output_budget(total_overlay_bytes, len(manifest_bytes))
        _check_remaining_disk(workspace.job_fd, total_overlay_bytes)
        _write_new_regular_at(workspace.batch_fd, "layout_manifest.json", manifest_bytes)
        os.fsync(workspace.overlays_fd)
        os.fsync(workspace.batch_fd)
        publish = _publish(workspace)
        workspace.assert_attached()
        _complete_run(
            database_path,
            job_id,
            len(page_results),
            len(questions),
            manifest_bytes,
            workspace.batch_id,
            job["sha256"],
            _sha256(render_bytes),
        )
        workspace.assert_attached()
        try:
            _finalize(publish)
        except OSError:
            pass
        return manifest
    except PageLayoutError as error:
        _mark_failed(database_path, job_id, str(error))
        raise
    except Exception as error:
        _mark_failed(database_path, job_id, SAFE_ANALYSIS_ERROR)
        raise PageLayoutError(SAFE_ANALYSIS_ERROR) from error
    finally:
        if document is not None:
            document.close()
        if workspace is not None:
            workspace.close()
