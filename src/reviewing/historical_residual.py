"""Fail-closed classification and admission for historical recovery residuals.

This module is intentionally separate from the ordinary whole-batch lanes.  Its
scope is always derived as ``recovery question numbers - frozen formal numbers``;
callers never supply question numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, NoReturn

from src.database.initialize import DEFAULT_DATABASE_PATH
from src.importing.admit_questions import (
    AdmissionError,
    BackupFileIdentity,
    _assess,
    _code,
    _effective_questions,
    _insert_one,
    _job_artifact_lock,
    _load_context,
    _verify_artifact_snapshots,
    backup_database,
    verify_backup_file_identity,
)
from src.processing.historical_v1_crop_recovery import _formal_snapshot
from src.processing.historical_v1_crop_recovery import (
    HistoricalV1RecoveryError,
    _pinned_recovery_paths,
    _pinned_sqlite_connection,
    _verify_bound_paths,
    _verify_locked_job,
)
from src.processing.pdf_page_renderer import _read_archived_pdf
from src.processing.secure_crop_artifacts import canonical_payload, load_hmac_key
from src.reviewing.knowledge_classification import classification_scope_sha256
from src.reviewing.local_knowledge_classification import (
    CodexKnowledgeClassificationRunner,
    _classification_vote,
    _normalize_level3,
    _parse_level2,
    _parse_level3,
    _prompt,
    _taxonomy,
)


SAFE_RESIDUAL = "历史恢复剩余题证据无效"
SAFE_CONFIRMATION = "必须提供与当前 dry-run 完全一致的显式确认令牌"
NO_ANSWER_AUTHORITY_NAME = "historical_no_answer_authority.json"
CLASSIFICATION_LEASE_SECONDS = 600


class HistoricalResidualError(RuntimeError):
    """The residual lane cannot proceed without weakening its invariants."""


@dataclass(frozen=True)
class ResidualSnapshot:
    job_id: int
    source_filename: str
    paper_title: str
    total_question_count: int
    full_question_nos: tuple[str, ...]
    existing_question_nos: tuple[str, ...]
    residual_question_nos: tuple[str, ...]
    formal_question_count: int
    formal_batch_sha256: str
    candidate_sha256: str
    audit_sha256: str
    crop_manifest_sha256: str
    crop_generation_id: str
    crop_manifest_signature: str
    audit_completed_at: str
    draft_bindings_sha256: str
    taxonomy_sha256: str
    input_sha256: str


@dataclass(frozen=True)
class HistoricalResidualClassificationClaim:
    database_path: Path
    private_root: Path
    job_id: int
    claim_token: str
    snapshot: ResidualSnapshot
    questions: tuple[dict[str, Any], ...]
    taxonomy: tuple[dict[str, Any], ...]
    runner: Any
    database_identity: tuple[int, int]
    private_identity: tuple[int, int]
    job_identity: tuple[int, int]


@dataclass(frozen=True)
class HistoricalResidualClassificationResult:
    job_id: int
    residual_question_nos: tuple[str, ...]
    question_count: int
    evidence_sha256: str
    applied: bool = False
    inserted: int = 0


@dataclass(frozen=True)
class HistoricalResidualAssessment:
    status: str
    source_filename: str
    paper_title: str
    job_id: int
    total_question_count: int
    existing_question_nos: tuple[str, ...]
    residual_question_nos: tuple[str, ...]
    eligible: tuple[str, ...]
    ineligible: tuple[dict[str, Any], ...]
    confirmation_token: str
    assessment_sha256: str


@dataclass(frozen=True)
class HistoricalResidualAdmissionResult:
    inserted: int
    already_present: int
    job_completed: bool
    backup_path: str
    backup_sha256: str
    question_codes: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lease_deadline() -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=CLASSIFICATION_LEASE_SECONDS)
    ).isoformat(timespec="seconds")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fail(message: str = SAFE_RESIDUAL) -> NoReturn:
    raise HistoricalResidualError(message)


def _pinned_entry(function):
    """Keep database/private descriptors pinned across one public operation."""
    @wraps(function)
    def guarded(database_path=DEFAULT_DATABASE_PATH, private_root=None, *args, **kwargs):
        database = Path(database_path)
        private = Path(private_root or database.parent)
        try:
            with _pinned_recovery_paths(database, private) as (pinned_db, pinned_private):
                return function(pinned_db, pinned_private, *args, **kwargs)
        except HistoricalResidualError:
            raise
        except HistoricalV1RecoveryError as exc:
            raise HistoricalResidualError(SAFE_RESIDUAL) from exc
    return guarded


def _entry_identity(path: Path, *, regular: bool) -> tuple[int, int]:
    info = path.lstat()
    if (
        (regular and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
        or (not regular and not stat.S_ISDIR(info.st_mode))
    ):
        _fail()
    return info.st_dev, info.st_ino


def _verify_claim_identity(claim: HistoricalResidualClassificationClaim) -> None:
    job_dir = claim.private_root / "processing" / f"import_job_{claim.job_id}"
    try:
        if (
            _entry_identity(claim.database_path, regular=True) != claim.database_identity
            or _entry_identity(claim.private_root, regular=False) != claim.private_identity
            or _entry_identity(job_dir, regular=False) != claim.job_identity
        ):
            _fail()
    except OSError as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


@contextmanager
def _claim_sqlite_connection(claim: HistoricalResidualClassificationClaim):
    """Open only the claim's original database inode for a bounded stage update."""
    with _pinned_recovery_paths(
        claim.database_path, claim.private_root,
    ) as (database_bound, private_bound):
        database_info = os.fstat(database_bound.descriptor)
        private_info = os.fstat(private_bound.descriptor)
        if (
            (database_info.st_dev, database_info.st_ino) != claim.database_identity
            or (private_info.st_dev, private_info.st_ino) != claim.private_identity
        ):
            _fail()
        _verify_claim_identity(claim)
        with _pinned_sqlite_connection(database_bound, timeout=10) as connection:
            yield connection


