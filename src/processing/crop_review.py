"""Safely persist a complete independent AI review of question crop images."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.processing.secure_crop_artifacts import (
    HEX_32,
    HEX_64,
    SecureCropArtifactError,
    canonical_payload,
    fsync_directory,
    load_hmac_key,
    locked_job,
    read_file_at,
    sign_manifest,
    validate_signed_manifest,
    write_file_at,
)


MANIFEST_NAME = "question_crops.json"
REVIEW_NAME = "crop_ai_review.json"
JOURNAL_NAME = ".crop_review.journal"
MANIFEST_BACKUP = ".crop_review.manifest.backup"
REVIEW_BACKUP = ".crop_review.evidence.backup"
MANIFEST_TEMP = ".crop_review.manifest.tmp"
REVIEW_TEMP = ".crop_review.evidence.tmp"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_REVIEW_BYTES = 2 * 1024 * 1024
MAX_CROP_BYTES = 64 * 1024 * 1024
MAX_TOTAL_CROP_BYTES = 256 * 1024 * 1024
MAX_QUESTIONS = 200
MAX_WARNINGS = 20
MAX_WARNING_LENGTH = 500
MAX_REVIEWER_RUN_ID = 200
REVIEWER_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
PAYLOAD_KEYS = {
    "version", "import_job_id", "input_generation_id", "input_manifest_sha256",
    "reviewer_run_id", "questions",
}
PAYLOAD_QUESTION_KEYS = {"question_no", "status", "warnings"}
EVIDENCE_KEYS = PAYLOAD_KEYS | {
    "reviewed_at", "output_manifest_sha256", "output_manifest_signature",
    "request_sha256", "signature",
}
ALLOWED_STATUSES = {"ai_review_passed", "needs_recrop"}
TRANSACTION_NAMES = {
    JOURNAL_NAME, MANIFEST_BACKUP, REVIEW_BACKUP, MANIFEST_TEMP, REVIEW_TEMP,
}


class CropReviewError(ValueError):
    """A safe, fail-closed crop review persistence failure."""


@dataclass(frozen=True)
class CropReviewResult:
    passed_count: int
    needs_recrop_count: int
    can_extract_candidates: bool
    manifest_sha256: str
    manifest_signature: str
    generation_id: str


def _strict_int(value: Any, *, minimum: int = 1, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _request_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _validate_payload(payload: Any) -> dict[str, Any]:
    try:
        if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
            raise TypeError
        if (
            payload["version"] != 1
            or not _strict_int(payload["import_job_id"])
            or not isinstance(payload["input_generation_id"], str)
            or not HEX_32.fullmatch(payload["input_generation_id"])
            or not isinstance(payload["input_manifest_sha256"], str)
            or not HEX_64.fullmatch(payload["input_manifest_sha256"])
            or not isinstance(payload["reviewer_run_id"], str)
            or not REVIEWER_RUN_ID.fullmatch(payload["reviewer_run_id"])
            or not isinstance(payload["questions"], list)
            or not 1 <= len(payload["questions"]) <= MAX_QUESTIONS
        ):
            raise TypeError
        numbers: list[int] = []
        for question in payload["questions"]:
            if not isinstance(question, dict) or set(question) != PAYLOAD_QUESTION_KEYS:
                raise TypeError
            number = question["question_no"]
            warnings = question["warnings"]
            if (
                not _strict_int(number, maximum=MAX_QUESTIONS)
                or number in numbers
                or question["status"] not in ALLOWED_STATUSES
                or not isinstance(warnings, list)
                or len(warnings) > MAX_WARNINGS
                or any(
                    not isinstance(item, str)
                    or not item.strip()
                    or len(item) > MAX_WARNING_LENGTH
                    for item in warnings
                )
            ):
                raise TypeError
            numbers.append(number)
        if numbers != sorted(numbers):
            raise TypeError
        return payload
    except (KeyError, TypeError, ValueError) as error:
        raise CropReviewError("完整题图审核payload无效") from error


def _parse_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise CropReviewError(f"{label}损坏") from error


def _validate_evidence(
    data: Any, key: bytes, *, expected_job_id: int, expected_generation_id: str,
    expected_output_sha256: str, expected_output_signature: str,
    expected_questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        if not isinstance(data, dict) or set(data) != EVIDENCE_KEYS:
            raise TypeError
        signature = data["signature"]
        expected_hmac = hmac.new(key, canonical_payload(data), hashlib.sha256).hexdigest()
        if (
            not isinstance(signature, str) or not HEX_64.fullmatch(signature)
            or not hmac.compare_digest(signature, expected_hmac)
            or data["version"] != 1
            or data["import_job_id"] != expected_job_id
            or data["input_generation_id"] != expected_generation_id
            or data["output_manifest_sha256"] != expected_output_sha256
            or data["output_manifest_signature"] != expected_output_signature
            or not isinstance(data["input_manifest_sha256"], str)
            or not HEX_64.fullmatch(data["input_manifest_sha256"])
            or not isinstance(data["request_sha256"], str)
            or not HEX_64.fullmatch(data["request_sha256"])
            or not isinstance(data["reviewed_at"], str)
            or not 20 <= len(data["reviewed_at"]) <= 40
        ):
            raise TypeError
        payload = {key_name: data[key_name] for key_name in PAYLOAD_KEYS}
        _validate_payload(payload)
        if _request_sha256(payload) != data["request_sha256"]:
            raise TypeError
        if expected_questions is not None and data["questions"] != expected_questions:
            raise TypeError
        return data
    except (KeyError, TypeError, ValueError) as error:
        raise CropReviewError("独立题图审核记录真实性校验失败") from error


def validate_current_crop_review(
    job_fd: int, key: bytes, manifest: dict[str, Any], manifest_sha256: str,
) -> dict[str, Any]:
    """Validate evidence against the current manifest and every recorded decision."""
    snapshot = read_file_at(job_fd, REVIEW_NAME, max_bytes=MAX_REVIEW_BYTES)
    expected_questions = [
        {
            "question_no": question["question_no"],
            "status": question["review_status"],
            "warnings": question["warnings"],
        }
        for question in manifest["questions"]
    ]
    return _validate_evidence(
        _parse_json(snapshot.data, "独立题图审核记录"), key,
        expected_job_id=manifest["import_job_id"],
        expected_generation_id=manifest["generation_id"],
        expected_output_sha256=manifest_sha256,
        expected_output_signature=manifest["signature"],
        expected_questions=expected_questions,
    )


def _database_row(database_path: Path, job_id: int) -> tuple[Any, ...] | None:
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            """SELECT j.status,s.status,s.question_count,s.crop_manifest_sha256,
                      s.crop_generation_id,s.crop_manifest_signature
               FROM import_jobs j JOIN import_question_split_runs s
                 ON s.import_job_id=j.id WHERE j.id=?""", (job_id,),
        ).fetchone()


def _unlink_optional(job_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=job_fd)
    except FileNotFoundError:
        pass


def _exists_regular(job_fd: int, name: str, *, max_bytes: int) -> bool:
    try:
        read_file_at(job_fd, name, max_bytes=max_bytes)
        return True
    except SecureCropArtifactError:
        try:
            os.stat(name, dir_fd=job_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        raise


def _rename(job_fd: int, source: str, destination: str) -> None:
    os.replace(source, destination, src_dir_fd=job_fd, dst_dir_fd=job_fd)


def _journal_payload(
    *, job_id: int, old_anchors: tuple[str, str, str],
    new_anchors: tuple[str, str, str], had_review: bool,
) -> dict[str, Any]:
    return {
        "version": 1,
        "import_job_id": job_id,
        "old_anchors": list(old_anchors),
        "new_anchors": list(new_anchors),
        "had_review": had_review,
    }


def _read_journal(job_fd: int, key: bytes) -> dict[str, Any] | None:
    try:
        snapshot = read_file_at(job_fd, JOURNAL_NAME, max_bytes=16 * 1024)
    except SecureCropArtifactError:
        try:
            os.stat(JOURNAL_NAME, dir_fd=job_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        raise CropReviewError("题图审核恢复日志不安全")
    data = _parse_json(snapshot.data, "题图审核恢复日志")
    try:
        if not isinstance(data, dict) or set(data) != {
            "version", "import_job_id", "old_anchors", "new_anchors", "had_review", "signature",
        }:
            raise TypeError
        expected = hmac.new(key, canonical_payload(data), hashlib.sha256).hexdigest()
        anchors = data["old_anchors"] + data["new_anchors"]
        if (
            data["version"] != 1 or not _strict_int(data["import_job_id"])
            or not isinstance(data["had_review"], bool)
            or not isinstance(data["old_anchors"], list) or len(data["old_anchors"]) != 3
            or not isinstance(data["new_anchors"], list) or len(data["new_anchors"]) != 3
            or any(not isinstance(value, str) for value in anchors)
            or not HEX_64.fullmatch(anchors[0]) or not HEX_32.fullmatch(anchors[1])
            or not HEX_64.fullmatch(anchors[2]) or not HEX_64.fullmatch(anchors[3])
            or not HEX_32.fullmatch(anchors[4]) or not HEX_64.fullmatch(anchors[5])
            or not isinstance(data["signature"], str)
            or not hmac.compare_digest(data["signature"], expected)
        ):
            raise TypeError
        return data
    except (KeyError, TypeError, ValueError) as error:
        raise CropReviewError("题图审核恢复日志真实性校验失败") from error


def _restore_old_files(job_fd: int, *, had_review: bool) -> None:
    _unlink_optional(job_fd, MANIFEST_TEMP)
    _unlink_optional(job_fd, REVIEW_TEMP)
    if _exists_regular(job_fd, MANIFEST_BACKUP, max_bytes=MAX_MANIFEST_BYTES):
        _unlink_optional(job_fd, MANIFEST_NAME)
        _rename(job_fd, MANIFEST_BACKUP, MANIFEST_NAME)
    if had_review:
        if _exists_regular(job_fd, REVIEW_BACKUP, max_bytes=MAX_REVIEW_BYTES):
            _unlink_optional(job_fd, REVIEW_NAME)
            _rename(job_fd, REVIEW_BACKUP, REVIEW_NAME)
    else:
        _unlink_optional(job_fd, REVIEW_NAME)
        _unlink_optional(job_fd, REVIEW_BACKUP)
    _unlink_optional(job_fd, JOURNAL_NAME)
    fsync_directory(job_fd)


def _finish_new_files(job_fd: int) -> None:
    for name in (MANIFEST_BACKUP, REVIEW_BACKUP, MANIFEST_TEMP, REVIEW_TEMP, JOURNAL_NAME):
        _unlink_optional(job_fd, name)
    fsync_directory(job_fd)


def _recover_if_needed(database_path: Path, job_fd: int, key: bytes, job_id: int) -> None:
    journal = _read_journal(job_fd, key)
    if journal is None:
        for name in TRANSACTION_NAMES - {JOURNAL_NAME}:
            try:
                os.stat(name, dir_fd=job_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise CropReviewError("发现无恢复日志的题图审核事务残留")
        return
    if journal["import_job_id"] != job_id:
        raise CropReviewError("题图审核恢复日志任务不匹配")
    row = _database_row(database_path, job_id)
    if row is None:
        raise CropReviewError("题图审核任务数据库绑定无效")
    current = tuple(row[3:6])
    old_anchors = tuple(journal["old_anchors"])
    new_anchors = tuple(journal["new_anchors"])
    if current == new_anchors:
        _finish_new_files(job_fd)
    elif current == old_anchors:
        _restore_old_files(job_fd, had_review=journal["had_review"])
    else:
        raise CropReviewError("题图审核文件与数据库锚点状态混合")


def _commit_database_anchors(
    database_path: Path, job_id: int, old_anchors: tuple[str, str, str],
    new_anchors: tuple[str, str, str], question_count: int,
) -> None:
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE import_question_split_runs
               SET crop_manifest_sha256=?,crop_generation_id=?,crop_manifest_signature=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE import_job_id=? AND status='completed' AND question_count=?
                 AND crop_manifest_sha256=? AND crop_generation_id=?
                 AND crop_manifest_signature=?""",
            (*new_anchors, job_id, question_count, *old_anchors),
        )
        if cursor.rowcount != 1:
            raise CropReviewError("题图审核输入已过期")
        connection.commit()


