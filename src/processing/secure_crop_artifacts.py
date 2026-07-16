"""Shared file-identity, locking, signing, and schema rules for crop artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from src.importing.upload_confirmation import (
    UploadConfirmationError,
    _load_or_create_hmac_key,
)


LOCK_FILENAME = ".crop_artifacts.lock"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
REVIEW_STATUSES = {
    "pending_ai_review",
    "ai_review_passed",
    "needs_fix",
    "needs_recrop",
}
TOP_LEVEL_KEYS = {
    "version", "import_job_id", "generation_id", "question_count",
    "source_pages", "questions", "signature",
}
SOURCE_KEYS = {"page_number", "relative_path", "pixel_width", "pixel_height", "sha256"}
QUESTION_KEYS = {
    "question_no", "regions", "composition", "output_relative_path", "width", "height",
    "byte_size", "sha256", "crop_status", "review_status", "warnings",
}
REGION_KEYS = {"page_number", "bbox"}


class SecureCropArtifactError(ValueError):
    """A crop artifact failed identity, signature, or schema validation."""


@dataclass(frozen=True)
class PinnedBytes:
    data: bytes
    sha256: str
    size: int
    mode: int
    mtime_ns: int
    identity: tuple[int, int, int, int, int]


@dataclass
class JobLock:
    path: Path
    descriptor: int


def _strict_int(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _safe_relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SecureCropArtifactError("工件路径无效")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise SecureCropArtifactError("工件路径不安全")
    return relative


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise OSError("not a directory")
    return descriptor


def open_directory_at(root_fd: int, relative: str) -> int:
    parts = _safe_relative(relative).parts
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = _open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_pinned_file_at(root_fd: int, relative: str, *, max_bytes: int) -> int:
    path = _safe_relative(relative)
    directory_fd = os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            child = _open_directory_at(directory_fd, part)
            os.close(directory_fd)
            directory_fd = child
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size < 0 or info.st_size > max_bytes):
            raise OSError("unsafe regular file identity")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_pinned_descriptor(descriptor: int, *, max_bytes: int) -> PinnedBytes:
    before = os.fstat(descriptor)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size < 0 or before.st_size > max_bytes):
        raise SecureCropArtifactError("文件身份或大小无效")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise SecureCropArtifactError("文件超过大小预算")
    after = os.fstat(descriptor)
    if _stat_identity(before) != _stat_identity(after) or total != before.st_size:
        raise SecureCropArtifactError("文件读取期间身份发生变化")
    data = b"".join(chunks)
    return PinnedBytes(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=total,
        mode=before.st_mode,
        mtime_ns=before.st_mtime_ns,
        identity=_stat_identity(before),
    )


def read_file_at(root_fd: int, relative: str, *, max_bytes: int) -> PinnedBytes:
    try:
        descriptor = open_pinned_file_at(root_fd, relative, max_bytes=max_bytes)
        try:
            return read_pinned_descriptor(descriptor, max_bytes=max_bytes)
        finally:
            os.close(descriptor)
    except SecureCropArtifactError:
        raise
    except OSError as error:
        raise SecureCropArtifactError("文件身份、链接或路径无效") from error


def write_file_at(root_fd: int, relative: str, data: bytes, *, mode: int = 0o600) -> None:
    path = _safe_relative(relative)
    directory_fd = os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            child = _open_directory_at(directory_fd, part)
            os.close(directory_fd)
            directory_fd = child
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path.name, flags, mode, dir_fd=directory_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def bounded_directory_names(descriptor: int, *, max_entries: int) -> list[str]:
    """Enumerate one directory without allowing unbounded list materialization."""
    if not _strict_int(max_entries):
        raise SecureCropArtifactError("目录项预算无效")
    names: list[str] = []
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    raise SecureCropArtifactError("目录项数量超出预算")
        return names
    except SecureCropArtifactError:
        raise
    except OSError as error:
        raise SecureCropArtifactError("目录无法安全枚举") from error


def canonical_payload(data: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in data.items() if key != "signature"}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sign_manifest(key: bytes, data: dict[str, Any]) -> dict[str, Any]:
    signed = dict(data)
    signed["signature"] = hmac.new(key, canonical_payload(signed), hashlib.sha256).hexdigest()
    return signed


def _validate_bbox(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(_strict_int(item, minimum=0) for item in value)
            and value[0] < value[2] and value[1] < value[3])


def validate_signed_manifest(data: Any, key: bytes, *, expected_job_id: int | None = None,
                             expected_question_nos: list[int] | None = None) -> dict[str, Any]:
    try:
        if not isinstance(data, dict) or set(data) != TOP_LEVEL_KEYS:
            raise TypeError
        signature = data["signature"]
        if not isinstance(signature, str) or not HEX_64.fullmatch(signature):
            raise TypeError
        expected = hmac.new(key, canonical_payload(data), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SecureCropArtifactError("question_crops manifest签名无效")
        if (not _strict_int(data["version"]) or data["version"] != 2
                or not _strict_int(data["import_job_id"])
                or (expected_job_id is not None and data["import_job_id"] != expected_job_id)
                or not isinstance(data["generation_id"], str)
                or not HEX_32.fullmatch(data["generation_id"])
                or not _strict_int(data["question_count"], minimum=0)
                or not isinstance(data["source_pages"], list)
                or not isinstance(data["questions"], list)
                or data["question_count"] != len(data["questions"])):
            raise TypeError
        source_numbers: list[int] = []
        for source in data["source_pages"]:
            if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
                raise TypeError
            number = source["page_number"]
            relative = source["relative_path"]
            if (not _strict_int(number) or number in source_numbers
                    or not _strict_int(source["pixel_width"])
                    or not _strict_int(source["pixel_height"])
                    or not isinstance(source["sha256"], str)
                    or not HEX_64.fullmatch(source["sha256"])):
                raise TypeError
            rel = _safe_relative(relative)
            if rel.parts[:1] != ("pages",) or rel.suffix.lower() != ".png":
                raise TypeError
            source_numbers.append(number)
        if source_numbers != sorted(source_numbers):
            raise TypeError
        page_map = {item["page_number"]: item for item in data["source_pages"]}
        question_numbers: list[int] = []
        for question in data["questions"]:
            if not isinstance(question, dict) or set(question) != QUESTION_KEYS:
                raise TypeError
            number = question["question_no"]
            if not _strict_int(number) or number in question_numbers:
                raise TypeError
            expected_output = f"question_crops/Q{number:03d}.png"
            if question["output_relative_path"] != expected_output:
                raise TypeError
            if (not _strict_int(question["width"]) or not _strict_int(question["height"])
                    or not _strict_int(question["byte_size"])
                    or not isinstance(question["sha256"], str)
                    or not HEX_64.fullmatch(question["sha256"])
                    or question["crop_status"] != "generated"
                    or question["review_status"] not in REVIEW_STATUSES
                    or not isinstance(question["warnings"], list)
                    or not all(isinstance(item, str) for item in question["warnings"])
                    or not isinstance(question["regions"], list) or not question["regions"]):
                raise TypeError
            for region in question["regions"]:
                if (not isinstance(region, dict) or set(region) != REGION_KEYS
                        or not _strict_int(region["page_number"])
                        or region["page_number"] not in page_map
                        or not _validate_bbox(region["bbox"])):
                    raise TypeError
                page = page_map[region["page_number"]]
                if (region["bbox"][2] > page["pixel_width"]
                        or region["bbox"][3] > page["pixel_height"]):
                    raise TypeError
            composition = question["composition"]
            if len(question["regions"]) == 1:
                if (not isinstance(composition, dict)
                        or set(composition) != {"mode", "region_count"}
                        or composition != {"mode": "single", "region_count": 1}):
                    raise TypeError
            else:
                if (not isinstance(composition, dict)
                        or set(composition) != {
                            "mode", "separator_height", "background", "region_count",
                        }
                        or composition.get("mode") != "vertical"
                        or not _strict_int(composition.get("separator_height"), minimum=0)
                        or composition.get("background") != "white"
                        or composition.get("region_count") != len(question["regions"])):
                    raise TypeError
            question_numbers.append(number)
        if question_numbers != sorted(question_numbers):
            raise TypeError
        if expected_question_nos is not None and question_numbers != expected_question_nos:
            raise TypeError
        return data
    except SecureCropArtifactError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SecureCropArtifactError("question_crops manifest schema无效") from error


def load_hmac_key(job_path: Path) -> bytes:
    private_root = job_path.parent.parent
    try:
        return _load_or_create_hmac_key(private_root)
    except UploadConfirmationError as error:
        raise SecureCropArtifactError("裁图签名密钥不可用") from error


@contextmanager
def locked_job(job_dir: Any) -> Iterator[JobLock]:
    supplied = Path(job_dir)
    try:
        if supplied.is_symlink() or supplied.parent.is_symlink():
            raise OSError("symbolic link job directory")
        path = supplied.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        job_fd = os.open(path, flags)
        mode = os.fstat(job_fd).st_mode
        if not stat.S_ISDIR(mode):
            raise OSError("not a job directory")
        lock_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(
                LOCK_FILENAME, lock_flags | os.O_CREAT, 0o600, dir_fd=job_fd,
            )
        except FileNotFoundError:
            # Darwin may report ENOENT when two O_CREAT|O_NOFOLLOW calls race.
            # The non-creating retry still rejects a symlink and pins the winner.
            lock_fd = os.open(LOCK_FILENAME, lock_flags, dir_fd=job_fd)
        lock_stat = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) != 0o600):
            raise OSError("unsafe lock file")
    except OSError as error:
        for descriptor in (locals().get("lock_fd"), locals().get("job_fd")):
            if descriptor is not None:
                os.close(descriptor)
        raise SecureCropArtifactError("job目录或共享锁不安全") from error
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield JobLock(path, job_fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(job_fd)
