"""Validate, deduplicate, and privately archive PDF source papers."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import DEFAULT_DATABASE_PATH, initialize_database


DEFAULT_PRIVATE_STORAGE_ROOT = PROJECT_ROOT / "data" / "private"
PDF_HEADER = b"%PDF-"
PAGE_RANGE_PATTERN = re.compile(r"([1-9]\d*)(?:-([1-9]\d*))?\Z")


class PdfIntakeError(ValueError):
    """A safe, user-facing intake failure."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_and_hash(source_path, temporary_path):
    digest = hashlib.sha256()
    with Path(source_path).open("rb") as source, Path(temporary_path).open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
            digest.update(chunk)
        target.flush()
        os.fsync(target.fileno())
    return digest.hexdigest()


def _parse_page_range(value):
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise PdfIntakeError("页码范围必须是字符串，例如 1-6 或 3")
    match = PAGE_RANGE_PATTERN.fullmatch(value)
    if not match:
        raise PdfIntakeError("页码范围格式无效，仅支持 1-6 或单页 3")
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if start > end:
        raise PdfIntakeError("页码范围起始页不能大于结束页")
    return start, end


def _validate_source(path):
    if not path.exists():
        raise PdfIntakeError("PDF 文件不存在")
    if not path.is_file():
        raise PdfIntakeError("PDF 路径必须是普通文件")
    if path.suffix.lower() != ".pdf":
        raise PdfIntakeError("文件扩展名必须为 .pdf")
    size = path.stat().st_size
    if size == 0:
        raise PdfIntakeError("PDF 文件不能为空")
    with path.open("rb") as source:
        if source.read(len(PDF_HEADER)) != PDF_HEADER:
            raise PdfIntakeError("文件头不是有效的 PDF 标识")
    return size


def _validate_dictionary(connection, table, code, label):
    if not isinstance(code, str) or not connection.execute(
        f"SELECT 1 FROM {table} WHERE code = ? AND is_active = 1", (code,)
    ).fetchone():
        raise PdfIntakeError(f"未知或未启用的{label}代码：{code}")


def _safe_destination(storage_root, region_code, exam_year, exam_type_code, digest):
    year_part = str(exam_year) if exam_year is not None else "unknown"
    relative = Path("raw_papers") / region_code / year_part / (
        f"{year_part}-{region_code}-{exam_type_code}-{digest[:12]}.pdf"
    )
    root = storage_root.resolve()
    destination = (root / relative).resolve()
    if not destination.is_relative_to(root / "raw_papers"):
        raise PdfIntakeError("归档路径越界")
    return relative, destination


