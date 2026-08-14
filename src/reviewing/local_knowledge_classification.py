"""Explicit Codex CLI knowledge-classification state machine."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import threading
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
from src.reviewing.candidate_review_ai import (
    classification_scope_sha256,
    validate_ai_approval,
    validated_official_answer_overlay,
)


SAFE_CLASSIFICATION_INPUT = "Codex 知识点分类输入或批准证据已变化"
SAFE_CLASSIFICATION_BUSY = "Codex 知识点分类正在处理，请稍后刷新"
SAFE_CLASSIFICATION_MODEL = "Codex 知识点分类失败，请稍后重试"
SAFE_CLASSIFICATION_STORAGE = "Codex 知识点分类结果保存失败，请重试"
SAFE_OFFICIAL_ANSWER_BINDING = "官方答案绑定证据不完整或已变化，请重新审核"
MAX_MODEL_OUTPUT_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MODEL = "codex-cli"
# Twenty-question taxonomy passes can legitimately take longer than two minutes.
# Keep a hard bound while allowing the independent verifier to finish.
CODEX_TIMEOUT_SECONDS = 300
STALE_AFTER = timedelta(minutes=15)
CONFIDENCES = {"low", "medium", "high"}
OUTPUT_CONSTRAINT = "字段名必须逐字使用，题号必须字符串。"
LEVEL3_RELATED_CODES_CONSTRAINT = (
    "related_codes必须是JSON数组；每个元素必须逐字等于一个候选代码；"
    "禁止在单个元素中用`、`、`,`、`/`、空格等拼接多个代码；最多2个；"
    "无关联时[]；不得重复primary_code。"
)
STAGE_SYSTEM_MESSAGES = {
    "level2": (
        "你是二级数学知识模块分类器。只根据题干独立初判所属二级模块，"
        "不得解题，不得补写题目。" + OUTPUT_CONSTRAINT
    ),
    "proposal": (
        "你是三级数学知识点初审分类器。请从每题给定的三级候选中独立提出"
        "主知识点和至多两个关联知识点。" + OUTPUT_CONSTRAINT
        + LEVEL3_RELATED_CODES_CONSTRAINT
    ),
    "verifier": (
        "你是独立的三级数学知识点复核器。不得假定任何先前 proposal 正确；"
        "必须从题干重新分类，并主动寻找更合适的替代知识点。" + OUTPUT_CONSTRAINT
        + LEVEL3_RELATED_CODES_CONSTRAINT
    ),
    "adjudicator": (
        "你是第三位独立的三级数学知识点仲裁分类器。必须从题干重新分类；"
        "不得接收、推测或复述 proposal/verifier 的答案、理由或选择。"
        + OUTPUT_CONSTRAINT
        + LEVEL3_RELATED_CODES_CONSTRAINT
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
    "adjudicator": (
        "仅对给出的待仲裁题独立分类，只能使用每题给出的原始三级候选代码。"
        "字段名必须逐字使用：source_question_no、primary_code、related_codes、"
        "confidence、reason；题号必须字符串。"
    ),
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
        # OpenAI structured outputs reject the JSON Schema ``uniqueItems``
        # keyword.  Duplicate codes are still rejected fail-closed by
        # ``_validate_codex_shape`` and ``_parse_level3`` below.
        "maxItems": 2,
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
        "adjudicator": _LEVEL3_ITEM_SCHEMA,
    }.items()
}


class KnowledgeClassificationRunError(RuntimeError):
    """A fixed, presentation-safe local classification failure."""


class _ClassificationClaimLost(RuntimeError):
    """Internal signal that another worker owns the durable claim."""


@dataclass
class _ArchivedClassificationGeneration:
    trusted_job: Any
    archive_fd: int
    final_name: str
    files: tuple[str, ...]

    def finalize(self) -> None:
        os.close(self.archive_fd)
        self.trusted_job.close()

    def rollback(self) -> None:
        final_fd = None
        try:
            final_fd = os.open(
                self.final_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.archive_fd,
            )
            os.replace(
                "knowledge_classification.json", "knowledge_classification.json",
                src_dir_fd=final_fd, dst_dir_fd=self.trusted_job.job_fd,
            )
            for name in self.files:
                if name != "knowledge_classification.json":
                    os.unlink(name, dir_fd=final_fd)
            os.fsync(self.trusted_job.job_fd)
        finally:
            if final_fd is not None:
                os.close(final_fd)
            try:
                os.rmdir(self.final_name, dir_fd=self.archive_fd)
                os.fsync(self.archive_fd)
            finally:
                os.close(self.archive_fd)
                self.trusted_job.close()


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
    replace_unapplied: bool = False
    previous_output_sha256: str | None = None
    previous_output_byte_size: int | None = None
    replacement_backup_name: str | None = None


@dataclass(frozen=True)
class ClassificationPage:
    exists: bool
    status: str
    stage: str
    question_count: int
    processed: int
    auto_approved: int
    double_approved: int
    adjudicated_approved: int
    pending: int
    approved: int
    applied: bool
    completed_evidence: bool
    can_replace: bool
    replacement_active: bool
    replacement_result: str | None
    replacement_completed_at: str | None
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
        try:
            overlay_prior = validated_official_answer_overlay(connection, item)
        except Exception as exc:
            raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_INPUT) from exc
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
            "classification_scope_sha256": classification_scope_sha256(edited),
            # Answer-only overlays retain classification binding to the prior draft.
            "classification_binding_version": (
                overlay_prior["version"] if overlay_prior is not None else row["version"]
            ),
            "classification_binding_edited_sha256": (
                _digest(_decode_object(overlay_prior["edited_json"]))
                if overlay_prior is not None else _digest(edited)
            ),
            "approval_source": row["approval_source"],
            "approval_evidence_sha256": hashlib.sha256(
                row["approval_evidence_json"].encode("utf-8")
            ).hexdigest(),
            "edited": edited,
        })
    taxonomy, taxonomy_digest = _taxonomy(connection)
    input_digest = _digest({
        "job_id": job_id, "candidate_sha256": anchors[0], "audit_sha256": anchors[2],
        "drafts": [{
            "source_question_no": item["source_question_no"],
            "approved_draft_version": item["classification_binding_version"],
            "edited_sha256": item["classification_binding_edited_sha256"],
            "approval_source": item["approval_source"],
            "approval_evidence_sha256": item["approval_evidence_sha256"],
        } for item in prepared],
        "taxonomy_digest": taxonomy_digest,
    })
    return tuple(prepared), taxonomy, input_digest, taxonomy_digest


def _validate_codex_shape(stage: str, raw: str) -> None:
    """Validate the schema-level shape before taxonomy-specific parsing."""
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_MODEL) from exc
    if raw[end:].strip() or not isinstance(value, dict) or set(value) != {"questions"}:
        _fail(SAFE_CLASSIFICATION_MODEL)
    rows = value["questions"]
    expected = (
        {"source_question_no", "level2_code", "confidence", "reason"}
        if stage == "level2"
        else {
            "source_question_no", "primary_code", "related_codes",
            "confidence", "reason",
        }
    )
    if not isinstance(rows, list):
        _fail(SAFE_CLASSIFICATION_MODEL)
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            _fail(SAFE_CLASSIFICATION_MODEL)
        related = row.get("related_codes", [])
        if (
            not isinstance(row["source_question_no"], str)
            or not isinstance(row.get("level2_code", row.get("primary_code")), str)
            or row["confidence"] not in CONFIDENCES
            or not isinstance(row["reason"], str)
            or not 1 <= len(row["reason"]) <= 200
            or not isinstance(related, list)
            or len(related) > 2
            or len(related) != len(set(related))
            or any(not isinstance(code, str) for code in related)
        ):
            _fail(SAFE_CLASSIFICATION_MODEL)


class CodexKnowledgeClassificationRunner:
    """Run every classification stage in a fresh bounded Codex CLI process."""

    def __init__(
        self, *, subprocess_run=subprocess.run, timeout: int = CODEX_TIMEOUT_SECONDS,
    ):
        self._subprocess_run = subprocess_run
        self._timeout = timeout

    def run(self, stage: str, prompt: str) -> str:
        if stage not in STAGE_OUTPUT_SCHEMAS or not isinstance(prompt, str):
            _fail(SAFE_CLASSIFICATION_MODEL)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            _fail(SAFE_CLASSIFICATION_MODEL)
        instruction = (
            STAGE_SYSTEM_MESSAGES[stage] + "\n"
            + STAGE_USER_INSTRUCTIONS[stage] + "\n" + prompt
        )
        if len(instruction.encode("utf-8")) > MAX_PROMPT_BYTES:
            _fail(SAFE_CLASSIFICATION_MODEL)
        try:
            with tempfile.TemporaryDirectory(prefix=f"codex-kc-{stage}-") as temporary:
                workdir = Path(temporary)
                schema_path = workdir / "output.schema.json"
                output_path = workdir / "output.json"
                descriptor = os.open(
                    schema_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                )
                try:
                    schema = _canonical(STAGE_OUTPUT_SCHEMAS[stage]).encode("utf-8")
                    if os.write(descriptor, schema) != len(schema):
                        _fail(SAFE_CLASSIFICATION_MODEL)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                command = [
                    "codex", "exec", "--ephemeral", "-s", "read-only",
                    "--output-schema", str(schema_path),
                    "-o", str(output_path), "-C", str(workdir),
                    "--skip-git-repo-check", "-",
                ]
                completed = self._subprocess_run(
                    command,
                    input=instruction,
                    text=True,
                    encoding="utf-8",
                    timeout=self._timeout,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if getattr(completed, "returncode", 0) != 0:
                    _fail(SAFE_CLASSIFICATION_MODEL)
                try:
                    raw = _read_bounded(output_path, MAX_MODEL_OUTPUT_BYTES).decode("utf-8")
                except (KnowledgeClassificationRunError, UnicodeError) as exc:
                    raise KnowledgeClassificationRunError(
                        SAFE_CLASSIFICATION_MODEL
                    ) from exc
                _validate_codex_shape(stage, raw)
                return raw
        except KnowledgeClassificationRunError:
            raise
        except (
            OSError, subprocess.SubprocessError, UnicodeError, TypeError, ValueError,
        ) as exc:
            raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_MODEL) from exc


def _snapshot_replacement_generation(connection, row, job_id, token, timestamp):
    drafts = [dict(item) for item in connection.execute(
        "SELECT * FROM candidate_knowledge_classification_drafts "
        "WHERE import_job_id=? ORDER BY CAST(source_question_no AS INTEGER)",
        (job_id,),
    )]
    backup_name = f".classification-replacement-{token}.bak"
    connection.execute(
        "DELETE FROM knowledge_classification_replacement_snapshots "
        "WHERE import_job_id=?",
        (job_id,),
    )
    connection.execute(
        """INSERT INTO knowledge_classification_replacement_snapshots
           (import_job_id,claim_token,run_snapshot_json,drafts_snapshot_json,
            output_sha256,output_byte_size,backup_name,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            job_id, token, _canonical(dict(row)), _canonical(drafts),
            row["output_sha256"], row["output_byte_size"], backup_name, timestamp,
        ),
    )
    return backup_name


