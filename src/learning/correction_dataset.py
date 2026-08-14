"""Derive a bounded local correction dataset from completed import evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


SCHEMA_VERSION = 1
DATASET_RELATIVE_PATH = Path("learning/correction_samples.jsonl")
LOCK_NAME = ".correction_samples.lock"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_SAMPLE_BYTES = 256 * 1024
MAX_DATASET_BYTES = 32 * 1024 * 1024
MAX_SAMPLES = 10_000
TASK_TYPES = frozenset({
    "crop_review", "transcription_review", "knowledge_classification",
})
SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret)", re.I)
SECRET_VALUE = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+\S+)", re.I)
LOCAL_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"/(?:Users|home|private|tmp|var|Volumes|etc|opt)"
    r"(?:/[^\s\"'<> ,，;；。)\]}]+)*"
    r"|[A-Za-z]:[\\/][^\s\"'<> ,，;；。)\]}]+"
    r")"
)
REQUIRED_FIELDS = frozenset({
    "schema_version", "sample_id", "task_type", "job_id", "question_no",
    "source_refs", "model_input", "correction", "final_target", "provenance",
    "quality_gate", "created_at",
})


class CorrectionDatasetError(ValueError):
    """A correction dataset operation failed closed without changing business data."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sanitize(value: Any) -> Any:
    """Drop credential-shaped fields and redact absolute local paths recursively."""
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in value.items()
            if isinstance(key, str) and not SENSITIVE_KEY.search(key)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        if SECRET_VALUE.search(value):
            return "[redacted_secret]"
        sanitized = LOCAL_ABSOLUTE_PATH.sub("[redacted_absolute_path]", value)
        if sanitized != value:
            return sanitized
    return value


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev, details.st_ino, details.st_mode, details.st_nlink,
        details.st_size, details.st_mtime_ns, details.st_ctime_ns,
    )


def _same_file_object(left: os.stat_result, right_identity: tuple[int, ...]) -> bool:
    return (
        left.st_dev == right_identity[0]
        and left.st_ino == right_identity[1]
        and left.st_mode == right_identity[2]
        and left.st_nlink == right_identity[3]
        and left.st_size == right_identity[4]
        and left.st_mtime_ns == right_identity[5]
    )


def _canonical_absolute(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise CorrectionDatasetError(f"{label}包含路径穿越")
    absolute = path.absolute()
    # Darwin exposes these conventional top-level aliases as symlinks.  Normalize
    # only those platform-owned aliases before securely walking every component.
    parts = absolute.parts
    if len(parts) > 1 and parts[1] in {"var", "tmp", "etc"}:
        prefix = Path("/") / parts[1]
        try:
            if prefix.is_symlink():
                absolute = Path(os.path.realpath(prefix)).joinpath(*parts[2:])
        except OSError as exc:
            raise CorrectionDatasetError(f"{label}路径不安全") from exc
    return absolute


def _open_absolute_directory(
    value: str | Path, *, label: str, create: bool = False,
) -> tuple[Path, int]:
    absolute = _canonical_absolute(value, label=label)
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            details = os.fstat(child)
            if not stat.S_ISDIR(details.st_mode):
                os.close(child)
                raise OSError("unsafe directory")
            os.close(descriptor)
            descriptor = child
        return absolute, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child_directory(parent_fd: int, name: str, *, create: bool = False) -> int:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError("unsafe directory name")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        os.close(descriptor)
        raise OSError("unsafe directory")
    return descriptor


def _open_regular_at(
    directory_fd: int, name: str, *, maximum: int, allow_empty: bool = True,
) -> int:
    descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
            or details.st_size > maximum or (not allow_empty and details.st_size <= 0)
        ):
            raise OSError("unsafe regular file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_stable_descriptor(descriptor: int, *, maximum: int) -> tuple[bytes, tuple[int, ...]]:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
        or before.st_size < 0 or before.st_size > maximum
    ):
        raise CorrectionDatasetError("文件身份或大小无效")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise CorrectionDatasetError("文件超过大小限制")
    after = os.fstat(descriptor)
    if _identity(before) != _identity(after) or total != before.st_size:
        raise CorrectionDatasetError("文件读取期间身份发生变化")
    return b"".join(chunks), _identity(before)


