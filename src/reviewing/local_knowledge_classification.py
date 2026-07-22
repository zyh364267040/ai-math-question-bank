"""Explicit, local-only Ollama knowledge classification state machine."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

from src.reviewing.knowledge_classification import (
    KnowledgeClassificationAdoption,
    KnowledgeClassificationError,
    adopt_knowledge_classifications_in_connection,
)
from src.reviewing.candidate_review_ai import validate_ai_approval


SAFE_CLASSIFICATION_INPUT = "本地知识点分类输入或批准证据已变化"
SAFE_CLASSIFICATION_BUSY = "本地知识点分类正在处理，请稍后刷新"
SAFE_CLASSIFICATION_MODEL = "本地 Ollama 知识点分类失败，请确认服务可用后重试"
SAFE_CLASSIFICATION_STORAGE = "本地知识点分类结果保存失败，请重试"
MAX_MODEL_OUTPUT_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MODEL = "qwen2.5:14b"
STALE_AFTER = timedelta(minutes=15)
CONFIDENCES = {"low", "medium", "high"}
OUTPUT_CONSTRAINT = "字段名必须逐字使用，题号必须字符串。"
STAGE_SYSTEM_MESSAGES = {
    "level2": (
        "你是二级数学知识模块分类器。只根据题干独立初判所属二级模块，"
        "不得解题，不得补写题目。" + OUTPUT_CONSTRAINT
    ),
    "proposal": (
        "你是三级数学知识点初审分类器。请从每题给定的三级候选中独立提出"
        "主知识点和至多两个关联知识点。" + OUTPUT_CONSTRAINT
    ),
    "verifier": (
        "你是独立的三级数学知识点复核器。不得假定任何先前 proposal 正确；"
        "必须从题干重新分类，并主动寻找更合适的替代知识点。" + OUTPUT_CONSTRAINT
    ),
}
STAGE_USER_INSTRUCTIONS = {
    "level2": (
        "逐题选择一个二级代码，并说明直接分类依据。字段名必须逐字使用："
        "source_question_no、level2_code、confidence、reason；题号必须字符串。"
    ),
    "proposal": (
        "逐题独立给出首次三级分类建议，只能使用该题候选代码。字段名必须逐字使用："
        "source_question_no、primary_code、related_codes、confidence、reason；题号必须字符串。"
    ),
    "verifier": (
        "从题干重新完成三级分类并检查替代项，只能使用该题候选代码。字段名必须逐字使用："
        "source_question_no、primary_code、related_codes、confidence、reason；题号必须字符串。"
    ),
}
STAGE_OPTIONS = {
    "level2": {"temperature": 0, "seed": 2102},
    "proposal": {"temperature": 0, "seed": 3103},
    "verifier": {"temperature": 0, "seed": 4104},
}


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_COMMON_OUTPUT_PROPERTIES = {
    "source_question_no": {"type": "string"},
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    "reason": {"type": "string", "minLength": 1, "maxLength": 200},
}
_LEVEL2_ITEM_SCHEMA = _object_schema({
    "source_question_no": _COMMON_OUTPUT_PROPERTIES["source_question_no"],
    "level2_code": {"type": "string"},
    "confidence": _COMMON_OUTPUT_PROPERTIES["confidence"],
    "reason": _COMMON_OUTPUT_PROPERTIES["reason"],
})
_LEVEL3_ITEM_SCHEMA = _object_schema({
    "source_question_no": _COMMON_OUTPUT_PROPERTIES["source_question_no"],
    "primary_code": {"type": "string"},
    "related_codes": {
        "type": "array", "items": {"type": "string"},
        "maxItems": 2, "uniqueItems": True,
    },
    "confidence": _COMMON_OUTPUT_PROPERTIES["confidence"],
    "reason": _COMMON_OUTPUT_PROPERTIES["reason"],
})
STAGE_OUTPUT_SCHEMAS = {
    stage: _object_schema({
        "questions": {"type": "array", "items": item_schema},
    })
    for stage, item_schema in {
        "level2": _LEVEL2_ITEM_SCHEMA,
        "proposal": _LEVEL3_ITEM_SCHEMA,
        "verifier": _LEVEL3_ITEM_SCHEMA,
    }.items()
}


class KnowledgeClassificationRunError(RuntimeError):
    """A fixed, presentation-safe local classification failure."""


class _ClassificationClaimLost(RuntimeError):
    """Internal signal that another worker owns the durable claim."""


@dataclass(frozen=True)
class ClassificationClaim:
    database_path: Path
    private_root: Path
    job_id: int
    runner: Any
    claim_token: str
    input_digest: str
    taxonomy_digest: str
    questions: tuple[dict[str, Any], ...]
    taxonomy: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ClassificationPage:
    exists: bool
    status: str
    stage: str
    question_count: int
    processed: int
    auto_approved: int
    pending: int
    approved: int
    applied: bool
    completed_evidence: bool
    error_message: str | None
    drafts: tuple[dict[str, Any], ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fail(message: str = SAFE_CLASSIFICATION_INPUT) -> NoReturn:
    raise KnowledgeClassificationRunError(message)


def _decode_object(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT) from exc
    if not isinstance(value, dict):
        _fail()
    return value


def _read_bounded(
    path: str | Path, maximum: int = 16 * 1024 * 1024, *, directory_fd: int | None = None,
) -> bytes:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > maximum
        ):
            _fail()
        chunks = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                _fail()
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail()
        return b"".join(chunks)
    except (OSError, KnowledgeClassificationRunError) as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_nlink < 2:
        _fail()
    return metadata.st_dev, metadata.st_ino


@dataclass
class _TrustedJobDirectory:
    root_fd: int
    processing_fd: int
    job_fd: int
    job_name: str
    processing_identity: tuple[int, int]
    job_identity: tuple[int, int]

    def verify(self) -> None:
        processing = os.stat("processing", dir_fd=self.root_fd, follow_symlinks=False)
        job = os.stat(self.job_name, dir_fd=self.processing_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(processing.st_mode) or processing.st_nlink < 2
            or (processing.st_dev, processing.st_ino) != self.processing_identity
            or not stat.S_ISDIR(job.st_mode) or job.st_nlink < 2
            or (job.st_dev, job.st_ino) != self.job_identity
        ):
            _fail()

    def close(self) -> None:
        for descriptor in (self.job_fd, self.processing_fd, self.root_fd):
            os.close(descriptor)


def _open_trusted_job_directory(private_root: Path, job_id: int) -> _TrustedJobDirectory:
    descriptors: list[int] = []
    try:
        root_fd = os.open(private_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(root_fd)
        _directory_identity(root_fd)
        processing_fd = os.open(
            "processing", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd,
        )
        descriptors.append(processing_fd)
        processing_identity = _directory_identity(processing_fd)
        job_name = f"import_job_{int(job_id)}"
        job_fd = os.open(
            job_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=processing_fd,
        )
        descriptors.append(job_fd)
        job_identity = _directory_identity(job_fd)
        result = _TrustedJobDirectory(
            root_fd, processing_fd, job_fd, job_name,
            processing_identity, job_identity,
        )
        result.verify()
        return result
    except (OSError, KnowledgeClassificationRunError, ValueError) as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT) from exc


def _taxonomy(connection: sqlite3.Connection) -> tuple[tuple[dict[str, Any], ...], str]:
    rows = connection.execute(
        """SELECT p.code,p.name,p.level,parent.code AS parent_code,p.system_version
           FROM knowledge_points p LEFT JOIN knowledge_points parent ON parent.id=p.parent_id
           WHERE p.is_active=1 ORDER BY p.level,p.sort_order,p.code"""
    ).fetchall()
    taxonomy = tuple(dict(row) for row in rows)
    levels = {row["level"] for row in taxonomy}
    if not taxonomy or not {2, 3}.issubset(levels):
        _fail()
    return taxonomy, _digest(taxonomy)


def _authoritative_input(
    connection: sqlite3.Connection, private_root: Path, job_id: int,
    *, trusted_job: _TrustedJobDirectory | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], str, str]:
    job = connection.execute(
        "SELECT id FROM import_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if job is None:
        raise KnowledgeClassificationRunError("未找到导入任务")
    owned_job = trusted_job is None
    trusted_job = trusted_job or _open_trusted_job_directory(private_root, job_id)
    try:
        trusted_job.verify()
        candidate_raw = _read_bounded(
            "candidate_questions.json", directory_fd=trusted_job.job_fd
        )
        audit_raw = _read_bounded("ai_audit.json", directory_fd=trusted_job.job_fd)
        trusted_job.verify()
    finally:
        if owned_job:
            trusted_job.close()
    try:
        candidate = json.loads(candidate_raw)
        audit = json.loads(audit_raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT) from exc
    if not isinstance(candidate, dict) or not isinstance(audit, dict):
        _fail()
    questions = candidate.get("questions")
    audits = audit.get("questions")
    if (
        candidate.get("import_job_id") != job_id
        or not isinstance(questions, list) or not questions
        or candidate.get("question_count") != len(questions)
        or audit.get("import_job_id") != job_id
        or not isinstance(audits, list) or audit.get("question_count") != len(questions)
        or len(audits) != len(questions)
    ):
        _fail()
    numbers = [item.get("source_question_no") for item in questions if isinstance(item, dict)]
    audit_numbers = [item.get("source_question_no") for item in audits if isinstance(item, dict)]
    if (
        len(numbers) != len(questions) or len(numbers) != len(set(numbers))
        or set(numbers) != set(audit_numbers) or len(audit_numbers) != len(set(audit_numbers))
    ):
        _fail()
    anchor = connection.execute(
        """SELECT status,question_count,processed_questions,input_candidate_sha256,
                  input_candidate_byte_size,output_sha256,output_byte_size
           FROM import_candidate_audit_runs WHERE import_job_id=?""", (job_id,)
    ).fetchone()
    anchors = (
        hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw),
        hashlib.sha256(audit_raw).hexdigest(), len(audit_raw),
    )
    if (
        anchor is None or anchor["status"] != "completed"
        or anchor["question_count"] != len(questions)
        or anchor["processed_questions"] != len(questions)
        or tuple(anchor[3:7]) != anchors
    ):
        _fail()
    candidate_by_number = {item["source_question_no"]: item for item in questions}
    audit_by_number = {item["source_question_no"]: item for item in audits}
    drafts = connection.execute(
        """SELECT source_question_no,edited_json,status,version,approval_source,
                  approval_evidence_json,reviewed_at,deleted_at,import_job_id,
                  source_candidate_sha256,source_snapshot_json
           FROM candidate_review_drafts WHERE import_job_id=? ORDER BY id""", (job_id,)
    ).fetchall()
    if len(drafts) != len(questions) or {row["source_question_no"] for row in drafts} != set(numbers):
        _fail()
    prepared = []
    for row in drafts:
        if (
            row["deleted_at"] is not None or row["status"] != "approved"
            or row["approval_source"] not in {"human", "ai_second_pass"}
            or not row["reviewed_at"] or not row["approval_evidence_json"]
        ):
            _fail()
        item = dict(row)
        try:
            approval_evidence = json.loads(row["approval_evidence_json"])
            reviewed = datetime.fromisoformat(row["reviewed_at"])
            source_snapshot = json.loads(row["source_snapshot_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT) from exc
        number = row["source_question_no"]
        valid_human = bool(
            row["approval_source"] == "human"
            and reviewed.tzinfo is not None and reviewed.utcoffset() is not None
            and reviewed.astimezone(timezone.utc) <= datetime.now(timezone.utc) + timedelta(minutes=5)
            and isinstance(approval_evidence, dict)
            and set(approval_evidence) == {"method", "reviewed_at"}
            and approval_evidence.get("method") in {
                "workbench", "workbench_quick", "existing_approval",
            }
            and approval_evidence.get("reviewed_at") == row["reviewed_at"]
        )
        valid_ai = row["approval_source"] == "ai_second_pass" and validate_ai_approval(
            connection, item, candidate_by_number[number],
            candidate_sha256=anchors[0], audit_sha256=anchors[2],
            audit_entry=audit_by_number[number],
        )
        if (
            row["source_candidate_sha256"] != anchors[0]
            or source_snapshot != candidate_by_number[number]
            or not (valid_human or valid_ai)
        ):
            _fail()
        edited = _decode_object(row["edited_json"])
        prepared.append({
            "source_question_no": row["source_question_no"],
            "approved_draft_version": row["version"],
            "edited_sha256": _digest(edited),
            "approval_source": row["approval_source"],
            "approval_evidence_sha256": hashlib.sha256(
                row["approval_evidence_json"].encode("utf-8")
            ).hexdigest(),
            "edited": edited,
        })
    taxonomy, taxonomy_digest = _taxonomy(connection)
    input_digest = _digest({
        "job_id": job_id, "candidate_sha256": anchors[0], "audit_sha256": anchors[2],
        "drafts": [{key: item[key] for key in (
            "source_question_no", "approved_draft_version", "edited_sha256",
            "approval_source", "approval_evidence_sha256",
        )} for item in prepared],
        "taxonomy_digest": taxonomy_digest,
    })
    return tuple(prepared), taxonomy, input_digest, taxonomy_digest


class OllamaKnowledgeClassificationRunner:
    """Bounded HTTP client for the fixed local Ollama endpoint; never invokes a shell."""

    endpoint = "http://127.0.0.1:11434/api/chat"

    def __init__(self, model: str = MODEL, opener=None):
        self.model = model
        self._opener = opener or urllib.request.urlopen

    def run(self, stage: str, prompt: str) -> str:
        if stage not in {"level2", "proposal", "verifier"}:
            _fail(SAFE_CLASSIFICATION_MODEL)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            _fail(SAFE_CLASSIFICATION_MODEL)
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": STAGE_SYSTEM_MESSAGES[stage]},
                {
                    "role": "user",
                    "content": STAGE_USER_INSTRUCTIONS[stage] + "\n" + prompt,
                },
            ],
            "stream": False, "format": STAGE_OUTPUT_SCHEMAS[stage],
            "options": STAGE_OPTIONS[stage],
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener(request, timeout=120) as response:
                raw = response.read(MAX_MODEL_OUTPUT_BYTES + 1)
            if len(raw) > MAX_MODEL_OUTPUT_BYTES:
                _fail(SAFE_CLASSIFICATION_MODEL)
            envelope = json.loads(raw)
            content = envelope["message"]["content"]
            if not isinstance(content, str):
                _fail(SAFE_CLASSIFICATION_MODEL)
            return content
        except KnowledgeClassificationRunError:
            raise
        except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError,
                KeyError, TypeError, ValueError) as exc:
            raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_MODEL) from exc


def claim_knowledge_classification(
    database_path: str | Path, private_root: str | Path, job_id: int,
    *, runner=None,
) -> ClassificationClaim | None:
    database_path, private_root = Path(database_path), Path(private_root)
    if not isinstance(job_id, int) or job_id <= 0:
        _fail()
    try:
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM candidate_knowledge_classifications WHERE import_job_id=? LIMIT 1",
                (job_id,),
            ).fetchone():
                connection.commit()
                return None
            questions, taxonomy, input_digest, taxonomy_digest = _authoritative_input(
                connection, private_root, job_id
            )
            row = connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
            now = datetime.now(timezone.utc)
            if row is not None:
                if row["status"] == "completed" or row["applied_at"] is not None:
                    connection.commit()
                    return None
                if row["status"] == "processing":
                    try:
                        updated = datetime.fromisoformat(row["updated_at"])
                    except (TypeError, ValueError):
                        updated = now - STALE_AFTER - timedelta(seconds=1)
                    if updated.tzinfo is not None and now - updated.astimezone(timezone.utc) <= STALE_AFTER:
                        connection.commit()
                        return None
            token = secrets.token_hex(32)
            timestamp = now.isoformat(timespec="seconds")
            connection.execute(
                """INSERT INTO import_knowledge_classification_runs
                   (import_job_id,status,question_count,processed_questions,model,
                    input_digest,taxonomy_digest,claim_token,started_at,updated_at,stage)
                   VALUES(?,'processing',?,0,?,?,?,?,?,?,'waiting')
                   ON CONFLICT(import_job_id) DO UPDATE SET
                     status='processing',question_count=excluded.question_count,
                     processed_questions=0,model=excluded.model,
                     input_digest=excluded.input_digest,taxonomy_digest=excluded.taxonomy_digest,
                     output_sha256=NULL,output_byte_size=NULL,error_message=NULL,
                     claim_token=excluded.claim_token,started_at=excluded.started_at,
                     completed_at=NULL,updated_at=excluded.updated_at,stage='waiting'""",
                (job_id, len(questions), MODEL, input_digest, taxonomy_digest,
                 token, timestamp, timestamp),
            )
            connection.commit()
        return ClassificationClaim(
            database_path, private_root, job_id,
            runner or OllamaKnowledgeClassificationRunner(), token,
            input_digest, taxonomy_digest, questions, taxonomy,
        )
    except KnowledgeClassificationRunError:
        raise
    except sqlite3.Error as exc:
        if isinstance(exc, KnowledgeClassificationRunError):
            raise
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc


def _json_exact(raw: str, expected_numbers: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_MODEL_OUTPUT_BYTES:
        _fail(SAFE_CLASSIFICATION_MODEL)
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_MODEL) from exc
    if raw[end:].strip() or not isinstance(value, dict) or set(value) != {"questions"}:
        _fail(SAFE_CLASSIFICATION_MODEL)
    rows = value["questions"]
    if not isinstance(rows, list) or len(rows) != len(expected_numbers):
        _fail(SAFE_CLASSIFICATION_MODEL)
    numbers = [row.get("source_question_no") for row in rows if isinstance(row, dict)]
    if len(numbers) != len(rows) or set(numbers) != expected_numbers or len(numbers) != len(set(numbers)):
        _fail(SAFE_CLASSIFICATION_MODEL)
    return rows


def _parse_level2(raw: str, numbers: set[str], level2: set[str]) -> dict[str, dict]:
    result = {}
    keys = {"source_question_no", "level2_code", "confidence", "reason"}
    for row in _json_exact(raw, numbers):
        if (
            set(row) != keys or row["level2_code"] not in level2
            or row["confidence"] not in CONFIDENCES
            or not isinstance(row["reason"], str) or not 1 <= len(row["reason"]) <= 200
        ):
            _fail(SAFE_CLASSIFICATION_MODEL)
        result[row["source_question_no"]] = row
    return result


def _parse_level3(
    raw: str, numbers: set[str], allowed_by_number: dict[str, set[str]],
) -> dict[str, dict]:
    result = {}
    keys = {"source_question_no", "primary_code", "related_codes", "confidence", "reason"}
    for row in _json_exact(raw, numbers):
        related = row.get("related_codes")
        allowed = allowed_by_number[row["source_question_no"]]
        if (
            set(row) != keys or row["primary_code"] not in allowed
            or not isinstance(related, list) or len(related) > 2
            or any(not isinstance(code, str) or code not in allowed for code in related)
            or len(related) != len(set(related)) or row["primary_code"] in related
            or row["confidence"] not in CONFIDENCES
            or not isinstance(row["reason"], str) or not 1 <= len(row["reason"]) <= 200
        ):
            _fail(SAFE_CLASSIFICATION_MODEL)
        result[row["source_question_no"]] = row
    return result


def _prompt(stage: str, questions: object, taxonomy: object) -> str:
    common = (
        "只做知识点分类，不解题，不生成答案或解析，不修改题干。"
        "仅输出单个JSON对象，不得使用Markdown围栏或任何前后文本；"
        "字段名必须逐字使用、题号必须字符串，且题号必须精确、无重复。"
    )
    return _canonical({
        "instruction": common + STAGE_USER_INSTRUCTIONS[stage],
        "questions": questions,
        "taxonomy": taxonomy,
    })


@dataclass
class _PublishedOutput:
    sha256: str
    size: int
    directory_fd: int
    backup_name: str | None
    output_identity: tuple[int, int]

    def __iter__(self):
        yield self.sha256
        yield self.size

    def _output_is_ours(self) -> bool:
        try:
            metadata = os.stat(
                "knowledge_classification.json",
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
        except OSError:
            return False
        return (metadata.st_dev, metadata.st_ino) == self.output_identity

    def finalize(self) -> None:
        try:
            if self.backup_name is not None:
                os.unlink(self.backup_name, dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
        finally:
            os.close(self.directory_fd)

    def rollback(self) -> None:
        try:
            output_is_ours = self._output_is_ours()
            if output_is_ours:
                os.unlink("knowledge_classification.json", dir_fd=self.directory_fd)
            if output_is_ours and self.backup_name is not None:
                os.replace(
                    self.backup_name,
                    "knowledge_classification.json",
                    src_dir_fd=self.directory_fd,
                    dst_dir_fd=self.directory_fd,
                )
            # If another writer replaced our inode, preserve both its target and
            # our hidden backup for explicit recovery; never overwrite the winner.
            os.fsync(self.directory_fd)
        finally:
            os.close(self.directory_fd)


def _publish_output(
    job_dir: Path | int, content: bytes, *, retain_backup: bool = False,
) -> tuple[str, int] | _PublishedOutput:
    if not content or len(content) > MAX_MODEL_OUTPUT_BYTES:
        _fail(SAFE_CLASSIFICATION_STORAGE)
    directory_fd = None
    temporary_name = f".classification-{secrets.token_hex(16)}.tmp"
    backup_name = None
    try:
        directory_fd = (
            os.dup(job_dir) if isinstance(job_dir, int) else
            os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        )
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            _fail(SAFE_CLASSIFICATION_STORAGE)
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise OSError("short output write")
                view = view[written:]
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        try:
            existing_fd = os.open(
                "knowledge_classification.json",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                existing = os.fstat(existing_fd)
                if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                    _fail(SAFE_CLASSIFICATION_STORAGE)
                backup_name = f".classification-{secrets.token_hex(16)}.bak"
                os.replace(
                    "knowledge_classification.json",
                    backup_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                moved = os.stat(backup_name, dir_fd=directory_fd, follow_symlinks=False)
                if (moved.st_dev, moved.st_ino) != (existing.st_dev, existing.st_ino):
                    _fail(SAFE_CLASSIFICATION_STORAGE)
            finally:
                os.close(existing_fd)
        os.replace(
            temporary_name,
            "knowledge_classification.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        published = os.stat(
            "knowledge_classification.json", dir_fd=directory_fd, follow_symlinks=False
        )
        if not stat.S_ISREG(published.st_mode) or published.st_nlink != 1:
            _fail(SAFE_CLASSIFICATION_STORAGE)
        os.fsync(directory_fd)
        result = _PublishedOutput(
            hashlib.sha256(content).hexdigest(),
            len(content),
            directory_fd,
            backup_name,
            (published.st_dev, published.st_ino),
        )
        directory_fd = None
        if retain_backup:
            return result
        values = tuple(result)
        result.finalize()
        return values
    except (OSError, KnowledgeClassificationRunError) as exc:
        if directory_fd is not None:
            try:
                if temporary_name is not None:
                    os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            if backup_name is not None:
                try:
                    os.replace(
                        backup_name,
                        "knowledge_classification.json",
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                except OSError:
                    pass
        if isinstance(exc, KnowledgeClassificationRunError):
            raise
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _mark_failed(claim: ClassificationClaim, message: str) -> None:
    try:
        with closing(sqlite3.connect(claim.database_path)) as connection:
            connection.execute(
                """UPDATE import_knowledge_classification_runs
                   SET status='failed',error_message=?,claim_token=NULL,updated_at=?
                   WHERE import_job_id=? AND status='processing' AND claim_token=?""",
                (message, _now(), claim.job_id, claim.claim_token),
            )
            connection.commit()
    except sqlite3.Error:
        pass


def _heartbeat(claim: ClassificationClaim, stage: str) -> None:
    """Renew only the exact durable lease, failing closed after ownership changes."""
    try:
        with closing(sqlite3.connect(claim.database_path, timeout=10)) as connection:
            cursor = connection.execute(
                """UPDATE import_knowledge_classification_runs
                   SET stage=?,updated_at=?
                   WHERE import_job_id=? AND status='processing' AND claim_token=?""",
                (stage, _now(), claim.job_id, claim.claim_token),
            )
            if cursor.rowcount != 1:
                raise _ClassificationClaimLost
            connection.commit()
    except _ClassificationClaimLost:
        raise
    except sqlite3.Error as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc


def _run_model_stage(claim: ClassificationClaim, stage: str, prompt: str) -> str:
    _heartbeat(claim, stage)
    result = claim.runner.run(stage, prompt)
    _heartbeat(claim, stage)
    return result


def _acquire_global_lock(claim: ClassificationClaim, lock_fd: int) -> None:
    while True:
        _heartbeat(claim, "waiting")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        threading.Event().wait(1.0)


def _commit_completed_run(connection: sqlite3.Connection) -> None:
    """Named completion boundary so publication rollback covers commit failures."""
    connection.commit()


def run_claimed_knowledge_classification(claim: ClassificationClaim) -> None:
    """Run one claimed batch while serializing all local Ollama classification work."""
    publication = None
    database_committed = False
    trusted_job = None
    lock_fd = None
    try:
        trusted_job = _open_trusted_job_directory(claim.private_root, claim.job_id)
        lock_fd = os.open(
            ".ollama-knowledge-classification.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
            dir_fd=trusted_job.root_fd,
        )
        lock_metadata = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_nlink != 1:
            _fail(SAFE_CLASSIFICATION_STORAGE)
        _acquire_global_lock(claim, lock_fd)
        numbers = {item["source_question_no"] for item in claim.questions}
        level2_rows = [row for row in claim.taxonomy if row["level"] == 2]
        level2 = _parse_level2(
            _run_model_stage(
                claim,
                "level2",
                _prompt(
                    "level2",
                    claim.questions,
                    [{"code": row["code"], "name": row["name"]} for row in level2_rows],
                ),
            ),
            numbers, {row["code"] for row in level2_rows},
        )
        level3_rows = [row for row in claim.taxonomy if row["level"] == 3]
        allowed = {
            number: {row["code"] for row in level3_rows
                     if row["parent_code"] == level2_rows_item["level2_code"]}
            for number, level2_rows_item in level2.items()
        }
        scoped_questions = []
        for source in claim.questions:
            number = source["source_question_no"]
            scoped_questions.append({
                "source_question_no": number,
                "question": source["edited"],
                "level3_candidates": [
                    {"code": row["code"], "name": row["name"]}
                    for row in level3_rows if row["code"] in allowed[number]
                ],
            })
        proposal = _parse_level3(
            _run_model_stage(claim, "proposal", _prompt("proposal", scoped_questions, [])),
            numbers, allowed,
        )
        verifier = _parse_level3(
            _run_model_stage(claim, "verifier", _prompt("verifier", scoped_questions, [])),
            numbers, allowed,
        )
        now = _now()
        drafts = []
        for source in claim.questions:
            number = source["source_question_no"]
            first, second = proposal[number], verifier[number]
            automatic = (
                first["primary_code"] == second["primary_code"]
                and first["confidence"] == second["confidence"] == "high"
                and first["related_codes"] == second["related_codes"]
                and level2[number]["confidence"] == "high"
            )
            drafts.append({
                "source_question_no": number,
                "approved_draft_version": source["approved_draft_version"],
                "edited_sha256": source["edited_sha256"],
                "proposal": first, "verifier": second,
                "final_primary_code": first["primary_code"],
                "final_related_codes": first["related_codes"],
                "status": "approved" if automatic else "pending",
                "approval_source": "local_double_pass" if automatic else None,
                "reviewed_at": now if automatic else None,
            })
        output = _canonical({
            "version": 1, "import_job_id": claim.job_id, "model": MODEL,
            "input_digest": claim.input_digest, "taxonomy_digest": claim.taxonomy_digest,
            "questions": drafts,
        }).encode("utf-8")
        _heartbeat(claim, "publishing")
        trusted_job.verify()
        publication = _publish_output(
            trusted_job.job_fd, output,
            retain_backup=True,
        )
        output_sha, output_size = publication
        with closing(sqlite3.connect(claim.database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            current_questions, _, current_digest, current_taxonomy = _authoritative_input(
                connection, claim.private_root, claim.job_id, trusted_job=trusted_job,
            )
            trusted_job.verify()
            row = connection.execute(
                "SELECT status,claim_token,input_digest FROM import_knowledge_classification_runs WHERE import_job_id=?",
                (claim.job_id,),
            ).fetchone()
            if (
                row is None or row["status"] != "processing" or row["claim_token"] != claim.claim_token
                or row["input_digest"] != claim.input_digest
                or current_digest != claim.input_digest or current_taxonomy != claim.taxonomy_digest
                or len(current_questions) != len(drafts)
            ):
                _fail()
            connection.execute(
                "DELETE FROM candidate_knowledge_classification_drafts WHERE import_job_id=?",
                (claim.job_id,),
            )
            for item in drafts:
                connection.execute(
                    """INSERT INTO candidate_knowledge_classification_drafts
                       (import_job_id,source_question_no,approved_draft_version,edited_sha256,
                        proposal_primary_code,proposal_related_codes_json,proposal_confidence,
                        proposal_reason,verifier_primary_code,verifier_related_codes_json,
                        verifier_confidence,verifier_reason,final_primary_code,
                        final_related_codes_json,status,approval_source,reviewed_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        claim.job_id, item["source_question_no"], item["approved_draft_version"],
                        item["edited_sha256"], item["proposal"]["primary_code"],
                        _canonical(item["proposal"]["related_codes"]), item["proposal"]["confidence"],
                        item["proposal"]["reason"], item["verifier"]["primary_code"],
                        _canonical(item["verifier"]["related_codes"]), item["verifier"]["confidence"],
                        item["verifier"]["reason"], item["final_primary_code"],
                        _canonical(item["final_related_codes"]), item["status"],
                        item["approval_source"], item["reviewed_at"], now, now,
                    ),
                )
            connection.execute(
                """UPDATE import_knowledge_classification_runs
                   SET status='completed',processed_questions=question_count,
                       stage='review_ready',
                       output_sha256=?,output_byte_size=?,error_message=NULL,
                       claim_token=NULL,completed_at=?,updated_at=?
                   WHERE import_job_id=? AND claim_token=?""",
                (output_sha, output_size, now, now, claim.job_id, claim.claim_token),
            )
            _commit_completed_run(connection)
            database_committed = True
        publication.finalize()
        publication = None
    except _ClassificationClaimLost:
        if publication is not None and not database_committed:
            publication.rollback()
        return
    except KnowledgeClassificationRunError as exc:
        if publication is not None and not database_committed:
            publication.rollback()
        _mark_failed(claim, str(exc))
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
        if publication is not None and not database_committed:
            publication.rollback()
        _mark_failed(claim, SAFE_CLASSIFICATION_STORAGE)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if trusted_job is not None:
            trusted_job.close()


