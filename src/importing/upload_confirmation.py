"""Private, atomic staging for user-selected PDF import confirmation."""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import fcntl
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import pymupdf


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PDF_HEADER = b"%PDF-"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_FILENAME = "manifest.json"
MANIFEST_HMAC_KEY_FILENAME = ".upload_manifest_hmac.key"
SIGNED_MANIFEST_FIELDS = (
    "token",
    "original_filename",
    "stored_filename",
    "size",
    "sha256",
    "page_count",
)
MANIFEST_FIELDS = {*SIGNED_MANIFEST_FIELDS, "signature"}
PAGE_RANGE_PATTERN = re.compile(r"([1-9]\d*)(?:-([1-9]\d*))?\Z")


class UploadConfirmationError(ValueError):
    """A safe, user-facing upload confirmation failure."""


class InvalidUploadToken(UploadConfirmationError):
    """The supplied pending-upload token is invalid or unavailable."""


class StagedUploadChanged(UploadConfirmationError):
    """A staged file no longer matches its server-generated manifest."""


@dataclass(frozen=True)
class UploadManifest:
    token: str
    original_filename: str
    stored_filename: str
    size: int
    sha256: str
    page_count: int
    signature: str


@dataclass(frozen=True)
class ImportMetadata:
    paper_name: str
    region_code: str
    exam_year: int | None
    exam_type_code: str
    page_range: str


def _safe_original_filename(filename):
    normalized = str(filename or "").replace("\\", "/")
    name = PurePosixPath(normalized).name.strip()
    if not name or name in {".", ".."}:
        raise UploadConfirmationError("请选择 PDF 文件")
    if Path(name).suffix.lower() != ".pdf":
        raise UploadConfirmationError("文件扩展名必须为 .pdf")
    return name


def _pending_root(private_root):
    return Path(private_root) / "pending_uploads"


def _ensure_real_directory(path, *, create=False, parents=False):
    path = Path(path)
    try:
        if create:
            path.mkdir(parents=parents, exist_ok=True)
        mode = path.lstat().st_mode
    except OSError as error:
        raise UploadConfirmationError("暂存目录不可用，请重试") from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UploadConfirmationError("暂存目录不可用，请重试")
    return path


def _read_regular_file(path, *, max_bytes=None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("not a regular file")
            if max_bytes is not None and file_stat.st_size > max_bytes:
                raise OSError("file too large")
            chunks = []
            remaining = max_bytes
            while True:
                chunk_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining + 1)
                chunk = os.read(descriptor, chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise OSError("file too large")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise StagedUploadChanged("暂存文件校验失败，请重新上传") from error


def _load_or_create_hmac_key(private_root):
    root = _ensure_real_directory(private_root, create=True, parents=True)
    key_path = root / MANIFEST_HMAC_KEY_FILENAME

    def read_existing():
        try:
            mode = key_path.lstat().st_mode
        except OSError as error:
            raise UploadConfirmationError("上传签名密钥不可用") from error
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise UploadConfirmationError("上传签名密钥不可用")
        try:
            key = _read_regular_file(key_path, max_bytes=4096)
        except StagedUploadChanged as error:
            raise UploadConfirmationError("上传签名密钥不可用") from error
        if len(key) < 32:
            raise UploadConfirmationError("上传签名密钥不可用")
        return key

    try:
        key_path.lstat()
    except FileNotFoundError:
        temporary = root / f".{MANIFEST_HMAC_KEY_FILENAME}.{secrets.token_hex(16)}.tmp"
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            key = secrets.token_bytes(32)
            os.write(descriptor, key)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, key_path, follow_symlinks=False)
            except FileExistsError:
                pass
            directory_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as error:
            raise UploadConfirmationError("上传签名密钥不可用") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise UploadConfirmationError("上传签名密钥不可用") from error
    return read_existing()


def _canonical_manifest(data):
    payload = {field: data[field] for field in SIGNED_MANIFEST_FIELDS}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest_signature(key, data):
    return hmac.new(key, _canonical_manifest(data), hashlib.sha256).hexdigest()