def _validate_sample(sample: Any) -> dict[str, Any]:
    if not isinstance(sample, dict) or not REQUIRED_FIELDS <= set(sample):
        raise CorrectionDatasetError("纠错样本JSONL损坏：字段不完整")
    encoded = _canonical(sample)
    gate = sample.get("quality_gate")
    if (
        len(encoded) > MAX_SAMPLE_BYTES
        or sample.get("schema_version") != SCHEMA_VERSION
        or sample.get("task_type") not in TASK_TYPES
        or not isinstance(sample.get("sample_id"), str)
        or len(sample["sample_id"]) != 64
        or not isinstance(sample.get("job_id"), int)
        or not isinstance(sample.get("question_no"), str)
        or not isinstance(sample.get("source_refs"), list)
        or not isinstance(gate, dict)
        or not isinstance(gate.get("passed"), bool)
    ):
        raise CorrectionDatasetError("纠错样本JSONL损坏：结构无效")
    return sample


def _decode_dataset(raw: bytes) -> list[dict[str, Any]]:
    if len(raw) > MAX_DATASET_BYTES:
        raise CorrectionDatasetError("纠错样本文件超过大小限制")
    rows = []
    try:
        for line in raw.splitlines():
            if not line.strip():
                raise CorrectionDatasetError("纠错样本JSONL损坏：存在空行")
            if len(line) > MAX_SAMPLE_BYTES:
                raise CorrectionDatasetError("纠错样本JSONL损坏：单条过大")
            rows.append(_validate_sample(json.loads(line)))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CorrectionDatasetError("纠错样本JSONL损坏") from exc
    if len(rows) > MAX_SAMPLES or len({row["sample_id"] for row in rows}) != len(rows):
        raise CorrectionDatasetError("纠错样本JSONL损坏：数量或ID异常")
    return rows


def _read_dataset_at(directory_fd: int) -> tuple[list[dict[str, Any]], tuple[int, ...] | None]:
    try:
        descriptor = _open_regular_at(
            directory_fd, DATASET_RELATIVE_PATH.name, maximum=MAX_DATASET_BYTES,
        )
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        message = "纠错样本目标不得为符号链接且必须是大小限制内的独立普通文件"
        raise CorrectionDatasetError(message) from exc
    try:
        raw, identity = _read_stable_descriptor(descriptor, maximum=MAX_DATASET_BYTES)
    finally:
        os.close(descriptor)
    return _decode_dataset(raw), identity


def _safe_artifact(
    job_fd: int, job_name: str, relative: str, *, directory_fd: int | None = None,
) -> tuple[dict[str, Any], dict[str, str]] | None:
    relative_path = Path(relative)
    if (
        ".." in relative_path.parts or relative_path.is_absolute()
        or len(relative_path.parts) != (2 if directory_fd is not None else 1)
    ):
        raise CorrectionDatasetError("job工件路径不安全")
    parent_fd = directory_fd if directory_fd is not None else job_fd
    name = relative_path.name
    try:
        descriptor = _open_regular_at(
            parent_fd, name, maximum=MAX_ARTIFACT_BYTES, allow_empty=False,
        )
        try:
            raw, _ = _read_stable_descriptor(descriptor, maximum=MAX_ARTIFACT_BYTES)
        finally:
            os.close(descriptor)
        value = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, CorrectionDatasetError) as exc:
        raise CorrectionDatasetError("job工件缺失、不安全或损坏") from exc
    if not isinstance(value, dict):
        raise CorrectionDatasetError("job工件结构无效")
    return value, {"path": f"processing/{job_name}/{relative}", "sha256": _sha256(raw)}


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    path = _canonical_absolute(database_path, label="数据库")
    parent_fd = descriptor = None
    try:
        _, parent_fd = _open_absolute_directory(path.parent, label="数据库父目录")
        descriptor = _open_regular_at(
            parent_fd, path.name, maximum=(1 << 63) - 1, allow_empty=False,
        )
        before = _identity(os.fstat(descriptor))
        uri = f"file:{quote(f'/dev/fd/{descriptor}', safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        if _identity(os.fstat(descriptor)) != before:
            connection.close()
            raise CorrectionDatasetError("数据库打开期间身份发生变化")
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(named) != before:
            connection.close()
            raise CorrectionDatasetError("数据库目标在打开期间被替换")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except CorrectionDatasetError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise CorrectionDatasetError("数据库路径、链接或打开过程不安全") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _authoritative_rows(connection: sqlite3.Connection, job_id: int) -> dict[str, dict[str, Any]]:
    job = connection.execute(
        "SELECT status FROM import_jobs WHERE id=?", (job_id,),
    ).fetchone()
    if job is None:
        raise CorrectionDatasetError("job不存在")
    if job["status"] != "completed":
        raise CorrectionDatasetError("只允许采集completed job")
    rows = connection.execute(
        """SELECT s.source_question_no,q.question_code,q.stem_markdown,
                  q.answer_markdown,q.analysis_markdown,qt.code AS question_type_code,
                  kp.code AS primary_knowledge_point_code
           FROM question_sources s JOIN questions q ON q.id=s.question_id
           JOIN question_types qt ON qt.code=q.question_type_code
           JOIN knowledge_points kp ON kp.id=q.primary_knowledge_point_id
           WHERE s.import_job_id=? ORDER BY s.source_question_no""",
        (job_id,),
    ).fetchall()
    if not rows:
        raise CorrectionDatasetError("completed job没有最终权威题目")
    return {row["source_question_no"]: dict(row) for row in rows}


