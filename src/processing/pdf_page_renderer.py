"""Safely render one explicitly confirmed import PDF into verified page PNGs."""

from __future__ import annotations

import hashlib
import fcntl
import io
import json
import math
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pymupdf
from PIL import Image, UnidentifiedImageError


DEFAULT_DPI = 300
ALLOWED_DPI = frozenset({DEFAULT_DPI})
MAX_RENDER_PAGES = 200
MAX_PAGE_PIXELS = 40_000_000
MAX_TOTAL_RENDER_PIXELS = 400_000_000
MAX_RENDER_OUTPUT_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MIN_FREE_BYTES_AFTER_PAGE = 512 * 1024 * 1024
MANIFEST_VERSION = 1

SAFE_SOURCE_ERROR = "归档 PDF 校验失败，请重新导入后重试"
SAFE_RANGE_ERROR = "确认页码范围无效，请重新导入后重试"
SAFE_LIMIT_ERROR = "PDF 页数或页面尺寸超过安全处理限制"
SAFE_RENDER_ERROR = "页面处理失败，请重试"
SAFE_EXISTING_ERROR = "现有页面结果校验失败，请点击重试"


class PageRenderError(ValueError):
    """A rendering failure containing only a fixed user-safe summary."""


@dataclass
class RenderClaim:
    """An exclusive cross-process lease held until one background worker exits."""

    database_path: Path
    private_root: Path
    job_id: int
    dpi: int
    lock_stream: object
    global_lock_stream: object

    def close(self) -> None:
        """Release the process lock; safe to call repeatedly."""
        streams = (self.lock_stream, self.global_lock_stream)
        self.lock_stream = self.global_lock_stream = None
        _close_lock_streams(*streams)


def _close_lock_streams(*streams) -> None:
    """Best-effort unlock and close for partially or fully acquired claims."""
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


def _open_safe_directory(path: Path) -> int:
    """Open one configured directory without following its final component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("not a directory")
    return descriptor


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    """Create/open one fixed child directory relative to a verified parent fd."""
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("not a directory")
    return descriptor


def _open_child_directory(parent_fd: int, name: str) -> int:
    """Open one existing child directory without following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("not a directory")
    return descriptor


