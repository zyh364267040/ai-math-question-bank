"""Finalize approved candidate drafts into their existing formal questions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.database.initialize import DEFAULT_DATABASE_PATH, initialize_database


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


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_audits(private_root: Path, job_id: int):
    path = private_root / "processing" / f"import_job_{job_id}" / "ai_audit.json"
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalizationError("AI审核文件缺失或损坏") from exc
    questions = payload.get("questions")
    if payload.get("import_job_id") != job_id or not isinstance(questions, list):
        raise FinalizationError("AI审核文件与导入任务不匹配")
    audits = {}
    for item in questions:
        if not isinstance(item, dict):
            raise FinalizationError("AI审核题目结构无效")
        number = str(item.get("source_question_no", "")).strip()
        if not number or number in audits:
            raise FinalizationError("AI审核题号缺失或重复")
        audits[number] = item
    return audits, _sha256(raw)


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
        f"""SELECT id,source_question_no,edited_json,status,version,reviewed_at,
                   deleted_at,{source},{evidence}
            FROM candidate_review_drafts WHERE import_job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()


def _plan(drafts, audits, audit_sha):
    planned = []
    for row in drafts:
        item = dict(row)
        number = item["source_question_no"]
        status = item["status"]
        source = item["approval_source"]
        evidence = item["approval_evidence_json"]
        if status == "approved" and source is None:
            source = "human"
            evidence = _canonical({"method": "existing_approval", "reviewed_at": item["reviewed_at"]})
        elif status == "pending" and is_ai_second_pass_eligible(audits.get(number)):
            status = "approved"
            source = "ai_second_pass"
            audit = audits[number]
            evidence = _canonical({
                "audit_file_sha256": audit_sha,
                "audit_status": audit["audit_status"],
                "audit_confidence": audit["audit_confidence"],
                "issues": audit["issues"],
                "suggested_corrections": audit["suggested_corrections"],
            })
        item.update(planned_status=status, planned_source=source, planned_evidence=evidence)
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
        try:
            edited = json.loads(item["edited_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise FinalizationError("审核草稿JSON损坏") from exc
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
    with sqlite3.connect(database_path) as source, sqlite3.connect(target) as destination:
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


def finalize_review(database_path=DEFAULT_DATABASE_PATH, private_root=None, job_id=1, *, apply=False):
    """Plan, or transactionally apply, finalization for one import job."""
    database_path = Path(database_path).expanduser().resolve()
    private_root = Path(private_root or database_path.parent).expanduser().resolve()
    audits, audit_sha = _load_audits(private_root, job_id)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("SELECT 1 FROM import_jobs WHERE id=?", (job_id,)).fetchone() is None:
            raise FinalizationError("导入任务不存在")
        planned = _plan(_drafts(connection, job_id), audits, audit_sha)
        approved = [x for x in planned if x["planned_status"] == "approved" and x["deleted_at"] is None]
        _, _, predicted_changes = _preflight(connection, job_id, approved)
    if not apply:
        return _result(planned, predicted_changes)

    backup_path, backup_sha = _backup(database_path)
    initialize_database(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    changed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            planned = _plan(_drafts(connection, job_id), audits, audit_sha)
            approved = [x for x in planned if x["planned_status"] == "approved" and x["deleted_at"] is None]
            formal, desired_by_number, _ = _preflight(connection, job_id, approved)
            now = datetime.now(timezone.utc).isoformat()
            for item in planned:
                if (item["status"], item["approval_source"], item["approval_evidence_json"]) != (
                    item["planned_status"], item["planned_source"], item["planned_evidence"]
                ):
                    cursor = connection.execute(
                        """UPDATE candidate_review_drafts
                           SET status=?,approval_source=?,approval_evidence_json=?,version=version+1,
                               reviewed_at=COALESCE(reviewed_at,?),updated_at=? WHERE id=? AND version=?""",
                        (item["planned_status"], item["planned_source"], item["planned_evidence"],
                         now if item["planned_status"] == "approved" else None, now, item["id"], item["version"]),
                    )
                    if cursor.rowcount != 1:
                        raise FinalizationError("审核草稿版本冲突，请重试")
            for item in approved:
                qid = formal[item["source_question_no"]]
                desired = desired_by_number[item["source_question_no"]]
                content_hash = connection.execute(
                    "SELECT content_hash FROM questions WHERE id=?", (qid,)
                ).fetchone()[0]
                if not _matches(_current_editable(connection, qid), desired, content_hash):
                    content_hash = _sync_question(connection, qid, desired)
                    _write_version(connection, qid)
                    changed += 1
                marker = (
                    f"finalize_review:job={job_id};question={item['source_question_no']};"
                    f"source={item['planned_source']};content_hash={content_hash}"
                )
                if connection.execute(
                    "SELECT 1 FROM question_reviews WHERE question_id=? AND notes=?", (qid, marker)
                ).fetchone() is None:
                    connection.execute(
                        """INSERT INTO question_reviews
                           (question_id,review_item,previous_status,new_status,reviewer,reviewed_at,notes)
                           VALUES(?,'usability','pending','passed',?,?,?)""",
                        (qid, item["planned_source"], now, marker),
                    )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"收口后外键检查失败：{violations[:3]}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return _result(planned, changed, backup_path, backup_sha)
    finally:
        connection.close()