def _replacement_lease_is_stale(row, now):
    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return now - updated.astimezone(timezone.utc) > STALE_AFTER


def _recover_replacement_artifact(private_root, job_id, snapshot):
    trusted_job = _open_trusted_job_directory(private_root, job_id)
    try:
        expected = (snapshot["output_sha256"], snapshot["output_byte_size"])

        def verified(name):
            try:
                content = _read_bounded(
                    name, MAX_MODEL_OUTPUT_BYTES, directory_fd=trusted_job.job_fd
                )
            except KnowledgeClassificationRunError:
                return False
            return len(content) == expected[1] and hashlib.sha256(content).hexdigest() == expected[0]

        if verified("knowledge_classification.json"):
            trusted_job.verify()
            return
        backup_name = snapshot["backup_name"]
        if not verified(backup_name):
            _fail(SAFE_CLASSIFICATION_STORAGE)
        try:
            current = os.stat(
                "knowledge_classification.json",
                dir_fd=trusted_job.job_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        if current is not None:
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                _fail(SAFE_CLASSIFICATION_STORAGE)
            os.unlink("knowledge_classification.json", dir_fd=trusted_job.job_fd)
        os.replace(
            backup_name,
            "knowledge_classification.json",
            src_dir_fd=trusted_job.job_fd,
            dst_dir_fd=trusted_job.job_fd,
        )
        os.fsync(trusted_job.job_fd)
        trusted_job.verify()
    finally:
        trusted_job.close()


def _recover_stale_replacement(connection, private_root, job_id, row):
    snapshot = connection.execute(
        "SELECT * FROM knowledge_classification_replacement_snapshots "
        "WHERE import_job_id=? AND claim_token=?",
        (job_id, row["claim_token"]),
    ).fetchone()
    if snapshot is None:
        _fail(SAFE_CLASSIFICATION_STORAGE)
    try:
        old_run = json.loads(snapshot["run_snapshot_json"])
        old_drafts = json.loads(snapshot["drafts_snapshot_json"])
    except (TypeError, json.JSONDecodeError):
        _fail(SAFE_CLASSIFICATION_STORAGE)
    if (
        not isinstance(old_run, dict)
        or not isinstance(old_drafts, list)
        or old_run.get("status") != "completed"
        or old_run.get("applied_at") is not None
        or old_run.get("output_sha256") != snapshot["output_sha256"]
        or old_run.get("output_byte_size") != snapshot["output_byte_size"]
    ):
        _fail(SAFE_CLASSIFICATION_STORAGE)
    try:
        _recover_replacement_artifact(private_root, job_id, snapshot)
    except KnowledgeClassificationRunError:
        connection.execute(
            """UPDATE import_knowledge_classification_runs
               SET status='failed',claim_token=NULL,replacement_active=0,
                   replacement_result='failed',error_message=?,updated_at=?
               WHERE import_job_id=? AND claim_token=? AND replacement_active=1""",
            (SAFE_CLASSIFICATION_STORAGE, _now(), job_id, row["claim_token"]),
        )
        connection.commit()
        return None
    connection.execute(
        "DELETE FROM candidate_knowledge_classification_drafts WHERE import_job_id=?",
        (job_id,),
    )
    for draft in old_drafts:
        if not isinstance(draft, dict) or draft.get("import_job_id") != job_id:
            _fail(SAFE_CLASSIFICATION_STORAGE)
        columns = list(draft)
        column_sql = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        connection.execute(
            f"INSERT INTO candidate_knowledge_classification_drafts ({column_sql}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(draft[name] for name in columns),
        )
    columns = [name for name in old_run if name != "import_job_id"]
    assignments = ",".join(
        '"' + name.replace('"', '""') + '"=?' for name in columns
    )
    connection.execute(
        f"UPDATE import_knowledge_classification_runs SET {assignments} "
        "WHERE import_job_id=?",
        tuple(old_run[name] for name in columns) + (job_id,),
    )
    connection.execute(
        "DELETE FROM knowledge_classification_replacement_snapshots "
        "WHERE import_job_id=?",
        (job_id,),
    )
    return connection.execute(
        "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?",
        (job_id,),
    ).fetchone()


def _write_archive_file(directory_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600, dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short archive write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_archive_bytes(value: object) -> bytes:
    return (_canonical(value) + "\n").encode("utf-8")


def _classification_generation_rows(connection, job_id):
    run = connection.execute(
        "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?",
        (job_id,),
    ).fetchone()
    drafts = [dict(row) for row in connection.execute(
        "SELECT * FROM candidate_knowledge_classification_drafts "
        "WHERE import_job_id=? ORDER BY CAST(source_question_no AS INTEGER)",
        (job_id,),
    )]
    evidence = [dict(row) for row in connection.execute(
        "SELECT * FROM candidate_knowledge_classifications "
        "WHERE import_job_id=? ORDER BY CAST(source_question_no AS INTEGER)",
        (job_id,),
    )]
    return (dict(run) if run is not None else None), drafts, evidence


def _validate_complete_classification_generation(
    run, drafts, evidence, expected_numbers, *, require_final_evidence,
) -> None:
    if run is None:
        if drafts or evidence:
            _fail(SAFE_CLASSIFICATION_STORAGE)
        return
    numbers = set(expected_numbers)
    draft_numbers = {row.get("source_question_no") for row in drafts}
    evidence_numbers = {row.get("source_question_no") for row in evidence}
    if (
        run.get("status") != "completed"
        or run.get("question_count") != len(numbers)
        or run.get("processed_questions") != len(numbers)
        or not isinstance(run.get("output_sha256"), str)
        or not isinstance(run.get("output_byte_size"), int)
        or len(drafts) != len(numbers) or draft_numbers != numbers
        or len(draft_numbers) != len(drafts)
        or (evidence and (len(evidence) != len(numbers) or evidence_numbers != numbers))
        or (require_final_evidence and len(evidence) != len(numbers))
        or (run.get("applied_at") is not None and len(evidence) != len(numbers))
    ):
        _fail(SAFE_CLASSIFICATION_STORAGE)
    by_number = {row["source_question_no"]: row for row in drafts}
    evidence_hashes = {row.get("evidence_sha256") for row in evidence}
    classifier_runs = {row.get("classifier_run_id") for row in evidence}
    if evidence and (
        len(evidence_hashes) != 1
        or any(
            not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in evidence_hashes
        )
        or len(classifier_runs) != 1
        or any(
            not isinstance(value, str) or not value or len(value) > 200
            for value in classifier_runs
        )
    ):
        _fail(SAFE_CLASSIFICATION_STORAGE)
    for row in evidence:
        draft = by_number[row["source_question_no"]]
        if (
            row.get("approved_draft_version") != draft.get("approved_draft_version")
            or row.get("edited_sha256") != draft.get("edited_sha256")
        ):
            _fail(SAFE_CLASSIFICATION_STORAGE)


def _archive_and_clear_classification_generation(
    connection, private_root: Path, job_id: int, expected_numbers,
    *, require_final_evidence: bool,
) -> _ArchivedClassificationGeneration | None:
    run, drafts, evidence = _classification_generation_rows(connection, job_id)
    _validate_complete_classification_generation(
        run, drafts, evidence, expected_numbers,
        require_final_evidence=require_final_evidence,
    )
    if run is None:
        return None
    trusted_job = _open_trusted_job_directory(private_root, job_id)
    archive_fd = stage_fd = None
    stage_name = final_name = None
    moved = False
    files = (
        "run.json", "drafts.json", "final_evidence.json",
        "knowledge_classification.json", "manifest.json",
    )
    try:
        artifact = _read_bounded(
            "knowledge_classification.json", MAX_MODEL_OUTPUT_BYTES,
            directory_fd=trusted_job.job_fd,
        )
        if (
            len(artifact) != run["output_byte_size"]
            or hashlib.sha256(artifact).hexdigest() != run["output_sha256"]
        ):
            _fail(SAFE_CLASSIFICATION_STORAGE)
        trusted_job.verify()
        try:
            os.mkdir("knowledge_classification_archive", 0o700, dir_fd=trusted_job.job_fd)
        except FileExistsError:
            pass
        archive_fd = os.open(
            "knowledge_classification_archive",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=trusted_job.job_fd,
        )
        archive_metadata = os.fstat(archive_fd)
        if not stat.S_ISDIR(archive_metadata.st_mode) or archive_metadata.st_nlink < 2:
            _fail(SAFE_CLASSIFICATION_STORAGE)
        os.fchmod(archive_fd, 0o700)
        token = secrets.token_hex(16)
        stage_name = f".generation-{token}.tmp"
        final_name = f"generation-{_now().replace(':', '').replace('+', '_')}-{token}"
        os.mkdir(stage_name, 0o700, dir_fd=archive_fd)
        stage_fd = os.open(
            stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=archive_fd,
        )
        payloads = {
            "run.json": _canonical_archive_bytes(run),
            "drafts.json": _canonical_archive_bytes(drafts),
            "final_evidence.json": _canonical_archive_bytes(evidence),
        }
        for name, content in payloads.items():
            _write_archive_file(stage_fd, name, content)
        os.replace(
            "knowledge_classification.json", "knowledge_classification.json",
            src_dir_fd=trusted_job.job_fd, dst_dir_fd=stage_fd,
        )
        moved = True
        moved_fd = os.open(
            "knowledge_classification.json", os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=stage_fd,
        )
        try:
            os.fchmod(moved_fd, 0o600)
            os.fsync(moved_fd)
        finally:
            os.close(moved_fd)
        artifacts = {
            name: {
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
            }
            for name, content in payloads.items()
        }
        artifacts["knowledge_classification.json"] = {
            "sha256": run["output_sha256"], "byte_size": run["output_byte_size"],
        }
        manifest = _canonical_archive_bytes({
            "version": 1, "import_job_id": job_id,
            "question_count": len(expected_numbers), "artifacts": artifacts,
        })
        _write_archive_file(stage_fd, "manifest.json", manifest)
        os.fsync(stage_fd)
        os.close(stage_fd)
        stage_fd = None
        os.replace(stage_name, final_name, src_dir_fd=archive_fd, dst_dir_fd=archive_fd)
        stage_name = None
        os.fsync(archive_fd)
        authorization = secrets.token_hex(32)
        connection.execute(
            "INSERT INTO knowledge_classification_archival_authorizations "
            "(import_job_id,authorization_token,created_at) VALUES(?,?,?)",
            (job_id, authorization, _now()),
        )
        connection.execute(
            "DELETE FROM knowledge_classification_replacement_snapshots WHERE import_job_id=?",
            (job_id,),
        )
        connection.execute(
            "DELETE FROM candidate_knowledge_classifications WHERE import_job_id=?", (job_id,)
        )
        connection.execute(
            "DELETE FROM candidate_knowledge_classification_drafts WHERE import_job_id=?",
            (job_id,),
        )
        connection.execute(
            "DELETE FROM import_knowledge_classification_runs WHERE import_job_id=?", (job_id,)
        )
        connection.execute(
            "DELETE FROM knowledge_classification_archival_authorizations WHERE import_job_id=?",
            (job_id,),
        )
        return _ArchivedClassificationGeneration(
            trusted_job, archive_fd, final_name, files
        )
    except Exception as exc:
        if final_name is not None and stage_name is None and archive_fd is not None:
            _ArchivedClassificationGeneration(
                trusted_job, archive_fd, final_name, files
            ).rollback()
            archive_fd = None
            trusted_job = None
        if stage_fd is not None:
            if moved:
                try:
                    os.replace(
                        "knowledge_classification.json", "knowledge_classification.json",
                        src_dir_fd=stage_fd, dst_dir_fd=trusted_job.job_fd,
                    )
                except OSError:
                    pass
            for name in files:
                try:
                    os.unlink(name, dir_fd=stage_fd)
                except OSError:
                    pass
            os.close(stage_fd)
        if stage_name is not None and archive_fd is not None:
            try:
                os.rmdir(stage_name, dir_fd=archive_fd)
            except OSError:
                pass
        if archive_fd is not None:
            os.close(archive_fd)
        if trusted_job is not None:
            trusted_job.close()
        if isinstance(exc, KnowledgeClassificationRunError):
            raise
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc


def _official_answers_fully_bound(connection, job_id: int):
    source = connection.execute(
        "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
    ).fetchone()
    if (
        source is None or source["source_answer_state"] != "source_answer_linked"
        or source["applied_at"] is None
        or connection.execute(
            "SELECT 1 FROM question_sources WHERE import_job_id=? LIMIT 1", (job_id,)
        ).fetchone()
    ):
        _fail(SAFE_OFFICIAL_ANSWER_BINDING)
    expected_count = source["expected_question_count"]
    drafts = [dict(row) for row in connection.execute(
        "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND deleted_at IS NULL",
        (job_id,),
    )]
    answers = [dict(row) for row in connection.execute(
        "SELECT * FROM candidate_official_answers WHERE import_job_id=?", (job_id,)
    )]
    reviews = [dict(row) for row in connection.execute(
        "SELECT * FROM candidate_official_answer_reviews WHERE import_job_id=?", (job_id,)
    )]
    extraction = connection.execute(
        "SELECT * FROM import_answer_extraction_runs WHERE import_job_id=?", (job_id,)
    ).fetchone()
    review_run = connection.execute(
        "SELECT * FROM import_answer_review_runs WHERE import_job_id=?", (job_id,)
    ).fetchone()
    numbers = {row["source_question_no"] for row in drafts}
    if (
        expected_count <= 0 or len(drafts) != expected_count or len(numbers) != expected_count
        or len(answers) != expected_count or {row["source_question_no"] for row in answers} != numbers
        or len(reviews) != expected_count or {row["source_question_no"] for row in reviews} != numbers
        or extraction is None or extraction["status"] != "completed"
        or extraction["question_count"] != expected_count
        or review_run is None or review_run["status"] != "completed"
        or review_run["question_count"] != expected_count
        or review_run["extraction_artifact_sha256"] != extraction["output_sha256"]
        or extraction["candidate_sha256"] != source["candidate_sha256"]
        or extraction["draft_batch_sha256"] != source["draft_batch_sha256"]
        or review_run["candidate_sha256"] != source["candidate_sha256"]
        or review_run["draft_batch_sha256"] != source["draft_batch_sha256"]
        or review_run["answer_pages_sha256"] != extraction["answer_pages_sha256"]
    ):
        _fail(SAFE_OFFICIAL_ANSWER_BINDING)
    answer_by_number = {row["source_question_no"]: row for row in answers}
    review_by_number = {row["source_question_no"]: row for row in reviews}
    bindings = {}
    for draft in drafts:
        number = draft["source_question_no"]
        answer = answer_by_number[number]
        review = review_by_number[number]
        try:
            edited = json.loads(draft["edited_json"])
            subanswers = json.loads(answer["subquestions_json"])
            source_pages = json.loads(answer["source_pages_json"])
        except (TypeError, json.JSONDecodeError):
            _fail(SAFE_OFFICIAL_ANSWER_BINDING)
        answer_payload = {
            "source_question_no": number, "content_kind": answer["content_kind"],
            "answer_markdown": answer["answer_markdown"],
            "analysis_markdown": answer["analysis_markdown"],
            "subquestions": subanswers, "source_pages": source_pages,
        }
        answer_analysis_payload = {
            "source_question_no": number,
            "answer_markdown": edited.get("answer_markdown", ""),
            "analysis_markdown": edited.get("analysis_markdown", ""),
            "subquestions": [{
                "label": item.get("label", ""),
                "stem_markdown": item.get("stem_markdown", ""),
                "answer_markdown": item.get("answer_markdown", ""),
                "analysis_markdown": item.get("analysis_markdown", ""),
            } for item in edited.get("subquestions", [])],
        }
        if (
            draft["status"] != "approved"
            or draft["approval_source"] not in {"human", "ai_second_pass"}
            or not draft["reviewed_at"] or not draft["approval_evidence_json"]
            or answer["candidate_sha256"] != extraction["candidate_sha256"]
            or answer["draft_batch_sha256"] != extraction["draft_batch_sha256"]
            or answer["extraction_artifact_sha256"] != extraction["output_sha256"]
            or review["decision"] != "passed"
            or review["answer_content_sha256"] != answer["content_sha256"]
            or answer["content_sha256"] != _digest(answer_payload)
            or review["answer_analysis_sha256"] != _digest(answer_analysis_payload)
            or review["extraction_artifact_sha256"] != extraction["output_sha256"]
            or review["answer_pages_sha256"] != extraction["answer_pages_sha256"]
            or review["source_pages_json"] != answer["source_pages_json"]
            or review["source_page_hashes_json"] != answer["source_page_hashes_json"]
            or edited.get("answer_markdown", "") != answer["answer_markdown"]
            or edited.get("analysis_markdown", "") != answer["analysis_markdown"]
            or len(edited.get("subquestions", [])) != len(subanswers)
            or any(
                target.get("label") != official.get("label")
                or target.get("answer_markdown", "") != official.get("answer_markdown", "")
                or target.get("analysis_markdown", "") != official.get("analysis_markdown", "")
                for target, official in zip(edited.get("subquestions", []), subanswers)
            )
        ):
            _fail(SAFE_OFFICIAL_ANSWER_BINDING)
        bindings[number] = (draft["version"], _digest(edited))
    return numbers, bindings


def repair_stale_knowledge_classification_after_official_answers(
    database_path: str | Path, private_root: str | Path, job_id: int,
) -> int:
    """Archive and clear the fully stale applied classification generation."""
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        _fail()
    archived = None
    committed = False
    try:
        with closing(sqlite3.connect(Path(database_path), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            numbers, current = _official_answers_fully_bound(connection, job_id)
            authoritative, _, _, _ = _authoritative_input(
                connection, Path(private_root), job_id
            )
            authoritative_bindings = {
                row["source_question_no"]: (
                    row["approved_draft_version"], row["edited_sha256"]
                ) for row in authoritative
            }
            if authoritative_bindings != current:
                _fail()
            run, drafts, evidence = _classification_generation_rows(connection, job_id)
            if run is None and not drafts and not evidence:
                connection.commit()
                return 0
            _validate_complete_classification_generation(
                run, drafts, evidence, numbers, require_final_evidence=True,
            )
            if run.get("applied_at") is None:
                _fail()
            old = {
                row["source_question_no"]: (
                    row["approved_draft_version"], row["edited_sha256"]
                ) for row in drafts
            }
            if set(old) != numbers or any(old[number] == current[number] for number in numbers):
                _fail()
            archived = _archive_and_clear_classification_generation(
                connection, Path(private_root), job_id, numbers,
                require_final_evidence=True,
            )
            connection.commit()
            committed = True
        archived.finalize()
        return 1
    except Exception as exc:
        if archived is not None and not committed:
            archived.rollback()
        if isinstance(exc, KnowledgeClassificationRunError):
            raise
        raise KnowledgeClassificationRunError(SAFE_CLASSIFICATION_STORAGE) from exc


def claim_knowledge_classification(
    database_path: str | Path, private_root: str | Path, job_id: int,
    *, runner=None, replace_unapplied: bool = False,
) -> ClassificationClaim | None:
    database_path, private_root = Path(database_path), Path(private_root)
    if (
        not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0
        or not isinstance(replace_unapplied, bool)
    ):
        _fail()
    try:
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            evidence_exists = connection.execute(
                "SELECT 1 FROM candidate_knowledge_classifications WHERE import_job_id=? LIMIT 1",
                (job_id,),
            ).fetchone()
            if evidence_exists:
                connection.commit()
                return None
            job = connection.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KnowledgeClassificationRunError("未找到导入任务")
            questions, taxonomy, input_digest, taxonomy_digest = _authoritative_input(
                connection, private_root, job_id
            )
            row = connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
            now = datetime.now(timezone.utc)
            if row is not None:
                if row["status"] == "processing" and row["replacement_active"]:
                    if not _replacement_lease_is_stale(row, now):
                        connection.commit()
                        return None
                    row = _recover_stale_replacement(
                        connection, private_root, job_id, row
                    )
                    if row is None:
                        return None
                if row["applied_at"] is not None:
                    connection.commit()
                    return None
                if row["status"] == "completed":
                    if not replace_unapplied:
                        connection.commit()
                        return None
                    old_drafts = connection.execute(
                        """SELECT source_question_no,approved_draft_version,
                                  edited_sha256
                           FROM candidate_knowledge_classification_drafts
                           WHERE import_job_id=?""",
                        (job_id,),
                    ).fetchall()
                    expected = {
                        item["source_question_no"]: (
                            item["approved_draft_version"],
                            item["edited_sha256"],
                        )
                        for item in questions
                    }
                    old_generation_complete = (
                        len(old_drafts) == len(expected)
                        and {
                            draft["source_question_no"] for draft in old_drafts
                        } == set(expected)
                    )
                    fully_invalidated = (
                        row["input_digest"] != input_digest
                        and (
                            not old_drafts
                            or (
                                old_generation_complete
                                and all(
                                    (
                                        draft["approved_draft_version"],
                                        draft["edited_sha256"],
                                    ) != expected[draft["source_question_no"]]
                                    for draft in old_drafts
                                )
                            )
                        )
                    )
                    if (
                        job["status"] == "completed"
                        or row["taxonomy_digest"] != taxonomy_digest
                        or row["question_count"] != len(questions)
                        or not row["output_sha256"]
                        or not row["output_byte_size"]
                        or connection.execute(
                            "SELECT 1 FROM question_sources "
                            "WHERE import_job_id=? LIMIT 1",
                            (job_id,),
                        ).fetchone()
                    ):
                        connection.commit()
                        return None
                    if not fully_invalidated and (
                        row["input_digest"] != input_digest
                        or len(old_drafts) != len(expected)
                        or {
                            draft["source_question_no"] for draft in old_drafts
                        } != set(expected)
                        or any(
                            (
                                draft["approved_draft_version"],
                                draft["edited_sha256"],
                            ) != expected[draft["source_question_no"]]
                            for draft in old_drafts
                        )
                    ):
                        connection.commit()
                        return None
                    trusted_job = None
                    try:
                        trusted_job = _open_trusted_job_directory(
                            private_root, job_id
                        )
                        old_output = _read_bounded(
                            "knowledge_classification.json",
                            MAX_MODEL_OUTPUT_BYTES,
                            directory_fd=trusted_job.job_fd,
                        )
                        trusted_job.verify()
                    except KnowledgeClassificationRunError:
                        connection.commit()
                        return None
                    finally:
                        if trusted_job is not None:
                            trusted_job.close()
                    if (
                        len(old_output) != row["output_byte_size"]
                        or hashlib.sha256(old_output).hexdigest()
                        != row["output_sha256"]
                    ):
                        connection.commit()
                        return None
                    token = secrets.token_hex(32)
                    timestamp = now.isoformat(timespec="seconds")
                    backup_name = _snapshot_replacement_generation(
                        connection, row, job_id, token, timestamp
                    )
                    cursor = connection.execute(
                        """UPDATE import_knowledge_classification_runs
                           SET status='processing',stage='waiting',
                               processed_questions=0,error_message=NULL,
                               claim_token=?,started_at=?,updated_at=?,
                               replacement_active=1,
                               replacement_attempted_at=?,
                               replacement_result='processing'
                           WHERE import_job_id=? AND status='completed'
                             AND applied_at IS NULL
                             AND replacement_active=0""",
                        (token, timestamp, timestamp, timestamp, job_id),
                    )
                    if cursor.rowcount != 1:
                        connection.commit()
                        return None
                    connection.commit()
                    return ClassificationClaim(
                        database_path, private_root, job_id,
                        runner or CodexKnowledgeClassificationRunner(), token,
                        input_digest, taxonomy_digest, questions, taxonomy,
                        True, row["output_sha256"], row["output_byte_size"],
                        backup_name,
                    )
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
                     completed_at=NULL,applied_at=NULL,
                     updated_at=excluded.updated_at,stage='waiting',
                     replacement_active=0""",
                (job_id, len(questions), MODEL, input_digest, taxonomy_digest,
                 token, timestamp, timestamp),
            )
            connection.commit()
        return ClassificationClaim(
            database_path, private_root, job_id,
            runner or CodexKnowledgeClassificationRunner(), token,
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


def _normalize_level3(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["related_codes"] = sorted(row["related_codes"])
    return normalized


def _classification_vote(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return row["primary_code"], tuple(row["related_codes"])


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
    expected_existing: tuple[str, int] | None = None,
    replacement_backup_name: str | None = None,
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
        if existing_fd is None and expected_existing is not None:
            _fail(SAFE_CLASSIFICATION_STORAGE)
        if existing_fd is not None:
            try:
                existing = os.fstat(existing_fd)
                if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                    _fail(SAFE_CLASSIFICATION_STORAGE)
                if expected_existing is not None:
                    expected_sha, expected_size = expected_existing
                    digest = hashlib.sha256()
                    remaining = existing.st_size
                    while remaining:
                        chunk = os.read(existing_fd, min(remaining, 64 * 1024))
                        if not chunk:
                            _fail(SAFE_CLASSIFICATION_STORAGE)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    verified = os.fstat(existing_fd)
                    if (
                        os.read(existing_fd, 1)
                        or (verified.st_dev, verified.st_ino)
                        != (existing.st_dev, existing.st_ino)
                        or verified.st_size != existing.st_size
                        or existing.st_size != expected_size
                        or digest.hexdigest() != expected_sha
                    ):
                        _fail(SAFE_CLASSIFICATION_STORAGE)
                backup_name = (
                    replacement_backup_name
                    or f".classification-{secrets.token_hex(16)}.bak"
                )
                if (
                    "/" in backup_name or "\\" in backup_name
                    or ".." in backup_name or len(backup_name) > 120
                ):
                    _fail(SAFE_CLASSIFICATION_STORAGE)
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
            if claim.replace_unapplied:
                cursor = connection.execute(
                    """UPDATE import_knowledge_classification_runs
                       SET status='completed',stage='review_ready',
                           processed_questions=question_count,error_message=NULL,
                           claim_token=NULL,updated_at=?,replacement_active=0,
                           replacement_result='failed'
                       WHERE import_job_id=? AND status='processing'
                         AND claim_token=? AND replacement_active=1""",
                    (_now(), claim.job_id, claim.claim_token),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        "DELETE FROM knowledge_classification_replacement_snapshots "
                        "WHERE import_job_id=? AND claim_token=?",
                        (claim.job_id, claim.claim_token),
                    )
            else:
                connection.execute(
                    """UPDATE import_knowledge_classification_runs
                       SET status='failed',error_message=?,claim_token=NULL,updated_at=?
                       WHERE import_job_id=? AND status='processing'
                         AND claim_token=?""",
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
    """Run one claimed batch while serializing classification publication."""
    publication = None
    database_committed = False
    trusted_job = None
    lock_fd = None
    try:
        trusted_job = _open_trusted_job_directory(claim.private_root, claim.job_id)
        lock_fd = os.open(
            ".codex-knowledge-classification.lock",
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
        proposal = {
            number: _normalize_level3(row)
            for number, row in _parse_level3(
                _run_model_stage(
                    claim, "proposal", _prompt("proposal", scoped_questions, [])
                ),
                numbers, allowed,
            ).items()
        }
        verifier = {
            number: _normalize_level3(row)
            for number, row in _parse_level3(
                _run_model_stage(
                    claim, "verifier", _prompt("verifier", scoped_questions, [])
                ),
                numbers, allowed,
            ).items()
        }
        pending_numbers = {
            number for number in numbers
            if not (
                level2[number]["confidence"] == "high"
                and proposal[number]["confidence"] == "high"
                and verifier[number]["confidence"] == "high"
                and _classification_vote(proposal[number])
                == _classification_vote(verifier[number])
            )
        }
        adjudicator: dict[str, dict[str, Any]] = {}
        if pending_numbers:
            adjudication_questions = [
                item for item in scoped_questions
                if item["source_question_no"] in pending_numbers
            ]
            adjudicator = {
                number: _normalize_level3(row)
                for number, row in _parse_level3(
                    _run_model_stage(
                        claim,
                        "adjudicator",
                        _prompt("adjudicator", adjudication_questions, []),
                    ),
                    pending_numbers,
                    {number: allowed[number] for number in pending_numbers},
                ).items()
            }
        now = _now()
        drafts = []
        for source in claim.questions:
            number = source["source_question_no"]
            first, second = proposal[number], verifier[number]
            double_pass = (
                level2[number]["confidence"] == "high"
                and first["confidence"] == second["confidence"] == "high"
                and _classification_vote(first) == _classification_vote(second)
            )
            third = adjudicator.get(number)
            adjudicated = (
                not double_pass
                and level2[number]["confidence"] == "high"
                and third is not None
                and third["confidence"] == "high"
                and _classification_vote(third) in {
                    _classification_vote(first), _classification_vote(second),
                }
            )
            automatic = double_pass or adjudicated
            final = third if adjudicated else first
            approval_source = (
                "codex_double_pass" if double_pass
                else "codex_adjudicated" if adjudicated
                else None
            )
            reviewer = (
                "codex_double_pass" if double_pass
                else "codex_adjudicator" if adjudicated
                else None
            )
            drafts.append({
                "source_question_no": number,
                "approved_draft_version": source["approved_draft_version"],
                "edited_sha256": source["edited_sha256"],
                "classification_scope_sha256": source["classification_scope_sha256"],
                "level2": level2[number],
                "proposal": first, "verifier": second, "adjudicator": third,
                "final_primary_code": final["primary_code"],
                "final_related_codes": final["related_codes"],
                "final_reason": final["reason"],
                "status": "approved" if automatic else "pending",
                "approval_source": approval_source,
                "reviewer": reviewer,
                "automatic_decision": ({
                    "approval_source": approval_source,
                    "reviewer": reviewer,
                    "primary_code": final["primary_code"],
                    "related_codes": final["related_codes"],
                    "reason": final["reason"],
                } if automatic else None),
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
            expected_existing=(
                (
                    claim.previous_output_sha256,
                    claim.previous_output_byte_size,
                )
                if claim.replace_unapplied
                else None
            ),
            replacement_backup_name=claim.replacement_backup_name,
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
                """SELECT status,claim_token,input_digest,replacement_active
                   FROM import_knowledge_classification_runs
                   WHERE import_job_id=?""",
                (claim.job_id,),
            ).fetchone()
            if (
                row is None or row["status"] != "processing" or row["claim_token"] != claim.claim_token
                or row["input_digest"] != claim.input_digest
                or bool(row["replacement_active"]) != claim.replace_unapplied
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
                        classification_scope_sha256,
                        proposal_primary_code,proposal_related_codes_json,proposal_confidence,
                        proposal_reason,verifier_primary_code,verifier_related_codes_json,
                        verifier_confidence,verifier_reason,adjudicator_primary_code,
                        adjudicator_related_codes_json,adjudicator_confidence,
                        adjudicator_reason,final_primary_code,final_related_codes_json,
                        final_reason,status,approval_source,reviewed_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        claim.job_id, item["source_question_no"], item["approved_draft_version"],
                        item["edited_sha256"], item["classification_scope_sha256"],
                        item["proposal"]["primary_code"],
                        _canonical(item["proposal"]["related_codes"]), item["proposal"]["confidence"],
                        item["proposal"]["reason"], item["verifier"]["primary_code"],
                        _canonical(item["verifier"]["related_codes"]), item["verifier"]["confidence"],
                        item["verifier"]["reason"],
                        item["adjudicator"]["primary_code"] if item["adjudicator"] else None,
                        (
                            _canonical(item["adjudicator"]["related_codes"])
                            if item["adjudicator"] else None
                        ),
                        item["adjudicator"]["confidence"] if item["adjudicator"] else None,
                        item["adjudicator"]["reason"] if item["adjudicator"] else None,
                        item["final_primary_code"],
                        _canonical(item["final_related_codes"]), item["final_reason"], item["status"],
                        item["approval_source"], item["reviewed_at"], now, now,
                    ),
                )
            connection.execute(
                """UPDATE import_knowledge_classification_runs
                   SET status='completed',processed_questions=question_count,
                       stage='review_ready',
                       output_sha256=?,output_byte_size=?,error_message=NULL,
                       claim_token=NULL,completed_at=?,updated_at=?,
                       replacement_completed_at=CASE
                           WHEN replacement_active=1 THEN ? ELSE replacement_completed_at END,
                       replacement_result=CASE
                           WHEN replacement_active=1 THEN 'completed' ELSE replacement_result END,
                       replacement_active=0
                   WHERE import_job_id=? AND claim_token=?""",
                (
                    output_sha, output_size, now, now, now,
                    claim.job_id, claim.claim_token,
                ),
            )
            if claim.replace_unapplied:
                connection.execute(
                    "DELETE FROM knowledge_classification_replacement_snapshots "
                    "WHERE import_job_id=? AND claim_token=?",
                    (claim.job_id, claim.claim_token),
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


def _anchored_artifact_questions(output, run, job_id, expected_numbers):
    try:
        artifact = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        _fail()
    if (
        not isinstance(artifact, dict)
        or artifact.get("version") != 1
        or artifact.get("import_job_id") != job_id
        or artifact.get("model") != run["model"]
        or artifact.get("input_digest") != run["input_digest"]
        or artifact.get("taxonomy_digest") != run["taxonomy_digest"]
        or not isinstance(artifact.get("questions"), list)
    ):
        _fail()
    questions = artifact["questions"]
    numbers = [
        item.get("source_question_no") if isinstance(item, dict) else None
        for item in questions
    ]
    # The publication order is canonical authoritative question order.  This
    # rejects missing/extra/duplicate and reordered evidence without guessing.
    if numbers != list(expected_numbers) or len(numbers) != len(set(numbers)):
        _fail()
    return dict(zip(numbers, questions, strict=True))


def _artifact_level3_matches_database(row, artifact, prefix):
    vote = artifact.get(prefix)
    if not isinstance(vote, dict):
        return False
    related = vote.get("related_codes")
    return (
        vote.get("primary_code") == row[f"{prefix}_primary_code"]
        and related == _decode_codes(row[f"{prefix}_related_codes_json"])
        and vote.get("confidence") == row[f"{prefix}_confidence"]
        and vote.get("reason") == row[f"{prefix}_reason"]
    )


def _validate_draft_provenance(connection, row, artifact):
    if (
        artifact.get("approved_draft_version") != row["approved_draft_version"]
        or artifact.get("edited_sha256") != row["edited_sha256"]
    ):
        overlay = connection.execute(
            """SELECT prior_draft_version,prior_edited_sha256,
                      new_draft_version,new_edited_sha256,
                      classification_scope_after_sha256
               FROM candidate_official_answer_overlays
               WHERE import_job_id=? AND source_question_no=?""",
            (row["import_job_id"], row["source_question_no"]),
        ).fetchone()
        if (
            overlay is None
            or row["approved_draft_version"] != overlay["new_draft_version"]
            or row["edited_sha256"] != overlay["new_edited_sha256"]
            or row.get("classification_scope_sha256")
                != overlay["classification_scope_after_sha256"]
            or artifact.get("approved_draft_version") != overlay["prior_draft_version"]
            or artifact.get("edited_sha256") != overlay["prior_edited_sha256"]
        ):
            _fail()
    source = row["approval_source"]
    if source == "human":
        if (
            row["version"] < 2
            or not row["reviewed_at"]
            or not row["human_review_note"].startswith("教师复核")
        ):
            _fail()
        return
    if source not in {"codex_double_pass", "codex_adjudicated"}:
        _fail()
    decision = artifact.get("automatic_decision")
    level2 = artifact.get("level2")
    proposal = artifact.get("proposal")
    verifier = artifact.get("verifier")
    adjudicator = artifact.get("adjudicator")
    reviewer = (
        "codex_double_pass" if source == "codex_double_pass"
        else "codex_adjudicator"
    )
    if (
        not isinstance(decision, dict)
        or not isinstance(level2, dict)
        or level2.get("confidence") != "high"
        or decision != {
            "approval_source": source,
            "reviewer": reviewer,
            "primary_code": artifact.get("final_primary_code"),
            "related_codes": artifact.get("final_related_codes"),
            "reason": artifact.get("final_reason"),
        }
        or artifact.get("status") != "approved"
        or artifact.get("approval_source") != source
        or artifact.get("reviewer") != reviewer
        or artifact.get("reviewed_at") != row["reviewed_at"]
        or artifact.get("final_primary_code") != row["final_primary_code"]
        or artifact.get("final_related_codes")
        != _decode_codes(row["final_related_codes_json"])
        or artifact.get("final_reason") != row["final_reason"]
        or not _artifact_level3_matches_database(row, artifact, "proposal")
        or not _artifact_level3_matches_database(row, artifact, "verifier")
    ):
        _fail()
    proposal_vote = _classification_vote(proposal)
    verifier_vote = _classification_vote(verifier)
    if source == "codex_double_pass":
        if (
            proposal.get("confidence") != "high"
            or verifier.get("confidence") != "high"
            or proposal_vote != verifier_vote
            or adjudicator is not None
            or decision["primary_code"] != proposal["primary_code"]
            or decision["related_codes"] != proposal["related_codes"]
            or decision["reason"] != proposal["reason"]
        ):
            _fail()
        return
    if (
        not isinstance(adjudicator, dict)
        or adjudicator.get("confidence") != "high"
        or not _artifact_level3_matches_database(row, artifact, "adjudicator")
        or _classification_vote(adjudicator) not in {proposal_vote, verifier_vote}
        or decision["primary_code"] != adjudicator["primary_code"]
        or decision["related_codes"] != adjudicator["related_codes"]
        or decision["reason"] != adjudicator["reason"]
    ):
        _fail()


def load_classification_page(database_path: str | Path, job_id: int) -> ClassificationPage:
    """Read status and DB-backed UX data without creating rows, drafts, or files."""
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            job = connection.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KnowledgeClassificationRunError("未找到导入任务")
            run = connection.execute(
                "SELECT * FROM import_knowledge_classification_runs WHERE import_job_id=?", (job_id,)
            ).fetchone()
            evidence_count = connection.execute(
                "SELECT COUNT(*) FROM candidate_knowledge_classifications WHERE import_job_id=?",
                (job_id,),
            ).fetchone()[0]
            formal_count = connection.execute(
                "SELECT COUNT(*) FROM question_sources WHERE import_job_id=?",
                (job_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT c.*,r.edited_json,k1.name AS final_name,k2.name AS proposal_name,
                          k3.name AS verifier_name,k4.name AS adjudicator_name
                   FROM candidate_knowledge_classification_drafts c
                   JOIN candidate_review_drafts r ON r.import_job_id=c.import_job_id
                     AND r.source_question_no=c.source_question_no
                   JOIN knowledge_points k1 ON k1.code=c.final_primary_code
                   JOIN knowledge_points k2 ON k2.code=c.proposal_primary_code
                   JOIN knowledge_points k3 ON k3.code=c.verifier_primary_code
                   LEFT JOIN knowledge_points k4 ON k4.code=c.adjudicator_primary_code
                   WHERE c.import_job_id=?
                   ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END,
                            CAST(c.source_question_no AS INTEGER)""", (job_id,)
            ).fetchall()
            drafts = []
            for row in rows:
                item = dict(row)
                edited = _decode_object(item["edited_json"])
                item["stem_markdown"] = str(edited.get("stem_markdown", ""))[:500]
                for key in (
                    "proposal_related_codes_json", "verifier_related_codes_json",
                    "adjudicator_related_codes_json", "final_related_codes_json",
                ):
                    item[key.removesuffix("_json")] = _decode_codes(item[key])
                item["reviewer"] = {
                    "codex_double_pass": "codex_double_pass",
                    "codex_adjudicated": "codex_adjudicator",
                    "local_double_pass": "local_double_pass",
                    "human": "teacher_human_review",
                }.get(item["approval_source"])
                drafts.append(item)
            pending = sum(item["status"] == "pending" for item in drafts)
            approved = sum(item["status"] == "approved" for item in drafts)
            auto = sum(
                item["approval_source"] in {
                    "codex_double_pass", "codex_adjudicated", "local_double_pass",
                }
                for item in drafts
            )
            double = sum(
                item["approval_source"] in {"codex_double_pass", "local_double_pass"}
                for item in drafts
            )
            adjudicated_count = sum(
                item["approval_source"] == "codex_adjudicated" for item in drafts
            )
            if run is None:
                return ClassificationPage(
                    exists=False,
                    status="completed" if evidence_count else "pending",
                    stage="review_ready" if evidence_count else "waiting",
                    question_count=evidence_count,
                    processed=evidence_count,
                    auto_approved=0,
                    double_approved=0,
                    adjudicated_approved=0,
                    pending=0,
                    approved=evidence_count,
                    applied=bool(evidence_count),
                    completed_evidence=bool(evidence_count),
                    can_replace=False,
                    replacement_active=False,
                    replacement_result=None,
                    replacement_completed_at=None,
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
                double_approved=double,
                adjudicated_approved=adjudicated_count,
                pending=pending,
                approved=approved,
                applied=run["applied_at"] is not None or bool(evidence_count),
                completed_evidence=bool(evidence_count),
                can_replace=bool(
                    run["status"] == "completed"
                    and run["applied_at"] is None
                    and not evidence_count
                    and not formal_count
                    and job["status"] != "completed"
                ),
                replacement_active=bool(run["replacement_active"]),
                replacement_result=run["replacement_result"],
                replacement_completed_at=run["replacement_completed_at"],
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
            authoritative_questions, _, input_digest, taxonomy_digest = _authoritative_input(
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
            expected_numbers = tuple(
                item["source_question_no"] for item in authoritative_questions
            )
            artifacts = _anchored_artifact_questions(
                output, run, job_id, expected_numbers
            )
            if tuple(row["source_question_no"] for row in rows) != expected_numbers:
                _fail()
            for row in rows:
                _validate_draft_provenance(
                    connection, row, artifacts[row["source_question_no"]]
                )
            sources = {row["approval_source"] for row in rows}
            source_classifier = (
                "codex_double_pass"
                if sources == {"codex_double_pass"}
                else "codex_adjudicated"
                if sources == {"codex_adjudicated"}
                else "codex_multi_pass"
            )
            batch_reviewer = (
                "codex_double_pass"
                if sources == {"codex_double_pass"}
                else "codex_adjudicator"
                if sources == {"codex_adjudicated"}
                else "mixed_classification_review"
            )
            reviewer_by_source = {
                "codex_double_pass": "codex_double_pass",
                "codex_adjudicated": "codex_adjudicator",
                "local_double_pass": "local_double_pass",
                "human": "teacher_human_review",
            }
            payload = {
                "version": 1, "import_job_id": job_id,
                "source_classifier": source_classifier,
                "reviewer": batch_reviewer,
                "scope": "knowledge_only_no_solution", "question_count": len(rows),
                "questions": [{
                    "source_question_no": row["source_question_no"],
                    "primary_code": row["final_primary_code"],
                    "related_codes": _decode_codes(row["final_related_codes_json"]),
                    "reason": (
                        row["human_review_note"]
                        if row["approval_source"] == "human"
                        else row["final_reason"] or row["proposal_reason"]
                    ),
                    "reviewer": reviewer_by_source[row["approval_source"]],
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
