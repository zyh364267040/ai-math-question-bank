"""Finalize approved candidate drafts into their existing formal questions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.database.initialize import DEFAULT_DATABASE_PATH
from src.processing.secure_crop_artifacts import (
    JobLock,
    SecureCropArtifactError,
    locked_job,
    read_file_at,
)
from src.reviewing.candidate_review_ai import validate_ai_approval
from src.reviewing.knowledge_classification import load_bound_knowledge_classification
from src.web.app import AuditDataError, _validate_audit_payload


MAX_JSON_ARTIFACT_BYTES = 16 * 1024 * 1024


class FinalizationError(ValueError):
    """A batch cannot be finalized without weakening its safety guarantees."""


@dataclass(frozen=True)
class FinalizationResult:
    approved: int
    pending: int
    human: int
    ai_second_pass: int
    changed_questions: int
    ai_second_pass_question_nos: tuple[str, ...]
    pending_question_nos: tuple[str, ...]
    backup_path: Path | None = None
    backup_sha256: str | None = None


@dataclass(frozen=True)
class ArtifactSnapshot:
    relative_path: str
    label: str
    identity: tuple[int, int, int, int, int]
    sha256: str


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json_bytes(artifact_lock: JobLock, relative_path: str, label: str):
    try:
        pinned = read_file_at(
            artifact_lock.descriptor, relative_path, max_bytes=MAX_JSON_ARTIFACT_BYTES
        )
        payload = json.loads(pinned.data)
    except (SecureCropArtifactError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"{label}缺失、不安全或损坏") from exc
    if not isinstance(payload, dict):
        raise FinalizationError(f"{label}结构无效")
    return payload, pinned.data, ArtifactSnapshot(
        relative_path, label, pinned.identity, pinned.sha256
    )


def _question_numbers(payload, job_id, label, key="source_question_no"):
    questions = payload.get("questions")
    count = payload.get("question_count")
    if (
        payload.get("import_job_id") != job_id
        or not isinstance(questions, list)
        or not questions
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != len(questions)
    ):
        raise FinalizationError(f"{label}批次数量不完整")
    numbers = []
    for item in questions:
        if not isinstance(item, dict):
            raise FinalizationError(f"{label}题目结构无效")
        number = item.get(key)
        if not isinstance(number, str) or not number or number != number.strip():
            raise FinalizationError(f"{label}题号缺失或无效")
        numbers.append(number)
    if len(numbers) != len(set(numbers)):
        raise FinalizationError(f"{label}题号重复")
    return tuple(numbers)


def _load_authoritative_batch(artifact_lock: JobLock, job_id: int):
    candidate, candidate_raw, candidate_snapshot = _load_json_bytes(
        artifact_lock, "candidate_questions.json", "候选题文件"
    )
    candidate_numbers = _question_numbers(candidate, job_id, "候选题")
    audit, audit_raw, audit_snapshot = _load_json_bytes(
        artifact_lock, "ai_audit.json", "AI审核文件"
    )
    try:
        _, audits = _validate_audit_payload(audit, job_id, candidate["questions"])
    except AuditDataError as exc:
        raise FinalizationError("AI审核文件结构、数量或题号无效") from exc
    return {
        "numbers": frozenset(candidate_numbers),
        "source_paper_id": candidate.get("source_paper_id"),
        "candidate_sha": _sha256(candidate_raw),
        "candidates": {
            item["source_question_no"]: item for item in candidate["questions"]
        },
        "audits": audits,
        "audit_sha": _sha256(audit_raw),
        "snapshots": (candidate_snapshot, audit_snapshot),
    }


def _verify_artifact_snapshots(artifact_lock: JobLock, snapshots) -> None:
    for expected in snapshots:
        try:
            current = read_file_at(
                artifact_lock.descriptor,
                expected.relative_path,
                max_bytes=MAX_JSON_ARTIFACT_BYTES,
            )
        except SecureCropArtifactError as exc:
            raise FinalizationError(f"{expected.label}在收口期间发生变化") from exc
        if current.identity != expected.identity or current.sha256 != expected.sha256:
            raise FinalizationError(f"{expected.label}在收口期间发生变化")


def is_ai_second_pass_eligible(audit: dict | None) -> bool:
    """Require every automatic second-pass signal, including explicit empty lists."""
    return bool(
        isinstance(audit, dict)
        and audit.get("audit_status") == "auto_pass"
        and audit.get("audit_confidence") == "high"
        and audit.get("issues") == []
        and audit.get("suggested_corrections") == []
    )


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _drafts(connection, job_id):
    columns = _columns(connection, "candidate_review_drafts")
    source = "approval_source" if "approval_source" in columns else "NULL AS approval_source"
    evidence = "approval_evidence_json" if "approval_evidence_json" in columns else "NULL AS approval_evidence_json"
    return connection.execute(
        f"""SELECT id,import_job_id,source_question_no,source_candidate_sha256,source_snapshot_json,
                   edited_json,status,version,reviewed_at,deleted_at,{source},{evidence}
            FROM candidate_review_drafts WHERE import_job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()