def _result(manifest: dict[str, Any], digest: str) -> CropReviewResult:
    passed = sum(
        question["review_status"] == "ai_review_passed"
        for question in manifest["questions"]
    )
    recrop = len(manifest["questions"]) - passed
    return CropReviewResult(
        passed, recrop, recrop == 0, digest, manifest["signature"],
        manifest["generation_id"],
    )


def record_crop_ai_review(database_path: Any, private_root: Any, payload: Any) -> CropReviewResult:
    """Persist one complete, ordered, independently produced review batch."""
    payload = _validate_payload(payload)
    database_path = Path(database_path)
    job_id = payload["import_job_id"]
    job_dir = Path(private_root) / "processing" / f"import_job_{job_id}"
    try:
        with locked_job(job_dir) as lock:
            key = load_hmac_key(lock.path)
            _recover_if_needed(database_path, lock.descriptor, key, job_id)
            row = _database_row(database_path, job_id)
            if (
                row is None or row[0] != "pending" or row[1] != "completed"
                or not _strict_int(row[2], maximum=MAX_QUESTIONS)
                or any(value is None for value in row[3:6])
            ):
                raise CropReviewError("仅可审核数据库绑定且已完成的当前切题结果")
            manifest_snapshot = read_file_at(
                lock.descriptor, MANIFEST_NAME, max_bytes=MAX_MANIFEST_BYTES,
            )
            manifest = validate_signed_manifest(
                _parse_json(manifest_snapshot.data, "question_crops manifest"), key,
                expected_job_id=job_id,
                expected_question_nos=list(range(1, row[2] + 1)),
            )
            current_anchors = (
                manifest_snapshot.sha256, manifest["generation_id"], manifest["signature"],
            )
            if current_anchors != tuple(row[3:6]):
                raise CropReviewError("当前切题manifest与数据库锚点不一致")
            total_crop_bytes = 0
            for question in manifest["questions"]:
                crop = read_file_at(
                    lock.descriptor, question["output_relative_path"],
                    max_bytes=MAX_CROP_BYTES,
                )
                total_crop_bytes += crop.size
                if (
                    total_crop_bytes > MAX_TOTAL_CROP_BYTES
                    or crop.size != question["byte_size"]
                    or crop.sha256 != question["sha256"]
                ):
                    raise CropReviewError("完整题图与签名manifest不一致")
            expected_numbers = [question["question_no"] for question in manifest["questions"]]
            if [question["question_no"] for question in payload["questions"]] != expected_numbers:
                raise CropReviewError("审核必须按manifest顺序完整覆盖全部题号")
            normalized_payload = json.loads(json.dumps(payload))
            for crop, decision in zip(
                manifest["questions"], normalized_payload["questions"], strict=True
            ):
                merged_warnings = list(dict.fromkeys([
                    *crop["warnings"], *decision["warnings"],
                ]))
                if len(merged_warnings) > MAX_WARNINGS:
                    raise CropReviewError("题图审核warnings超过安全上限")
                decision["warnings"] = merged_warnings
            request_digest = _request_sha256(normalized_payload)
            evidence_exists = _exists_regular(
                lock.descriptor, REVIEW_NAME, max_bytes=MAX_REVIEW_BYTES,
            )
            try:
                if not evidence_exists:
                    raise FileNotFoundError
                evidence = validate_current_crop_review(
                    lock.descriptor, key, manifest, manifest_snapshot.sha256,
                )
            except FileNotFoundError:
                evidence = None
            if evidence is not None and evidence["request_sha256"] == request_digest:
                return _result(manifest, manifest_snapshot.sha256)
            if (
                payload["input_generation_id"] != manifest["generation_id"]
                or payload["input_manifest_sha256"] != manifest_snapshot.sha256
            ):
                raise CropReviewError("题图审核输入generation或manifest摘要已过期")
            updated = json.loads(json.dumps(manifest))
            for crop, decision in zip(
                updated["questions"], normalized_payload["questions"], strict=True
            ):
                crop["review_status"] = decision["status"]
                crop["warnings"] = list(decision["warnings"])
            updated = sign_manifest(key, updated)
            manifest_content = _json_bytes(updated)
            manifest_digest = hashlib.sha256(manifest_content).hexdigest()
            evidence_data = {
                **normalized_payload,
                "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "output_manifest_sha256": manifest_digest,
                "output_manifest_signature": updated["signature"],
                "request_sha256": request_digest,
            }
            evidence_data = sign_manifest(key, evidence_data)
            evidence_content = _json_bytes(evidence_data)
            if len(manifest_content) > MAX_MANIFEST_BYTES or len(evidence_content) > MAX_REVIEW_BYTES:
                raise CropReviewError("题图审核输出超过大小预算")
            had_review = evidence is not None
            new_anchors = (manifest_digest, updated["generation_id"], updated["signature"])
            journal = sign_manifest(key, _journal_payload(
                job_id=job_id, old_anchors=current_anchors,
                new_anchors=new_anchors, had_review=had_review,
            ))
            write_file_at(lock.descriptor, MANIFEST_TEMP, manifest_content)
            write_file_at(lock.descriptor, REVIEW_TEMP, evidence_content)
            write_file_at(lock.descriptor, JOURNAL_NAME, _json_bytes(journal))
            _rename(lock.descriptor, MANIFEST_NAME, MANIFEST_BACKUP)
            if had_review:
                _rename(lock.descriptor, REVIEW_NAME, REVIEW_BACKUP)
            _rename(lock.descriptor, REVIEW_TEMP, REVIEW_NAME)
            _rename(lock.descriptor, MANIFEST_TEMP, MANIFEST_NAME)
            fsync_directory(lock.descriptor)
            try:
                _commit_database_anchors(
                    database_path, job_id, current_anchors, new_anchors, row[2],
                )
            except Exception:
                _restore_old_files(lock.descriptor, had_review=had_review)
                raise
            _finish_new_files(lock.descriptor)
            return _result(updated, manifest_digest)
    except CropReviewError:
        raise
    except (OSError, sqlite3.Error, SecureCropArtifactError) as error:
        raise CropReviewError("完整题图审核结果无法安全落盘") from error