def _read_regular_at(
    parent_fd: int, name: str, *, max_bytes: int, expected_size: int | None = None
) -> bytes:
    """Read one regular child through an O_NOFOLLOW descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OSError("not a regular file")
        if expected_size is not None and details.st_size != expected_size:
            raise OSError("size mismatch")
        if details.st_size <= 0 or details.st_size > max_bytes:
            raise OSError("unsafe file size")
        chunks = []
        received = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - received + 1))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > max_bytes:
                raise OSError("file too large")
        if received != details.st_size:
            raise OSError("file changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _prepare_lock_streams(private_root: Path, job_id: int):
    """Open global and per-job single-link regular locks beneath safe directories."""
    root_fd = processing_fd = locks_fd = None
    streams = []
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_or_create_directory(root_fd, "processing")
        locks_fd = _open_or_create_directory(processing_fd, ".render_locks")
        for lock_name in ("global.lock", f"import_job_{job_id}.lock"):
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                lock_fd = os.open(lock_name, flags, 0o600, dir_fd=locks_fd)
            except FileNotFoundError:
                lock_fd = os.open(lock_name, flags, 0o600, dir_fd=locks_fd)
            details = os.fstat(lock_fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                os.close(lock_fd)
                raise OSError("unsafe lock")
            streams.append(os.fdopen(lock_fd, "a+b"))
        return tuple(streams)
    except Exception:
        for stream in streams:
            stream.close()
        raise
    finally:
        for descriptor in (locks_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one child tree without ever following a symlink."""
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(details.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = _open_child_directory(parent_fd, name)
    try:
        for child_name in os.listdir(child_fd):
            _remove_tree_at(child_fd, child_name)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _write_new_regular_at(parent_fd: int, name: str, content: bytes) -> None:
    """Create, fully write, and sync one new regular file beneath a pinned fd."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OSError("unsafe output")
    finally:
        os.close(descriptor)


def _available_bytes(descriptor: int) -> int:
    """Return bytes available to an unprivileged writer on the pinned filesystem."""
    details = os.fstatvfs(descriptor)
    return details.f_bavail * details.f_frsize


@dataclass
class RenderWorkspace:
    """Pinned render workspace whose operations never re-resolve configured paths."""

    root_fd: int
    processing_fd: int
    job_fd: int
    job_name: str
    device: int
    inode: int
    batch_name: str | None = None
    batch_fd: int | None = None
    pages_fd: int | None = None

    @classmethod
    def open(cls, private_root: Path, job_id: int) -> RenderWorkspace:
        root_fd = processing_fd = job_fd = None
        try:
            root_fd = _open_safe_directory(private_root)
            processing_fd = _open_or_create_directory(root_fd, "processing")
            job_name = f"import_job_{job_id}"
            job_fd = _open_or_create_directory(processing_fd, job_name)
            for name, expected in (
                ("pages", stat.S_ISDIR), ("render_manifest.json", stat.S_ISREG)
            ):
                try:
                    details = os.stat(name, dir_fd=job_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(details.st_mode) or not expected(details.st_mode):
                    raise OSError("unsafe formal output")
            details = os.fstat(job_fd)
            workspace = cls(
                root_fd, processing_fd, job_fd, job_name,
                details.st_dev, details.st_ino,
            )
            workspace.assert_attached()
            root_fd = processing_fd = job_fd = None
            return workspace
        finally:
            for descriptor in (job_fd, processing_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def assert_attached(self) -> None:
        """Require the fixed processing entry to remain the pinned job inode."""
        current = os.stat(
            self.job_name, dir_fd=self.processing_fd, follow_symlinks=False
        )
        pinned = os.fstat(self.job_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (self.device, self.inode)
            or (pinned.st_dev, pinned.st_ino) != (self.device, self.inode)
        ):
            raise OSError("job directory replaced")

    def create_batch(self) -> None:
        """Create one unpredictable fixed-name batch beneath the pinned job fd."""
        for _ in range(8):
            name = f".render_batch.{secrets.token_hex(16)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.job_fd)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("unable to allocate batch")
        self.batch_name = name
        self.batch_fd = _open_child_directory(self.job_fd, name)
        os.mkdir("pages", mode=0o700, dir_fd=self.batch_fd)
        self.pages_fd = _open_child_directory(self.batch_fd, "pages")

    def close(self) -> None:
        """Close descriptors and remove only this fixed batch through the job fd."""
        for attribute in ("pages_fd", "batch_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)
        if self.batch_name is not None:
            try:
                _remove_tree_at(self.job_fd, self.batch_name)
            except OSError:
                pass
            self.batch_name = None
        for attribute in ("job_fd", "processing_fd", "root_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)


def _pending_job_exists(database_path: Path, job_id: int) -> bool:
    """Reject unknown and historical imports before creating lock artifacts."""
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
    except sqlite3.Error as error:
        raise PageRenderError("页面处理任务暂时无法启动") from error
    if row is None:
        raise PageRenderError("未找到导入任务")
    if row[0] != "pending":
        raise PageRenderError("该历史任务不能启动页面处理")
    return True


def claim_render_job(
    database_path, private_root, job_id: int, dpi: int = DEFAULT_DPI
) -> RenderClaim | None:
    """Atomically claim one explicit start, returning ``None`` if already active/done."""
    if type(job_id) is not int or job_id <= 0 or dpi not in ALLOWED_DPI:
        raise PageRenderError("页面处理参数无效")
    database_path = Path(database_path)
    private_root = Path(private_root)
    _pending_job_exists(database_path, job_id)
    global_lock_stream = lock_stream = None
    try:
        global_lock_stream, lock_stream = _prepare_lock_streams(private_root, job_id)
        try:
            fcntl.flock(
                global_lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            _close_lock_streams(lock_stream, global_lock_stream)
            return None
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _close_lock_streams(lock_stream, global_lock_stream)
            return None
    except OSError as error:
        _close_lock_streams(lock_stream, global_lock_stream)
        _mark_failed(database_path, job_id, SAFE_RENDER_ERROR)
        raise PageRenderError("页面处理任务暂时无法启动") from error

    try:
        with sqlite3.connect(database_path, timeout=10) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise PageRenderError("未找到导入任务")
            if job[0] != "pending":
                raise PageRenderError("该历史任务不能启动页面处理")
            run = connection.execute(
                """SELECT status,manifest_sha256,manifest_byte_size,
                          published_batch_id,source_pdf_sha256
                   FROM import_page_render_runs WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
            now = _utc_now()
            if run is None:
                connection.execute(
                    """INSERT INTO import_page_render_runs
                       (import_job_id, status, dpi, rendered_pages, started_at, updated_at)
                       VALUES (?, 'processing', ?, 0, ?, ?)""",
                    (job_id, dpi, now, now),
                )
            elif run[0] != "completed" or any(value is None for value in run[1:]):
                connection.execute(
                    """UPDATE import_page_render_runs
                       SET status='processing', dpi=?, total_pages=NULL,
                           rendered_pages=0, manifest_sha256=NULL,
                           manifest_byte_size=NULL, published_batch_id=NULL,
                           source_pdf_sha256=NULL, error_message=NULL, started_at=?,
                           completed_at=NULL, updated_at=?
                       WHERE import_job_id=?""",
                    (dpi, now, now, job_id),
                )
    except sqlite3.Error as error:
        _close_lock_streams(lock_stream, global_lock_stream)
        raise PageRenderError("页面处理任务暂时无法启动") from error
    except Exception:
        _close_lock_streams(lock_stream, global_lock_stream)
        raise
    return RenderClaim(
        database_path, private_root, job_id, dpi, lock_stream, global_lock_stream
    )


def run_claimed_render(claim: RenderClaim):
    """Run the worker under its claim and always release the process lock."""
    if not isinstance(claim, RenderClaim) or claim.lock_stream is None:
        raise PageRenderError("页面处理任务无效")
    try:
        try:
            return render_import_job(
                claim.database_path, claim.private_root, claim.job_id, claim.dpi
            )
        except PageRenderError:
            return None
    finally:
        claim.close()


def _utc_now() -> str:
    """Return one timezone-aware timestamp for persisted progress state."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest for already-read bytes."""
    return hashlib.sha256(content).hexdigest()


def _read_archived_pdf(private_root: Path, stored_path: str, size: int) -> bytes:
    """Read exactly one DB-selected regular file without following symlinks."""
    if (
        not isinstance(stored_path, str)
        or "\\" in stored_path
        or ".." in stored_path
        or PurePosixPath(stored_path).is_absolute()
    ):
        raise PageRenderError(SAFE_SOURCE_ERROR)
    relative = PurePosixPath(stored_path)
    if (
        not relative.parts
        or relative.parts[0] != "raw_papers"
        or any(part in ("", ".", "..") for part in relative.parts)
        or len(relative.parts) < 2
    ):
        raise PageRenderError(SAFE_SOURCE_ERROR)
    if not isinstance(size, int) or size <= 0 or size > MAX_SOURCE_BYTES:
        raise PageRenderError(SAFE_SOURCE_ERROR)

    descriptors = []
    try:
        descriptor = _open_safe_directory(Path(private_root))
        descriptors.append(descriptor)
        for part in relative.parts[:-1]:
            descriptor = _open_child_directory(descriptor, part)
            descriptors.append(descriptor)
        content = _read_regular_at(
            descriptor, relative.parts[-1], max_bytes=MAX_SOURCE_BYTES,
            expected_size=size,
        )
    except (OSError, ValueError) as error:
        raise PageRenderError(SAFE_SOURCE_ERROR) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if len(content) != size or not content.startswith(b"%PDF"):
        raise PageRenderError(SAFE_SOURCE_ERROR)
    return content


def _load_job(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    """Load the one DB-authorized source and render run for a pending import."""
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """SELECT j.id, j.status AS import_status, j.page_start, j.page_end,
                  p.stored_path, p.sha256, p.file_size, r.status AS render_status,
                  r.dpi AS render_dpi, r.manifest_sha256, r.manifest_byte_size,
                  r.published_batch_id, r.source_pdf_sha256
           FROM import_jobs j
           JOIN source_papers p ON p.id = j.source_paper_id
           JOIN import_page_render_runs r ON r.import_job_id = j.id
           WHERE j.id = ?""",
        (job_id,),
    ).fetchone()
    if row is None or row["import_status"] != "pending":
        raise PageRenderError("导入任务不存在或当前状态不允许页面处理")
    return row


def _load_valid_existing(
    private_root: Path,
    job_id: int,
    dpi: int,
    source_digest: str,
    source_page_count: int,
    page_start: int,
    page_end: int,
    dimensions: list[tuple[int, int, int]],
) -> tuple[dict, bytes]:
    """Return an existing completed result and pinned manifest bytes after validation."""
    root_fd = processing_fd = job_fd = pages_fd = None
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        manifest_bytes = _read_regular_at(
            job_fd, "render_manifest.json", max_bytes=10 * 1024 * 1024
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        expected_header = {
            "version": MANIFEST_VERSION,
            "import_job_id": job_id,
            "dpi": dpi,
            "source_pdf_sha256": source_digest,
            "source_page_count": source_page_count,
            "page_start": page_start,
            "page_end": page_end,
            "page_count": len(dimensions),
        }
        if not isinstance(manifest, dict) or any(
            manifest.get(key) != value for key, value in expected_header.items()
        ):
            raise PageRenderError(SAFE_EXISTING_ERROR)
        pages = manifest.get("pages")
        if not isinstance(pages, list) or len(pages) != len(dimensions):
            raise PageRenderError(SAFE_EXISTING_ERROR)
        pages_fd = _open_child_directory(job_fd, "pages")
        expected_names = set()
        cumulative_output_bytes = 0
        validated_pages = []
        for entry, (number, width, height) in zip(pages, dimensions):
            relative_path = f"pages/page_{number:03d}.png"
            name = f"page_{number:03d}.png"
            expected_names.add(name)
            if not isinstance(entry, dict) or (
                entry.get("page_number") != number
                or entry.get("relative_path") != relative_path
                or entry.get("pixel_width") != width
                or entry.get("pixel_height") != height
            ):
                raise PageRenderError(SAFE_EXISTING_ERROR)
            try:
                declared_size = entry.get("byte_size")
                if type(declared_size) is not int or declared_size <= 0:
                    raise PageRenderError(SAFE_EXISTING_ERROR)
                details = os.stat(name, dir_fd=pages_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_nlink != 1
                    or details.st_size != declared_size
                ):
                    raise PageRenderError(SAFE_EXISTING_ERROR)
                cumulative_output_bytes += details.st_size
                if cumulative_output_bytes > MAX_RENDER_OUTPUT_BYTES:
                    raise PageRenderError(SAFE_EXISTING_ERROR)
                validated_pages.append(
                    (entry, name, width, height, details.st_size)
                )
            except (OSError, PageRenderError) as error:
                raise PageRenderError(SAFE_EXISTING_ERROR) from error
        actual_names = set(os.listdir(pages_fd))
        if actual_names != expected_names:
            raise PageRenderError(SAFE_EXISTING_ERROR)

        for entry, name, width, height, expected_size in validated_pages:
            try:
                content = _read_regular_at(
                    pages_fd, name, max_bytes=200 * 1024 * 1024,
                    expected_size=expected_size,
                )
                byte_size, digest = _verify_png_bytes(content, width, height)
            except (OSError, PageRenderError, UnidentifiedImageError) as error:
                raise PageRenderError(SAFE_EXISTING_ERROR) from error
            if entry.get("byte_size") != byte_size or entry.get("sha256") != digest:
                raise PageRenderError(SAFE_EXISTING_ERROR)
        return manifest, manifest_bytes
    except PageRenderError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, UnidentifiedImageError) as error:
        raise PageRenderError(SAFE_EXISTING_ERROR) from error
    finally:
        for descriptor in (pages_fd, job_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _set_total(connection: sqlite3.Connection, job_id: int, total: int) -> None:
    """Persist the validated page total before allocating page images."""
    result = connection.execute(
        """UPDATE import_page_render_runs
           SET status='processing', total_pages=?, rendered_pages=0,
               error_message=NULL, started_at=COALESCE(started_at, ?),
               completed_at=NULL, updated_at=?
           WHERE import_job_id=? AND status IN ('processing', 'failed')""",
        (total, _utc_now(), _utc_now(), job_id),
    )
    if result.rowcount != 1:
        raise PageRenderError(SAFE_RENDER_ERROR)
    connection.commit()


def _set_progress(connection: sqlite3.Connection, job_id: int, rendered: int) -> None:
    """Commit progress after one PNG has been fully verified."""
    result = connection.execute(
        """UPDATE import_page_render_runs
           SET rendered_pages=?, updated_at=?
           WHERE import_job_id=? AND status='processing'""",
        (rendered, _utc_now(), job_id),
    )
    if result.rowcount != 1:
        raise PageRenderError(SAFE_RENDER_ERROR)
    connection.commit()


def _mark_completed(
    connection: sqlite3.Connection,
    job_id: int,
    total: int,
    manifest_content: bytes,
    published_batch_id: str,
    source_pdf_sha256: str,
) -> None:
    """Commit completion and immutable render trust anchors after publication."""
    now = _utc_now()
    result = connection.execute(
        """UPDATE import_page_render_runs
           SET status='completed', rendered_pages=?, manifest_sha256=?,
               manifest_byte_size=?, published_batch_id=?, source_pdf_sha256=?,
               error_message=NULL, completed_at=?, updated_at=?
           WHERE import_job_id=? AND status='processing'""",
        (
            total, _sha256_bytes(manifest_content), len(manifest_content),
            published_batch_id, source_pdf_sha256, now, now, job_id,
        ),
    )
    if result.rowcount != 1:
        raise PageRenderError(SAFE_RENDER_ERROR)
    connection.commit()


def _mark_failed(database_path: Path, job_id: int, message: str) -> None:
    """Best-effort persistence of one fixed, user-safe failure summary."""
    try:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """UPDATE import_page_render_runs
                   SET status='failed', error_message=?, completed_at=NULL,
                       updated_at=? WHERE import_job_id=?""",
                (message, _utc_now(), job_id),
            )
    except sqlite3.Error:
        pass


def _verify_png_bytes(content: bytes, width: int, height: int) -> tuple[int, str]:
    """Validate PNG format and dimensions using already verified file bytes."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or image.size != (width, height):
                raise PageRenderError(SAFE_RENDER_ERROR)
    except PageRenderError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise PageRenderError(SAFE_RENDER_ERROR) from error
    return len(content), _sha256_bytes(content)


def _verify_png_at(
    parent_fd: int, name: str, width: int, height: int
) -> tuple[int, str]:
    """Verify a generated PNG by reading it from an already pinned directory."""
    try:
        content = _read_regular_at(
            parent_fd, name, max_bytes=200 * 1024 * 1024
        )
        return _verify_png_bytes(content, width, height)
    except PageRenderError:
        raise
    except OSError as error:
        raise PageRenderError(SAFE_RENDER_ERROR) from error


@dataclass
class _PublishHandle:
    workspace: RenderWorkspace
    backup_pages: str
    backup_manifest: str
    had_pages: bool
    had_manifest: bool
    pages_installed: bool = False
    manifest_installed: bool = False


def _rollback_publish(handle: _PublishHandle) -> None:
    """Remove the new pair and restore every previous formal output."""
    job_fd = handle.workspace.job_fd
    if handle.pages_installed:
        _remove_tree_at(job_fd, "pages")
    if handle.manifest_installed:
        _remove_tree_at(job_fd, "render_manifest.json")
    if _exists_at(job_fd, handle.backup_pages):
        os.replace(
            handle.backup_pages, "pages", src_dir_fd=job_fd, dst_dir_fd=job_fd
        )
    if _exists_at(job_fd, handle.backup_manifest):
        os.replace(
            handle.backup_manifest, "render_manifest.json",
            src_dir_fd=job_fd, dst_dir_fd=job_fd,
        )
    _remove_tree_at(job_fd, handle.backup_pages)
    _remove_tree_at(job_fd, handle.backup_manifest)


def _finalize_publish(handle: _PublishHandle) -> None:
    """Delete retained backups after the database completion commit succeeds."""
    _remove_tree_at(handle.workspace.job_fd, handle.backup_pages)
    _remove_tree_at(handle.workspace.job_fd, handle.backup_manifest)


def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _publish(workspace: RenderWorkspace) -> _PublishHandle:
    """Install a new pair while retaining old files for a later DB rollback."""
    if workspace.batch_fd is None or workspace.batch_name is None:
        raise PageRenderError(SAFE_RENDER_ERROR)
    job_fd = workspace.job_fd
    batch_fd = workspace.batch_fd
    backup_pages = f".pages.backup.{workspace.batch_name}"
    backup_manifest = f".manifest.backup.{workspace.batch_name}"
    handle = _PublishHandle(
        workspace, backup_pages, backup_manifest,
        _exists_at(job_fd, "pages"), _exists_at(job_fd, "render_manifest.json")
    )
    try:
        if handle.had_pages:
            workspace.assert_attached()
            os.replace(
                "pages", backup_pages, src_dir_fd=job_fd, dst_dir_fd=job_fd
            )
        if handle.had_manifest:
            workspace.assert_attached()
            os.replace(
                "render_manifest.json", backup_manifest,
                src_dir_fd=job_fd, dst_dir_fd=job_fd,
            )
        workspace.assert_attached()
        os.replace("pages", "pages", src_dir_fd=batch_fd, dst_dir_fd=job_fd)
        handle.pages_installed = True
        workspace.assert_attached()
        os.replace(
            "render_manifest.json", "render_manifest.json",
            src_dir_fd=batch_fd, dst_dir_fd=job_fd,
        )
        handle.manifest_installed = True
    except OSError as error:
        try:
            _rollback_publish(handle)
        except OSError:
            pass
        raise PageRenderError(SAFE_RENDER_ERROR) from error
    return handle


def render_import_job(database_path, private_root, job_id: int, dpi: int = DEFAULT_DPI):
    """Render one already-claimed import job and atomically publish verified files."""
    if type(job_id) is not int or job_id <= 0 or dpi not in ALLOWED_DPI:
        raise PageRenderError("页面处理参数无效")
    database_path = Path(database_path)
    private_root = Path(private_root)
    workspace = None
    document = None
    try:
        with sqlite3.connect(database_path) as connection:
            job = _load_job(connection, job_id)
            content = _read_archived_pdf(
                private_root, job["stored_path"], job["file_size"]
            )
            if _sha256_bytes(content) != job["sha256"]:
                raise PageRenderError(SAFE_SOURCE_ERROR)
            try:
                document = pymupdf.open(stream=content, filetype="pdf")
                source_page_count = document.page_count
            except Exception as error:
                raise PageRenderError(SAFE_SOURCE_ERROR) from error
            if source_page_count <= 0:
                raise PageRenderError(SAFE_SOURCE_ERROR)
            page_start = job["page_start"] if job["page_start"] is not None else 1
            page_end = job["page_end"] if job["page_end"] is not None else source_page_count
            if page_start < 1 or page_end < page_start or page_end > source_page_count:
                raise PageRenderError(SAFE_RANGE_ERROR)
            total = page_end - page_start + 1
            if total > MAX_RENDER_PAGES:
                raise PageRenderError(SAFE_LIMIT_ERROR)
            dimensions = []
            total_pixels = 0
            for number in range(page_start, page_end + 1):
                rectangle = document.load_page(number - 1).rect
                width = math.ceil(rectangle.width * dpi / 72)
                height = math.ceil(rectangle.height * dpi / 72)
                if width <= 0 or height <= 0 or width * height > MAX_PAGE_PIXELS:
                    raise PageRenderError(SAFE_LIMIT_ERROR)
                total_pixels += width * height
                if total_pixels > MAX_TOTAL_RENDER_PIXELS:
                    raise PageRenderError(SAFE_LIMIT_ERROR)
                dimensions.append((number, width, height))

            if job["render_status"] == "completed":
                anchors = (
                    job["manifest_sha256"], job["manifest_byte_size"],
                    job["published_batch_id"], job["source_pdf_sha256"],
                )
                if job["render_dpi"] != dpi or any(value is None for value in anchors):
                    raise PageRenderError(SAFE_EXISTING_ERROR)
                existing, existing_bytes = _load_valid_existing(
                    private_root, job_id, dpi, job["sha256"], source_page_count,
                    page_start, page_end, dimensions,
                )
                if (
                    len(existing_bytes) != job["manifest_byte_size"]
                    or _sha256_bytes(existing_bytes) != job["manifest_sha256"]
                    or job["source_pdf_sha256"] != job["sha256"]
                ):
                    raise PageRenderError(SAFE_EXISTING_ERROR)
                return existing

            try:
                workspace = RenderWorkspace.open(private_root, job_id)
                if _available_bytes(workspace.job_fd) < MIN_FREE_BYTES_AFTER_PAGE:
                    raise PageRenderError(SAFE_LIMIT_ERROR)
            except PageRenderError:
                raise
            except OSError as error:
                raise PageRenderError(SAFE_RENDER_ERROR) from error
            _set_total(connection, job_id, total)
            workspace.assert_attached()
            workspace.create_batch()
            entries = []
            output_bytes = 0
            matrix = pymupdf.Matrix(dpi / 72, dpi / 72)
            for rendered, (number, width, height) in enumerate(dimensions, start=1):
                pixmap = document.load_page(number - 1).get_pixmap(
                    matrix=matrix, alpha=False
                )
                if (pixmap.width, pixmap.height) != (width, height):
                    raise PageRenderError(SAFE_RENDER_ERROR)
                name = f"page_{number:03d}.png"
                png_content = pixmap.tobytes("png")
                output_bytes += len(png_content)
                if output_bytes > MAX_RENDER_OUTPUT_BYTES:
                    raise PageRenderError(SAFE_LIMIT_ERROR)
                if (
                    _available_bytes(workspace.job_fd) - len(png_content)
                    < MIN_FREE_BYTES_AFTER_PAGE
                ):
                    raise PageRenderError(SAFE_LIMIT_ERROR)
                _write_new_regular_at(workspace.pages_fd, name, png_content)
                byte_size, digest = _verify_png_at(
                    workspace.pages_fd, name, width, height
                )
                entries.append(
                    {
                        "page_number": number,
                        "relative_path": f"pages/page_{number:03d}.png",
                        "pixel_width": width,
                        "pixel_height": height,
                        "byte_size": byte_size,
                        "sha256": digest,
                    }
                )
                _set_progress(connection, job_id, rendered)
            manifest = {
                "version": MANIFEST_VERSION,
                "import_job_id": job_id,
                "dpi": dpi,
                "source_pdf_sha256": job["sha256"],
                "source_page_count": source_page_count,
                "page_start": page_start,
                "page_end": page_end,
                "page_count": total,
                "pages": entries,
            }
            manifest_content = (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            _write_new_regular_at(
                workspace.batch_fd, "render_manifest.json", manifest_content
            )
            saved_manifest = _read_regular_at(
                workspace.batch_fd, "render_manifest.json", max_bytes=10 * 1024 * 1024
            )
            if json.loads(saved_manifest.decode("utf-8")) != manifest:
                raise PageRenderError(SAFE_RENDER_ERROR)
            publish = _publish(workspace)
            try:
                workspace.assert_attached()
                if workspace.batch_name is None:
                    raise PageRenderError(SAFE_RENDER_ERROR)
                _mark_completed(
                    connection, job_id, total, manifest_content,
                    workspace.batch_name, job["sha256"],
                )
                workspace.assert_attached()
            except Exception:
                try:
                    _rollback_publish(publish)
                except OSError as rollback_error:
                    raise PageRenderError(SAFE_RENDER_ERROR) from rollback_error
                raise
            try:
                _finalize_publish(publish)
            except OSError:
                pass
            return manifest
    except PageRenderError as error:
        _mark_failed(database_path, job_id, str(error))
        raise
    except Exception as error:
        _mark_failed(database_path, job_id, SAFE_RENDER_ERROR)
        raise PageRenderError(SAFE_RENDER_ERROR) from error
    finally:
        if document is not None:
            document.close()
        if workspace is not None:
            workspace.close()