def _decode_codes(raw: object) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def load_classification_page(database_path: str | Path, job_id: int) -> ClassificationPage:
    """Read status and DB-backed UX data without creating rows, drafts, or files."""
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute("SELECT 1 FROM import_jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise KnowledgeClassificationRunError("未找到导入任务")
            run = connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?", (job_id,)
            ).fetchone()
            evidence_count = connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications WHERE import_job_id=?",
                (job_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT c.*,r.edited_json,k1.name AS final_name,k2.name AS proposal_name,
                          k3.name AS verifier_name
                   FROM candidate_knowledge_classification_drafts c
                   JOIN candidate_review_drafts r ON r.import_job_id=c.import_job_id
                     AND r.source_question_no=c.source_question_no
                   JOIN knowledge_points k1 ON k1.code=c.final_primary_code
                   JOIN knowledge_points k2 ON k2.code=c.proposal_primary_code
                   JOIN knowledge_points k3 ON k3.code=c.verifier_primary_code
                   WHERE c.import_job_id=?
                   ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END,
                            CAST(c.source_question_no AS INTEGER)""", (job_id,)
            ).fetchall()
            drafts = []
            for row in rows:
                item = dict(row)
                edited = _decode_object(item["edited_json"])
                item["stem_markdown"] = str(edited.get("stem_markdown", ""))[:500]
                for key in ("proposal_related_codes_json", "verifier_related_codes_json", "final_related_codes_json"):
                    item[key.removesuffix("_json")] = _decode_codes(item[key])
                drafts.append(item)
            pending = sum(item["status"] == "pending" for item in drafts)
            approved = sum(item["status"] == "approved" for item in drafts)
            auto = sum(item["approval_source"] == "local_double_pass" for item in drafts)
            if run is None:
                return ClassificationPage(
                    exists=False,
                    status="completed" if evidence_count else "pending",
                    stage="review_ready" if evidence_count else "waiting",
                    question_count=evidence_count,
                    processed=evidence_count,
                    auto_approved=0,
                    pending=0,
                    approved=evidence_count,
                    applied=bool(evidence_count),
                    completed_evidence=bool(evidence_count),
                    error_message=None,
                    drafts=(),
                )
            return ClassificationPage(
                exists=True,
                status=run["status"],
                stage=run["stage"],
                question_count=run["question_count"] or 0,
                processed=run["processed_questions"],
                auto_approved=auto,
                pending=pending,
                approved=approved,
                applied=run["applied_at"] is not None or bool(evidence_count),
                completed_evidence=bool(evidence_count),
                error_message=run["error_message"],
                drafts=tuple(drafts),
            )
    except KnowledgeClassificationRunError:
        raise
    except sqlite3.Error as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc


def _validate_final_codes(connection, primary_code: str, related_codes: list[str]) -> None:
    valid = {row[0] for row in connection.execute(
        "SELECT code FROM knowledge_points WHERE is_active=1 AND level=3"
    )}
    if (
        primary_code not in valid or not isinstance(related_codes, list) or len(related_codes) > 2
        or any(not isinstance(code, str) or code not in valid for code in related_codes)
        or len(related_codes) != len(set(related_codes)) or primary_code in related_codes
    ):
        _fail()


def review_classification_draft(
    database_path: str | Path, job_id: int, question_no: str, *, version: int,
    primary_code: str, related_codes: list[str], approve: bool = False,
) -> dict[str, Any]:
    try:
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status,applied_at FROM import_knowledge_classification_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM candidate_knowledge_classification_drafts WHERE import_job_id=? AND source_question_no=?",
                (job_id, question_no),
            ).fetchone()
            if run is None or run["status"] != "completed" or run["applied_at"] is not None or row is None:
                raise KnowledgeClassificationRunError("分类草稿不可修改，请刷新页面")
            if row["version"] != version:
                raise KnowledgeClassificationRunError("分类草稿版本冲突，请刷新后重试")
            _validate_final_codes(connection, primary_code, related_codes)
            original_related = _decode_codes(row["final_related_codes_json"])
            changed = primary_code != row["final_primary_code"] or related_codes != original_related
            status = (
                "approved"
                if approve or (row["status"] == "approved" and not changed)
                else "pending"
            )
            source = row["approval_source"]
            if status == "pending":
                source = None
            elif changed or (approve and row["status"] != "approved"):
                source = "human"
            human_note = row["human_review_note"]
            if changed or source == "human":
                disposition = (
                    f"教师复核：最终主知识点 {primary_code}"
                    if status == "approved"
                    else f"教师复核草稿：拟定主知识点 {primary_code}"
                )
                human_note = (
                    f"{disposition}；原始建议：{row['proposal_reason']}"
                )[:200]
            reviewed_at = _now() if status == "approved" else None
            cursor = connection.execute(
                """UPDATE candidate_knowledge_classification_drafts
                   SET final_primary_code=?,final_related_codes_json=?,status=?,approval_source=?,
                       human_review_note=?,reviewed_at=?,version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (primary_code, _canonical(related_codes), status, source,
                 human_note, reviewed_at, _now(), row["id"], version),
            )
            if cursor.rowcount != 1:
                raise KnowledgeClassificationRunError("分类草稿版本冲突，请刷新后重试")
            result = dict(connection.execute(
                "SELECT * FROM candidate_knowledge_classification_drafts WHERE id=?", (row["id"],)
            ).fetchone())
            connection.commit()
            return result
    except KnowledgeClassificationRunError:
        raise
    except sqlite3.Error as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc


def apply_classification_evidence(
    database_path: str | Path, private_root: str | Path, job_id: int,
) -> KnowledgeClassificationAdoption:
    trusted_job = None
    try:
        trusted_job = _open_trusted_job_directory(Path(private_root), job_id)
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
            if run is None or run["status"] != "completed":
                _fail()
            output = _read_bounded(
                "knowledge_classification.json",
                MAX_MODEL_OUTPUT_BYTES,
                directory_fd=trusted_job.job_fd,
            )
            trusted_job.verify()
            if (
                hashlib.sha256(output).hexdigest() != run["output_sha256"]
                or len(output) != run["output_byte_size"]
            ):
                _fail()
            _, _, input_digest, taxonomy_digest = _authoritative_input(
                connection, Path(private_root), job_id, trusted_job=trusted_job,
            )
            if input_digest != run["input_digest"] or taxonomy_digest != run["taxonomy_digest"]:
                _fail()
            rows = connection.execute(
                """SELECT * FROM candidate_knowledge_classification_drafts
                   WHERE import_job_id=? ORDER BY CAST(source_question_no AS INTEGER)""",
                (job_id,),
            ).fetchall()
            if len(rows) != run["question_count"] or any(row["status"] != "approved" for row in rows):
                _fail()
            payload = {
                "version": 1, "import_job_id": job_id,
                "source_classifier": f"ollama:{run['model']}",
                "reviewer": "mixed_local_review",
                "scope": "knowledge_only_no_solution", "question_count": len(rows),
                "questions": [{
                    "source_question_no": row["source_question_no"],
                    "primary_code": row["final_primary_code"],
                    "related_codes": _decode_codes(row["final_related_codes_json"]),
                    "reason": (
                        row["human_review_note"]
                        if row["approval_source"] == "human"
                        else row["proposal_reason"]
                    ),
                    "reviewer": (
                        "teacher_human_review"
                        if row["approval_source"] == "human"
                        else "local_double_pass"
                    ),
                    "approval_source": row["approval_source"],
                } for row in rows],
            }
            raw = _canonical(payload)
            result = adopt_knowledge_classifications_in_connection(
                connection, job_id, raw, f"local-kc-job-{job_id}-{run['input_digest'][:12]}"
            )
            if run["applied_at"] is None:
                connection.execute(
                    "UPDATE import_knowledge_classification_runs SET applied_at=?,updated_at=? WHERE import_job_id=?",
                    (_now(), _now(), job_id),
                )
            connection.commit()
            return result
    except (KnowledgeClassificationRunError, KnowledgeClassificationError):
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT)
    except sqlite3.Error as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc
    finally:
        if trusted_job is not None:
            trusted_job.close()