@contextmanager
def pending_upload_operation(private_root, token):
    """Serialize confirm/cancel for one token; OS locks recover on process exit."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise InvalidUploadToken("无效或已失效的确认令牌")
    private_root = Path(private_root)
    lock_root = private_root / ".pending_upload_locks"
    try:
        private_root.mkdir(parents=True, exist_ok=True)
        lock_root.mkdir(exist_ok=True)
        if stat.S_ISLNK(lock_root.lstat().st_mode):
            raise OSError("symbolic link lock directory")
        descriptor = os.open(
            lock_root / f"{token}.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise InvalidUploadToken("无效或已失效的确认令牌") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _token_directory(private_root, token):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise InvalidUploadToken("无效或已失效的确认令牌")
    root = _ensure_real_directory(_pending_root(private_root))
    directory = root / token
    try:
        mode = directory.lstat().st_mode
    except OSError as error:
        raise InvalidUploadToken("无效或已失效的确认令牌") from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise InvalidUploadToken("无效或已失效的确认令牌")
    return directory


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _pdf_page_count(path):
    try:
        with pymupdf.open(path) as document:
            if not document.is_pdf or document.page_count < 1:
                raise UploadConfirmationError("PDF 文件没有可读取的页面")
            for page_number in range(document.page_count):
                document.load_page(page_number)
            return document.page_count
    except UploadConfirmationError:
        raise
    except Exception as error:
        raise UploadConfirmationError("PDF 文件已损坏或无法读取") from error


def _atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def stage_pdf_upload(upload, private_root):
    """Stream one explicit browser upload into a private pending directory."""
    original_filename = _safe_original_filename(getattr(upload, "filename", ""))
    token = secrets.token_hex(32)
    key = _load_or_create_hmac_key(private_root)
    pending_root = _ensure_real_directory(
        _pending_root(private_root), create=True, parents=True
    )
    directory = pending_root / token
    temporary_path = directory / ".upload.tmp"
    stored_filename = original_filename
    stored_path = directory / stored_filename
    digest = hashlib.sha256()
    size = 0
    try:
        directory.mkdir(parents=True, exist_ok=False)
        with temporary_path.open("xb") as target:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UploadConfirmationError("PDF 文件不能超过 50 MiB")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if size == 0:
            raise UploadConfirmationError("PDF 文件不能为空")
        with temporary_path.open("rb") as source:
            if source.read(len(PDF_HEADER)) != PDF_HEADER:
                raise UploadConfirmationError("文件内容不是 PDF")
        os.replace(temporary_path, stored_path)
        page_count = _pdf_page_count(stored_path)
        manifest_values = {
            "token": token,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "size": size,
            "sha256": digest.hexdigest(),
            "page_count": page_count,
        }
        manifest = UploadManifest(
            **manifest_values,
            signature=_manifest_signature(key, manifest_values),
        )
        _atomic_json(directory / MANIFEST_FILENAME, asdict(manifest))
        return manifest
    except UploadConfirmationError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise UploadConfirmationError("暂存 PDF 失败，请重试") from error


def load_verified_upload(private_root, token):
    """Load a manifest and re-check its fixed-name file before confirmation."""
    directory = _token_directory(private_root, token)
    try:
        manifest_bytes = _read_regular_file(directory / MANIFEST_FILENAME, max_bytes=64 * 1024)
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (StagedUploadChanged, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidUploadToken("无效或已失效的确认令牌") from error
    if not isinstance(data, dict) or set(data) != MANIFEST_FIELDS:
        raise StagedUploadChanged("暂存文件校验失败，请重新上传")
    fields_have_valid_types = (
        isinstance(data["token"], str)
        and isinstance(data["original_filename"], str)
        and isinstance(data["stored_filename"], str)
        and isinstance(data["size"], int)
        and not isinstance(data["size"], bool)
        and isinstance(data["sha256"], str)
        and isinstance(data["page_count"], int)
        and not isinstance(data["page_count"], bool)
        and isinstance(data["signature"], str)
    )
    if not fields_have_valid_types:
        raise StagedUploadChanged("暂存文件校验失败，请重新上传")
    key = _load_or_create_hmac_key(private_root)
    expected_signature = _manifest_signature(key, data)
    if not hmac.compare_digest(data["signature"], expected_signature):
        raise StagedUploadChanged("暂存文件校验失败，请重新上传")
    manifest = UploadManifest(**data)
    if (
        manifest.token != token
        or manifest.stored_filename != _safe_original_filename(manifest.original_filename)
        or not TOKEN_PATTERN.fullmatch(manifest.sha256)
        or manifest.size < 1
        or manifest.page_count < 1
    ):
        raise StagedUploadChanged("暂存文件校验失败，请重新上传")
    stored_path = directory / manifest.stored_filename
    try:
        pdf_bytes = _read_regular_file(stored_path, max_bytes=MAX_UPLOAD_BYTES)
        size = len(pdf_bytes)
        digest = hashlib.sha256(pdf_bytes).hexdigest()
    except StagedUploadChanged:
        raise
    if size != manifest.size or digest != manifest.sha256:
        raise StagedUploadChanged("暂存文件已发生变化，请重新上传")
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            if not document.is_pdf or document.page_count < 1:
                raise StagedUploadChanged("暂存文件页数校验失败，请重新上传")
            page_count = document.page_count
            for page_number in range(page_count):
                document.load_page(page_number)
    except StagedUploadChanged:
        raise
    except Exception as error:
        raise StagedUploadChanged("暂存文件页数校验失败，请重新上传") from error
    if page_count != manifest.page_count:
        raise StagedUploadChanged("暂存文件页数校验失败，请重新上传")
    return manifest, stored_path


def validate_import_metadata(values, page_count):
    """Validate user-editable confirmation fields against the staged PDF."""
    paper_name = str(values.get("paper_name", "")).strip()
    region_code = str(values.get("region_code", ""))
    exam_type_code = str(values.get("exam_type_code", ""))
    year_text = str(values.get("exam_year", "")).strip()
    page_range = str(values.get("page_range", "")).strip()
    if not paper_name or len(paper_name) > 200:
        raise UploadConfirmationError("试卷名称不能为空且不能超过 200 个字符")
    if year_text:
        if not year_text.isascii() or not year_text.isdecimal():
            raise UploadConfirmationError("年份必须为空或为 1900 至 9999 的整数")
        exam_year = int(year_text)
        if not 1900 <= exam_year <= 9999:
            raise UploadConfirmationError("年份必须为空或为 1900 至 9999 的整数")
    else:
        exam_year = None
    match = PAGE_RANGE_PATTERN.fullmatch(page_range)
    if not match:
        raise UploadConfirmationError("页码范围格式无效，仅支持 1-6 或单页 3")
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if start > end:
        raise UploadConfirmationError("页码范围起始页不能大于结束页")
    if end > page_count:
        raise UploadConfirmationError(f"页码范围不能超过 PDF 实际页数 {page_count}")
    return ImportMetadata(
        paper_name=paper_name,
        region_code=region_code,
        exam_year=exam_year,
        exam_type_code=exam_type_code,
        page_range=page_range,
    )


def discard_staged_upload(private_root, token):
    """Remove exactly one validated pending-upload token directory."""
    directory = _token_directory(private_root, token)
    shutil.rmtree(directory, ignore_errors=True)