def _new_sample(
    task_type: str, job_id: int, question_no: str, source_refs: list[dict[str, str]],
    model_input: Any, correction: Any, final_target: Any, provenance: dict[str, Any],
    *, model_output: Any | None = None,
) -> dict[str, Any]:
    identity = _sanitize({
        "schema_version": SCHEMA_VERSION, "task_type": task_type, "job_id": job_id,
        "question_no": question_no, "source_refs": source_refs,
        "model_input": model_input, "model_output": model_output,
        "correction": correction, "final_target": final_target,
        "provenance": provenance,
    })
    sample = {
        **identity,
        "sample_id": _sha256(_canonical(identity)),
        "quality_gate": {
            "passed": True,
            "checks": ["job_completed", "formal_question_bound", "recorded_evidence_only"],
        },
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if model_output is None:
        sample.pop("model_output")
    return _validate_sample(sample)


def _transcription_samples(connection, job_id, authoritative, job_fd, job_name, stats):
    candidate_result = _safe_artifact(job_fd, job_name, "candidate_questions.json")
    audit_result = _safe_artifact(job_fd, job_name, "ai_audit.json")
    if candidate_result is None or audit_result is None:
        stats["skipped"]["transcription_missing_evidence"] = len(authoritative)
        return []
    candidate, candidate_ref = candidate_result
    audit, audit_ref = audit_result
    candidates = {str(item.get("source_question_no")): item for item in candidate.get("questions", []) if isinstance(item, dict)}
    audits = {str(item.get("source_question_no")): item for item in audit.get("questions", []) if isinstance(item, dict)}
    samples = []
    for number, final in authoritative.items():
        initial, review = candidates.get(number), audits.get(number)
        if not initial or not review:
            stats["skipped"]["transcription_missing_evidence"] += 1
            continue
        issues = review.get("issues")
        suggestions = review.get("suggested_corrections")
        if not isinstance(issues, list) or not isinstance(suggestions, list) or not (issues or suggestions):
            stats["skipped"]["transcription_no_recorded_correction"] += 1
            continue
        samples.append(_new_sample(
            "transcription_review", job_id, number, [candidate_ref, audit_ref],
            {"candidate": initial, "audit_issues": issues},
            {"suggested_corrections": suggestions},
            final,
            {"candidate_artifact": candidate_ref["sha256"], "audit_artifact": audit_ref["sha256"], "authority": "formal_database"},
            model_output=initial,
        ))
    return samples


def _knowledge_samples(connection, job_id, authoritative, stats):
    if not _table_exists(connection, "candidate_knowledge_classifications"):
        stats["skipped"]["knowledge_missing_evidence"] = len(authoritative)
        return []
    drafts = {}
    if _table_exists(connection, "candidate_knowledge_classification_drafts"):
        drafts = {row["source_question_no"]: dict(row) for row in connection.execute(
            "SELECT * FROM candidate_knowledge_classification_drafts WHERE import_job_id=?",
            (job_id,),
        )}
    rows = connection.execute(
        "SELECT * FROM candidate_knowledge_classifications WHERE import_job_id=? ORDER BY source_question_no",
        (job_id,),
    ).fetchall()
    samples = []
    for row_value in rows:
        row = dict(row_value)
        number = row["source_question_no"]
        if number not in authoritative:
            stats["skipped"]["knowledge_not_formal"] += 1
            continue
        draft = drafts.get(number)
        model_output = None
        correction = {"reason": row["reason"], "approval_source": row.get("approval_source")}
        if draft:
            model_output = {
                "primary_code": draft["proposal_primary_code"],
                "related_codes": json.loads(draft["proposal_related_codes_json"]),
                "confidence": draft["proposal_confidence"],
                "reason": draft["proposal_reason"],
            }
            correction["verifier"] = {
                "primary_code": draft["verifier_primary_code"],
                "related_codes": json.loads(draft["verifier_related_codes_json"]),
                "confidence": draft["verifier_confidence"],
                "reason": draft["verifier_reason"],
            }
            if draft.get("adjudicator_primary_code"):
                correction["adjudicator"] = {
                    "primary_code": draft["adjudicator_primary_code"],
                    "related_codes": json.loads(draft["adjudicator_related_codes_json"]),
                    "confidence": draft["adjudicator_confidence"],
                    "reason": draft["adjudicator_reason"],
                }
        samples.append(_new_sample(
            "knowledge_classification", job_id, number,
            [{"database": "candidate_knowledge_classifications", "evidence_sha256": row["evidence_sha256"]}],
            {"stem_markdown": authoritative[number]["stem_markdown"], "question_type_code": authoritative[number]["question_type_code"]},
            correction,
            {"primary_code": row["primary_knowledge_point_code"], "related_codes": json.loads(row["related_knowledge_point_codes_json"])},
            {"classifier": row["classifier"], "reviewer": row["reviewer"], "approval_source": row.get("approval_source"), "authority": "bound_classification_record"},
            model_output=model_output,
        ))
    missing = len(set(authoritative) - {row["source_question_no"] for row in rows})
    stats["skipped"]["knowledge_missing_evidence"] += missing
    return samples


def _crop_samples(job_id, authoritative, job_fd, job_name, stats):
    final_result = _safe_artifact(job_fd, job_name, "crop_ai_review.json")
    try:
        frozen_fd = _open_child_directory(job_fd, "frozen_crop_reviews")
    except FileNotFoundError:
        stats["skipped"]["crop_no_reliable_transition"] = len(authoritative)
        return []
    except OSError as exc:
        raise CorrectionDatasetError("frozen_crop_reviews目录不安全") from exc
    if final_result is None:
        os.close(frozen_fd)
        stats["skipped"]["crop_no_reliable_transition"] = len(authoritative)
        return []
    final, final_ref = final_result
    final_by_number = {str(item.get("question_no")): item for item in final.get("questions", []) if isinstance(item, dict)}
    previous = {}
    previous_refs = {}
    try:
        for name in sorted(os.listdir(frozen_fd)):
            if not name.endswith(".json") or "/" in name or "\\" in name:
                continue
            result = _safe_artifact(
                job_fd, job_name, f"frozen_crop_reviews/{name}", directory_fd=frozen_fd,
            )
            if result is None:
                continue
            payload, ref = result
            for item in payload.get("questions", []):
                if isinstance(item, dict) and item.get("status") == "needs_recrop":
                    number = str(item.get("question_no"))
                    previous[number] = item
                    previous_refs[number] = ref
    finally:
        os.close(frozen_fd)
    samples = []
    for number in authoritative:
        old, new = previous.get(number), final_by_number.get(number)
        if not old or not new or new.get("status") != "ai_review_passed":
            stats["skipped"]["crop_no_reliable_transition"] += 1
            continue
        refs = [previous_refs[number], final_ref]
        samples.append(_new_sample(
            "crop_review", job_id, number, refs,
            {"crop_ref": f"processing/{job_name}/question_crops/Q{int(number):03d}.png", "warnings": old.get("warnings", [])},
            {"from": "needs_recrop", "to": "ai_review_passed"},
            {"status": "ai_review_passed", "warnings": new.get("warnings", [])},
            {"initial_reviewer_run_id": old.get("reviewer_run_id"), "final_reviewer_run_id": final.get("reviewer_run_id"), "authority": "completed_formal_question_and_recorded_crop_reviews"},
            model_output={"status": "needs_recrop", "warnings": old.get("warnings", [])},
        ))
    return samples


def _encoded_dataset(rows: list[dict[str, Any]]) -> bytes:
    encoded_rows = [_canonical(_validate_sample(row)) + b"\n" for row in rows]
    if len(rows) > MAX_SAMPLES or any(len(row) > MAX_SAMPLE_BYTES for row in encoded_rows):
        raise CorrectionDatasetError("纠错样本超过大小限制")
    payload = b"".join(encoded_rows)
    if len(payload) > MAX_DATASET_BYTES:
        raise CorrectionDatasetError("纠错样本文件超过总大小限制")
    return payload


def _verify_written_payload(descriptor: int, expected: bytes) -> None:
    actual, _ = _read_stable_descriptor(descriptor, maximum=MAX_DATASET_BYTES)
    if actual != expected or _sha256(actual) != _sha256(expected):
        raise CorrectionDatasetError("纠错样本写后验证失败")


def _verify_dataset_name(directory_fd: int, expected: tuple[int, ...] | None) -> None:
    try:
        details = os.stat(
            DATASET_RELATIVE_PATH.name, dir_fd=directory_fd, follow_symlinks=False,
        )
    except FileNotFoundError:
        if expected is not None:
            raise CorrectionDatasetError("纠错样本在替换前被删除")
        return
    if expected is None or _identity(details) != expected:
        raise CorrectionDatasetError("纠错样本在替换前被竞态替换")


def _write_dataset_at(
    directory_fd: int, rows: list[dict[str, Any]],
    expected_identity: tuple[int, ...] | None,
) -> None:
    payload = _encoded_dataset(rows)
    temporary = f".correction_samples.{secrets.token_hex(16)}.tmp"
    descriptor = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=directory_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        _verify_written_payload(descriptor, payload)
        temporary_identity = _identity(os.fstat(descriptor))
        if temporary_identity[3] != 1:
            raise CorrectionDatasetError("纠错样本临时文件存在硬链接")
        _verify_dataset_name(directory_fd, expected_identity)
        os.replace(
            temporary, DATASET_RELATIVE_PATH.name,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        published = True
        installed = os.stat(
            DATASET_RELATIVE_PATH.name, dir_fd=directory_fd, follow_symlinks=False,
        )
        if not _same_file_object(installed, temporary_identity) or installed.st_nlink != 1:
            raise CorrectionDatasetError("纠错样本原子替换验证失败")
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass


def _open_learning_directory(root_fd: int, *, create: bool) -> int:
    try:
        return _open_child_directory(root_fd, DATASET_RELATIVE_PATH.parent.name, create=create)
    except OSError as exc:
        raise CorrectionDatasetError("learning目录不安全或不可用") from exc


def _open_dataset_lock(learning_fd: int) -> int:
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(LOCK_NAME, flags, 0o600, dir_fd=learning_fd)
        except FileNotFoundError:
            lock_fd = os.open(
                LOCK_NAME, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=learning_fd,
            )
        details = os.fstat(lock_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            os.close(lock_fd)
            raise OSError("unsafe lock")
        return lock_fd
    except OSError as exc:
        raise CorrectionDatasetError("纠错样本锁不安全") from exc


def _verify_lock_name(learning_fd: int, lock_fd: int) -> None:
    try:
        named = os.stat(LOCK_NAME, dir_fd=learning_fd, follow_symlinks=False)
        opened = os.fstat(lock_fd)
    except OSError as exc:
        raise CorrectionDatasetError("纠错样本锁在等待期间被替换") from exc
    if (
        not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
        or _identity(named) != _identity(opened)
    ):
        raise CorrectionDatasetError("纠错样本锁在等待期间被替换")


def _collect_candidates(database_path, root_fd: int, job_id: int, stats):
    processing_fd = job_fd = None
    job_name = f"import_job_{job_id}"
    try:
        processing_fd = _open_child_directory(root_fd, "processing")
        job_fd = _open_child_directory(processing_fd, job_name)
        with closing(_readonly_connection(database_path)) as connection:
            authoritative = _authoritative_rows(connection, job_id)
            return [
                *_crop_samples(job_id, authoritative, job_fd, job_name, stats),
                *_transcription_samples(
                    connection, job_id, authoritative, job_fd, job_name, stats,
                ),
                *_knowledge_samples(connection, job_id, authoritative, stats),
            ]
    except CorrectionDatasetError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise CorrectionDatasetError("job工件目录、路径或权威证据不安全") from exc
    finally:
        if job_fd is not None:
            os.close(job_fd)
        if processing_fd is not None:
            os.close(processing_fd)


def harvest_completed_job(database_path, private_root, job_id: int) -> dict[str, Any]:
    """Append derived samples for one completed job; never mutate the database."""
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise CorrectionDatasetError("job_id无效")
    root_fd = learning_fd = lock_fd = None
    stats = {
        "job_id": job_id, "candidates": 0, "added": 0, "duplicates": 0,
        "by_task_type": {task: 0 for task in sorted(TASK_TYPES)},
        "skipped": {
            "crop_no_reliable_transition": 0,
            "transcription_missing_evidence": 0,
            "transcription_no_recorded_correction": 0,
            "knowledge_missing_evidence": 0,
            "knowledge_not_formal": 0,
        },
        "dataset": DATASET_RELATIVE_PATH.as_posix(),
    }
    try:
        _, root_fd = _open_absolute_directory(
            private_root, label="private_root", create=True,
        )
        candidates = _collect_candidates(database_path, root_fd, job_id, stats)
        stats["candidates"] = len(candidates)
        learning_fd = _open_learning_directory(root_fd, create=True)
        lock_fd = _open_dataset_lock(learning_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _verify_lock_name(learning_fd, lock_fd)
        existing, dataset_identity = _read_dataset_at(learning_fd)
        known = {row["sample_id"] for row in existing}
        additions = [row for row in candidates if row["sample_id"] not in known]
        stats["duplicates"] = len(candidates) - len(additions)
        if additions:
            _verify_lock_name(learning_fd, lock_fd)
            _write_dataset_at(
                learning_fd, [*existing, *additions], dataset_identity,
            )
        stats["added"] = len(additions)
        for row in additions:
            stats["by_task_type"][row["task_type"]] += 1
        return stats
    except CorrectionDatasetError:
        raise
    except OSError as exc:
        raise CorrectionDatasetError("纠错样本安全采集失败") from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if learning_fd is not None:
            os.close(learning_fd)
        if root_fd is not None:
            os.close(root_fd)


def load_few_shot_examples(
    private_root, *, task_type: str, limit: int = 3,
) -> list[dict[str, Any]]:
    """Return a stable, small, text/metadata-only set of quality-gated examples."""
    if task_type not in TASK_TYPES:
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise CorrectionDatasetError("limit必须在1到20之间")
    root_fd = learning_fd = None
    try:
        _, root_fd = _open_absolute_directory(
            private_root, label="private_root", create=True,
        )
        try:
            learning_fd = _open_child_directory(
                root_fd, DATASET_RELATIVE_PATH.parent.name,
            )
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise CorrectionDatasetError("learning目录不安全") from exc
        rows, _ = _read_dataset_at(learning_fd)
    except CorrectionDatasetError:
        raise
    except OSError as exc:
        raise CorrectionDatasetError("纠错样本安全读取失败") from exc
    finally:
        if learning_fd is not None:
            os.close(learning_fd)
        if root_fd is not None:
            os.close(root_fd)
    selected = [
        row for row in rows
        if row["task_type"] == task_type and row["quality_gate"].get("passed") is True
    ]
    return sorted(selected, key=lambda row: row["sample_id"])[:limit]


def _stats(private_root) -> dict[str, Any]:
    root_fd = learning_fd = None
    try:
        _, root_fd = _open_absolute_directory(
            private_root, label="private_root", create=True,
        )
        try:
            learning_fd = _open_child_directory(root_fd, DATASET_RELATIVE_PATH.parent.name)
        except FileNotFoundError:
            rows = []
        else:
            rows, _ = _read_dataset_at(learning_fd)
    except CorrectionDatasetError:
        raise
    except OSError as exc:
        raise CorrectionDatasetError("纠错样本统计读取失败") from exc
    finally:
        if learning_fd is not None:
            os.close(learning_fd)
        if root_fd is not None:
            os.close(root_fd)
    by_type = {task: 0 for task in sorted(TASK_TYPES)}
    for row in rows:
        if row["quality_gate"].get("passed"):
            by_type[row["task_type"]] += 1
    return {"samples": len(rows), "quality_passed_by_task_type": by_type, "dataset": DATASET_RELATIVE_PATH.as_posix()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    harvest = subparsers.add_parser("harvest")
    harvest.add_argument("--database", required=True)
    harvest.add_argument("--private-root", required=True)
    harvest.add_argument("--job-id", required=True, type=int)
    stats = subparsers.add_parser("stats")
    stats.add_argument("--private-root", required=True)
    export = subparsers.add_parser("export-few-shot")
    export.add_argument("--private-root", required=True)
    export.add_argument("--task-type", required=True, choices=sorted(TASK_TYPES))
    export.add_argument("--limit", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        if args.command == "harvest":
            result = harvest_completed_job(args.database, args.private_root, args.job_id)
        elif args.command == "stats":
            result = _stats(args.private_root)
        else:
            result = {"examples": load_few_shot_examples(
                args.private_root, task_type=args.task_type, limit=args.limit,
            )}
    except CorrectionDatasetError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
