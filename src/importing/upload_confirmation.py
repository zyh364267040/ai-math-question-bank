"""Private, atomic staging for user-selected PDF import confirmation."""

import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import pymupdf


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PDF_HEADER = b"%PDF-"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_FILENAME = "manifest.json"
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


def _token_directory(private_root, token):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise InvalidUploadToken("无效或已失效的确认令牌")
    root = _pending_root(private_root).resolve()
    directory = (root / token).resolve()
    if directory.parent != root:
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
    pending_root = _pending_root(private_root)
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
        manifest = UploadManifest(
            token=token,
            original_filename=original_filename,
            stored_filename=stored_filename,
            size=size,
            sha256=digest.hexdigest(),
            page_count=page_count,
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
        data = json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        manifest = UploadManifest(**data)
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as error:
        raise InvalidUploadToken("无效或已失效的确认令牌") from error
    fields_have_valid_types = (
        isinstance(manifest.token, str)
        and isinstance(manifest.original_filename, str)
        and isinstance(manifest.stored_filename, str)
        and isinstance(manifest.size, int)
        and not isinstance(manifest.size, bool)
        and isinstance(manifest.sha256, str)
        and isinstance(manifest.page_count, int)
        and not isinstance(manifest.page_count, bool)
    )
    if not fields_have_valid_types:
        raise StagedUploadChanged("暂存文件校验失败，请重新上传")
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
        size, digest = _hash_file(stored_path)
    except OSError as error:
        raise StagedUploadChanged("暂存文件校验失败，请重新上传") from error
    if size != manifest.size or digest != manifest.sha256:
        raise StagedUploadChanged("暂存文件已发生变化，请重新上传")
    if _pdf_page_count(stored_path) != manifest.page_count:
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