def intake_pdf(
    pdf_path,
    region_code,
    exam_year,
    exam_type_code,
    paper_name,
    page_range=None,
    database_path=DEFAULT_DATABASE_PATH,
    private_storage_root=DEFAULT_PRIVATE_STORAGE_ROOT,
    idempotency_key=None,
):
    """Archive one PDF and create a pending import job; return a JSON-safe dict."""
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 200
    ):
        raise PdfIntakeError("幂等键必须是 1 至 200 个字符的字符串")
    source_path = Path(pdf_path).expanduser()
    file_size = _validate_source(source_path)
    if exam_year is not None and (
        not isinstance(exam_year, int) or isinstance(exam_year, bool) or not 1900 <= exam_year <= 9999
    ):
        raise PdfIntakeError("年份必须为空或为 1900 至 9999 的整数")
    if not isinstance(paper_name, str) or not paper_name.strip():
        raise PdfIntakeError("试卷名称不能为空")
    page_start, page_end = _parse_page_range(page_range)
    digest = _sha256_file(source_path)
    storage_root = Path(private_storage_root).expanduser().resolve()
    relative_path, destination = _safe_destination(
        storage_root, region_code, exam_year, exam_type_code, digest
    )

    connection = initialize_database(database_path)
    temporary_path = None
    created_final = False
    try:
        _validate_dictionary(connection, "regions", region_code, "地区")
        _validate_dictionary(connection, "exam_types", exam_type_code, "考试类型")
        connection.execute("BEGIN IMMEDIATE")
        if idempotency_key is not None:
            receipt = connection.execute(
                """SELECT r.source_paper_id, r.import_job_id, p.stored_path,
                          p.sha256, j.status
                   FROM import_upload_receipts AS r
                   JOIN source_papers AS p ON p.id = r.source_paper_id
                   JOIN import_jobs AS j ON j.id = r.import_job_id
                   WHERE r.token = ?""",
                (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                connection.commit()
                return {
                    "source_paper_id": receipt[0],
                    "import_job_id": receipt[1],
                    "stored_path": receipt[2],
                    "sha256": receipt[3],
                    "deduplicated": True,
                    "status": receipt[4],
                }
        existing = connection.execute(
            "SELECT id, stored_path FROM source_papers WHERE sha256 = ?", (digest,)
        ).fetchone()
        deduplicated = existing is not None
        if existing:
            source_paper_id, stored_path = existing
            archived_path = (storage_root / stored_path).resolve()
            if not archived_path.is_relative_to(storage_root / "raw_papers"):
                raise PdfIntakeError("已有归档路径越界")
            if not archived_path.is_file():
                raise PdfIntakeError("已有归档文件缺失，未创建新任务")
            if _sha256_file(archived_path) != digest:
                raise PdfIntakeError("已有归档文件哈希不一致，未创建新任务")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".pdf-intake-", suffix=".tmp", dir=destination.parent
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
            copied_digest = _copy_and_hash(source_path, temporary_path)
            if copied_digest != digest:
                raise PdfIntakeError("复制后哈希不一致，归档已取消")
            if destination.exists():
                if _sha256_file(destination) != digest:
                    raise PdfIntakeError("目标归档文件已存在但哈希不一致")
                temporary_path.unlink()
                temporary_path = None
            else:
                os.replace(temporary_path, destination)
                temporary_path = None
                created_final = True
            stored_path = relative_path.as_posix()
            source_paper_id = connection.execute(
                """INSERT INTO source_papers
                   (sha256, file_size, original_filename, stored_path,
                    region_code, exam_year, exam_type_code, paper_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    digest, file_size, source_path.name, stored_path,
                    region_code, exam_year, exam_type_code, paper_name.strip(),
                ),
            ).lastrowid
        import_job_id = connection.execute(
            """INSERT INTO import_jobs (source_paper_id, page_start, page_end, status)
               VALUES (?, ?, ?, 'pending')""",
            (source_paper_id, page_start, page_end),
        ).lastrowid
        if idempotency_key is not None:
            connection.execute(
                """INSERT INTO import_upload_receipts
                   (token, source_paper_id, import_job_id) VALUES (?, ?, ?)""",
                (idempotency_key, source_paper_id, import_job_id),
            )
        connection.commit()
        return {
            "source_paper_id": source_paper_id,
            "import_job_id": import_job_id,
            "stored_path": stored_path,
            "sha256": digest,
            "deduplicated": deduplicated,
            "status": "pending",
        }
    except PdfIntakeError:
        connection.rollback()
        if created_final:
            destination.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error) as error:
        connection.rollback()
        if created_final:
            destination.unlink(missing_ok=True)
        raise PdfIntakeError(f"PDF 导入失败：{error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        connection.close()


def has_intake_receipt(database_path, idempotency_key):
    """Return whether a committed intake exists for an upload token."""
    connection = initialize_database(database_path)
    try:
        return connection.execute(
            "SELECT 1 FROM import_upload_receipts WHERE token = ?",
            (idempotency_key,),
        ).fetchone() is not None
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全归档 PDF 并创建导入任务")
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--region", required=True, dest="region_code")
    parser.add_argument("--year", type=int, dest="exam_year")
    parser.add_argument("--exam-type", required=True, dest="exam_type_code")
    parser.add_argument("--paper-name", required=True)
    parser.add_argument("--pages", dest="page_range")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--private-storage-root", type=Path, default=DEFAULT_PRIVATE_STORAGE_ROOT
    )
    args = parser.parse_args(argv)
    arguments = vars(args)
    arguments["database_path"] = arguments.pop("database")
    try:
        result = intake_pdf(**arguments)
    except PdfIntakeError as error:
        parser.exit(2, f"PDF 导入失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