def _verify_no_answer_authority(
    connection: sqlite3.Connection, private_root: Path, job_id: int,
    *, artifact_lock: Any, confirmation_receipt: bytes | None,
    expected_receipt_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Verify a locally archived user confirmation and its caller-supplied receipt."""
    if isinstance(confirmation_receipt, bytes) and 1 <= len(confirmation_receipt) <= 65536:
        receipt_sha256 = hashlib.sha256(confirmation_receipt).hexdigest()
    elif (
        confirmation_receipt is None
        and isinstance(expected_receipt_sha256, str)
        and len(expected_receipt_sha256) == 64
        and not any(character not in "0123456789abcdef" for character in expected_receipt_sha256)
    ):
        receipt_sha256 = expected_receipt_sha256
    else:
        _fail("必须提供外部归档的用户确认 receipt bytes")
    row = connection.execute(
        """SELECT j.source_paper_id,p.sha256,p.file_size,p.stored_path
           FROM import_jobs j JOIN source_papers p ON p.id=j.source_paper_id
           WHERE j.id=?""", (job_id,),
    ).fetchone()
    if row is None:
        _fail()
    try:
        source = _read_archived_pdf(private_root, row[3], row[2])
        if len(source) != row[2] or hashlib.sha256(source).hexdigest() != row[1]:
            _fail()
        descriptor = os.open(
            NO_ANSWER_AUTHORITY_NAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=artifact_lock.descriptor,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= 65536:
                _fail()
            raw = os.read(descriptor, info.st_size + 1)
            if len(raw) != info.st_size or os.fstat(descriptor).st_ino != info.st_ino:
                _fail()
        finally:
            os.close(descriptor)
        evidence = json.loads(raw.decode("utf-8"))
        required = {
            "version", "scope", "import_job_id", "source_paper_id",
            "source_pdf_sha256", "source_pdf_byte_size", "provenance",
            "confirmation_text_summary", "confirmation_text_sha256",
            "session_reference", "recorded_at", "receipt_sha256",
            "conclusion", "signature",
        }
        signature = evidence.get("signature") if isinstance(evidence, dict) else None
        expected = hmac.new(
            load_hmac_key(artifact_lock.path), canonical_payload(evidence), hashlib.sha256,
        ).hexdigest()
        if (
            not isinstance(evidence, dict) or set(evidence) != required
            or evidence["version"] != 2
            or evidence["scope"] != "historical_residual_no_answer_authority"
            or evidence["import_job_id"] != job_id
            or evidence["source_paper_id"] != row[0]
            or evidence["source_pdf_sha256"] != row[1]
            or evidence["source_pdf_byte_size"] != row[2]
            or evidence["provenance"] != "archived_user_confirmation_migration"
            or not isinstance(evidence["confirmation_text_summary"], str)
            or not 1 <= len(evidence["confirmation_text_summary"].strip()) <= 500
            or evidence["confirmation_text_sha256"]
            != receipt_sha256
            or not isinstance(evidence["session_reference"], str)
            or not 1 <= len(evidence["session_reference"].strip()) <= 500
            or not isinstance(evidence["recorded_at"], str)
            or evidence["receipt_sha256"]
            != receipt_sha256
            or evidence["conclusion"] != "source_has_no_answer"
            or not isinstance(signature, str)
            or not hmac.compare_digest(signature, expected)
        ):
            _fail("缺少完整绑定的历史用户确认迁移证据")
        recorded = datetime.fromisoformat(evidence["recorded_at"])
        if (
            recorded.tzinfo is None
            or recorded.astimezone(timezone.utc)
            > datetime.now(timezone.utc) + timedelta(minutes=5)
        ):
            _fail("历史用户确认记录时间无效")
        return evidence, hashlib.sha256(raw).hexdigest()
    except HistoricalResidualError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise HistoricalResidualError("缺少完整绑定的历史用户确认迁移证据") from exc


def _effective_evidence_row(
    row: dict[str, Any], *, source: dict[str, Any], snapshot: ResidualSnapshot,
    taxonomy: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    """Return only generated, exactly anchored classification evidence."""
    top_keys = {
        "source_question_no", "approved_draft_version", "edited_sha256",
        "classification_scope_sha256", "candidate_sha256", "audit_sha256",
        "draft_bindings_sha256", "taxonomy_sha256", "level2", "proposal",
        "verifier", "adjudicator", "final_primary_code", "final_related_codes",
        "final_reason", "status", "approval_source",
    }
    level2_keys = {"source_question_no", "level2_code", "confidence", "reason"}
    level3_keys = {
        "source_question_no", "primary_code", "related_codes", "confidence", "reason",
    }
    confidences = {"low", "medium", "high"}
    if not isinstance(row, dict) or set(row) != top_keys:
        return None
    anchor_keys = (
        "source_question_no", "approved_draft_version", "edited_sha256",
        "classification_scope_sha256",
    )
    if any(row.get(key) != source.get(key) for key in anchor_keys) or any((
        row.get("candidate_sha256") != snapshot.candidate_sha256,
        row.get("audit_sha256") != snapshot.audit_sha256,
        row.get("draft_bindings_sha256") != snapshot.draft_bindings_sha256,
        row.get("taxonomy_sha256") != snapshot.taxonomy_sha256,
    )):
        return None
    level2_codes = {item["code"] for item in taxonomy if item.get("level") == 2}
    parent_by_level3 = {
        item["code"]: item.get("parent_code")
        for item in taxonomy if item.get("level") == 3
    }
    number = source.get("source_question_no")
    level2 = row.get("level2")
    if (
        not isinstance(level2, dict) or set(level2) != level2_keys
        or level2.get("source_question_no") != number
        or not isinstance(level2.get("level2_code"), str)
        or level2.get("level2_code") not in level2_codes
        or level2.get("confidence") not in confidences
        or not isinstance(level2.get("reason"), str)
        or not 1 <= len(level2["reason"]) <= 200
    ):
        return None

    def valid_level3(value: object, *, optional: bool = False) -> bool:
        if value is None:
            return optional
        if not isinstance(value, dict) or set(value) != level3_keys:
            return False
        related = value.get("related_codes")
        primary = value.get("primary_code")
        return bool(
            value.get("source_question_no") == number
            and isinstance(primary, str)
            and primary in parent_by_level3
            and parent_by_level3[primary] == level2["level2_code"]
            and isinstance(related, list) and len(related) <= 2
            and all(isinstance(code, str) for code in related)
            and len(related) == len(set(related)) and primary not in related
            and all(code in parent_by_level3
                    and parent_by_level3[code] == level2["level2_code"] for code in related)
            and value.get("confidence") in confidences
            and isinstance(value.get("reason"), str)
            and 1 <= len(value["reason"]) <= 200
        )

    proposal, verifier, adjudicator = (
        row.get("proposal"), row.get("verifier"), row.get("adjudicator")
    )
    if not valid_level3(proposal) or not valid_level3(verifier) or not valid_level3(
        adjudicator, optional=True
    ):
        return None
    double = bool(
        level2["confidence"] == "high" and proposal["confidence"] == "high"
        and verifier["confidence"] == "high"
        and _classification_vote(proposal) == _classification_vote(verifier)
    )
    adjudicated = bool(
        not double and level2["confidence"] == "high" and adjudicator is not None
        and adjudicator["confidence"] == "high"
    )
    if double and adjudicator is not None:
        return None
    if row.get("status") == "approved":
        final = proposal if double else adjudicator if adjudicated else None
        approval = "codex_double_pass" if double else "codex_adjudicated"
        if (
            final is None or row.get("approval_source") != approval
            or row.get("final_primary_code") != final["primary_code"]
            or row.get("final_related_codes") != final["related_codes"]
            or row.get("final_reason") != final["reason"]
        ):
            return None
        return row
    if (
        row.get("status") != "pending"
        or not adjudicated
        or row.get("approval_source") is not None
        or row.get("final_primary_code") != proposal["primary_code"]
        or row.get("final_related_codes") != proposal["related_codes"]
        or row.get("final_reason") != proposal["reason"]
    ):
        return None
    effective = dict(row)
    effective.update({
        "final_primary_code": adjudicator["primary_code"],
        "final_related_codes": adjudicator["related_codes"],
        "final_reason": adjudicator["reason"], "status": "approved",
        "approval_source": "codex_adjudicated",
    })
    return effective


def _decode_numbers(raw: object) -> tuple[str, ...]:
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        _fail()
    if (
        not isinstance(values, list)
        or not values
        or any(type(value) is not int or value < 1 or value > 999 for value in values)
        or len(values) != len(set(values))
        or values != sorted(values)
    ):
        _fail()
    return tuple(str(value) for value in values)


def _residual_snapshot(
    connection: sqlite3.Connection,
    private_root: Path,
    job_id: int,
    *,
    artifact_lock: Any,
) -> tuple[ResidualSnapshot, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[Any, ...]]:
    recovery = connection.execute(
        "SELECT * FROM historical_v1_crop_recoveries WHERE import_job_id=?", (job_id,)
    ).fetchone()
    resumption = connection.execute(
        "SELECT * FROM historical_v1_pipeline_resumptions WHERE import_job_id=?", (job_id,)
    ).fetchone()
    if recovery is None or resumption is None:
        _fail()
    full_numbers = _decode_numbers(recovery["question_nos_json"])
    formal_count, formal_sha, formal_numbers = _formal_snapshot(connection, job_id)
    existing = tuple(sorted(formal_numbers, key=int))
    if (
        formal_count != recovery["formal_question_count"]
        or formal_sha != recovery["formal_batch_sha256"]
        or len(existing) != len(set(existing))
        or not set(existing).issubset(full_numbers)
    ):
        _fail("历史恢复冻结正式题基线发生漂移")
    residual = tuple(number for number in full_numbers if number not in set(existing))
    if not residual:
        _fail("历史恢复任务没有非空剩余题集合")

    try:
        context = _load_context(
            connection,
            private_root,
            job_id,
            artifact_lock=artifact_lock,
            eligible_question_numbers=residual,
        )
    except AdmissionError as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc
    candidate_numbers = tuple(question["source_question_no"] for question in context[2])
    crop_numbers = tuple(sorted(context[4], key=int))
    audit_numbers = tuple(sorted(context[3], key=int))
    if (
        candidate_numbers != full_numbers
        or crop_numbers != full_numbers
        or audit_numbers != full_numbers
        or recovery["source_paper_id"] != context[0]["source_paper_id"]
        or resumption["crop_question_count"] != len(full_numbers)
        or resumption["crop_manifest_sha256"] != context[8]
    ):
        _fail()
    crop_row = connection.execute(
        """SELECT input_crop_generation_id,input_manifest_signature,completed_at
           FROM import_candidate_audit_runs WHERE import_job_id=?""",
        (job_id,),
    ).fetchone()
    if (
        crop_row is None
        or crop_row["input_crop_generation_id"] != resumption["crop_generation_id"]
        or crop_row["input_manifest_signature"] != resumption["crop_manifest_signature"]
        or crop_row["completed_at"] != context[10]
    ):
        _fail()

    effective = _effective_questions(connection, context)
    prepared: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for number in residual:
        question, draft, reasons, legacy = effective[number]
        unexpected = set(reasons) - {"knowledge_classification_missing"}
        if draft is None or draft["deleted_at"] is not None or legacy or unexpected:
            _fail()
        try:
            approval_sha = hashlib.sha256(
                draft["approval_evidence_json"].encode("utf-8")
            ).hexdigest()
        except (KeyError, TypeError, AttributeError) as exc:
            raise HistoricalResidualError(SAFE_RESIDUAL) from exc
        edited_sha = _digest(question)
        binding = {
            "source_question_no": number,
            "approved_draft_version": draft["version"],
            "edited_sha256": edited_sha,
            "classification_scope_sha256": classification_scope_sha256(question),
            "approval_source": draft["approval_source"],
            "approval_evidence_sha256": approval_sha,
        }
        bindings.append(binding)
        prepared.append({**binding, "edited": question})
    taxonomy, taxonomy_sha = _taxonomy(connection)
    draft_sha = _digest(bindings)
    input_payload = {
        "scope": "historical_recovery_residual",
        "job_id": job_id,
        "full_question_nos": full_numbers,
        "existing_question_nos": existing,
        "residual_question_nos": residual,
        "formal_question_count": formal_count,
        "formal_batch_sha256": formal_sha,
        "candidate_sha256": context[6],
        "audit_sha256": context[7],
        "crop_manifest_sha256": context[8],
        "crop_generation_id": resumption["crop_generation_id"],
        "crop_manifest_signature": resumption["crop_manifest_signature"],
        "audit_completed_at": context[10],
        "draft_bindings_sha256": draft_sha,
        "taxonomy_sha256": taxonomy_sha,
    }
    source = connection.execute(
        """SELECT original_filename,paper_name FROM source_papers
           WHERE id=?""",
        (recovery["source_paper_id"],),
    ).fetchone()
    if source is None:
        _fail()
    snapshot = ResidualSnapshot(
        job_id, source["original_filename"], source["paper_name"], len(full_numbers),
        full_numbers, existing, residual, formal_count, formal_sha, context[6],
        context[7], context[8], resumption["crop_generation_id"],
        resumption["crop_manifest_signature"], context[10], draft_sha, taxonomy_sha,
        _digest(input_payload),
    )
    return snapshot, tuple(prepared), taxonomy, context


@_pinned_entry
def claim_historical_residual_classification(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    private_root: str | Path | None = None,
    job_id: int = 1,
    *,
    runner: Any | None = None,
) -> HistoricalResidualClassificationClaim:
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_locked_job(private_bound, job_id, lock.descriptor)
            with _pinned_sqlite_connection(database_bound, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                snapshot, questions, taxonomy, _ = _residual_snapshot(
                    connection, private_root, job_id, artifact_lock=lock
                )
                row = connection.execute(
                    "SELECT status,input_sha256,lease_expires_at FROM "
                    "historical_residual_classification_runs "
                    "WHERE import_job_id=?", (job_id,),
                ).fetchone()
                if row is not None and row["status"] == "completed":
                    _fail("剩余题分类已完成；请直接 apply，不得覆盖不可变证据")
                if row is not None and row["status"] == "processing":
                    try:
                        expires = datetime.fromisoformat(row["lease_expires_at"])
                    except (TypeError, ValueError):
                        _fail()
                    if expires.tzinfo is None or expires > datetime.now(timezone.utc):
                        _fail("剩余题分类已有活跃 processing claim")
                    if row["input_sha256"] != snapshot.input_sha256:
                        _fail("过期分类 claim 的输入快照已漂移")
                token = secrets.token_hex(32)
                now = _now()
                lease = _lease_deadline()
                values = (
                    job_id, "processing", "waiting", len(snapshot.residual_question_nos),
                    _canonical([int(x) for x in snapshot.full_question_nos]),
                    _canonical([int(x) for x in snapshot.existing_question_nos]),
                    _canonical([int(x) for x in snapshot.residual_question_nos]),
                    snapshot.formal_question_count, snapshot.formal_batch_sha256,
                    snapshot.candidate_sha256, snapshot.audit_sha256,
                    snapshot.crop_manifest_sha256, snapshot.crop_generation_id,
                    snapshot.crop_manifest_signature, snapshot.audit_completed_at,
                    snapshot.draft_bindings_sha256, snapshot.taxonomy_sha256,
                    snapshot.input_sha256, token, now, now, lease, now,
                )
                connection.execute(
                    "DELETE FROM historical_residual_classification_runs "
                    "WHERE import_job_id=? AND status IN ('failed','processing')", (job_id,),
                )
                connection.execute(
                    """INSERT INTO historical_residual_classification_runs
                       (import_job_id,status,stage,question_count,full_question_nos_json,
                        existing_question_nos_json,residual_question_nos_json,
                        formal_question_count,formal_batch_sha256,candidate_sha256,
                        audit_sha256,crop_manifest_sha256,crop_generation_id,
                        crop_manifest_signature,audit_completed_at,draft_bindings_sha256,
                        taxonomy_sha256,input_sha256,claim_token,started_at,
                        heartbeat_at,lease_expires_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                _verify_bound_paths(database_bound, private_bound)
                _verify_locked_job(private_bound, job_id, lock.descriptor)
                connection.commit()
        return HistoricalResidualClassificationClaim(
            database_path, private_root, job_id, token, snapshot, questions,
            taxonomy, runner or CodexKnowledgeClassificationRunner(),
            _entry_identity(database_path, regular=True),
            _entry_identity(private_root, regular=False),
            _entry_identity(job_dir, regular=False),
        )
    except HistoricalResidualError:
        raise
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


def _runner_stage(claim: HistoricalResidualClassificationClaim, stage: str, prompt: str) -> str:
    _verify_claim_identity(claim)
    with _claim_sqlite_connection(claim) as connection:
        now = _now()
        changed = connection.execute(
            """UPDATE historical_residual_classification_runs
               SET stage=?,heartbeat_at=?,lease_expires_at=?,updated_at=?
               WHERE import_job_id=? AND status='processing' AND claim_token=?""",
            (stage, now, _lease_deadline(), now, claim.job_id, claim.claim_token),
        ).rowcount
        if changed != 1:
            _fail()
        connection.commit()
    result = claim.runner.run(stage, prompt)
    with _claim_sqlite_connection(claim) as connection:
        now = _now()
        changed = connection.execute(
            """UPDATE historical_residual_classification_runs
               SET heartbeat_at=?,lease_expires_at=?,updated_at=?
               WHERE import_job_id=? AND status='processing' AND claim_token=?""",
            (now, _lease_deadline(), now, claim.job_id, claim.claim_token),
        ).rowcount
        if changed != 1:
            _fail()
        connection.commit()
    return result


def run_claimed_historical_residual_classification(
    claim: HistoricalResidualClassificationClaim,
) -> HistoricalResidualClassificationResult:
    try:
        _verify_claim_identity(claim)
        numbers = set(claim.snapshot.residual_question_nos)
        level2_rows = [row for row in claim.taxonomy if row["level"] == 2]
        level2 = _parse_level2(
            _runner_stage(claim, "level2", _prompt(
                "level2", claim.questions,
                [{"code": row["code"], "name": row["name"]} for row in level2_rows],
            )), numbers, {row["code"] for row in level2_rows},
        )
        level3_rows = [row for row in claim.taxonomy if row["level"] == 3]
        allowed = {
            number: {row["code"] for row in level3_rows
                     if row["parent_code"] == level2[number]["level2_code"]}
            for number in numbers
        }
        scoped = [{
            "source_question_no": item["source_question_no"],
            "question": item["edited"],
            "level3_candidates": [
                {"code": row["code"], "name": row["name"]}
                for row in level3_rows if row["code"] in allowed[item["source_question_no"]]
            ],
        } for item in claim.questions]
        proposal = {number: _normalize_level3(row) for number, row in _parse_level3(
            _runner_stage(claim, "proposal", _prompt("proposal", scoped, [])),
            numbers, allowed,
        ).items()}
        verifier = {number: _normalize_level3(row) for number, row in _parse_level3(
            _runner_stage(claim, "verifier", _prompt("verifier", scoped, [])),
            numbers, allowed,
        ).items()}
        pending = {
            number for number in numbers if not (
                level2[number]["confidence"] == "high"
                and proposal[number]["confidence"] == verifier[number]["confidence"] == "high"
                and _classification_vote(proposal[number]) == _classification_vote(verifier[number])
            )
        }
        adjudicator: dict[str, dict[str, Any]] = {}
        if pending:
            adjudicator = {
                number: _normalize_level3(row)
                for number, row in _parse_level3(
                    _runner_stage(claim, "adjudicator", _prompt(
                        "adjudicator",
                        [item for item in scoped if item["source_question_no"] in pending],
                        [],
                    )), pending, {number: allowed[number] for number in pending},
                ).items()
            }
        evidence_questions = []
        for source in claim.questions:
            number = source["source_question_no"]
            first, second, third = proposal[number], verifier[number], adjudicator.get(number)
            double = (
                level2[number]["confidence"] == "high"
                and first["confidence"] == second["confidence"] == "high"
                and _classification_vote(first) == _classification_vote(second)
            )
            adjudicated = (
                not double and level2[number]["confidence"] == "high" and third is not None
                and third["confidence"] == "high"
            )
            final = third if adjudicated else first
            evidence_questions.append({
                "source_question_no": number,
                "approved_draft_version": source["approved_draft_version"],
                "edited_sha256": source["edited_sha256"],
                "classification_scope_sha256": source["classification_scope_sha256"],
                "candidate_sha256": claim.snapshot.candidate_sha256,
                "audit_sha256": claim.snapshot.audit_sha256,
                "draft_bindings_sha256": claim.snapshot.draft_bindings_sha256,
                "taxonomy_sha256": claim.snapshot.taxonomy_sha256,
                "level2": level2[number], "proposal": first, "verifier": second,
                "adjudicator": third,
                "final_primary_code": final["primary_code"],
                "final_related_codes": final["related_codes"],
                "final_reason": final["reason"],
                "status": "approved" if double or adjudicated else "pending",
                "approval_source": (
                    "codex_double_pass" if double else "codex_adjudicated" if adjudicated else None
                ),
            })
        evidence = {
            "version": 1, "scope": "historical_recovery_residual",
            "job_id": claim.job_id, "input_sha256": claim.snapshot.input_sha256,
            "full_question_nos": list(claim.snapshot.full_question_nos),
            "existing_question_nos": list(claim.snapshot.existing_question_nos),
            "residual_question_nos": list(claim.snapshot.residual_question_nos),
            "formal_question_count": claim.snapshot.formal_question_count,
            "formal_batch_sha256": claim.snapshot.formal_batch_sha256,
            "crop_manifest_sha256": claim.snapshot.crop_manifest_sha256,
            "crop_generation_id": claim.snapshot.crop_generation_id,
            "crop_manifest_signature": claim.snapshot.crop_manifest_signature,
            "audit_completed_at": claim.snapshot.audit_completed_at,
            "questions": evidence_questions,
        }
        evidence_raw = _canonical(evidence)
        evidence_sha = hashlib.sha256(evidence_raw.encode("utf-8")).hexdigest()
        job_dir = claim.private_root / "processing" / f"import_job_{claim.job_id}"
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_claim_identity(claim)
            with _claim_sqlite_connection(claim) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                current, _, _, _ = _residual_snapshot(
                    connection, claim.private_root, claim.job_id, artifact_lock=lock
                )
                if current.input_sha256 != claim.snapshot.input_sha256:
                    _fail()
                changed = connection.execute(
                    """UPDATE historical_residual_classification_runs
                       SET status='completed',stage='completed',evidence_json=?,
                           evidence_sha256=?,claim_token=NULL,completed_at=?,error_message=NULL,
                           heartbeat_at=?,lease_expires_at=NULL,updated_at=?
                       WHERE import_job_id=? AND status='processing' AND claim_token=?
                         AND input_sha256=?""",
                    (evidence_raw, evidence_sha, _now(), _now(), _now(), claim.job_id,
                     claim.claim_token, claim.snapshot.input_sha256),
                ).rowcount
                if changed != 1:
                    _fail()
                connection.commit()
        _verify_claim_identity(claim)
        return HistoricalResidualClassificationResult(
            claim.job_id, claim.snapshot.residual_question_nos,
            len(evidence_questions), evidence_sha,
        )
    except HistoricalResidualError:
        try:
            _verify_claim_identity(claim)
        except HistoricalResidualError:
            pass
        else:
            with _claim_sqlite_connection(claim) as connection:
                connection.execute(
                    """UPDATE historical_residual_classification_runs
                       SET status='failed',claim_token=NULL,error_message=?,
                           heartbeat_at=?,lease_expires_at=NULL,updated_at=?
                       WHERE import_job_id=? AND claim_token=?""",
                    (SAFE_RESIDUAL, _now(), _now(), claim.job_id, claim.claim_token),
                )
                connection.commit()
        raise
    except Exception as exc:
        try:
            _verify_claim_identity(claim)
        except HistoricalResidualError:
            pass
        else:
            with _claim_sqlite_connection(claim) as connection:
                connection.execute(
                    """UPDATE historical_residual_classification_runs
                       SET status='failed',claim_token=NULL,error_message=?,
                           heartbeat_at=?,lease_expires_at=NULL,updated_at=?
                       WHERE import_job_id=? AND claim_token=?""",
                    (SAFE_RESIDUAL, _now(), _now(), claim.job_id, claim.claim_token),
                )
                connection.commit()
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


@_pinned_entry
def apply_historical_residual_classification(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    private_root: str | Path | None = None,
    job_id: int = 1,
) -> HistoricalResidualClassificationResult:
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_locked_job(private_bound, job_id, lock.descriptor)
            with _pinned_sqlite_connection(database_bound, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                snapshot, questions, taxonomy, _ = _residual_snapshot(
                    connection, private_root, job_id, artifact_lock=lock
                )
                run = connection.execute(
                    "SELECT * FROM historical_residual_classification_runs WHERE import_job_id=?",
                    (job_id,),
                ).fetchone()
                if run is None or run["status"] != "completed" or run["input_sha256"] != snapshot.input_sha256:
                    _fail()
                raw = run["evidence_json"]
                if hashlib.sha256(raw.encode("utf-8")).hexdigest() != run["evidence_sha256"]:
                    _fail()
                evidence = json.loads(raw)
                rows = evidence.get("questions") if isinstance(evidence, dict) else None
                effective_rows = (
                    [
                        _effective_evidence_row(
                            row,
                            source=next(
                                (item for item in questions
                                 if item["source_question_no"] == row.get("source_question_no")),
                                {},
                            ),
                            snapshot=snapshot,
                            taxonomy=taxonomy,
                        )
                        for row in rows if isinstance(row, dict)
                    ]
                    if isinstance(rows, list)
                    else None
                )
                if (
                    evidence.get("scope") != "historical_recovery_residual"
                    or tuple(evidence.get("residual_question_nos", ())) != snapshot.residual_question_nos
                    or not isinstance(rows, list) or len(rows) != len(questions)
                    or effective_rows is None
                    or any(row is None for row in effective_rows)
                ):
                    _fail("剩余题分类仍需独立复核或裁决")
                effective_rows = [row for row in effective_rows if row is not None]
                by_number = {row["source_question_no"]: row for row in effective_rows}
                inserted = 0
                classifier_run_id = f"historical-residual-kc-{job_id}-{snapshot.input_sha256[:12]}"
                now = _now()
                for source in questions:
                    number = source["source_question_no"]
                    row = by_number.get(number)
                    if (
                        row is None
                        or row["approved_draft_version"] != source["approved_draft_version"]
                        or row["edited_sha256"] != source["edited_sha256"]
                        or row["classification_scope_sha256"] != source["classification_scope_sha256"]
                        or row["candidate_sha256"] != snapshot.candidate_sha256
                        or row["audit_sha256"] != snapshot.audit_sha256
                        or row["draft_bindings_sha256"] != snapshot.draft_bindings_sha256
                        or row["taxonomy_sha256"] != snapshot.taxonomy_sha256
                    ):
                        _fail()
                    related = row["final_related_codes"]
                    existing = connection.execute(
                        """SELECT * FROM candidate_knowledge_classifications
                           WHERE import_job_id=? AND source_question_no=?
                             AND approved_draft_version=? AND edited_sha256=?""",
                        (job_id, number, source["approved_draft_version"], source["edited_sha256"]),
                    ).fetchone()
                    expected = (
                        row["final_primary_code"], _canonical(related), row["approval_source"],
                        classifier_run_id, run["evidence_sha256"], row["final_reason"],
                    )
                    if existing is not None:
                        actual = tuple(existing[key] for key in (
                            "primary_knowledge_point_code",
                            "related_knowledge_point_codes_json", "approval_source",
                            "classifier_run_id", "evidence_sha256", "reason",
                        ))
                        if actual != expected:
                            _fail()
                        continue
                    connection.execute(
                        """INSERT INTO candidate_knowledge_classifications
                           (import_job_id,source_question_no,approved_draft_version,
                            edited_sha256,classification_scope_sha256,
                            primary_knowledge_point_code,related_knowledge_point_codes_json,
                            classifier,reviewer,approval_source,classifier_run_id,
                            evidence_sha256,reason,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (job_id, number, source["approved_draft_version"], source["edited_sha256"],
                         source["classification_scope_sha256"], row["final_primary_code"],
                         _canonical(related), "codex-residual-multi-pass",
                         "codex_double_pass" if row["approval_source"] == "codex_double_pass"
                         else "codex_adjudicator", row["approval_source"], classifier_run_id,
                         run["evidence_sha256"], row["final_reason"], now),
                    )
                    inserted += 1
                _verify_bound_paths(database_bound, private_bound)
                _verify_locked_job(private_bound, job_id, lock.descriptor)
                connection.commit()
                return HistoricalResidualClassificationResult(
                    job_id, snapshot.residual_question_nos, len(questions),
                    run["evidence_sha256"], True, inserted,
                )
    except HistoricalResidualError:
        raise
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


def _no_answer_decision_in_connection(
    connection: sqlite3.Connection, private_root: Path, job_id: int, *, artifact_lock: Any,
    confirmation_receipt: bytes | None,
    expected_receipt_sha256: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    snapshot, prepared, _, _ = _residual_snapshot(
        connection, private_root, job_id, artifact_lock=artifact_lock
    )
    recovery = connection.execute(
        "SELECT * FROM historical_v1_crop_recoveries WHERE import_job_id=?", (job_id,)
    ).fetchone()
    resumption = connection.execute(
        "SELECT * FROM historical_v1_pipeline_resumptions WHERE import_job_id=?", (job_id,)
    ).fetchone()
    if recovery is None or resumption is None:
        _fail()
    authority_evidence, authority_artifact_sha = _verify_no_answer_authority(
        connection, private_root, job_id, artifact_lock=artifact_lock,
        confirmation_receipt=confirmation_receipt,
        expected_receipt_sha256=expected_receipt_sha256,
    )
    evidence = {
        "version": 1,
        "scope": "historical_recovery_residual_answer_source",
        "authority": "local_archived_user_confirmation",
        "authority_artifact_sha256": authority_artifact_sha,
        "provenance": authority_evidence["provenance"],
        "confirmation_text_summary": authority_evidence["confirmation_text_summary"],
        "confirmation_text_sha256": authority_evidence["confirmation_text_sha256"],
        "session_reference": authority_evidence["session_reference"],
        "recorded_at": authority_evidence["recorded_at"],
        "receipt_sha256": authority_evidence["receipt_sha256"],
        "import_job_id": job_id,
        "source_paper_id": recovery["source_paper_id"],
        "source_pdf_sha256": recovery["source_pdf_sha256"],
        "recovery_record_sha256": _digest(dict(recovery)),
        "resumption_record_sha256": _digest(dict(resumption)),
        "source_answer_state": "source_has_no_answer",
        "answer_pages": None,
        "candidate_sha256": snapshot.candidate_sha256,
        "audit_sha256": snapshot.audit_sha256,
        "crop_manifest_sha256": snapshot.crop_manifest_sha256,
        "formal_question_count": snapshot.formal_question_count,
        "formal_batch_sha256": snapshot.formal_batch_sha256,
        "full_question_nos": snapshot.full_question_nos,
        "existing_question_nos": snapshot.existing_question_nos,
        "residual_question_nos": snapshot.residual_question_nos,
        "draft_bindings_sha256": snapshot.draft_bindings_sha256,
        "residual_drafts": [
            {
                key: item[key] for key in (
                    "source_question_no", "approved_draft_version", "edited_sha256",
                    "classification_scope_sha256", "approval_source",
                    "approval_evidence_sha256",
                )
            }
            for item in prepared
        ],
        "evidence_summary": {
            "decision": "original_paper_contains_no_answers",
            "residual_question_count": len(snapshot.residual_question_nos),
            "all_residual_drafts_freshly_approved": True,
        },
    }
    evidence_raw = _canonical(evidence)
    evidence_sha = hashlib.sha256(evidence_raw.encode("utf-8")).hexdigest()
    token = hashlib.sha256(
        f"confirm-historical-residual-no-answer:{evidence_sha}".encode("utf-8")
    ).hexdigest()
    return evidence, evidence_raw, token


@_pinned_entry
def assess_historical_residual_no_answer_source(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    private_root: str | Path | None = None,
    job_id: int = 1,
    *, confirmation_receipt: bytes | None = None,
) -> dict[str, Any]:
    """Recompute the exact authority payload without writing any state."""
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_locked_job(private_bound, job_id, lock.descriptor)
            with _pinned_sqlite_connection(database_bound) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                evidence, evidence_raw, token = _no_answer_decision_in_connection(
                    connection, private_root, job_id, artifact_lock=lock,
                    confirmation_receipt=confirmation_receipt,
                )
                result = {
                    "job_id": job_id, "source_answer_state": "source_has_no_answer",
                    "evidence_sha256": hashlib.sha256(evidence_raw.encode("utf-8")).hexdigest(),
                    "confirmation_token": token, "evidence": evidence,
                }
                _verify_locked_job(private_bound, job_id, lock.descriptor)
                return result
    except HistoricalResidualError:
        raise
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


@_pinned_entry
def register_historical_residual_no_answer_source(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    private_root: str | Path | None = None,
    job_id: int = 1,
    *, confirmation_token: str | None = None,
    confirmation_receipt: bytes | None = None,
) -> dict[str, Any]:
    """Persist one immutable, explicitly confirmed no-answer authority record."""
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    if not isinstance(confirmation_token, str) or len(confirmation_token) != 64:
        _fail(SAFE_CONFIRMATION)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_locked_job(private_bound, job_id, lock.descriptor)
            with _pinned_sqlite_connection(database_bound, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                evidence, evidence_raw, expected_token = _no_answer_decision_in_connection(
                    connection, private_root, job_id, artifact_lock=lock,
                    confirmation_receipt=confirmation_receipt,
                )
                if confirmation_token != expected_token:
                    _fail(SAFE_CONFIRMATION)
                evidence_sha = hashlib.sha256(evidence_raw.encode("utf-8")).hexdigest()
                authority = connection.execute(
                    "SELECT * FROM historical_residual_no_answer_decisions WHERE import_job_id=?",
                    (job_id,),
                ).fetchone()
                if authority is not None:
                    if tuple(authority[key] for key in (
                        "confirmation_token", "evidence_json", "evidence_sha256",
                    )) != (expected_token, evidence_raw, evidence_sha):
                        _fail("已有不一致的原卷无答案 authority record")
                if connection.execute(
                    "SELECT 1 FROM import_answer_extraction_runs WHERE import_job_id=?",
                    (job_id,),
                ).fetchone() is not None:
                    _fail("答案来源已有处理记录，不能登记原卷无答案")
                expected = (
                    "source_has_no_answer", None, None, None,
                    evidence["candidate_sha256"], evidence["draft_bindings_sha256"],
                    len(evidence["full_question_nos"]), evidence_sha,
                )
                existing = connection.execute(
                    "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
                ).fetchone()
                if existing is not None:
                    actual = tuple(existing[key] for key in (
                        "source_answer_state", "answer_page_start", "answer_page_end",
                        "render_manifest_sha256", "candidate_sha256", "draft_batch_sha256",
                        "expected_question_count", "classification_evidence_sha256",
                    ))
                    if actual != expected:
                        _fail("已有不一致的官方答案来源登记")
                    if authority is None:
                        _fail("答案来源缺少专用不可变 authority record")
                    _verify_bound_paths(database_bound, private_bound)
                    _verify_locked_job(private_bound, job_id, lock.descriptor)
                    connection.commit()
                    return {"job_id": job_id, "source_answer_state": expected[0],
                            "evidence_sha256": evidence_sha,
                            "confirmation_token": expected_token,
                            "already_registered": True}
                now = _now()
                if authority is None:
                    connection.execute(
                        """INSERT INTO historical_residual_no_answer_decisions
                           (import_job_id,confirmation_token,evidence_json,evidence_sha256,decided_at)
                           VALUES(?,?,?,?,?)""",
                        (job_id, expected_token, evidence_raw, evidence_sha, now),
                    )
                connection.execute(
                    """INSERT INTO import_answer_sources
                       (import_job_id,source_answer_state,answer_page_start,
                        answer_page_end,render_manifest_sha256,candidate_sha256,
                        draft_batch_sha256,expected_question_count,
                        classification_evidence_sha256,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, *expected, now, now),
                )
                _verify_bound_paths(database_bound, private_bound)
                _verify_locked_job(private_bound, job_id, lock.descriptor)
                connection.commit()
                return {
                    "job_id": job_id, "source_answer_state": expected[0],
                    "evidence_sha256": evidence_sha, "confirmation_token": expected_token,
                    "already_registered": False,
                }
    except HistoricalResidualError:
        raise
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


def _assessment_payload(
    snapshot: ResidualSnapshot,
    eligible: tuple[str, ...],
    ineligible: tuple[dict[str, Any], ...],
    classification_sha: str,
) -> dict[str, Any]:
    return {
        "scope": "historical_recovery_residual_admission",
        "source_filename": snapshot.source_filename,
        "paper_title": snapshot.paper_title,
        "job_id": snapshot.job_id,
        "total_question_count": snapshot.total_question_count,
        "full_question_nos": snapshot.full_question_nos,
        "existing_question_nos": snapshot.existing_question_nos,
        "residual_question_nos": snapshot.residual_question_nos,
        "formal_question_count": snapshot.formal_question_count,
        "formal_batch_sha256": snapshot.formal_batch_sha256,
        "candidate_sha256": snapshot.candidate_sha256,
        "audit_sha256": snapshot.audit_sha256,
        "crop_manifest_sha256": snapshot.crop_manifest_sha256,
        "classification_evidence_sha256": classification_sha,
        "eligible": eligible,
        "ineligible": ineligible,
    }


def _applied_classification_is_exact(
    connection: sqlite3.Connection,
    job_id: int,
    snapshot: ResidualSnapshot,
    prepared: tuple[dict[str, Any], ...],
    run: sqlite3.Row,
    taxonomy: tuple[dict[str, Any], ...],
) -> bool:
    try:
        evidence = json.loads(run["evidence_json"])
        evidence_rows = evidence["questions"]
        sources = {item["source_question_no"]: item for item in prepared}
        effective_rows = [
            _effective_evidence_row(
                row, source=sources.get(row.get("source_question_no"), {}),
                snapshot=snapshot, taxonomy=taxonomy,
            )
            for row in evidence_rows if isinstance(row, dict)
        ]
        if any(row is None for row in effective_rows):
            return False
        by_number = {
            row["source_question_no"]: row
            for row in effective_rows
            if row is not None
        }
    except (TypeError, KeyError, json.JSONDecodeError):
        return False
    if (
        hashlib.sha256(run["evidence_json"].encode("utf-8")).hexdigest()
        != run["evidence_sha256"]
        or tuple(by_number) != snapshot.residual_question_nos
    ):
        return False
    classifier_run_id = f"historical-residual-kc-{job_id}-{snapshot.input_sha256[:12]}"
    for source in prepared:
        number = source["source_question_no"]
        evidence_row = by_number[number]
        row = connection.execute(
            """SELECT * FROM candidate_knowledge_classifications
               WHERE import_job_id=? AND source_question_no=?
                 AND approved_draft_version=? AND edited_sha256=?""",
            (job_id, number, source["approved_draft_version"], source["edited_sha256"]),
        ).fetchone()
        if row is None or tuple(row[key] for key in (
            "classification_scope_sha256", "primary_knowledge_point_code",
            "related_knowledge_point_codes_json", "approval_source",
            "classifier_run_id", "evidence_sha256", "reason",
        )) != (
            source["classification_scope_sha256"], evidence_row["final_primary_code"],
            _canonical(evidence_row["final_related_codes"]),
            evidence_row["approval_source"], classifier_run_id,
            run["evidence_sha256"], evidence_row["final_reason"],
        ):
            return False
    return True


def _assess_residual_in_connection(
    connection: sqlite3.Connection, private_root: Path, job_id: int, *, artifact_lock: Any,
) -> tuple[HistoricalResidualAssessment, ResidualSnapshot, dict[str, Any], tuple[Any, ...]]:
    snapshot, prepared, taxonomy, context = _residual_snapshot(
        connection, private_root, job_id, artifact_lock=artifact_lock
    )
    answer_source = connection.execute(
        "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
    ).fetchone()
    if answer_source is not None and answer_source["source_answer_state"] == "source_has_no_answer":
        authority = connection.execute(
            "SELECT * FROM historical_residual_no_answer_decisions WHERE import_job_id=?",
            (job_id,),
        ).fetchone()
        try:
            archived_receipt_sha = json.loads(authority["evidence_json"])["receipt_sha256"]
        except (TypeError, KeyError, json.JSONDecodeError):
            _fail("原卷无答案登记缺少精确不可变 authority record")
        _, evidence_raw, confirmation_token = _no_answer_decision_in_connection(
            connection, private_root, job_id, artifact_lock=artifact_lock,
            confirmation_receipt=None,
            expected_receipt_sha256=archived_receipt_sha,
        )
        evidence_sha = hashlib.sha256(evidence_raw.encode("utf-8")).hexdigest()
        if (
            authority is None
            or tuple(authority[key] for key in (
                "confirmation_token", "evidence_json", "evidence_sha256",
            )) != (confirmation_token, evidence_raw, evidence_sha)
            or tuple(answer_source[key] for key in (
                "candidate_sha256", "draft_batch_sha256", "expected_question_count",
                "classification_evidence_sha256",
            )) != (
                snapshot.candidate_sha256, snapshot.draft_bindings_sha256,
                snapshot.total_question_count, evidence_sha,
            )
        ):
            _fail("原卷无答案登记缺少精确不可变 authority record")
    run = connection.execute(
        "SELECT * FROM historical_residual_classification_runs "
        "WHERE import_job_id=?", (job_id,),
    ).fetchone()
    if run is None or run["status"] != "completed" or run["input_sha256"] != snapshot.input_sha256:
        _fail("缺少 fresh residual classification evidence")
    effective = _effective_questions(connection, context)
    classification_exact = _applied_classification_is_exact(
        connection, job_id, snapshot, prepared, run, taxonomy
    )
    if classification_exact:
        effective = dict(effective)
        for source in prepared:
            number = source["source_question_no"]
            classification = connection.execute(
                """SELECT primary_knowledge_point_code,
                          related_knowledge_point_codes_json
                   FROM candidate_knowledge_classifications
                   WHERE import_job_id=? AND source_question_no=?
                     AND approved_draft_version=? AND edited_sha256=?""",
                (job_id, number, source["approved_draft_version"], source["edited_sha256"]),
            ).fetchone()
            if classification is None:
                _fail()
            question = dict(effective[number][0])
            question["primary_knowledge_point_code"] = classification[0]
            question["related_knowledge_point_codes"] = json.loads(classification[1])
            effective[number] = (question, *effective[number][1:])
    report = _assess(connection, context, effective)
    by_ineligible = {item.question_no: item.reasons for item in report.ineligible}
    if not classification_exact:
        for number in snapshot.residual_question_nos:
            by_ineligible[number] = tuple(dict.fromkeys((
                *by_ineligible.get(number, ()),
                "residual_classification_evidence_invalid",
            )))
    eligible = tuple(
        number for number in snapshot.residual_question_nos if number not in by_ineligible
    )
    ineligible = tuple(
        {"source_question_no": number, "reasons": by_ineligible[number]}
        for number in snapshot.residual_question_nos if number in by_ineligible
    )
    payload = _assessment_payload(snapshot, eligible, ineligible, run["evidence_sha256"])
    assessment_sha = _digest(payload)
    confirmation = hashlib.sha256(
        f"confirm-historical-residual-admission:{assessment_sha}".encode("utf-8")
    ).hexdigest()
    result = HistoricalResidualAssessment(
        "ready" if not ineligible and eligible == snapshot.residual_question_nos else "blocked",
        snapshot.source_filename, snapshot.paper_title, job_id, snapshot.total_question_count,
        snapshot.existing_question_nos, snapshot.residual_question_nos, eligible, ineligible,
        confirmation, assessment_sha,
    )
    return result, snapshot, effective, context


@_pinned_entry
def assess_historical_residual_admission(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    private_root: str | Path | None = None,
    job_id: int = 1,
) -> HistoricalResidualAssessment:
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_locked_job(private_bound, job_id, lock.descriptor)
            with _pinned_sqlite_connection(database_bound) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                result, _, _, _ = _assess_residual_in_connection(
                    connection, private_root, job_id, artifact_lock=lock
                )
                _verify_locked_job(private_bound, job_id, lock.descriptor)
                return result
    except HistoricalResidualError:
        raise
    except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc


def _remove_unreferenced_new_backup(
    database_path: Any,
    backup_path: Path,
    identity: BackupFileIdentity | tuple[int, int, int],
    database_identity: tuple[int, int] | None = None,
) -> None:
    """Quarantine and remove only this attempt's exact unreferenced inode."""
    if not hasattr(database_path, "descriptor"):
        database = Path(database_path)
        try:
            with _pinned_recovery_paths(database, database.parent) as (pinned, _):
                _remove_unreferenced_new_backup(
                    pinned, backup_path, identity, database_identity,
                )
        except HistoricalV1RecoveryError:
            pass
        return
    if isinstance(identity, BackupFileIdentity):
        file_identity = (identity.file_dev, identity.file_ino, identity.file_size)
        parent_identity = (identity.parent_dev, identity.parent_ino)
    else:
        file_identity = identity
        parent_identity = None
    directory_fd = None
    connection = None
    connection_context = None
    quarantine = None
    candidate = Path(backup_path).name

    def restore() -> None:
        if directory_fd is None or quarantine is None:
            return
        try:
            os.link(
                quarantine, candidate,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(quarantine, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            # Never overwrite an entry concurrently placed at the public name.
            # Keeping the quarantined inode is safer than deleting uncertainty.
            return

    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory or candidate in {"", ".", ".."}:
            return
        parent = Path(os.path.abspath(os.fspath(Path(backup_path).parent)))
        if parent.parts[:2] == (os.sep, "var"):
            parent = Path("/private").joinpath(*parent.parts[1:])
        directory_fd = os.open(os.sep, os.O_RDONLY | directory | nofollow)
        for part in parent.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | directory | nofollow, dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child
        parent_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_identity is not None
            and (parent_info.st_dev, parent_info.st_ino) != parent_identity
        ):
            return
        pinned_info = os.fstat(database_path.descriptor)
        if database_identity is not None and (
            pinned_info.st_dev, pinned_info.st_ino
        ) != database_identity:
            return
        connection_context = _pinned_sqlite_connection(database_path, timeout=10)
        connection = connection_context.__enter__()
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("BEGIN IMMEDIATE")
        referenced = connection.execute(
            "SELECT 1 FROM historical_residual_admissions WHERE backup_path=?",
            (str(backup_path),),
        ).fetchone()
        if referenced is not None:
            connection.rollback()
            return

        quarantine = f".historical-residual-cleanup-{secrets.token_hex(16)}"
        os.rename(
            candidate, quarantine,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        quarantine_fd = os.open(
            quarantine, os.O_RDONLY | nofollow, dir_fd=directory_fd,
        )
        try:
            current = os.fstat(quarantine_fd)
        finally:
            os.close(quarantine_fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino, current.st_size) != file_identity
        ):
            restore()
            connection.rollback()
            return

        referenced = connection.execute(
            "SELECT 1 FROM historical_residual_admissions WHERE backup_path=?",
            (str(backup_path),),
        ).fetchone()
        if referenced is not None:
            restore()
            connection.rollback()
            return
        os.unlink(quarantine, dir_fd=directory_fd)
        quarantine = None
        os.fsync(directory_fd)
        connection.commit()
    except (OSError, sqlite3.Error, HistoricalV1RecoveryError):
        restore()
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        return
    finally:
        if connection_context is not None:
            try:
                connection_context.__exit__(None, None, None)
            except HistoricalV1RecoveryError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)


@_pinned_entry
def admit_historical_residual_questions(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    private_root: str | Path | None = None,
    job_id: int = 1,
    *,
    confirmation_token: str | None = None,
    backup_dir: str | Path | None = None,
    _transaction_callback: Any | None = None,
) -> HistoricalResidualAdmissionResult:
    database_bound, private_bound = database_path, private_root
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    if not isinstance(confirmation_token, str) or len(confirmation_token) != 64:
        _fail(SAFE_CONFIRMATION)
    with _pinned_sqlite_connection(database_bound) as check:
        check.row_factory = sqlite3.Row
        prior = check.execute(
            "SELECT * FROM historical_residual_admissions WHERE import_job_id=?", (job_id,)
        ).fetchone()
        if prior is not None:
            if prior["confirmation_token"] != confirmation_token:
                _fail(SAFE_CONFIRMATION)
            numbers = tuple(json.loads(prior["residual_question_nos_json"]))
            final_count, final_sha, final_numbers = _formal_snapshot(check, job_id)
            job = check.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                job is None or job["status"] != "completed"
                or final_count != prior["final_formal_question_count"]
                or final_sha != prior["final_formal_batch_sha256"]
                or tuple(final_numbers) != tuple(json.loads(prior["full_question_nos_json"]))
            ):
                _fail()
            return HistoricalResidualAdmissionResult(
                0, len(numbers), True, prior["backup_path"], prior["backup_sha256"],
                tuple(),
            )
    dry = assess_historical_residual_admission(database_path, private_root, job_id)
    if dry.status != "ready" or confirmation_token != dry.confirmation_token:
        _fail(SAFE_CONFIRMATION)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    backup_path: Path | None = None
    backup_identity: BackupFileIdentity | None = None
    committed = False
    try:
        with _job_artifact_lock(job_dir, create=False) as lock:
            _verify_locked_job(private_bound, job_id, lock.descriptor)
            with _pinned_sqlite_connection(database_bound, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("BEGIN IMMEDIATE")
                if _transaction_callback is not None:
                    _transaction_callback(connection)
                current, snapshot, effective, context = _assess_residual_in_connection(
                    connection, private_root, job_id, artifact_lock=lock
                )
                if (
                    current.status != "ready"
                    or current.confirmation_token != confirmation_token
                    or current.assessment_sha256 != dry.assessment_sha256
                ):
                    _fail(SAFE_CONFIRMATION)
                # BEGIN IMMEDIATE prevents any other writer from changing the
                # state after this revalidation.  The online backup is made
                # only now, so no rejected drift can leave a misleading copy.
                backup_path, backup_sha, backup_identity = backup_database(
                    database_path, backup_dir, _with_identity=True,
                    _source_connection=connection,
                )
                codes = []
                for number in snapshot.residual_question_nos:
                    code = _code(context[0]["sha256"], number)
                    if connection.execute(
                        "SELECT 1 FROM questions WHERE question_code=?", (code,)
                    ).fetchone() is not None:
                        _fail()
                    _insert_one(connection, context, effective[number][0], code, effective[number][1])
                    codes.append(code)
                verify_backup_file_identity(backup_path, backup_identity)
                _verify_artifact_snapshots(lock.descriptor, context[9])
                rows = connection.execute(
                    """SELECT s.source_question_no,COUNT(*) FROM question_sources s
                       WHERE s.import_job_id=? GROUP BY s.source_question_no
                       ORDER BY CAST(s.source_question_no AS INTEGER)""", (job_id,),
                ).fetchall()
                if (
                    tuple(row[0] for row in rows) != snapshot.full_question_nos
                    or any(row[1] != 1 for row in rows)
                    or len(rows) != snapshot.total_question_count
                    or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
                    or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                ):
                    _fail()
                final_count, final_sha, _ = _formal_snapshot(connection, job_id)
                classification_sha = connection.execute(
                    "SELECT evidence_sha256 FROM historical_residual_classification_runs "
                    "WHERE import_job_id=?", (job_id,),
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO historical_residual_admissions
                       (import_job_id,confirmation_token,assessment_sha256,
                        full_question_nos_json,existing_question_nos_json,
                        residual_question_nos_json,baseline_formal_question_count,
                        baseline_formal_batch_sha256,candidate_sha256,audit_sha256,
                        crop_manifest_sha256,classification_evidence_sha256,
                        backup_path,backup_sha256,inserted_count,
                        final_formal_question_count,final_formal_batch_sha256,completed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, confirmation_token, current.assessment_sha256,
                     _canonical(list(snapshot.full_question_nos)),
                     _canonical(list(snapshot.existing_question_nos)),
                     _canonical(list(snapshot.residual_question_nos)),
                     snapshot.formal_question_count, snapshot.formal_batch_sha256,
                     snapshot.candidate_sha256, snapshot.audit_sha256,
                     snapshot.crop_manifest_sha256, classification_sha,
                     str(backup_path), backup_sha, len(codes), final_count, final_sha, _now()),
                )
                connection.execute(
                    "UPDATE import_jobs SET status='completed',updated_at=? WHERE id=?",
                    (_now(), job_id),
                )
                verify_backup_file_identity(backup_path, backup_identity)
                _verify_bound_paths(database_bound, private_bound)
                _verify_locked_job(private_bound, job_id, lock.descriptor)
                connection.commit()
                committed = True
                return HistoricalResidualAdmissionResult(
                    len(codes), 0, True, str(backup_path), backup_sha, tuple(codes)
                )
    except HistoricalResidualError:
        raise
    except (AdmissionError, sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
        raise HistoricalResidualError(SAFE_RESIDUAL) from exc
    finally:
        if not committed and backup_path is not None and backup_identity is not None:
            pinned_database_info = os.fstat(database_bound.descriptor)
            _remove_unreferenced_new_backup(
                database_bound, backup_path, backup_identity,
                (pinned_database_info.st_dev, pinned_database_info.st_ino),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="历史恢复任务剩余题分类与增量准入")
    parser.add_argument(
        "action",
        choices=(
            "classify", "apply-classification", "assess-no-answer",
            "register-no-answer", "dry-run", "admit",
        ),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--confirmation-token")
    parser.add_argument("--confirmation-receipt", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args(argv)
    receipt = None
    if args.confirmation_receipt is not None:
        descriptor = os.open(
            args.confirmation_receipt, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 65536:
                _fail("用户确认 receipt 文件无效")
            receipt = os.read(descriptor, info.st_size + 1)
            if len(receipt) != info.st_size:
                _fail("用户确认 receipt 文件无效")
        finally:
            os.close(descriptor)
    if args.action == "classify":
        claim = claim_historical_residual_classification(
            args.database, args.private_root, args.job_id
        )
        result: Any = run_claimed_historical_residual_classification(claim)
    elif args.action == "apply-classification":
        result = apply_historical_residual_classification(
            args.database, args.private_root, args.job_id
        )
    elif args.action == "assess-no-answer":
        result = assess_historical_residual_no_answer_source(
            args.database, args.private_root, args.job_id,
            confirmation_receipt=receipt,
        )
    elif args.action == "register-no-answer":
        result = register_historical_residual_no_answer_source(
            args.database, args.private_root, args.job_id,
            confirmation_token=args.confirmation_token,
            confirmation_receipt=receipt,
        )
    elif args.action == "dry-run":
        result = assess_historical_residual_admission(
            args.database, args.private_root, args.job_id
        )
    else:
        result = admit_historical_residual_questions(
            args.database, args.private_root, args.job_id,
            confirmation_token=args.confirmation_token, backup_dir=args.backup_dir,
        )
    print(_canonical(result if isinstance(result, dict) else asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