def _decoded_object(value, message):
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FinalizationError(message) from exc
    if not isinstance(payload, dict):
        raise FinalizationError(message)
    return payload


def _valid_human_evidence(item) -> bool:
    try:
        evidence = json.loads(item["approval_evidence_json"])
        reviewed_at = datetime.fromisoformat(item["reviewed_at"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        item["approval_source"] == "human"
        and reviewed_at.tzinfo is not None
        and reviewed_at.utcoffset() is not None
        and reviewed_at.astimezone(timezone.utc)
        <= datetime.now(timezone.utc) + timedelta(minutes=5)
        and isinstance(evidence, dict)
        and set(evidence) == {"method", "reviewed_at"}
        and evidence.get("method") in {"workbench", "workbench_quick", "existing_approval"}
        and evidence.get("reviewed_at") == item["reviewed_at"]
    )


def _ai_evidence(audit, audit_sha):
    return {
        "audit_file_sha256": audit_sha,
        "audit_status": audit["audit_status"],
        "audit_confidence": audit["audit_confidence"],
        "issues": audit["issues"],
        "suggested_corrections": audit["suggested_corrections"],
    }


def _plan(connection, drafts, batch):
    planned = []
    for row in drafts:
        item = dict(row)
        number = item["source_question_no"]
        status = item["status"]
        source = item["approval_source"]
        evidence = item["approval_evidence_json"]
        candidate = batch["candidates"].get(number)
        source_snapshot = _decoded_object(item["source_snapshot_json"], "审核草稿来源快照损坏")
        edited = _decoded_object(item["edited_json"], "审核草稿JSON损坏")
        bound = bool(
            candidate is not None
            and item["source_candidate_sha256"] == batch["candidate_sha"]
            and source_snapshot == candidate
            and source_snapshot.get("source_question_no") == number
            and edited.get("source_question_no") == number
        )
        unedited = bound and _canonical(edited) == _canonical(candidate)
        audit = batch["audits"].get(number)
        planned_reviewed_at = item["reviewed_at"]
        if status == "approved" and source == "human":
            if not bound or not _valid_human_evidence(item):
                raise FinalizationError(f"Q{number} 人工批准证据或候选绑定无效")
        elif status == "approved" and source == "ai_second_pass":
            if (
                not validate_ai_approval(
                    connection, item, candidate,
                    candidate_sha256=batch["candidate_sha"],
                    audit_sha256=batch["audit_sha"], audit_entry=audit,
                )
            ):
                raise FinalizationError(f"Q{number} AI批准证据、内容或候选绑定无效")
        elif status == "approved":
            raise FinalizationError(f"Q{number} 人工批准证据或候选绑定无效")
        elif status == "pending" and unedited and is_ai_second_pass_eligible(audit):
            anchor = connection.execute(
                """SELECT status,codex_run_id,input_candidate_sha256,output_sha256,
                          completed_at FROM import_candidate_audit_runs
                   WHERE import_job_id=?""", (item["import_job_id"],),
            ).fetchone()
            if (
                anchor is not None and anchor["status"] == "completed"
                and anchor["input_candidate_sha256"] == batch["candidate_sha"]
                and anchor["output_sha256"] == batch["audit_sha"]
                and anchor["codex_run_id"] and anchor["completed_at"]
            ):
                status, source = "approved", "ai_second_pass"
                planned_reviewed_at = anchor["completed_at"]
                evidence = _canonical({
                    "method": "batch_auto_pass",
                    "audit_output_sha256": batch["audit_sha"],
                    "candidate_sha256": batch["candidate_sha"],
                    "source_snapshot_sha256": hashlib.sha256(
                        _canonical(candidate).encode("utf-8")
                    ).hexdigest(),
                    "edited_sha256": hashlib.sha256(
                        _canonical(candidate).encode("utf-8")
                    ).hexdigest(),
                    "audit_run_id": anchor["codex_run_id"],
                    "audited_at": anchor["completed_at"],
                    "reviewed_at": anchor["completed_at"],
                    "approved_draft_version": item["version"] + 1,
                })
        item.update(
            planned_status=status, planned_source=source,
            planned_evidence=evidence, planned_reviewed_at=planned_reviewed_at,
        )
        planned.append(item)
    return planned


def _formal_questions(connection, job_id, approved):
    mapping = {}
    for item in approved:
        rows = connection.execute(
            """SELECT q.id FROM questions q JOIN question_sources s ON s.question_id=q.id
               WHERE s.import_job_id=? AND s.source_question_no=?""",
            (job_id, item["source_question_no"]),
        ).fetchall()
        if len(rows) != 1:
            raise FinalizationError(f"Q{item['source_question_no']} 对应正式题不存在或不唯一")
        mapping[item["source_question_no"]] = rows[0][0]
    return mapping


def _edited_with_classification(connection, job_id, item):
    try:
        edited = json.loads(item["edited_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise FinalizationError("审核草稿JSON损坏") from exc
    valid_points = {
        row[0] for row in connection.execute(
            "SELECT code FROM knowledge_points WHERE is_active=1"
        )
    }
    primary = edited.get("primary_knowledge_point_code")
    related = edited.get("related_knowledge_point_codes")
    if (
        primary in valid_points and isinstance(related, list)
        and all(code in valid_points for code in related)
    ):
        return edited
    bound = load_bound_knowledge_classification(
        connection, job_id, item["source_question_no"], item
    )
    if bound is None:
        raise FinalizationError(
            f"Q{item['source_question_no']} 知识点分类证据缺失或已失效"
        )
    edited = dict(edited)
    edited["primary_knowledge_point_code"] = bound["primary_code"]
    edited["related_knowledge_point_codes"] = bound["related_codes"]
    return edited


def _preflight(connection, job_id, approved):
    """Validate all references and calculate the exact content change count."""
    formal = _formal_questions(connection, job_id, approved)
    desired_by_number = {}
    changed = 0
    valid_types = {row[0] for row in connection.execute(
        "SELECT code FROM question_types WHERE is_active=1"
    )}
    valid_points = {row[0] for row in connection.execute(
        "SELECT code FROM knowledge_points WHERE is_active=1"
    )}
    for item in approved:
        edited = _edited_with_classification(connection, job_id, item)
        desired = _desired(edited)
        if desired["question_type_code"] not in valid_types:
            raise FinalizationError(f"题型不存在：{desired['question_type_code']}")
        point_codes = [desired["primary_knowledge_point_code"], *desired["related_knowledge_point_codes"]]
        missing = [code for code in point_codes if code not in valid_points]
        if missing:
            raise FinalizationError(f"知识点不存在：{missing[0]}")
        number = item["source_question_no"]
        qid = formal[number]
        content_hash = connection.execute(
            "SELECT content_hash FROM questions WHERE id=?", (qid,)
        ).fetchone()[0]
        changed += not _matches(_current_editable(connection, qid), desired, content_hash)
        desired_by_number[number] = desired
    return formal, desired_by_number, changed


def _desired(edited):
    try:
        desired = {
            "stem_markdown": edited["stem_markdown"],
            "question_type_code": edited["question_type_code"],
            "primary_knowledge_point_code": edited["primary_knowledge_point_code"],
            "related_knowledge_point_codes": list(dict.fromkeys(edited.get("related_knowledge_point_codes", []))),
            "options": [{"code": x["code"], "content": x["content"]} for x in edited.get("options", [])],
            "subquestions": [{
                "label": str(x.get("label", "")).strip(),
                "stem_markdown": str(x.get("stem_markdown", "")).strip(),
            } for x in edited.get("subquestions", [])],
        }
    except (KeyError, TypeError) as exc:
        raise FinalizationError("审核草稿结构无效") from exc
    if not isinstance(desired["stem_markdown"], str) or not desired["stem_markdown"].strip():
        raise FinalizationError("审核草稿题干为空")
    return desired


def _subquestion_stem(sub):
    return " ".join(x for x in (sub["label"], sub["stem_markdown"]) if x).strip()


def _current_editable(connection, question_id):
    row = connection.execute(
        """SELECT q.stem_markdown,q.question_type_code,k.code
           FROM questions q JOIN knowledge_points k ON k.id=q.primary_knowledge_point_id
           WHERE q.id=?""", (question_id,),
    ).fetchone()
    return {
        "stem_markdown": row[0],
        "question_type_code": row[1],
        "primary_knowledge_point_code": row[2],
        "related_knowledge_point_codes": [r[0] for r in connection.execute(
            """SELECT k.code FROM question_related_knowledge_points r
               JOIN knowledge_points k ON k.id=r.knowledge_point_id
               WHERE r.question_id=? ORDER BY r.rowid""", (question_id,)
        )],
        "options": [{"code": r[0], "content": r[1]} for r in connection.execute(
            """SELECT option_code,content_markdown FROM question_options
               WHERE question_id=? ORDER BY display_order""", (question_id,)
        )],
        "subquestion_stems": [r[0] for r in connection.execute(
            "SELECT stem_markdown FROM subquestions WHERE question_id=? ORDER BY display_order",
            (question_id,),
        )],
    }


def _matches(current, desired, content_hash):
    comparison = dict(desired)
    comparison.pop("subquestions")
    comparison["subquestion_stems"] = [_subquestion_stem(x) for x in desired["subquestions"]]
    return current == comparison and content_hash == _sha256(_canonical(desired).encode())


def _snapshot(connection, question_id):
    question = dict(connection.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone())
    question["options"] = [dict(row) for row in connection.execute(
        "SELECT * FROM question_options WHERE question_id=? ORDER BY display_order", (question_id,)
    )]
    question["subquestions"] = [dict(row) for row in connection.execute(
        "SELECT * FROM subquestions WHERE question_id=? ORDER BY display_order", (question_id,)
    )]
    question["related_knowledge_point_ids"] = [row[0] for row in connection.execute(
        "SELECT knowledge_point_id FROM question_related_knowledge_points WHERE question_id=? ORDER BY rowid",
        (question_id,),
    )]
    return _canonical(question)


def _write_version(connection, question_id):
    previous = connection.execute(
        "SELECT id FROM question_versions WHERE question_id=? ORDER BY version_no DESC LIMIT 1",
        (question_id,),
    ).fetchone()
    version_no = connection.execute(
        "SELECT COALESCE(MAX(version_no),0)+1 FROM question_versions WHERE question_id=?",
        (question_id,),
    ).fetchone()[0]
    connection.execute(
        "UPDATE question_versions SET version_status='superseded' WHERE question_id=? AND version_status='current'",
        (question_id,),
    )
    connection.execute(
        """INSERT INTO question_versions
           (question_id,version_no,version_status,previous_version_id,snapshot_json)
           VALUES(?,?,'current',?,?)""",
        (question_id, version_no, previous[0] if previous else None, _snapshot(connection, question_id)),
    )


def _sync_question(connection, question_id, desired):
    primary = connection.execute(
        "SELECT id FROM knowledge_points WHERE code=? AND is_active=1",
        (desired["primary_knowledge_point_code"],),
    ).fetchone()
    if primary is None:
        raise FinalizationError(f"主知识点不存在：{desired['primary_knowledge_point_code']}")
    related_ids = []
    for code in desired["related_knowledge_point_codes"]:
        row = connection.execute(
            "SELECT id FROM knowledge_points WHERE code=? AND is_active=1", (code,)
        ).fetchone()
        if row is None:
            raise FinalizationError(f"关联知识点不存在：{code}")
        if row[0] != primary[0]:
            related_ids.append(row[0])
    content_hash = _sha256(_canonical(desired).encode())
    connection.execute(
        """UPDATE questions SET stem_markdown=?,question_type_code=?,
                  primary_knowledge_point_id=?,content_hash=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (desired["stem_markdown"], desired["question_type_code"], primary[0], content_hash, question_id),
    )
    connection.execute("DELETE FROM question_options WHERE question_id=?", (question_id,))
    connection.executemany(
        """INSERT INTO question_options(question_id,option_code,content_markdown,display_order)
           VALUES(?,?,?,?)""",
        [(question_id, x["code"], x["content"], order)
         for order, x in enumerate(desired["options"], 1)],
    )
    existing = connection.execute(
        "SELECT id FROM subquestions WHERE question_id=? ORDER BY display_order", (question_id,)
    ).fetchall()
    for order, sub in enumerate(desired["subquestions"], 1):
        stem = _subquestion_stem(sub)
        if order <= len(existing):
            connection.execute(
                "UPDATE subquestions SET display_order=?,stem_markdown=? WHERE id=?",
                (order, stem, existing[order - 1][0]),
            )
        else:
            connection.execute(
                """INSERT INTO subquestions
                   (question_id,display_order,stem_markdown,answer_markdown,answer_status)
                   VALUES(?,?,?,'','missing')""", (question_id, order, stem),
            )
    for row in existing[len(desired["subquestions"]):]:
        connection.execute("UPDATE question_figures SET subquestion_id=NULL WHERE subquestion_id=?", (row[0],))
        connection.execute("UPDATE question_formulas SET subquestion_id=NULL WHERE subquestion_id=?", (row[0],))
        connection.execute("DELETE FROM subquestions WHERE id=?", (row[0],))
    connection.execute("DELETE FROM question_related_knowledge_points WHERE question_id=?", (question_id,))
    connection.executemany(
        "INSERT INTO question_related_knowledge_points(question_id,knowledge_point_id) VALUES(?,?)",
        [(question_id, knowledge_id) for knowledge_id in related_ids],
    )
    return content_hash


def _backup(database_path: Path):
    directory = database_path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = directory / f"question-bank-before-finalize-{stamp}.db"
    with closing(sqlite3.connect(database_path)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
    return target, _sha256(target.read_bytes())


def _result(planned, changed, backup_path=None, backup_sha=None):
    approved = [x for x in planned if x["planned_status"] == "approved" and x["deleted_at"] is None]
    pending = [x for x in planned if x["planned_status"] != "approved" and x["deleted_at"] is None]
    ai_new = [x["source_question_no"] for x in planned
              if x["status"] == "pending" and x["planned_source"] == "ai_second_pass"]
    return FinalizationResult(
        approved=len(approved), pending=len(pending),
        human=sum(x["planned_source"] == "human" for x in approved),
        ai_second_pass=sum(x["planned_source"] == "ai_second_pass" for x in approved),
        changed_questions=changed,
        ai_second_pass_question_nos=tuple(ai_new),
        pending_question_nos=tuple(x["source_question_no"] for x in pending),
        backup_path=backup_path, backup_sha256=backup_sha,
    )


def _foreign_key_violations(connection):
    return connection.execute("PRAGMA foreign_key_check").fetchall()


def _validate_draft_batch(drafts, authoritative_numbers):
    draft_numbers = [row["source_question_no"] for row in drafts]
    if (
        not authoritative_numbers
        or len(draft_numbers) != len(set(draft_numbers))
        or set(draft_numbers) != authoritative_numbers
    ):
        raise FinalizationError("审核草稿题号集合不完整或与权威批次不一致")


def _completion_ready(planned, authoritative_numbers):
    return bool(authoritative_numbers) and len(planned) == len(authoritative_numbers) and all(
        item["deleted_at"] is None and item["planned_status"] == "approved"
        for item in planned
    )


def _validate_completed_job(planned, predicted_changes, authoritative_numbers):
    if not _completion_ready(planned, authoritative_numbers):
        raise FinalizationError("已完成任务的审核草稿一致性已破坏")
    if any(
        item["status"] != "approved"
        or (item["status"], item["approval_source"], item["approval_evidence_json"]) != (
            item["planned_status"], item["planned_source"], item["planned_evidence"]
        )
        for item in planned
    ):
        raise FinalizationError("已完成任务不允许产生新的审核草稿写入")
    if predicted_changes:
        raise FinalizationError("已完成任务的正式题内容不一致")


def _inspect_database(connection, job_id, batch):
    job = connection.execute(
        "SELECT status,source_paper_id FROM import_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if job is None:
        raise FinalizationError("导入任务不存在")
    if job["status"] not in {"needs_review", "completed"}:
        raise FinalizationError(f"导入任务状态不允许收口：{job['status']}")
    if batch["source_paper_id"] != job["source_paper_id"]:
        raise FinalizationError("候选题文件与导入任务来源不匹配")
    drafts = _drafts(connection, job_id)
    _validate_draft_batch(drafts, batch["numbers"])
    planned = _plan(connection, drafts, batch)
    approved = [
        item for item in planned
        if item["planned_status"] == "approved" and item["deleted_at"] is None
    ]
    formal, desired_by_number, predicted_changes = _preflight(
        connection, job_id, approved
    )
    if job["status"] == "completed":
        _validate_completed_job(planned, predicted_changes, batch["numbers"])
    return job, planned, approved, formal, desired_by_number, predicted_changes


def finalize_review(
    database_path=DEFAULT_DATABASE_PATH, private_root=None, job_id=1, *,
    apply=False, backup_result=None, pre_apply_callback=None,
    completion_callback=None,
):
    """Plan, or transactionally apply, finalization for one import job."""
    database_path = Path(database_path).expanduser().resolve()
    private_root = Path(private_root or database_path.parent).expanduser().resolve()
    try:
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        with locked_job(job_dir) as artifact_lock:
            batch = _load_authoritative_batch(artifact_lock, job_id)
            with closing(sqlite3.connect(database_path)) as connection:
                connection.row_factory = sqlite3.Row
                _, planned, _, _, _, predicted_changes = _inspect_database(
                    connection, job_id, batch
                )
            if not apply:
                _verify_artifact_snapshots(artifact_lock, batch["snapshots"])
                return _result(planned, predicted_changes)

            if backup_result is None:
                backup_path, backup_sha = _backup(database_path)
            else:
                try:
                    backup_path, backup_sha = backup_result
                    backup_path = Path(backup_path).expanduser().resolve()
                except (TypeError, ValueError) as exc:
                    raise FinalizationError("收口备份锚点无效") from exc
                if (
                    not isinstance(backup_sha, str) or len(backup_sha) != 64
                    or any(character not in "0123456789abcdef" for character in backup_sha)
                    or not backup_path.is_file()
                    or _sha256(backup_path.read_bytes()) != backup_sha
                ):
                    raise FinalizationError("收口备份锚点无效")
            connection = sqlite3.connect(database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            changed = 0
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if pre_apply_callback is not None:
                        pre_apply_callback(connection)
                    (job, planned, approved, formal, desired_by_number,
                     predicted_changes) = _inspect_database(connection, job_id, batch)
                    should_complete = _completion_ready(planned, batch["numbers"])
                    if job["status"] != "completed":
                        now = datetime.now(timezone.utc).isoformat()
                        for item in planned:
                            current = (
                                item["status"], item["approval_source"],
                                item["approval_evidence_json"],
                            )
                            planned_values = (
                                item["planned_status"], item["planned_source"],
                                item["planned_evidence"],
                            )
                            if current != planned_values:
                                cursor = connection.execute(
                                    """UPDATE candidate_review_drafts
                                       SET status=?,approval_source=?,approval_evidence_json=?,
                                           version=version+1,reviewed_at=COALESCE(reviewed_at,?),
                                           updated_at=? WHERE id=? AND version=?""",
                                    (*planned_values,
                                     item["planned_reviewed_at"] if item["planned_status"] == "approved" else None,
                                     now, item["id"], item["version"]),
                                )
                                if cursor.rowcount != 1:
                                    raise FinalizationError("审核草稿版本冲突，请重试")
                        for item in approved:
                            qid = formal[item["source_question_no"]]
                            desired = desired_by_number[item["source_question_no"]]
                            content_hash = connection.execute(
                                "SELECT content_hash FROM questions WHERE id=?", (qid,)
                            ).fetchone()[0]
                            if not _matches(
                                _current_editable(connection, qid), desired, content_hash
                            ):
                                content_hash = _sync_question(connection, qid, desired)
                                _write_version(connection, qid)
                                changed += 1
                            marker = (
                                f"finalize_review:job={job_id};"
                                f"question={item['source_question_no']};"
                                f"source={item['planned_source']};content_hash={content_hash}"
                            )
                            if connection.execute(
                                "SELECT 1 FROM question_reviews "
                                "WHERE question_id=? AND notes=?", (qid, marker)
                            ).fetchone() is None:
                                connection.execute(
                                    """INSERT INTO question_reviews
                                       (question_id,review_item,previous_status,new_status,
                                        reviewer,reviewed_at,notes)
                                       VALUES(?,'usability','pending','passed',?,?,?)""",
                                    (qid, item["planned_source"], now, marker),
                                )
                        if should_complete:
                            cursor = connection.execute(
                                """UPDATE import_jobs
                                   SET status='completed',error_message=NULL,updated_at=?
                                   WHERE id=? AND status='needs_review'""",
                                (now, job_id),
                            )
                            if cursor.rowcount != 1:
                                raise FinalizationError("导入任务状态冲突，请重试")
                    violations = _foreign_key_violations(connection)
                    if violations:
                        raise sqlite3.IntegrityError(
                            f"收口后外键检查失败：{violations[:3]}"
                        )
                    _verify_artifact_snapshots(artifact_lock, batch["snapshots"])
                    if completion_callback is not None:
                        completion_callback(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                return _result(planned, changed, backup_path, backup_sha)
            finally:
                connection.close()
    except SecureCropArtifactError as exc:
        raise FinalizationError("导入任务文件锁不安全或不可用") from exc
