"""Safely render one explicitly confirmed import PDF into verified page PNGs."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
import fcntl
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

    def close(self) -> None:
        """Release the process lock; safe to call repeatedly."""
        if self.lock_stream is not None:
            try:
                fcntl.flock(self.lock_stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.lock_stream.close()
                self.lock_stream = None


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
        if not stat.S_ISREG(details.st_mode):
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


def _prepare_lock_stream(private_root: Path, job_id: int):
    """Open the fixed lock as a single-link regular file beneath safe directories."""
    root_fd = processing_fd = locks_fd = None
    lock_fd = None
    try:
        root_fd = _open_safe_directory(private_root)
        processing_fd = _open_or_create_directory(root_fd, "processing")
        locks_fd = _open_or_create_directory(processing_fd, ".render_locks")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_name = f"import_job_{job_id}.lock"
        try:
            lock_fd = os.open(lock_name, flags, 0o600, dir_fd=locks_fd)
        except FileNotFoundError:
            # macOS can transiently report ENOENT when two threads create the
            # same parent/file at once.  Retry only against the still-open,
            # O_NOFOLLOW-verified directory descriptor.
            lock_fd = os.open(lock_name, flags, 0o600, dir_fd=locks_fd)
        details = os.fstat(lock_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OSError("unsafe lock")
        stream = os.fdopen(lock_fd, "a+b")
        lock_fd = None
        return stream
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        for descriptor in (locks_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _prepare_job_directory(private_root: Path, job_id: int) -> Path:
    """Create/open the fixed job directory and reject linked formal outputs."""
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
        return private_root / "processing" / job_name
    finally:
        for descriptor in (job_fd, processing_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


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
    try:
        lock_stream = _prepare_lock_stream(private_root, job_id)
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_stream.close()
            return None
    except OSError as error:
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
                "SELECT status FROM import_page_render_runs WHERE import_job_id=?",
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
            elif run[0] != "completed":
                connection.execute(
                    """UPDATE import_page_render_runs
                       SET status='processing', dpi=?, total_pages=NULL,
                           rendered_pages=0, error_message=NULL, started_at=?,
                           completed_at=NULL, updated_at=?
                       WHERE import_job_id=?""",
                    (dpi, now, now, job_id),
                )
    except Exception:
        lock_stream.close()
        raise
    return RenderClaim(database_path, private_root, job_id, dpi, lock_stream)


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
                  r.dpi AS render_dpi
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
) -> dict:
    """Return an existing completed result only after validating every byte."""
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
                content = _read_regular_at(
                    pages_fd, name, max_bytes=200 * 1024 * 1024
                )
                byte_size, digest = _verify_png_bytes(content, width, height)
            except (OSError, PageRenderError, UnidentifiedImageError) as error:
                raise PageRenderError(SAFE_EXISTING_ERROR) from error
            if entry.get("byte_size") != byte_size or entry.get("sha256") != digest:
                raise PageRenderError(SAFE_EXISTING_ERROR)
        actual_names = set(os.listdir(pages_fd))
        if actual_names != expected_names:
            raise PageRenderError(SAFE_EXISTING_ERROR)
        for name in actual_names:
            details = os.stat(name, dir_fd=pages_fd, follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode):
                raise PageRenderError(SAFE_EXISTING_ERROR)
        return manifest
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


def _mark_completed(connection: sqlite3.Connection, job_id: int, total: int) -> None:
    """Commit completion only after the new formal pair is installed."""
    now = _utc_now()
    result = connection.execute(
        """UPDATE import_page_render_runs
           SET status='completed', rendered_pages=?, error_message=NULL,
               completed_at=?, updated_at=?
           WHERE import_job_id=? AND status='processing'""",
        (total, now, now, job_id),
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


def _verify_png(path: Path, width: int, height: int) -> tuple[int, str]:
    """Open a generated temporary PNG without following a replaced symlink."""
    parent_fd = None
    try:
        parent_fd = _open_safe_directory(path.parent)
        content = _read_regular_at(
            parent_fd, path.name, max_bytes=200 * 1024 * 1024
        )
        return _verify_png_bytes(content, width, height)
    except PageRenderError:
        raise
    except OSError as error:
        raise PageRenderError(SAFE_RENDER_ERROR) from error
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


@dataclass
class _PublishHandle:
    job_dir: Path
    backup_pages: Path
    backup_manifest: Path
    had_pages: bool
    had_manifest: bool
    pages_installed: bool = False
    manifest_installed: bool = False


def _rollback_publish(handle: _PublishHandle) -> None:
    """Remove the new pair and restore every previous formal output."""
    pages = handle.job_dir / "pages"
    manifest = handle.job_dir / "render_manifest.json"
    if handle.pages_installed and pages.exists():
        shutil.rmtree(pages)
    if handle.manifest_installed and manifest.exists():
        manifest.unlink()
    if handle.backup_pages.exists():
        os.replace(handle.backup_pages, pages)
    if handle.backup_manifest.exists():
        os.replace(handle.backup_manifest, manifest)
    shutil.rmtree(handle.backup_pages, ignore_errors=True)
    handle.backup_manifest.unlink(missing_ok=True)


def _finalize_publish(handle: _PublishHandle) -> None:
    """Delete retained backups after the database completion commit succeeds."""
    if handle.backup_pages.exists():
        shutil.rmtree(handle.backup_pages, ignore_errors=False)
    handle.backup_manifest.unlink(missing_ok=True)


def _publish(job_dir: Path, batch: Path) -> _PublishHandle:
    """Install a new pair while retaining old files for a later DB rollback."""
    pages = job_dir / "pages"
    manifest = job_dir / "render_manifest.json"
    backup_pages = job_dir / f".pages.backup.{batch.name}"
    backup_manifest = job_dir / f".manifest.backup.{batch.name}"
    handle = _PublishHandle(
        job_dir, backup_pages, backup_manifest, pages.exists(), manifest.exists()
    )
    try:
        if handle.had_pages:
            os.replace(pages, backup_pages)
        if handle.had_manifest:
            os.replace(manifest, backup_manifest)
        os.replace(batch / "pages", pages)
        handle.pages_installed = True
        os.replace(batch / "render_manifest.json", manifest)
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
    batch = None
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
                if job["render_dpi"] != dpi:
                    raise PageRenderError(SAFE_EXISTING_ERROR)
                return _load_valid_existing(
                    private_root, job_id, dpi, job["sha256"], source_page_count,
                    page_start, page_end, dimensions,
                )

            try:
                job_dir = _prepare_job_directory(private_root, job_id)
            except OSError as error:
                raise PageRenderError(SAFE_RENDER_ERROR) from error
            _set_total(connection, job_id, total)
            batch = Path(tempfile.mkdtemp(prefix=".render_batch.", dir=job_dir))
            pages_dir = batch / "pages"
            pages_dir.mkdir()
            entries = []
            output_bytes = 0
            matrix = pymupdf.Matrix(dpi / 72, dpi / 72)
            for rendered, (number, width, height) in enumerate(dimensions, start=1):
                pixmap = document.load_page(number - 1).get_pixmap(
                    matrix=matrix, alpha=False
                )
                if (pixmap.width, pixmap.height) != (width, height):
                    raise PageRenderError(SAFE_RENDER_ERROR)
                path = pages_dir / f"page_{number:03d}.png"
                png_content = pixmap.tobytes("png")
                output_bytes += len(png_content)
                if output_bytes > MAX_RENDER_OUTPUT_BYTES:
                    raise PageRenderError(SAFE_LIMIT_ERROR)
                path.write_bytes(png_content)
                byte_size, digest = _verify_png(path, width, height)
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
            manifest_path = batch / "render_manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise PageRenderError(SAFE_RENDER_ERROR)
            publish = _publish(job_dir, batch)
            try:
                _mark_completed(connection, job_id, total)
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
        if batch is not None:
            shutil.rmtree(batch, ignore_errors=True)
