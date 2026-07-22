"""Fail-closed AI approvals for pristine and corrected candidate drafts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.processing.candidate_extractor import _open_job_and_lock
from src.processing.secure_crop_artifacts import SecureCropArtifactError, read_file_at
from src.reviewing.candidate_auditor import (
    MAX_AUDIT_BYTES,
    SAFE_AUDIT_ERROR,
    CandidateAuditCodexCliRunner,
    CandidateAuditError,
    CandidateAuditRunResult,
    _audit_prompt,
    _database_input_from_connection,
    _read_inputs,
    parse_candidate_audit_output,
)


SAFE_REAUDIT_INPUT = "修正后单题复审输入或证据锚点无效"
SAFE_REAUDIT_BUSY = "修正后单题复审正在处理，请稍后刷新"
SAFE_REAUDIT_ERROR = "修正后单题AI复审失败，请重试"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
ALLOWED_EDIT_FIELDS = {
    "stem_markdown", "question_type_code", "primary_knowledge_point_code",
    "related_knowledge_point_codes", "options", "subquestions",
}


@dataclass(frozen=True)
class BatchApprovalResult:
    eligible: int
    changed: int


@dataclass(frozen=True)
class CorrectedDraftAuditResult:
    record_id: int
    approved: bool
    decision: str


@dataclass
class CorrectedDraftAuditClaim:
    database_path: Path
    private_root: Path
    job_id: int
    question_no: str
    runner: Any
    edited: dict[str, Any]
    draft_version: int
    edited_sha256: str
    source_candidate_sha256: str
    source_snapshot_sha256: str
    batch_audit_sha256: str
    crop_generation_id: str
    crop_manifest_sha256: str
    crop_manifest_signature: str
    crop_relative_path: str
    crop_sha256: str
    crop_byte_size: int
    input_database_row: tuple
    batch_audit_row: tuple
    record_id: int
    image_paths: tuple[Path, ...]
    temporary: Any
    lock_fd: int | None
    job_fd: int | None

    def close(self) -> None:
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.job_fd is not None:
            os.close(self.job_fd)
            self.job_fd = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id) is not None


def _audit_database_row(connection, job_id: int):
    return connection.execute(
        """SELECT status,question_count,processed_questions,codex_run_id,
                  input_candidate_sha256,input_candidate_byte_size,
                  input_crop_generation_id,input_manifest_sha256,
                  input_manifest_signature,output_sha256,output_byte_size,completed_at
           FROM import_candidate_audit_runs WHERE import_job_id=?""",
        (job_id,),
    ).fetchone()


def _completed_bundle(database_path: Path, private_root: Path, job_id: int,
                      job_fd: int, temporary_root: Path):
    input_row, manifest_sha, manifest, images, candidate, candidate_file = _read_inputs(
        database_path, private_root, job_id, job_fd, temporary_root
    )
    with closing(sqlite3.connect(database_path)) as connection:
        audit_row = _audit_database_row(connection, job_id)
    try:
        audit_file = read_file_at(job_fd, "ai_audit.json", max_bytes=MAX_AUDIT_BYTES)
        audit = parse_candidate_audit_output(
            audit_file.data.decode("utf-8"), job_id, candidate["questions"]
        )
    except (SecureCropArtifactError, UnicodeError) as exc:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT) from exc
    anchors = (
        candidate_file.sha256, candidate_file.size, manifest["generation_id"],
        manifest_sha, manifest["signature"], audit_file.sha256, audit_file.size,
    )
    if (
        audit_row is None or audit_row[0] != "completed"
        or audit_row[1] != len(candidate["questions"])
        or audit_row[2] != len(candidate["questions"])
        or tuple(audit_row[4:11]) != anchors
        or not _bounded_run_id(audit_row[3]) or not audit_row[11]
    ):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    return input_row, manifest_sha, manifest, images, candidate, candidate_file, audit, audit_file, tuple(audit_row)


def _release(job_fd, lock_fd, temporary) -> None:
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    if job_fd is not None:
        os.close(job_fd)
    if temporary is not None:
        temporary.cleanup()


def apply_batch_auto_pass(database_path, private_root, job_id) -> BatchApprovalResult:
    """Explicitly apply only pristine, DB-anchored batch auto-pass decisions."""
    database_path, private_root = Path(database_path), Path(private_root)
    job_fd = lock_fd = None
    temporary = None
    try:
        job_fd, lock_fd = _open_job_and_lock(private_root, job_id)
        if job_fd is None:
            raise CandidateAuditError(SAFE_REAUDIT_BUSY)
        temporary = tempfile.TemporaryDirectory(prefix="batch-auto-pass-read-")
        bundle = _completed_bundle(
            database_path, private_root, job_id, job_fd, Path(temporary.name)
        )
        input_row, _, _, _, candidate, candidate_file, audit, audit_file, audit_row = bundle
        audit_by_no = {item["source_question_no"]: item for item in audit["questions"]}
        questions = candidate["questions"]
        if set(audit_by_no) != {item["source_question_no"] for item in questions}:
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        now = _now()
        eligible = changed = 0
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            if tuple(_database_input_from_connection(connection, job_id)) != tuple(input_row):
                raise CandidateAuditError(SAFE_REAUDIT_INPUT)
            if tuple(_audit_database_row(connection, job_id)) != audit_row:
                raise CandidateAuditError(SAFE_REAUDIT_INPUT)
            for source in questions:
                number = source["source_question_no"]
                strict = audit_by_no[number]["audit_status"] == "auto_pass"
                snapshot = _canonical(source)
                row = connection.execute(
                    "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no=?",
                    (job_id, number),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """INSERT INTO candidate_review_drafts
                           (import_job_id,source_question_no,source_candidate_sha256,
                            source_snapshot_json,edited_json,status)
                           VALUES(?,?,?,?,?,'pending')""",
                        (job_id, number, candidate_file.sha256, snapshot, snapshot),
                    )
                    row = connection.execute(
                        "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no=?",
                        (job_id, number),
                    ).fetchone()
                if not strict:
                    continue
                eligible += 1
                if (
                    row["deleted_at"] is not None
                    or row["source_candidate_sha256"] != candidate_file.sha256
                    or _decode_object(row["source_snapshot_json"]) != source
                    or _decode_object(row["edited_json"]) != source
                ):
                    continue
                approved_version = (
                    row["version"] if row["status"] == "approved"
                    else row["version"] + 1
                )
                evidence = {
                    "method": "batch_auto_pass", "audit_output_sha256": audit_file.sha256,
                    "candidate_sha256": candidate_file.sha256,
                    "source_snapshot_sha256": _canonical_sha(source),
                    "edited_sha256": _canonical_sha(source), "audit_run_id": audit_row[3],
                    "audited_at": audit_row[11], "reviewed_at": now,
                    "approved_draft_version": approved_version,
                }
                encoded = _canonical(evidence)
                if row["status"] == "approved" and row["approval_source"] == "ai_second_pass":
                    continue
                if row["status"] != "pending" or row["approval_source"] is not None:
                    continue
                cursor = connection.execute(
                    """UPDATE candidate_review_drafts SET status='approved',version=?,
                              reviewed_at=?,approval_source='ai_second_pass',
                              approval_evidence_json=?,updated_at=?
                       WHERE id=? AND version=? AND status='pending' AND deleted_at IS NULL""",
                    (approved_version, now, encoded, now, row["id"], row["version"]),
                )
                changed += cursor.rowcount
            connection.commit()
        return BatchApprovalResult(eligible, changed)
    except CandidateAuditError:
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT) from exc
    finally:
        _release(job_fd, lock_fd, temporary)


def _decode_object(raw: object) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else None
    if not isinstance(value, dict):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    return value


def validate_ai_approval(connection, draft: dict[str, Any], candidate: dict[str, Any],
                         *, candidate_sha256: str, audit_sha256: str,
                         audit_entry: dict[str, Any]) -> bool:
    """Validate exact AI provenance; malformed/stale claims always return false."""
    try:
        if (
            draft.get("status") != "approved"
            or draft.get("approval_source") != "ai_second_pass"
            or draft.get("source_candidate_sha256") != candidate_sha256
            or _decode_object(draft.get("source_snapshot_json")) != candidate
            or draft.get("deleted_at") is not None
        ):
            return False
        edited = _decode_object(draft.get("edited_json"))
        evidence = _decode_object(draft.get("approval_evidence_json"))
        method = evidence.get("method")
        if method == "batch_auto_pass":
            row = _audit_database_row(connection, draft["import_job_id"])
            if row is None:
                return False
            expected = {
                "method": "batch_auto_pass", "audit_output_sha256": audit_sha256,
                "candidate_sha256": candidate_sha256,
                "source_snapshot_sha256": _canonical_sha(candidate),
                "edited_sha256": _canonical_sha(candidate), "audit_run_id": row[3],
                "audited_at": row[11], "reviewed_at": draft["reviewed_at"],
                "approved_draft_version": draft["version"],
            }
            return bool(
                row is not None and row[0] == "completed"
                and row[4] == candidate_sha256 and row[9] == audit_sha256
                and edited == candidate
                and audit_entry.get("audit_status") == "auto_pass"
                and audit_entry.get("audit_confidence") == "high"
                and audit_entry.get("issues") == []
                and audit_entry.get("suggested_corrections") == []
                and evidence == expected
            )
        if method != "corrected_draft_reaudit":
            return False
        record = connection.execute(
            "SELECT * FROM corrected_draft_reaudits WHERE id=?",
            (evidence.get("reaudit_record_id"),),
        ).fetchone()
        if record is None:
            return False
        record = dict(record)
        expected = {
            "method": "corrected_draft_reaudit", "reaudit_record_id": record["id"],
            "fresh_model_run_id": record["fresh_model_run_id"],
            "source_candidate_sha256": record["source_candidate_sha256"],
            "source_snapshot_sha256": record["source_snapshot_sha256"],
            "batch_audit_output_sha256": record["batch_audit_output_sha256"],
            "edited_sha256": record["edited_sha256"],
            "crop_generation_id": record["crop_generation_id"],
            "crop_manifest_sha256": record["crop_manifest_sha256"],
            "crop_manifest_signature": record["crop_manifest_signature"],
            "crop_sha256": record["crop_sha256"],
            "reviewed_draft_version": record["reviewed_draft_version"],
            "approved_draft_version": record["approved_draft_version"],
            "reviewed_at": record["reviewed_at"],
        }
        return bool(
            record["status"] == "completed" and record["decision"] == "passed"
            and record["import_job_id"] == draft["import_job_id"]
            and record["source_question_no"] == draft["source_question_no"]
            and record["source_candidate_sha256"] == candidate_sha256
            and record["source_snapshot_sha256"] == _canonical_sha(candidate)
            and record["batch_audit_output_sha256"] == audit_sha256
            and record["edited_sha256"] == _canonical_sha(edited)
            and record["approved_draft_version"] == draft["version"]
            and record["reviewed_at"] == draft["reviewed_at"]
            and evidence == expected
        )
    except (CandidateAuditError, sqlite3.Error, TypeError, KeyError, IndexError):
        return False


def _valid_edited(connection, edited: dict, source: dict) -> bool:
    if set(edited) != set(source) or edited.get("source_question_no") != source.get("source_question_no"):
        return False
    if any(edited.get(key) != source.get(key) for key in set(source) - ALLOWED_EDIT_FIELDS):
        return False
    if _canonical(edited) == _canonical(source):
        return False
    if not isinstance(edited.get("stem_markdown"), str) or not edited["stem_markdown"].strip():
        return False
    types = {row[0] for row in connection.execute("SELECT code FROM question_types WHERE is_active=1")}
    points = {row[0] for row in connection.execute("SELECT code FROM knowledge_points WHERE is_active=1")}
    if edited.get("question_type_code") not in types:
        return False
    primary = edited.get("primary_knowledge_point_code")
    if not isinstance(primary, str) or (primary and primary not in points):
        return False
    related = edited.get("related_knowledge_point_codes")
    options, subquestions = edited.get("options"), edited.get("subquestions")
    if not isinstance(related, list) or any(code not in points for code in related):
        return False
    if not isinstance(options, list) or not isinstance(subquestions, list):
        return False
    if edited["question_type_code"] in {"single_choice", "multiple_choice"} and len(options) < 2:
        return False
    if edited["question_type_code"] not in {"single_choice", "multiple_choice"} and options:
        return False
    codes = []
    for option in options:
        if set(option) != {"code", "content"} or not all(isinstance(option[x], str) and option[x].strip() for x in option):
            return False
        codes.append(option["code"].strip().casefold())
    if len(codes) != len(set(codes)):
        return False
    return all(
        isinstance(item, dict) and set(item) == {"label", "stem_markdown"}
        and all(isinstance(item[key], str) and item[key].strip() for key in item)
        for item in subquestions
    )


def _draft_inputs(connection, candidate: dict, candidate_sha: str, question_no: str):
    source_by_no = {item["source_question_no"]: item for item in candidate["questions"]}
    source = source_by_no.get(question_no)
    row = connection.execute(
        "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no=?",
        (candidate["import_job_id"], question_no),
    ).fetchone()
    if source is None or row is None or row["deleted_at"] is not None:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    snapshot, edited = _decode_object(row["source_snapshot_json"]), _decode_object(row["edited_json"])
    if row["source_candidate_sha256"] != candidate_sha or snapshot != source:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    if row["status"] not in {"draft", "needs_fix"} or row["approval_source"] is not None:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    if not _valid_edited(connection, edited, source):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    return row, source, edited


def _crop_anchor(job_fd: int, manifest: dict, question_no: str):
    entries = [item for item in manifest["questions"] if str(item.get("question_no")) == question_no]
    relative = f"question_crops/Q{int(question_no):03d}.png"
    if len(entries) != 1 or entries[0].get("output_relative_path") != relative:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    try:
        crop = read_file_at(job_fd, relative, max_bytes=64 * 1024 * 1024)
    except SecureCropArtifactError as exc:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT) from exc
    entry = entries[0]
    if crop.sha256 != entry.get("sha256") or crop.size != entry.get("byte_size"):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    return relative, crop.sha256, crop.size


def claim_corrected_draft_audit(database_path, private_root, job_id, question_no,
                                runner=None, *,
                                expected_draft_version=None, expected_edited_sha256=None):
    database_path, private_root = Path(database_path), Path(private_root)
    question_no = str(question_no)
    if not question_no.isascii() or not question_no.isdigit() or question_no.startswith("0"):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    job_fd = lock_fd = None
    temporary = None
    try:
        job_fd, lock_fd = _open_job_and_lock(private_root, job_id)
        if job_fd is None:
            return None
        temporary = tempfile.TemporaryDirectory(prefix="corrected-draft-audit-")
        bundle = _completed_bundle(database_path, private_root, job_id, job_fd, Path(temporary.name))
        input_row, manifest_sha, manifest, images, candidate, candidate_file, _, audit_file, audit_row = bundle
        with closing(sqlite3.connect(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            current = connection.execute(
                "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no=?",
                (job_id, question_no),
            ).fetchone()
            source = next((
                item for item in candidate["questions"]
                if item["source_question_no"] == question_no
            ), None)
            if current is not None and source is not None:
                current_edited = _decode_object(current["edited_json"])
                current_sha = _canonical_sha(current_edited)
                current_evidence = _decode_object(
                    current["approval_evidence_json"]
                ) if current["approval_evidence_json"] else {}
                if (
                    current["status"] == "approved"
                    and current["approval_source"] == "ai_second_pass"
                    and current["source_candidate_sha256"] == candidate_file.sha256
                    and _decode_object(current["source_snapshot_json"]) == source
                    and current_evidence.get("method") == "corrected_draft_reaudit"
                ):
                    record = connection.execute(
                        """SELECT id,reviewed_draft_version FROM corrected_draft_reaudits
                           WHERE approved_draft_version=? AND edited_sha256=?
                             AND import_job_id=? AND source_question_no=?
                             AND status='completed' AND decision='passed'""",
                        (current["version"], current_sha, job_id, question_no),
                    ).fetchone()
                    if record is not None and record[0] == current_evidence.get(
                        "reaudit_record_id"
                    ) and (
                        expected_draft_version is None
                        or record[1] == expected_draft_version
                    ) and (
                        expected_edited_sha256 is None
                        or current_sha == expected_edited_sha256
                    ):
                        return None
            draft, source, edited = _draft_inputs(connection, candidate, candidate_file.sha256, question_no)
            edited_sha = _canonical_sha(edited)
            if (
                expected_draft_version is not None
                and draft["version"] != expected_draft_version
            ) or (
                expected_edited_sha256 is not None
                and edited_sha != expected_edited_sha256
            ):
                raise CandidateAuditError(SAFE_REAUDIT_INPUT)
            existing = connection.execute(
                """SELECT id,status,decision,approved_draft_version,reviewed_draft_version
                   FROM corrected_draft_reaudits
                   WHERE import_job_id=? AND source_question_no=? AND edited_sha256=?
                   ORDER BY id DESC""", (job_id, question_no, edited_sha),
            ).fetchone()
            if existing and existing["status"] == "completed" and (
                (
                    existing["decision"] != "passed"
                    and existing["reviewed_draft_version"] == draft["version"]
                )
                or existing["approved_draft_version"] == draft["version"]
            ):
                return None
        if runner is None:
            runner = CandidateAuditCodexCliRunner()
        relative, crop_sha, crop_size = _crop_anchor(job_fd, manifest, question_no)
        now = _now()
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            if tuple(_database_input_from_connection(connection, job_id)) != tuple(input_row) or tuple(_audit_database_row(connection, job_id)) != audit_row:
                raise CandidateAuditError(SAFE_REAUDIT_INPUT)
            current, _, current_edited = _draft_inputs(connection, candidate, candidate_file.sha256, question_no)
            if current["version"] != draft["version"] or _canonical_sha(current_edited) != edited_sha:
                raise CandidateAuditError(SAFE_REAUDIT_INPUT)
            connection.execute(
                """INSERT INTO corrected_draft_reaudits
                   (import_job_id,source_question_no,reviewed_draft_version,edited_sha256,
                    status,source_candidate_sha256,source_snapshot_sha256,
                    batch_audit_output_sha256,crop_generation_id,crop_manifest_sha256,
                    crop_manifest_signature,crop_relative_path,crop_sha256,crop_byte_size,
                    started_at,updated_at)
                   VALUES(?,?,?,?, 'processing',?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(import_job_id,source_question_no,reviewed_draft_version,edited_sha256)
                   DO UPDATE SET status='processing',fresh_model_run_id=NULL,
                     audit_output_sha256=NULL,audit_output_byte_size=NULL,decision=NULL,
                     confidence=NULL,reviewed_at=NULL,approved_draft_version=NULL,
                     error_message=NULL,started_at=excluded.started_at,updated_at=excluded.updated_at""",
                (job_id, question_no, draft["version"], edited_sha,
                 candidate_file.sha256, _canonical_sha(source), audit_file.sha256,
                 manifest["generation_id"], manifest_sha, manifest["signature"],
                 relative, crop_sha, crop_size, now, now),
            )
            record_id = connection.execute(
                """SELECT id FROM corrected_draft_reaudits WHERE import_job_id=?
                   AND source_question_no=? AND reviewed_draft_version=? AND edited_sha256=?""",
                (job_id, question_no, draft["version"], edited_sha),
            ).fetchone()[0]
            connection.commit()
        image_by_no = {
            item["source_question_no"]: image
            for item, image in zip(candidate["questions"], images)
        }
        exact_image = image_by_no[question_no]
        claim = CorrectedDraftAuditClaim(
            database_path, private_root, job_id, question_no, runner, edited,
            draft["version"], edited_sha, candidate_file.sha256, _canonical_sha(source),
            audit_file.sha256, manifest["generation_id"], manifest_sha, manifest["signature"],
            relative, crop_sha, crop_size, tuple(input_row), audit_row, record_id,
            (exact_image,), temporary, lock_fd, job_fd,
        )
        temporary = lock_fd = job_fd = None
        return claim
    except CandidateAuditError:
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError, StopIteration) as exc:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT) from exc
    finally:
        _release(job_fd, lock_fd, temporary)


def _publish(claim: CorrectedDraftAuditClaim, raw: str, run_id: str) -> CorrectedDraftAuditResult:
    if not _bounded_run_id(run_id):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    parsed = parse_candidate_audit_output(raw, claim.job_id, [claim.edited])
    output = raw.encode("utf-8")
    if not output or len(output) > MAX_AUDIT_BYTES:
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    entry = parsed["questions"][0]
    passed = entry["audit_status"] == "auto_pass"
    decision = "passed" if passed else "not_passed"
    now = _now()
    with closing(sqlite3.connect(claim.database_path, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        if tuple(_database_input_from_connection(connection, claim.job_id)) != claim.input_database_row or tuple(_audit_database_row(connection, claim.job_id)) != claim.batch_audit_row:
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        row = connection.execute(
            "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND source_question_no=?",
            (claim.job_id, claim.question_no),
        ).fetchone()
        if row is None or row["version"] != claim.draft_version or _canonical_sha(_decode_object(row["edited_json"])) != claim.edited_sha256 or row["deleted_at"] is not None:
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        approved_version = claim.draft_version + 1 if passed else None
        if passed:
            evidence = _canonical({
                "method": "corrected_draft_reaudit", "reaudit_record_id": claim.record_id,
                "fresh_model_run_id": run_id, "source_candidate_sha256": claim.source_candidate_sha256,
                "source_snapshot_sha256": claim.source_snapshot_sha256,
                "batch_audit_output_sha256": claim.batch_audit_sha256,
                "edited_sha256": claim.edited_sha256, "crop_generation_id": claim.crop_generation_id,
                "crop_manifest_sha256": claim.crop_manifest_sha256,
                "crop_manifest_signature": claim.crop_manifest_signature,
                "crop_sha256": claim.crop_sha256, "reviewed_draft_version": claim.draft_version,
                "approved_draft_version": approved_version, "reviewed_at": now,
            })
            cursor = connection.execute(
                """UPDATE candidate_review_drafts SET status='approved',version=?,reviewed_at=?,
                          approval_source='ai_second_pass',approval_evidence_json=?,updated_at=?
                   WHERE id=? AND version=? AND deleted_at IS NULL""",
                (approved_version, now, evidence, now, row["id"], claim.draft_version),
            )
        else:
            cursor = connection.execute(
                """UPDATE candidate_review_drafts SET status='needs_fix',reviewed_at=?,
                          approval_source=NULL,approval_evidence_json=NULL,updated_at=?
                   WHERE id=? AND version=? AND deleted_at IS NULL""",
                (now, now, row["id"], claim.draft_version),
            )
        if cursor.rowcount != 1:
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        cursor = connection.execute(
            """UPDATE corrected_draft_reaudits SET status='completed',fresh_model_run_id=?,
                      audit_output_sha256=?,audit_output_byte_size=?,decision=?,confidence=?,
                      reviewed_at=?,approved_draft_version=?,error_message=NULL,updated_at=?
               WHERE id=? AND status='processing'""",
            (run_id, hashlib.sha256(output).hexdigest(), len(output), decision,
             entry["audit_confidence"], now, approved_version, now, claim.record_id),
        )
        if cursor.rowcount != 1:
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        connection.commit()
    return CorrectedDraftAuditResult(claim.record_id, passed, decision)


def _mark_failed(claim: CorrectedDraftAuditClaim) -> None:
    try:
        with closing(sqlite3.connect(claim.database_path)) as connection:
            connection.execute(
                """UPDATE corrected_draft_reaudits SET status='failed',decision='error',
                          error_message=?,updated_at=? WHERE id=? AND status='processing'""",
                (SAFE_REAUDIT_ERROR, _now(), claim.record_id),
            )
            connection.commit()
    except sqlite3.Error:
        pass


def run_claimed_corrected_draft_audit(claim):
    if not isinstance(claim, CorrectedDraftAuditClaim):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    try:
        response = claim.runner.run(
            image_paths=claim.image_paths, prompt=_audit_prompt(claim.job_id, [claim.edited])
        )
        if not isinstance(response, CandidateAuditRunResult):
            raise CandidateAuditError(SAFE_AUDIT_ERROR)
        return _publish(claim, response.final_message, response.run_id)
    except Exception:
        _mark_failed(claim)
        raise CandidateAuditError(SAFE_REAUDIT_ERROR) from None
    finally:
        claim.close()


def adopt_corrected_draft_audit(database_path, private_root, job_id, question_no,
                                  audit_json: str, fresh_run_id: str, *,
                                  reviewed_draft_version: int,
                                  edited_sha256: str):
    """Register an external strict single-question result without invoking a runner."""
    if (
        not _bounded_run_id(fresh_run_id)
        or not isinstance(reviewed_draft_version, int)
        or isinstance(reviewed_draft_version, bool)
        or reviewed_draft_version < 1
        or not isinstance(edited_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", edited_sha256) is None
    ):
        raise CandidateAuditError(SAFE_REAUDIT_INPUT)
    claim = claim_corrected_draft_audit(
        database_path, private_root, job_id, question_no,
        runner=object(),
        expected_draft_version=reviewed_draft_version,
        expected_edited_sha256=edited_sha256,
    )
    if claim is None:
        database_path = Path(database_path)
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                """SELECT id,decision,reviewed_draft_version,edited_sha256
                   FROM corrected_draft_reaudits WHERE import_job_id=?
                   AND source_question_no=? AND status='completed' ORDER BY id DESC""",
                (job_id, str(question_no)),
            ).fetchone()
        if (
            row is None or row[2] != reviewed_draft_version
            or row[3] != edited_sha256
        ):
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        return CorrectedDraftAuditResult(row[0], row[1] == "passed", row[1])
    try:
        if (
            claim.draft_version != reviewed_draft_version
            or claim.edited_sha256 != edited_sha256
        ):
            raise CandidateAuditError(SAFE_REAUDIT_INPUT)
        return _publish(claim, audit_json, fresh_run_id)
    except Exception:
        _mark_failed(claim)
        raise
    finally:
        claim.close()
