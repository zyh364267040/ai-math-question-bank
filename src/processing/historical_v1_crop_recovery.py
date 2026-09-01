"""Explicit, fail-closed recovery of historical unsigned v1 crop batches."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from src.database.initialize import DEFAULT_DATABASE_PATH
from src.processing.crop_review import (
    CropReviewError,
    _load_current_crop_review_locked,
    load_current_crop_review,
)
from src.processing.question_crop import (
    MAX_CROP_PIXELS_PER_QUESTION,
    MAX_MANIFEST_BYTES,
    MAX_QUESTIONS,
    MAX_TOTAL_CROP_PIXELS,
    MAX_TOTAL_REGIONS,
    QuestionCropError,
    _composition,
    _validate_plans,
    generate_question_crops_report,
)
from src.processing.pdf_page_renderer import PageRenderError, _read_archived_pdf
from src.processing.question_splitter import (
    MAX_CODEX_OUTPUT_BYTES,
    QuestionSplitError,
    _copy_bound_entry,
    _journal_signature,
    _prepare_locks,
    _remove_tree_at,
    parse_codex_question_plan,
)
from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    bounded_directory_names,
    fsync_directory,
    load_hmac_key,
    locked_job,
    read_file_at,
    write_file_at,
)


SAFE_INVALID = "历史 v1 裁图恢复输入不安全或证据不完整"
SAFE_STATUS = "仅允许 failed/needs_review 的历史 v1 任务恢复"
SAFE_ACTIVE = "检测到活跃 worker 或 lease，拒绝历史恢复"
MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
BACKUP_NAME = ".historical-v1-recovery-backup"
JOURNAL_NAME = ".historical-v1-recovery-journal.json"
RECOVERY_OUTPUTS = ("question_crops", "question_crops.json", "crop_ai_review.json")
PRODUCTION_RENDER_KEYS = {
    "version", "import_job_id", "dpi", "source_pdf_sha256", "source_page_count",
    "page_start", "page_end", "page_count", "pages",
}
LEGACY_RENDER_KEYS = {
    "import_job_id", "source_paper_id", "pdf_sha256", "dpi", "page_count", "pages",
}
RENDER_PAGE_KEYS = {
    "page_number", "relative_path", "pixel_width", "pixel_height", "byte_size", "sha256",
}


class HistoricalV1RecoveryError(ValueError):
    """The historical batch cannot be recovered without weakening provenance."""


def _required_path_flags() -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    if not isinstance(directory, int) or directory == 0:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    return nofollow, directory


def _open_absolute_directory_components(path: Path) -> list[int]:
    """Pin every directory component without ever following a symlink."""
    nofollow, directory = _required_path_flags()
    absolute = Path(os.path.abspath(os.fspath(path)))
    # macOS exposes /var as a system compatibility symlink to /private/var.
    # Normalize that OS-owned alias before the component walk; arbitrary
    # caller-controlled symlinks remain forbidden.
    if absolute.parts[:2] == (os.sep, "var"):
        absolute = Path("/private").joinpath(*absolute.parts[1:])
    descriptors = [os.open(os.sep, os.O_RDONLY | directory | nofollow)]
    try:
        for part in absolute.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | directory | nofollow,
                dir_fd=descriptors[-1],
            )
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise OSError("not directory")
            descriptors.append(child)
        return descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _descriptor_path(descriptor: int) -> Path:
    """Obtain a stable name for a pinned descriptor or fail closed."""
    try:
        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
        value = raw.split(b"\0", 1)[0]
        if value.startswith(b"/"):
            return Path(os.fsdecode(value))
    except OSError:
        pass
    proc = Path(f"/proc/self/fd/{descriptor}")
    if proc.exists():
        value = os.readlink(proc)
        if value.startswith("/"):
            return Path(value)
    raise HistoricalV1RecoveryError(SAFE_INVALID)


@dataclass(frozen=True)
class _DescriptorBoundPath(os.PathLike[str]):
    """Fail closed if a pinned descriptor's kernel name ever changes."""

    descriptor: int
    expected_path: Path
    parent_descriptor: int | None = None
    entry_name: str | None = None
    regular_file: bool = False

    def verify(self) -> None:
        current = _descriptor_path(self.descriptor)
        if current != self.expected_path:
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        pinned = os.fstat(self.descriptor)
        if self.regular_file and (
            not stat.S_ISREG(pinned.st_mode) or pinned.st_nlink != 1
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        if self.parent_descriptor is not None and self.entry_name is not None:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if not self.regular_file:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                current_fd = os.open(
                    self.entry_name, flags, dir_fd=self.parent_descriptor,
                )
                try:
                    current_info = os.fstat(current_fd)
                finally:
                    os.close(current_fd)
            except OSError as error:
                raise HistoricalV1RecoveryError(SAFE_INVALID) from error
            if (current_info.st_dev, current_info.st_ino) != (
                pinned.st_dev, pinned.st_ino,
            ):
                raise HistoricalV1RecoveryError(SAFE_INVALID)

    def __fspath__(self) -> str:
        self.verify()
        return os.fspath(self.expected_path)

    def __str__(self) -> str:
        return self.__fspath__()

    def __truediv__(self, child: str) -> Path:
        return Path(self.__fspath__()) / child


@contextmanager
def _pinned_recovery_paths(database_path: Any, private_root: Any):
    """Hold verified database/private ancestors for the whole operation."""
    nofollow, _ = _required_path_flags()
    database = Path(os.path.abspath(os.fspath(database_path)))
    private = Path(os.path.abspath(os.fspath(private_root)))
    if database.parts[:2] == (os.sep, "var"):
        database = Path("/private").joinpath(*database.parts[1:])
    if private.parts[:2] == (os.sep, "var"):
        private = Path("/private").joinpath(*private.parts[1:])
    parent_descriptors: list[int] = []
    private_descriptors: list[int] = []
    database_fd = None
    try:
        parent_descriptors = _open_absolute_directory_components(database.parent)
        database_fd = os.open(
            database.name, os.O_RDONLY | nofollow, dir_fd=parent_descriptors[-1]
        )
        info = os.fstat(database_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("unsafe database")
        private_descriptors = _open_absolute_directory_components(private)
        pinned_database = _DescriptorBoundPath(
                database_fd, _descriptor_path(database_fd), parent_descriptors[-1],
                database.name, True,
            )
        pinned_private = _DescriptorBoundPath(
                private_descriptors[-1], _descriptor_path(private_descriptors[-1]),
                private_descriptors[-2] if len(private_descriptors) > 1 else None,
                private.name,
            )
        yield pinned_database, pinned_private
        pinned_database.verify()
        pinned_private.verify()
    except HistoricalV1RecoveryError:
        raise
    except OSError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    finally:
        if database_fd is not None:
            os.close(database_fd)
        for descriptor in reversed(private_descriptors):
            os.close(descriptor)
        for descriptor in reversed(parent_descriptors):
            os.close(descriptor)


def _verify_bound_paths(*paths: Any) -> None:
    for path in paths:
        if isinstance(path, _DescriptorBoundPath):
            path.verify()


def _open_descriptor_numbers(limit: int = 4096) -> set[int]:
    result = set()
    for descriptor in range(limit):
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        result.add(descriptor)
    return result


def _descriptor_matches(descriptor: int, expected: os.stat_result) -> bool:
    try:
        info = os.fstat(descriptor)
    except OSError:
        return False
    return (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino)


@contextmanager
def _pinned_sqlite_connection(
    database: Any, *args: Any, readonly: bool = False, **kwargs: Any,
):
    """Prove SQLite opened the pinned inode, including a swap-and-restore race."""
    if not isinstance(database, _DescriptorBoundPath) or not database.regular_file:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    database.verify()
    expected = os.fstat(database.descriptor)
    before = _open_descriptor_numbers()
    connect_target: Any = database
    if readonly:
        connect_target = f"file:{database.expected_path}?mode=ro"
        kwargs["uri"] = True
    connection = sqlite3.connect(connect_target, *args, **kwargs)
    candidates: list[int] = []
    try:
        for descriptor in _open_descriptor_numbers() - before:
            try:
                info = os.fstat(descriptor)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                candidates.append(descriptor)
        if not candidates:
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        database.verify()
        yield connection
        database.verify()
        if not any(_descriptor_matches(descriptor, expected) for descriptor in candidates):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
    finally:
        connection.close()


def _verify_locked_job(
    private_root: Any, job_id: int, descriptor: int,
) -> None:
    """Require the locked job fd to remain the canonical private-root entry."""
    _verify_bound_paths(private_root)
    if not isinstance(private_root, _DescriptorBoundPath):
        return
    processing_fd = job_fd = None
    try:
        processing_fd = os.open(
            "processing", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=private_root.descriptor,
        )
        job_fd = os.open(
            f"import_job_{job_id}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=processing_fd,
        )
        expected = os.fstat(descriptor)
        current = os.fstat(job_fd)
        if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
    except OSError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    finally:
        if job_fd is not None:
            os.close(job_fd)
        if processing_fd is not None:
            os.close(processing_fd)


@contextmanager
def _pinned_job_directory(private_root: Any, job_id: int):
    """Pin processing and job directory entries until the operation returns."""
    _verify_bound_paths(private_root)
    if not isinstance(private_root, _DescriptorBoundPath):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    processing_fd = job_fd = None
    try:
        processing_fd = os.open(
            "processing", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=private_root.descriptor,
        )
        job_fd = os.open(
            f"import_job_{job_id}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=processing_fd,
        )
        processing_identity = os.fstat(processing_fd)
        job_identity = os.fstat(job_fd)
        yield job_fd
        current_processing = os.open(
            "processing", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=private_root.descriptor,
        )
        try:
            current_job = os.open(
                f"import_job_{job_id}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_processing,
            )
            try:
                processing_after = os.fstat(current_processing)
                job_after = os.fstat(current_job)
            finally:
                os.close(current_job)
        finally:
            os.close(current_processing)
        if (
            (processing_after.st_dev, processing_after.st_ino)
            != (processing_identity.st_dev, processing_identity.st_ino)
            or (job_after.st_dev, job_after.st_ino)
            != (job_identity.st_dev, job_identity.st_ino)
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
    except HistoricalV1RecoveryError:
        raise
    except OSError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    finally:
        if job_fd is not None:
            os.close(job_fd)
        if processing_fd is not None:
            os.close(processing_fd)


@dataclass(frozen=True)
class HistoricalV1RecoveryAssessment:
    status: str
    job_id: int
    source_paper_id: int
    legacy_manifest_sha256: str
    legacy_manifest_byte_size: int
    regions_manifest_sha256: str
    regions_manifest_byte_size: int
    render_manifest_sha256: str
    render_manifest_byte_size: int
    render_page_count: int
    source_pdf_sha256: str
    question_nos: list[int]
    questions: list[dict[str, Any]]
    formal_question_count: int
    formal_batch_sha256: str
    candidate_sha256: str | None
    candidate_byte_size: int | None
    draft_batch_sha256: str
    prior_job_status: str
    prior_split_status: str | None
    preserved_codex_run_id: str | None
    render_anchor_missing: bool
    regions_from_legacy_manifest: bool
    generation_id: str | None = None
    crop_manifest_sha256: str | None = None
    crop_manifest_signature: str | None = None
    recropped_question_nos: list[int] | None = None
    reused_question_nos: list[int] | None = None
    next_stages: tuple[str, ...] = (
        "fresh_complete_crop_visual_review",
        "fresh_candidate_extraction",
        "fresh_candidate_visual_audit",
        "fresh_draft_approval_for_unadmitted_questions",
        "fresh_knowledge_classification_for_unadmitted_questions",
        "fresh_strict_dry_assessment",
        "explicit_user_confirmation_before_final_admission",
    )


@dataclass(frozen=True)
class HistoricalV1ResumeAssessment:
    status: str
    job_id: int
    source_paper_id: int
    formal_question_count: int
    formal_batch_sha256: str
    crop_question_count: int
    crop_manifest_sha256: str
    crop_generation_id: str
    crop_manifest_signature: str
    reviewer_run_id: str
    review_request_sha256: str
    review_evidence_signature: str
    reviewed_at: str
    resumed_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _regular(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size < 1
            or details.st_size > max_bytes
        ):
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            pinned = os.fstat(descriptor)
            if (pinned.st_dev, pinned.st_ino, pinned.st_size) != (
                details.st_dev, details.st_ino, details.st_size
            ):
                raise OSError
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(content) != details.st_size or (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ) != (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns):
                raise OSError
            return content, details
        finally:
            os.close(descriptor)
    except OSError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error


def _decode_object(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _reject_active_runs(connection: sqlite3.Connection, job_id: int) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for (table,) in tables:
        if not isinstance(table, str) or "\x00" in table:
            continue
        columns = {row[1] for row in connection.execute(
            f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
        )}
        if not {"import_job_id", "status"}.issubset(columns):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        row = connection.execute(
            f"SELECT 1 FROM {quoted} WHERE import_job_id=? AND status='processing' LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is not None:
            raise HistoricalV1RecoveryError(SAFE_ACTIVE)
    if _table_exists(connection, "import_knowledge_classification_runs"):
        replacement = connection.execute(
            """SELECT 1 FROM import_knowledge_classification_runs
               WHERE import_job_id=? AND replacement_active=1""", (job_id,),
        ).fetchone()
        if replacement is not None:
            raise HistoricalV1RecoveryError(SAFE_ACTIVE)


def _formal_snapshot(connection: sqlite3.Connection, job_id: int) -> tuple[int, str, list[str]]:
    rows = [tuple(row) for row in connection.execute(
        """SELECT q.id,q.question_code,q.stem_markdown,q.answer_markdown,q.answer_status,
                  q.analysis_markdown,q.region_code,q.exam_year,q.exam_type_code,q.paper_name,
                  q.source_question_no,q.source_page,q.score,q.source_file_path,
                  q.question_type_code,q.difficulty_level,q.difficulty_basis,
                  q.primary_knowledge_point_id,q.ocr_review_status,q.formula_review_status,
                  q.figure_review_status,q.answer_review_status,q.analysis_review_status,
                  q.tag_review_status,q.usability_status,q.content_hash,q.duplicate_group_id,
                  q.deleted_at,q.deletion_reason,q.deletion_note,q.created_at,q.updated_at,
                  s.question_id,s.source_paper_id,s.import_job_id,s.source_question_no,
                  s.source_pages_json
           FROM questions q JOIN question_sources s ON s.question_id=q.id
           WHERE s.import_job_id=? ORDER BY CAST(s.source_question_no AS INTEGER),q.id""",
        (job_id,),
    ).fetchall()]
    numbers = [row[35] for row in rows]
    graph = []
    child_queries = (
        ("options", "SELECT * FROM question_options WHERE question_id=? ORDER BY display_order,id"),
        ("subquestions", "SELECT * FROM subquestions WHERE question_id=? ORDER BY display_order,id"),
        ("formulas", "SELECT * FROM question_formulas WHERE question_id=? ORDER BY location,display_order,id"),
        ("figures", "SELECT * FROM question_figures WHERE question_id=? ORDER BY display_order,id"),
        ("assets", "SELECT * FROM question_assets WHERE question_id=? ORDER BY asset_kind,display_order,id"),
        ("knowledge", "SELECT * FROM question_related_knowledge_points WHERE question_id=? ORDER BY knowledge_point_id"),
        ("tags", "SELECT * FROM question_tags WHERE question_id=? ORDER BY tag_id"),
        ("reviews", "SELECT * FROM question_reviews WHERE question_id=? ORDER BY id"),
        ("versions", "SELECT * FROM question_versions WHERE question_id=? ORDER BY id"),
    )
    for row in rows:
        question_id = row[0]
        graph.append({
            "question_and_source": row,
            **{
                name: [tuple(child) for child in connection.execute(query, (question_id,))]
                for name, query in child_queries
            },
        })
    return len(rows), hashlib.sha256(_canonical(graph)).hexdigest(), numbers


def _validate_render(
    connection: sqlite3.Connection, job_dir: Path, job_id: int,
    source_paper_id: int, source_sha: str,
) -> tuple[bytes, dict[str, Any], dict[int, tuple[int, int]], bool]:
    row = connection.execute(
        """SELECT status,dpi,total_pages,rendered_pages,manifest_sha256,
                  manifest_byte_size,published_batch_id,source_pdf_sha256
           FROM import_page_render_runs WHERE import_job_id=?""",
        (job_id,),
    ).fetchone()
    raw, _ = _regular(job_dir / "render_manifest.json", max_bytes=MAX_MANIFEST_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = _decode_object(raw)
    keys = set(manifest)
    if keys == PRODUCTION_RENDER_KEYS:
        if (
            type(manifest["version"]) is not int or manifest["version"] != 1
            or type(manifest["import_job_id"]) is not int
            or manifest["import_job_id"] != job_id
            or type(manifest["dpi"]) is not int or manifest["dpi"] != 300
            or manifest["source_pdf_sha256"] != source_sha
            or type(manifest["source_page_count"]) is not int
            or type(manifest["page_start"]) is not int
            or type(manifest["page_end"]) is not int
            or type(manifest["page_count"]) is not int
            or manifest["source_page_count"] < 1
            or manifest["page_start"] < 1
            or manifest["page_end"] < manifest["page_start"]
            or manifest["page_end"] > manifest["source_page_count"]
            or manifest["page_count"] != manifest["page_end"] - manifest["page_start"] + 1
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        normalized = manifest
    elif keys == LEGACY_RENDER_KEYS:
        if (
            type(manifest["import_job_id"]) is not int
            or manifest["import_job_id"] != job_id
            or type(manifest["source_paper_id"]) is not int
            or manifest["source_paper_id"] != source_paper_id
            or manifest["pdf_sha256"] != source_sha
            or type(manifest["dpi"]) is not int or manifest["dpi"] != 300
            or type(manifest["page_count"]) is not int
            or manifest["page_count"] < 1
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        normalized = {
            "version": 1,
            "import_job_id": job_id,
            "dpi": 300,
            "source_pdf_sha256": source_sha,
            "source_page_count": manifest["page_count"],
            "page_start": 1,
            "page_end": manifest["page_count"],
            "page_count": manifest["page_count"],
            "pages": manifest["pages"],
        }
    else:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    pages = normalized["pages"]
    if not isinstance(pages, list) or len(pages) != normalized["page_count"]:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    if row is not None and (
        row[0] != "completed" or row[1] != 300
        or row[2] != normalized["page_count"] or row[3] != normalized["page_count"]
        or row[4] != digest or row[5] != len(raw)
        or not isinstance(row[6], str) or not row[6]
        or row[7] != normalized["source_pdf_sha256"]
    ):
        # A pre-existing row is an authority claim.  Never repair or replace a
        # contradictory/incomplete claim as if it had merely been absent.
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    page_sizes: dict[int, tuple[int, int]] = {}
    for expected, entry in enumerate(pages, normalized["page_start"]):
        if not isinstance(entry, dict) or set(entry) != RENDER_PAGE_KEYS:
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        width, height = entry["pixel_width"], entry["pixel_height"]
        if (
            type(entry["page_number"]) is not int or entry["page_number"] != expected
            or entry["relative_path"] != f"pages/page_{expected:03d}.png"
            or type(width) is not int or width < 1 or type(height) is not int or height < 1
            or type(entry["byte_size"]) is not int or entry["byte_size"] < 1
            or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        content, _ = _regular(job_dir / entry["relative_path"], max_bytes=200 * 1024 * 1024)
        if len(content) != entry["byte_size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if image.format != "PNG" or image.size != (width, height):
                    raise TypeError
        except (OSError, TypeError, UnidentifiedImageError) as error:
            raise HistoricalV1RecoveryError(SAFE_INVALID) from error
        page_sizes[expected] = (width, height)
    return raw, normalized, page_sizes, row is None


def _validate_regions(
    job_dir: Path, job_id: int, page_sizes: dict[int, tuple[int, int]], split_row: Any,
) -> tuple[bytes, dict[str, Any]]:
    raw, _ = _regular(job_dir / "question_regions.json", max_bytes=MAX_CODEX_OUTPUT_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if split_row is not None and split_row[3] is not None and split_row[3] != digest:
        raise HistoricalV1RecoveryError("历史 question_regions 与 SQLite regions 锚点不一致")
    try:
        plan = parse_codex_question_plan(raw.decode("utf-8"), job_id, page_sizes)
    except (UnicodeError, QuestionSplitError) as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    return raw, plan


def _validate_legacy_manifest(
    job_dir: Path, job_id: int, render: dict[str, Any], plan: dict[str, Any] | None,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    raw, _ = _regular(job_dir / "question_crops.json", max_bytes=MAX_MANIFEST_BYTES)
    manifest = _decode_object(raw)
    base_keys = {
        "version", "import_job_id", "question_count", "source_pages", "questions"
    }
    allowed_keys = base_keys | {"review_status", "review_summary"}
    if (
        not base_keys.issubset(manifest)
        or not set(manifest).issubset(allowed_keys)
        or ("review_status" in manifest) != ("review_summary" in manifest)
        or manifest.get("version") != 1
        or manifest.get("import_job_id") != job_id
    ):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    if "review_summary" in manifest:
        summary = manifest["review_summary"]
        if (
            manifest["review_status"] not in {"pending", "approved", "rejected"}
            or not isinstance(summary, dict)
            or set(summary) != {"approved_count", "rejected_count", "pending_count"}
            or any(type(summary[name]) is not int or summary[name] < 0 for name in summary)
            or sum(summary.values()) != manifest.get("question_count")
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
    count = manifest.get("question_count")
    questions = manifest.get("questions")
    expected_sources = [{
        key: entry[key]
        for key in ("page_number", "relative_path", "pixel_width", "pixel_height", "sha256")
    } for entry in render["pages"]]
    if (
        type(count) is not int or not 1 <= count <= MAX_QUESTIONS
        or manifest.get("source_pages") != expected_sources
        or not isinstance(questions, list) or len(questions) != count
    ):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    if plan is not None and plan["question_count"] != count:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    expected_numbers = list(range(1, count + 1))
    page_metadata = {entry["page_number"]: entry for entry in render["pages"]}
    legacy_plans = []
    for expected, entry in enumerate(questions, 1):
        required = {
            "question_no", "regions", "composition", "output_relative_path", "width",
            "height", "byte_size", "sha256", "crop_status", "review_status", "warnings",
        }
        if (
            not isinstance(entry, dict) or set(entry) != required
            or type(entry.get("question_no")) is not int
            or entry["question_no"] != expected
            or not isinstance(entry.get("regions"), list)
            or any(
                not isinstance(region, dict) or set(region) != {"page_number", "bbox"}
                for region in entry.get("regions", [])
            )
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        legacy_plans.append({
            "question_no": entry["question_no"],
            "regions": entry["regions"],
            "output_relative_path": entry["output_relative_path"],
            "warnings": entry["warnings"],
        })
    try:
        _, validated_plans = _validate_plans(
            legacy_plans, expected_numbers, page_metadata,
            max_questions=MAX_QUESTIONS,
            max_total_regions=MAX_TOTAL_REGIONS,
            max_crop_pixels_per_question=MAX_CROP_PIXELS_PER_QUESTION,
            max_total_crop_pixels=MAX_TOTAL_CROP_PIXELS,
            separator_height=12,
        )
    except QuestionCropError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    normalized_plan = {
        "version": 1,
        "import_job_id": job_id,
        "question_count": count,
        "questions": [{
            "question_no": item["question_no"],
            "regions": item["regions"],
            # Historical review/model annotations have no authority in recovery.
            "warnings": [],
            "mask_regions_normalized": [],
            "mask_regions": [],
        } for item in validated_plans],
    }
    comparison_plan = plan if plan is not None else normalized_plan
    crop_dir = job_dir / "question_crops"
    try:
        details = crop_dir.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_nlink < 2:
            raise OSError
        directory_fd = os.open(
            crop_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    try:
        names = bounded_directory_names(directory_fd, max_entries=count + 1)
        expected_names = [f"Q{number:03d}.png" for number in range(1, count + 1)]
        if sorted(names) != expected_names:
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        for expected, (entry, validated, region_plan) in enumerate(
            zip(questions, validated_plans, comparison_plan["questions"], strict=True), 1
        ):
            expected_path = f"question_crops/Q{expected:03d}.png"
            if (
                entry["question_no"] != expected or entry["output_relative_path"] != expected_path
                or entry["regions"] != [
                    {"page_number": region["page_number"], "bbox": region["bbox"]}
                    for region in region_plan["regions"]
                ]
                or entry["crop_status"] != "generated"
                or entry["review_status"] not in {
                    "pending_ai_review", "ai_review_passed", "needs_fix", "needs_recrop",
                }
                or not isinstance(entry["warnings"], list)
                or not all(isinstance(warning, str) for warning in entry["warnings"])
                or entry["composition"] != _composition(len(entry["regions"]), 12)
                or type(entry["byte_size"]) is not int or entry["byte_size"] < 1
                or not isinstance(entry["sha256"], str)
                or len(entry["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in entry["sha256"])
            ):
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            if (entry["width"], entry["height"]) != validated["expected_size"]:
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            artifact = read_file_at(directory_fd, f"Q{expected:03d}.png", max_bytes=64 * 1024 * 1024)
            if artifact.size != entry["byte_size"] or artifact.sha256 != entry["sha256"]:
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            try:
                with Image.open(io.BytesIO(artifact.data)) as image:
                    image.load()
                    if image.format != "PNG" or image.size != (entry["width"], entry["height"]):
                        raise TypeError
            except (OSError, TypeError, UnidentifiedImageError) as error:
                raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    except SecureCropArtifactError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    finally:
        os.close(directory_fd)
    return raw, manifest, normalized_plan


def _candidate_snapshot(job_dir: Path, expected_numbers: list[str]) -> tuple[str | None, int | None]:
    path = job_dir / "candidate_questions.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None, None
    raw, _ = _regular(path, max_bytes=MAX_CANDIDATE_BYTES)
    value = _decode_object(raw)
    questions = value.get("questions")
    if (
        value.get("question_count") != len(expected_numbers)
        or not isinstance(questions, list)
        or [item.get("source_question_no") for item in questions if isinstance(item, dict)]
        != expected_numbers
    ):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def assess_historical_v1_recovery(
    database_path: Any = DEFAULT_DATABASE_PATH, private_root: Any | None = None,
    job_id: int | None = None,
) -> HistoricalV1RecoveryAssessment:
    database = Path(database_path)
    private = Path(private_root or database.parent)
    with _pinned_recovery_paths(database, private) as (pinned_database, pinned_private):
        with _pinned_job_directory(pinned_private, job_id):
            return _assess_historical_v1_recovery(
                pinned_database, pinned_private, job_id
            )


def _assess_historical_v1_recovery(
    database_path: Any = DEFAULT_DATABASE_PATH, private_root: Any | None = None,
    job_id: int | None = None,
) -> HistoricalV1RecoveryAssessment:
    """Read-only verification. It never creates locks, keys, files, or database rows."""
    database_bound = database_path
    if type(job_id) is not int or job_id < 1:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    if not database_path.is_file() or database_path.is_symlink():
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        details = job_dir.lstat()
        if job_dir.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise OSError
    except OSError as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    try:
        with _pinned_sqlite_connection(database_bound) as connection:
            job = connection.execute(
                """SELECT j.status,j.source_paper_id,p.sha256,p.stored_path,p.file_size
                   FROM import_jobs j JOIN source_papers p ON p.id=j.source_paper_id
                   WHERE j.id=?""", (job_id,),
            ).fetchone()
            if job is None:
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            if job[0] == "completed" or job[0] not in {"failed", "needs_review"}:
                raise HistoricalV1RecoveryError(SAFE_STATUS)
            source_content = _read_archived_pdf(private_root, job[3], job[4])
            if hashlib.sha256(source_content).hexdigest() != job[2]:
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            _reject_active_runs(connection, job_id)
            if _table_exists(connection, "historical_v1_crop_recoveries"):
                existing = connection.execute(
                    "SELECT new_crop_generation_id FROM historical_v1_crop_recoveries WHERE import_job_id=?",
                    (job_id,),
                ).fetchone()
                if existing is not None:
                    raise HistoricalV1RecoveryError("任务已有历史恢复记录，不能再次按 v1 评估")
            split = connection.execute(
                """SELECT status,question_count,processed_pages,result_manifest_sha256,
                          render_manifest_sha256,source_pdf_sha256,codex_run_id
                   FROM import_question_split_runs WHERE import_job_id=?""", (job_id,),
            ).fetchone()
            render_raw, render, page_sizes, render_anchor_missing = _validate_render(
                connection, job_dir, job_id, job[1], job[2]
            )
            try:
                os.stat(job_dir / "question_regions.json", follow_symlinks=False)
                regions_exist = True
            except FileNotFoundError:
                regions_exist = False
            if regions_exist:
                regions_raw, plan = _validate_regions(job_dir, job_id, page_sizes, split)
                legacy_raw, _, _ = _validate_legacy_manifest(
                    job_dir, job_id, render, plan
                )
                regions_from_legacy_manifest = False
            else:
                # Absence is recoverable only when SQLite also contains no split
                # authority claim at all.  A row plus a missing result is contradictory.
                if split is not None:
                    raise HistoricalV1RecoveryError(SAFE_INVALID)
                legacy_raw, _, plan = _validate_legacy_manifest(
                    job_dir, job_id, render, None
                )
                regions_raw = legacy_raw
                regions_from_legacy_manifest = True
            numbers = list(range(1, plan["question_count"] + 1))
            if split is not None and (
                (split[1] is not None and split[1] != len(numbers))
                or (split[2] not in (0, len(page_sizes)))
                or (split[4] is not None and split[4] != hashlib.sha256(render_raw).hexdigest())
                or (split[5] is not None and split[5] != job[2])
            ):
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            formal_count, formal_digest, formal_numbers = _formal_snapshot(connection, job_id)
            if (
                len(formal_numbers) != len(set(formal_numbers))
                or any(not number.isascii() or not number.isdigit() for number in formal_numbers)
                or not set(map(int, formal_numbers)).issubset(numbers)
            ):
                raise HistoricalV1RecoveryError("已有正式题状态无法安全识别")
            expected_text = [str(number) for number in numbers]
            candidate_sha, candidate_size = _candidate_snapshot(job_dir, expected_text)
            drafts = connection.execute(
                """SELECT source_question_no,source_candidate_sha256,source_snapshot_json,
                          edited_json,deleted_at FROM candidate_review_drafts
                   WHERE import_job_id=? ORDER BY CAST(source_question_no AS INTEGER)""",
                (job_id,),
            ).fetchall()
            draft_digest = hashlib.sha256(_canonical(drafts)).hexdigest()
            if drafts:
                if candidate_sha is None or [row[0] for row in drafts] != expected_text:
                    raise HistoricalV1RecoveryError("历史草稿批次不是权威题号全集")
                candidate_questions = _decode_object(
                    (job_dir / "candidate_questions.json").read_bytes()
                )["questions"]
                for row, candidate in zip(drafts, candidate_questions, strict=True):
                    try:
                        source = json.loads(row[2])
                        edited = json.loads(row[3])
                    except (json.JSONDecodeError, TypeError) as error:
                        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
                    if row[1] != candidate_sha or source != candidate or not isinstance(edited, dict) or row[4] is not None:
                        raise HistoricalV1RecoveryError("历史草稿与候选正文不再严格绑定")
            return HistoricalV1RecoveryAssessment(
                "ready", job_id, job[1], hashlib.sha256(legacy_raw).hexdigest(), len(legacy_raw),
                hashlib.sha256(regions_raw).hexdigest(), len(regions_raw),
                hashlib.sha256(render_raw).hexdigest(), len(render_raw), render["page_count"],
                job[2], numbers,
                plan["questions"], formal_count, formal_digest, candidate_sha, candidate_size,
                draft_digest, job[0], split[0] if split else None, split[6] if split else None,
                render_anchor_missing, regions_from_legacy_manifest,
            )
    except HistoricalV1RecoveryError:
        raise
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, PageRenderError) as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error


def _commit_recovery(
    database_path: Any, assessment: HistoricalV1RecoveryAssessment,
    crop_digest: str, generation_id: str, signature: str,
) -> None:
    with _pinned_sqlite_connection(database_path, timeout=10) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT status,source_paper_id FROM import_jobs WHERE id=?", (assessment.job_id,)
        ).fetchone()
        if current != (assessment.prior_job_status, assessment.source_paper_id):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        _reject_active_runs(connection, assessment.job_id)
        row = connection.execute(
            "SELECT status,codex_run_id FROM import_question_split_runs WHERE import_job_id=?",
            (assessment.job_id,),
        ).fetchone()
        if row is not None and row != (assessment.prior_split_status, assessment.preserved_codex_run_id):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        formal_count, formal_digest, _ = _formal_snapshot(connection, assessment.job_id)
        if (formal_count, formal_digest) != (
            assessment.formal_question_count, assessment.formal_batch_sha256,
        ):
            raise HistoricalV1RecoveryError("已有正式题在恢复期间发生变化")
        current_drafts = connection.execute(
            """SELECT source_question_no,source_candidate_sha256,source_snapshot_json,
                      edited_json,deleted_at FROM candidate_review_drafts
               WHERE import_job_id=? ORDER BY CAST(source_question_no AS INTEGER)""",
            (assessment.job_id,),
        ).fetchall()
        if hashlib.sha256(_canonical(current_drafts)).hexdigest() != assessment.draft_batch_sha256:
            raise HistoricalV1RecoveryError("历史草稿在恢复期间发生变化")
        render_row = connection.execute(
            """SELECT status,dpi,total_pages,rendered_pages,manifest_sha256,
                      manifest_byte_size,published_batch_id,source_pdf_sha256
               FROM import_page_render_runs WHERE import_job_id=?""",
            (assessment.job_id,),
        ).fetchone()
        now = _now()
        if assessment.render_anchor_missing:
            if render_row is not None:
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            connection.execute(
                """INSERT INTO import_page_render_runs
                   (import_job_id,status,dpi,total_pages,rendered_pages,manifest_sha256,
                    manifest_byte_size,published_batch_id,source_pdf_sha256,error_message,
                    started_at,completed_at,updated_at)
                   VALUES (?,'completed',300,?,?,?,?,?, ?,NULL,?,?,?)""",
                (
                    assessment.job_id, assessment.render_page_count,
                    assessment.render_page_count, assessment.render_manifest_sha256,
                    assessment.render_manifest_byte_size,
                    "historical-v1-recovery-" + assessment.render_manifest_sha256[:24],
                    assessment.source_pdf_sha256, now, now, now,
                ),
            )
        elif (
            render_row is None
            or render_row[0] != "completed" or render_row[1] != 300
            or render_row[2] != assessment.render_page_count
            or render_row[3] != assessment.render_page_count
            or render_row[4] != assessment.render_manifest_sha256
            or render_row[5] != assessment.render_manifest_byte_size
            or not isinstance(render_row[6], str) or not render_row[6]
            or render_row[7] != assessment.source_pdf_sha256
        ):
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        provenance = assessment.preserved_codex_run_id or (
            (
                "historical-v1-manifest-unattributed-"
                if assessment.regions_from_legacy_manifest
                else "historical-v1-regions-unattributed-"
            ) + assessment.regions_manifest_sha256[:24]
        )
        connection.execute(
            """INSERT INTO import_question_split_runs
               (import_job_id,status,question_count,processed_pages,error_message,codex_run_id,
                result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                started_at,completed_at,updated_at)
               VALUES (?,'completed',?,?,NULL,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(import_job_id) DO UPDATE SET status='completed',
                 question_count=excluded.question_count,processed_pages=excluded.processed_pages,
                 error_message=NULL,codex_run_id=excluded.codex_run_id,
                 result_manifest_sha256=excluded.result_manifest_sha256,
                 render_manifest_sha256=excluded.render_manifest_sha256,
                 source_pdf_sha256=excluded.source_pdf_sha256,
                 crop_manifest_sha256=excluded.crop_manifest_sha256,
                 crop_generation_id=excluded.crop_generation_id,
                 crop_manifest_signature=excluded.crop_manifest_signature,
                 completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
            (
                assessment.job_id, len(assessment.question_nos),
                assessment.render_page_count,
                provenance, assessment.regions_manifest_sha256, assessment.render_manifest_sha256,
                assessment.source_pdf_sha256, crop_digest, generation_id, signature,
                now, now, now,
            ),
        )
        connection.execute(
            """UPDATE candidate_review_drafts SET status='draft',version=version+1,
                      reviewed_at=NULL,approval_source=NULL,approval_evidence_json=NULL,
                      updated_at=? WHERE import_job_id=?""",
            (now, assessment.job_id),
        )
        cursor = connection.execute(
            "UPDATE import_jobs SET status='pending',error_message=NULL,updated_at=? WHERE id=? AND status=?",
            (now, assessment.job_id, assessment.prior_job_status),
        )
        if cursor.rowcount != 1:
            raise HistoricalV1RecoveryError(SAFE_INVALID)
        connection.execute(
            """INSERT INTO historical_v1_crop_recoveries
               (import_job_id,source_paper_id,source_pdf_sha256,render_manifest_sha256,
                render_manifest_byte_size,regions_manifest_sha256,regions_manifest_byte_size,
                legacy_crop_manifest_sha256,legacy_crop_manifest_byte_size,question_nos_json,
                prior_job_status,prior_split_status,preserved_codex_run_id,
                new_crop_manifest_sha256,new_crop_generation_id,new_crop_manifest_signature,
                formal_question_count,formal_batch_sha256,candidate_sha256,candidate_byte_size,
                draft_batch_sha256,migration_evidence_kind,migration_evidence_json,recovered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                assessment.job_id, assessment.source_paper_id, assessment.source_pdf_sha256,
                assessment.render_manifest_sha256, assessment.render_manifest_byte_size,
                assessment.regions_manifest_sha256, assessment.regions_manifest_byte_size,
                assessment.legacy_manifest_sha256, assessment.legacy_manifest_byte_size,
                json.dumps(assessment.question_nos, separators=(",", ":")),
                assessment.prior_job_status, assessment.prior_split_status,
                assessment.preserved_codex_run_id, crop_digest, generation_id, signature,
                assessment.formal_question_count, assessment.formal_batch_sha256,
                assessment.candidate_sha256, assessment.candidate_byte_size,
                assessment.draft_batch_sha256, "system_migration_placeholder",
                json.dumps({
                    "actor_kind": "system_recovery",
                    "authority": "migration_placeholder_only",
                    "question_plan_source": (
                        "historical_v1_crop_manifest_unattributed"
                        if assessment.regions_from_legacy_manifest
                        else "historical_question_regions_unattributed"
                    ),
                    "state": "awaiting_independent_crop_review",
                    "can_approve": False,
                }, sort_keys=True, separators=(",", ":")), now,
            ),
        )
        _verify_bound_paths(database_path)
        connection.commit()
        _verify_bound_paths(database_path)


def _recovery_commit_is_durable(
    database_path: Any, assessment: HistoricalV1RecoveryAssessment,
    crop_digest: str, generation_id: str, signature: str,
) -> bool:
    try:
        _verify_bound_paths(database_path)
        with _pinned_sqlite_connection(database_path, readonly=True) as connection:
            row = connection.execute(
                """SELECT j.status,r.new_crop_manifest_sha256,r.new_crop_generation_id,
                          r.new_crop_manifest_signature,r.legacy_crop_manifest_sha256,
                          r.formal_batch_sha256
                   FROM import_jobs j JOIN historical_v1_crop_recoveries r
                     ON r.import_job_id=j.id WHERE j.id=?""", (assessment.job_id,),
            ).fetchone()
        _verify_bound_paths(database_path)
        return row == (
            "pending", crop_digest, generation_id, signature,
            assessment.legacy_manifest_sha256, assessment.formal_batch_sha256,
        )
    except (sqlite3.Error, HistoricalV1RecoveryError):
        return False


def _journal_bytes(job_dir: Path, payload: dict[str, Any]) -> bytes:
    signed = {**payload, "signature": _journal_signature(load_hmac_key(job_dir), payload)}
    return (json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _snapshot_legacy_outputs(job_dir: Path, assessment: HistoricalV1RecoveryAssessment) -> Path:
    job_fd = os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    backup_fd = None
    try:
        for name in (BACKUP_NAME, JOURNAL_NAME):
            try:
                os.stat(name, dir_fd=job_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise HistoricalV1RecoveryError("发现未恢复的历史恢复事务")
        os.mkdir(BACKUP_NAME, 0o700, dir_fd=job_fd)
        backup_fd = os.open(
            BACKUP_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=job_fd,
        )
        saved = []
        budget = [0, 5_000]
        for name in RECOVERY_OUTPUTS:
            try:
                os.stat(name, dir_fd=job_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _copy_bound_entry(job_fd, name, backup_fd, name, budget)
            saved.append(name)
        fsync_directory(backup_fd)
        payload = {
            "version": 1,
            "import_job_id": assessment.job_id,
            "saved_outputs": saved,
            "legacy_manifest_sha256": assessment.legacy_manifest_sha256,
            "legacy_manifest_byte_size": assessment.legacy_manifest_byte_size,
            "new_anchors": None,
        }
        write_file_at(job_fd, JOURNAL_NAME, _journal_bytes(job_dir, payload))
        return job_dir / BACKUP_NAME
    except Exception:
        try:
            _remove_tree_at(job_fd, BACKUP_NAME)
        except Exception:
            pass
        try:
            os.unlink(JOURNAL_NAME, dir_fd=job_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(job_fd)


def _read_recovery_journal(job_dir: Path) -> dict[str, Any]:
    raw, _ = _regular(job_dir / JOURNAL_NAME, max_bytes=64 * 1024)
    value = _decode_object(raw)
    if not isinstance(value.get("signature"), str):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    signature = value.pop("signature")
    if signature != _journal_signature(load_hmac_key(job_dir), value):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    if (
        set(value) != {"version", "import_job_id", "saved_outputs", "legacy_manifest_sha256",
                       "legacy_manifest_byte_size", "new_anchors"}
        or value["version"] != 1
        or not isinstance(value["saved_outputs"], list)
        or any(name not in RECOVERY_OUTPUTS for name in value["saved_outputs"])
    ):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    return value


def _update_recovery_journal(job_dir: Path, anchors: tuple[str, str, str]) -> None:
    payload = _read_recovery_journal(job_dir)
    if payload["new_anchors"] is not None:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    payload["new_anchors"] = list(anchors)
    job_fd = os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        temporary = JOURNAL_NAME + ".tmp"
        write_file_at(job_fd, temporary, _journal_bytes(job_dir, payload))
        os.replace(temporary, JOURNAL_NAME, src_dir_fd=job_fd, dst_dir_fd=job_fd)
        fsync_directory(job_fd)
    finally:
        os.close(job_fd)


def _restore_legacy_outputs(job_dir: Path, backup: Path) -> None:
    if backup != job_dir / BACKUP_NAME:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    payload = _read_recovery_journal(job_dir)
    job_fd = os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    backup_fd = os.open(
        BACKUP_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=job_fd
    )
    stages = []
    try:
        budget = [0, 5_000]
        for name in RECOVERY_OUTPUTS:
            stage = ".historical-v1-restore-" + name
            _remove_tree_at(job_fd, stage)
            if name in payload["saved_outputs"]:
                _copy_bound_entry(backup_fd, name, job_fd, stage, budget)
                stages.append((name, stage))
        for name in RECOVERY_OUTPUTS:
            _remove_tree_at(job_fd, name)
        for name, stage in stages:
            os.replace(stage, name, src_dir_fd=job_fd, dst_dir_fd=job_fd)
        fsync_directory(job_fd)
        os.close(backup_fd)
        backup_fd = None
        _remove_tree_at(job_fd, BACKUP_NAME)
        os.unlink(JOURNAL_NAME, dir_fd=job_fd)
        fsync_directory(job_fd)
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(job_fd)


def _finish_recovery_files(job_fd: int) -> None:
    """Remove recovery metadata relative to the already locked job inode."""
    job_fd = os.dup(job_fd)
    try:
        _remove_tree_at(job_fd, BACKUP_NAME)
        try:
            os.unlink(JOURNAL_NAME, dir_fd=job_fd)
        except FileNotFoundError:
            pass
        fsync_directory(job_fd)
    finally:
        os.close(job_fd)


def _recover_interrupted_recovery(
    database_path: Any, job_dir: Path, job_id: int, job_fd: int,
) -> None:
    journal = job_dir / JOURNAL_NAME
    backup = job_dir / BACKUP_NAME
    if not journal.exists():
        if backup.exists():
            raise HistoricalV1RecoveryError("发现无日志的历史恢复备份")
        return
    payload = _read_recovery_journal(job_dir)
    if payload["import_job_id"] != job_id or not backup.is_dir() or backup.is_symlink():
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    with _pinned_sqlite_connection(database_path, readonly=True) as connection:
        row = connection.execute(
            """SELECT new_crop_manifest_sha256,new_crop_generation_id,new_crop_manifest_signature
               FROM historical_v1_crop_recoveries WHERE import_job_id=?""", (job_id,),
        ).fetchone()
    if payload["new_anchors"] is not None and row == tuple(payload["new_anchors"]):
        _finish_recovery_files(job_fd)
    elif row is None:
        _restore_legacy_outputs(job_dir, backup)
    else:
        raise HistoricalV1RecoveryError("历史恢复的文件与 SQLite 状态混合")


def _already_recovered(database_path: Any, private_root: Path, job_id: int):
    try:
        with _pinned_sqlite_connection(database_path, readonly=True) as connection:
            job = connection.execute("SELECT status FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job[0] == "completed":
                return None
            row = connection.execute(
                """SELECT source_paper_id,source_pdf_sha256,render_manifest_sha256,
                          render_manifest_byte_size,regions_manifest_sha256,
                          regions_manifest_byte_size,legacy_crop_manifest_sha256,
                          legacy_crop_manifest_byte_size,question_nos_json,formal_question_count,
                          formal_batch_sha256,candidate_sha256,candidate_byte_size,
                          draft_batch_sha256,prior_job_status,prior_split_status,preserved_codex_run_id,
                          new_crop_manifest_sha256,new_crop_generation_id,new_crop_manifest_signature,
                          (SELECT total_pages FROM import_page_render_runs
                           WHERE import_job_id=historical_v1_crop_recoveries.import_job_id)
                   FROM historical_v1_crop_recoveries WHERE import_job_id=?""", (job_id,),
            ).fetchone()
            if row is None or job[0] != "pending":
                return None
            manifest_raw, _ = _regular(
                private_root / "processing" / f"import_job_{job_id}" / "question_crops.json",
                max_bytes=MAX_MANIFEST_BYTES,
            )
            manifest = _decode_object(manifest_raw)
            if (
                hashlib.sha256(manifest_raw).hexdigest() != row[17]
                or manifest.get("generation_id") != row[18]
                or manifest.get("signature") != row[19]
            ):
                raise HistoricalV1RecoveryError(SAFE_INVALID)
            numbers = json.loads(row[8])
            return HistoricalV1RecoveryAssessment(
                "already_recovered", job_id, row[0], row[6], row[7], row[4], row[5],
                row[2], row[3], row[20], row[1], numbers, [], row[9], row[10], row[11], row[12],
                row[13], row[14], row[15], row[16], False,
                row[4] == row[6], row[18], row[17], row[19], [], [],
            )
    except sqlite3.OperationalError:
        return None


def recover_historical_v1_crops(
    database_path: Any = DEFAULT_DATABASE_PATH, private_root: Any | None = None,
    job_id: int | None = None,
) -> HistoricalV1RecoveryAssessment:
    database = Path(database_path)
    private = Path(private_root or database.parent)
    with _pinned_recovery_paths(database, private) as (pinned_database, pinned_private):
        with _pinned_job_directory(pinned_private, job_id):
            return _recover_historical_v1_crops(pinned_database, pinned_private, job_id)


def _recover_historical_v1_crops(
    database_path: Any = DEFAULT_DATABASE_PATH, private_root: Any | None = None,
    job_id: int | None = None,
) -> HistoricalV1RecoveryAssessment:
    """Apply the explicit v1 recovery after repeating every read-only check under locks."""
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    if type(job_id) is not int or job_id < 1:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    global_stream = job_stream = None
    backup = None
    database_committed = False
    try:
        global_stream, job_stream = _prepare_locks(private_root, job_id)
        for stream in (global_stream, job_stream):
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise HistoricalV1RecoveryError(SAFE_ACTIVE) from error
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        with locked_job(job_dir) as artifact_lock:
            _recover_interrupted_recovery(
                database_bound, job_dir, job_id, artifact_lock.descriptor,
            )
            existing = _already_recovered(database_bound, private_root, job_id)
            if existing is not None:
                return existing
            # Re-assess after owning both worker locks and the shared artifact lock.
            assessment = assess_historical_v1_recovery(database_path, private_root, job_id)
            backup = _snapshot_legacy_outputs(job_dir, assessment)
            try:
                crop_questions = [{
                    "question_no": item["question_no"],
                    "regions": [{"page_number": region["page_number"], "bbox": region["bbox"]}
                                for region in item["regions"]],
                    "mask_regions_normalized": item.get("mask_regions_normalized", []),
                    "mask_regions": item.get("mask_regions", []),
                    "warnings": item.get("warnings", []),
                } for item in assessment.questions]
                report = generate_question_crops_report(
                    job_dir=job_dir, questions=crop_questions,
                    expected_question_nos=assessment.question_nos,
                    force_recrop_question_nos=assessment.question_nos,
                    reset_review_summary=True,
                    job_lock=artifact_lock,
                )
                if (
                    report.recropped_question_nos != assessment.question_nos
                    or report.reused_question_nos
                    or any(q["review_status"] != "pending_ai_review" for q in report.manifest["questions"])
                ):
                    raise HistoricalV1RecoveryError("生产裁图器未执行完整零复用重裁")
                expected_summary = {
                    "approved_count": 0,
                    "rejected_count": 0,
                    "pending_count": len(assessment.question_nos),
                }
                if (
                    report.manifest.get("review_status") != "pending"
                    or report.manifest.get("review_summary") != expected_summary
                ):
                    raise HistoricalV1RecoveryError("新裁图批次审核摘要未整体归零")
                # This digest was computed while the complete batch was staged,
                # before the production writer made the new pair visible.
                crop_digest = report.manifest_sha256
                _update_recovery_journal(job_dir, (
                    crop_digest, report.generation_id, report.manifest["signature"],
                ))
                _verify_bound_paths(database_bound, private_bound)
                _verify_locked_job(private_bound, job_id, artifact_lock.descriptor)
                try:
                    _commit_recovery(
                        database_bound, assessment, crop_digest,
                        report.generation_id, report.manifest["signature"],
                    )
                    database_committed = True
                except Exception:
                    database_committed = _recovery_commit_is_durable(
                        database_bound, assessment, crop_digest,
                        report.generation_id, report.manifest["signature"],
                    )
                    if not database_committed:
                        raise
                _verify_bound_paths(database_bound, private_bound)
                _verify_locked_job(private_bound, job_id, artifact_lock.descriptor)
                _finish_recovery_files(artifact_lock.descriptor)
                return HistoricalV1RecoveryAssessment(
                    **{
                        **assessment.__dict__, "status": "recovered",
                        "generation_id": report.generation_id,
                        "crop_manifest_sha256": crop_digest,
                        "crop_manifest_signature": report.manifest["signature"],
                        "recropped_question_nos": report.recropped_question_nos,
                        "reused_question_nos": report.reused_question_nos,
                    }
                )
            except Exception:
                if not database_committed and backup is not None:
                    _restore_legacy_outputs(job_dir, backup)
                raise
    except HistoricalV1RecoveryError:
        raise
    except (OSError, sqlite3.Error, QuestionCropError, QuestionSplitError,
            SecureCropArtifactError, RuntimeError) as error:
        raise HistoricalV1RecoveryError("历史 v1 裁图恢复失败，已回滚") from error
    finally:
        for stream in (job_stream, global_stream):
            if stream is not None:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                stream.close()


def _valid_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str) and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_active_fresh_runs(connection: sqlite3.Connection, job_id: int) -> None:
    run_tables = (
        "import_candidate_extraction_runs",
        "import_candidate_audit_runs",
        "import_knowledge_classification_runs",
        "import_web_admission_runs",
        "import_answer_extraction_runs",
        "import_answer_review_runs",
    )
    for table in run_tables:
        if not _table_exists(connection, table):
            continue
        columns = {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        clauses = ["status='processing'"]
        if "replacement_active" in columns:
            clauses.append("replacement_active=1")
        if "lease_expires_at" in columns:
            clauses.append("lease_expires_at IS NOT NULL")
        if "claim_token" in columns:
            clauses.append("claim_token IS NOT NULL")
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE import_job_id=? AND ({' OR '.join(clauses)}) LIMIT 1",
            (job_id,),
        ).fetchone() is not None:
            raise HistoricalV1RecoveryError(SAFE_ACTIVE)


def _validate_resume_database_state(
    connection: sqlite3.Connection, private_root: Path, job_id: int,
    review: dict[str, Any],
) -> HistoricalV1ResumeAssessment:
    job = connection.execute(
        """SELECT j.status,j.source_paper_id,j.error_message,p.sha256,p.stored_path,p.file_size
           FROM import_jobs j JOIN source_papers p ON p.id=j.source_paper_id
           WHERE j.id=?""", (job_id,),
    ).fetchone()
    if job is None or job[0] in {"completed", "failed"}:
        raise HistoricalV1RecoveryError("历史恢复任务当前状态不可恢复 fresh pipeline")
    if job[0] not in {"needs_review", "pending"}:
        raise HistoricalV1RecoveryError("仅允许 needs_review 的历史恢复任务恢复 fresh pipeline")
    recovery = connection.execute(
        """SELECT source_paper_id,source_pdf_sha256,render_manifest_sha256,
                  render_manifest_byte_size,regions_manifest_sha256,
                  regions_manifest_byte_size,legacy_crop_manifest_sha256,
                  legacy_crop_manifest_byte_size,question_nos_json,prior_job_status,
                  prior_split_status,preserved_codex_run_id,new_crop_manifest_sha256,
                  new_crop_generation_id,new_crop_manifest_signature,
                  formal_question_count,formal_batch_sha256,candidate_sha256,
                  candidate_byte_size,draft_batch_sha256,migration_evidence_kind,
                  migration_evidence_json,recovered_at
           FROM historical_v1_crop_recoveries WHERE import_job_id=?""", (job_id,),
    ).fetchone()
    if recovery is None:
        raise HistoricalV1RecoveryError("任务没有 historical v1 recovery 记录")
    # prior_split_status and preserved_codex_run_id may both be NULL for the
    # oldest regions-less recoveries; their validity is checked separately.
    required = (*recovery[:10], *recovery[12:17], recovery[19], *recovery[20:])
    if (
        any(value is None for value in required)
        or recovery[0] != job[1] or recovery[1] != job[3]
        or recovery[9] not in {"failed", "needs_review"}
        or recovery[10] not in {None, "pending", "processing", "completed", "failed"}
        or (recovery[17] is None) != (recovery[18] is None)
        or not _valid_hex(recovery[1], 64)
        or not all(_valid_hex(recovery[index], 64) for index in (2, 4, 6, 12, 14, 16, 19))
        or not _valid_hex(recovery[13], 32)
        or recovery[20] != "system_migration_placeholder"
    ):
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    try:
        numbers = json.loads(recovery[8])
        migration = json.loads(recovery[21])
    except (TypeError, json.JSONDecodeError) as error:
        raise HistoricalV1RecoveryError(SAFE_INVALID) from error
    if (
        not isinstance(numbers, list) or numbers != list(range(1, len(numbers) + 1))
        or not numbers or not isinstance(migration, dict)
        or migration.get("authority") != "migration_placeholder_only"
        or migration.get("can_approve") is not False
    ):
        raise HistoricalV1RecoveryError(SAFE_INVALID)

    source = _read_archived_pdf(private_root, job[4], job[5])
    if hashlib.sha256(source).hexdigest() != recovery[1]:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    formal_count, formal_digest, _ = _formal_snapshot(connection, job_id)
    if (formal_count, formal_digest) != (recovery[15], recovery[16]):
        raise HistoricalV1RecoveryError("历史恢复后的正式题批次已漂移")

    render = connection.execute(
        """SELECT status,dpi,total_pages,rendered_pages,manifest_sha256,
                  manifest_byte_size,published_batch_id,source_pdf_sha256
           FROM import_page_render_runs WHERE import_job_id=?""", (job_id,),
    ).fetchone()
    split = connection.execute(
        """SELECT status,question_count,processed_pages,codex_run_id,
                  result_manifest_sha256,render_manifest_sha256,source_pdf_sha256,
                  crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                  completed_at
           FROM import_question_split_runs WHERE import_job_id=?""", (job_id,),
    ).fetchone()
    if (
        render is None or render[0] != "completed" or render[1] != 300
        or type(render[2]) is not int or render[2] < 1 or render[3] != render[2]
        or tuple(render[4:6]) != (recovery[2], recovery[3])
        or not isinstance(render[6], str) or not render[6]
        or render[7] != recovery[1]
        or split is None or split[0] != "completed"
        or split[1] != len(numbers) or split[2] != render[2]
        or not isinstance(split[3], str) or not split[3]
        or split[4] != recovery[4] or split[5] != recovery[2]
        or split[6] != recovery[1] or split[10] is None
    ):
        raise HistoricalV1RecoveryError("历史恢复的 split/render 锚点不完整或已漂移")
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    render_raw, _ = _regular(job_dir / "render_manifest.json", max_bytes=MAX_MANIFEST_BYTES)
    regions_from_legacy = (
        migration.get("question_plan_source")
        == "historical_v1_crop_manifest_unattributed"
    )
    if regions_from_legacy:
        # The oldest batches never had question_regions.json.  Recovery already
        # froze its exact legacy-manifest digest/size and derived the completed
        # split row from it; do not invent a mutable replacement file later.
        try:
            (job_dir / "question_regions.json").lstat()
        except FileNotFoundError:
            regions_match = type(recovery[5]) is int and recovery[5] > 0
        except OSError as error:
            raise HistoricalV1RecoveryError(SAFE_INVALID) from error
        else:
            raise HistoricalV1RecoveryError(
                "regions-less 历史恢复后出现了未锚定的 question_regions.json"
            )
    else:
        regions_raw, _ = _regular(
            job_dir / "question_regions.json", max_bytes=MAX_CODEX_OUTPUT_BYTES
        )
        regions_match = (
            hashlib.sha256(regions_raw).hexdigest(), len(regions_raw)
        ) == (recovery[4], recovery[5])
    crop_raw, _ = _regular(job_dir / "question_crops.json", max_bytes=MAX_MANIFEST_BYTES)
    current_crop = _decode_object(crop_raw)
    if (
        (hashlib.sha256(render_raw).hexdigest(), len(render_raw)) != (recovery[2], recovery[3])
        or not regions_match
        or hashlib.sha256(crop_raw).hexdigest() != split[7]
        or current_crop.get("generation_id") != split[8]
        or current_crop.get("signature") != split[9]
    ):
        raise HistoricalV1RecoveryError("历史恢复的 split/render artifacts 已漂移")

    current_anchors = (split[7], split[8], split[9])
    review_output_anchors = (
        review.get("output_manifest_sha256"), review.get("input_generation_id"),
        review.get("output_manifest_signature"),
    )
    if (
        current_anchors != review_output_anchors
        or recovery[13] != split[8]
        or recovery[13] != review.get("input_generation_id")
        or recovery[12] != review.get("input_manifest_sha256")
        or len(review.get("questions", ())) != len(numbers)
        or [item.get("question_no") for item in review.get("questions", ())] != numbers
        or any(item.get("status") != "ai_review_passed" for item in review["questions"])
        or review.get("reviewer_run_id") == split[3]
        or review.get("reviewer_run_id") == recovery[20]
        or review.get("reviewer_run_id") == "system_migration_placeholder"
        or not all(_valid_hex(review.get(name), 64) for name in (
            "request_sha256", "signature",
        ))
        or not isinstance(review.get("reviewed_at"), str) or not review["reviewed_at"]
    ):
        raise HistoricalV1RecoveryError("fresh crop review 不完整、非独立或锚点不一致")
    result = HistoricalV1ResumeAssessment(
        "ready", job_id, job[1], formal_count, formal_digest, len(numbers),
        split[7], split[8], split[9], review["reviewer_run_id"],
        review["request_sha256"], review["signature"], review["reviewed_at"],
    )
    existing = connection.execute(
        """SELECT source_paper_id,source_pdf_sha256,formal_question_count,
                  formal_batch_sha256,crop_question_count,crop_manifest_sha256,
                  crop_generation_id,crop_manifest_signature,reviewer_run_id,
                  review_request_sha256,review_evidence_signature,reviewed_at,resumed_at
           FROM historical_v1_pipeline_resumptions WHERE import_job_id=?""", (job_id,),
    ).fetchone()
    expected = (
        result.source_paper_id, recovery[1], result.formal_question_count,
        result.formal_batch_sha256, result.crop_question_count,
        result.crop_manifest_sha256, result.crop_generation_id,
        result.crop_manifest_signature, result.reviewer_run_id,
        result.review_request_sha256, result.review_evidence_signature,
        result.reviewed_at,
    )
    if job[0] == "pending":
        if existing is None:
            _reject_active_fresh_runs(connection, job_id)
            return HistoricalV1ResumeAssessment(
                **{**result.__dict__, "status": "ready_pending_anchor"}
            )
        if existing[:-1] != expected:
            raise HistoricalV1RecoveryError("pending 任务的不可变 resume 记录不一致")
        return HistoricalV1ResumeAssessment(
            **{**result.__dict__, "status": "already_resumed", "resumed_at": existing[-1]}
        )
    if existing is not None:
        raise HistoricalV1RecoveryError("needs_review 任务已有矛盾的 resume 记录")
    _reject_active_fresh_runs(connection, job_id)
    return result


def assess_historical_v1_resume(
    database_path: Any = DEFAULT_DATABASE_PATH, private_root: Any | None = None,
    job_id: int | None = None,
) -> HistoricalV1ResumeAssessment:
    """Read-only gate for resuming the fresh pipeline after historical recovery."""
    if type(job_id) is not int or job_id < 1:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    try:
        with _pinned_recovery_paths(database_path, private_root) as (
            pinned_database, pinned_private,
        ):
            with _pinned_job_directory(pinned_private, job_id):
                with _pinned_sqlite_connection(pinned_database) as connection:
                    status = connection.execute(
                        "SELECT status FROM import_jobs WHERE id=?", (job_id,),
                    ).fetchone()
                if status is None or status[0] not in {"needs_review", "pending"}:
                    raise HistoricalV1RecoveryError(
                        "历史恢复任务当前状态不可恢复 fresh pipeline"
                    )
                review = load_current_crop_review(
                    pinned_database, pinned_private, job_id,
                    expected_job_status=status[0],
                )
                with _pinned_sqlite_connection(pinned_database) as connection:
                    return _validate_resume_database_state(
                        connection, pinned_private, job_id, review,
                    )
    except HistoricalV1RecoveryError:
        raise
    except (CropReviewError, OSError, sqlite3.Error, PageRenderError) as error:
        raise HistoricalV1RecoveryError("fresh pipeline resume 证据验证失败") from error


def resume_historical_v1_fresh_pipeline(
    database_path: Any = DEFAULT_DATABASE_PATH, private_root: Any | None = None,
    job_id: int | None = None,
) -> HistoricalV1ResumeAssessment:
    """Atomically change only needs_review to pending and anchor that decision."""
    if type(job_id) is not int or job_id < 1:
        raise HistoricalV1RecoveryError(SAFE_INVALID)
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    try:
        with _pinned_recovery_paths(database_path, private_root) as (
            pinned_database, pinned_private,
        ):
            with _pinned_job_directory(pinned_private, job_id):
                global_stream = job_stream = None
                try:
                    global_stream, job_stream = _prepare_locks(pinned_private, job_id)
                    for stream in (global_stream, job_stream):
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError as error:
                            raise HistoricalV1RecoveryError(SAFE_ACTIVE) from error
                    job_dir = pinned_private / "processing" / f"import_job_{job_id}"
                    with locked_job(job_dir) as artifact_lock, _pinned_sqlite_connection(
                        pinned_database, timeout=10,
                    ) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        status = connection.execute(
                            "SELECT status FROM import_jobs WHERE id=?", (job_id,),
                        ).fetchone()
                        if status is None or status[0] not in {"needs_review", "pending"}:
                            raise HistoricalV1RecoveryError(
                                "历史恢复任务当前状态不可恢复 fresh pipeline"
                            )
                        review = _load_current_crop_review_locked(
                            pinned_database, artifact_lock, job_id, recover=False,
                            expected_job_status=status[0],
                        )
                        assessment = _validate_resume_database_state(
                            connection, pinned_private, job_id, review,
                        )
                        if assessment.status == "already_resumed":
                            connection.rollback()
                            return assessment
                        now = _now()
                        connection.execute(
                            """INSERT INTO historical_v1_pipeline_resumptions
                               (import_job_id,source_paper_id,source_pdf_sha256,
                                formal_question_count,formal_batch_sha256,crop_question_count,
                                crop_manifest_sha256,crop_generation_id,crop_manifest_signature,
                                reviewer_run_id,review_request_sha256,review_evidence_signature,
                                reviewed_at,resumed_at)
                               SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?""",
                            (
                                job_id, assessment.source_paper_id,
                                connection.execute(
                                    "SELECT source_pdf_sha256 FROM historical_v1_crop_recoveries "
                                    "WHERE import_job_id=?", (job_id,),
                                ).fetchone()[0],
                                assessment.formal_question_count,
                                assessment.formal_batch_sha256,
                                assessment.crop_question_count,
                                assessment.crop_manifest_sha256,
                                assessment.crop_generation_id,
                                assessment.crop_manifest_signature,
                                assessment.reviewer_run_id,
                                assessment.review_request_sha256,
                                assessment.review_evidence_signature,
                                assessment.reviewed_at, now,
                            ),
                        )
                        if status[0] == "needs_review":
                            cursor = connection.execute(
                                """UPDATE import_jobs
                                   SET status='pending',error_message=NULL,updated_at=?
                                   WHERE id=? AND status='needs_review'""",
                                (now, job_id),
                            )
                            if cursor.rowcount != 1:
                                raise HistoricalV1RecoveryError("resume 写入时任务状态发生漂移")
                            result_status = "resumed"
                        else:
                            current_status = connection.execute(
                                "SELECT status FROM import_jobs WHERE id=?", (job_id,),
                            ).fetchone()
                            if current_status != ("pending",):
                                raise HistoricalV1RecoveryError("resume 写入时任务状态发生漂移")
                            result_status = "anchored_pending"
                        _verify_bound_paths(pinned_database, pinned_private)
                        _verify_locked_job(pinned_private, job_id, artifact_lock.descriptor)
                        connection.commit()
                        return HistoricalV1ResumeAssessment(
                            **{**assessment.__dict__, "status": result_status, "resumed_at": now}
                        )
                finally:
                    for stream in (job_stream, global_stream):
                        if stream is not None:
                            try:
                                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                            except OSError:
                                pass
                            stream.close()
    except HistoricalV1RecoveryError:
        raise
    except (CropReviewError, OSError, sqlite3.Error, PageRenderError) as error:
        raise HistoricalV1RecoveryError("fresh pipeline resume 失败，已回滚") from error


def _result_json(result: HistoricalV1RecoveryAssessment | HistoricalV1ResumeAssessment) -> str:
    value = dict(result.__dict__)
    value.pop("questions", None)
    return json.dumps(value, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "历史 v1 裁图任务安全恢复。默认仅做零写入 dry-run；--apply 执行所选恢复阶段。"
            "--resume-fresh-pipeline 只恢复已完成 fresh review 的 pipeline，不自动提取或入库。"
        )
    )
    parser.add_argument("--job-id", type=int, required=True, help="历史 import job ID")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--apply", action="store_true", help="显式执行所选阶段的数据库/文件写入")
    parser.add_argument(
        "--resume-fresh-pipeline", action="store_true",
        help="验证 fresh review 后显式恢复 pending；默认 dry-run，需配合 --apply 写入",
    )
    args = parser.parse_args(argv)
    try:
        if args.resume_fresh_pipeline:
            function = (
                resume_historical_v1_fresh_pipeline
                if args.apply else assess_historical_v1_resume
            )
        else:
            function = recover_historical_v1_crops if args.apply else assess_historical_v1_recovery
        result = function(args.database, args.private_root, args.job_id)
        print(_result_json(result))
        return 0
    except HistoricalV1RecoveryError as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
