"""Strict, draft-bound knowledge-classification provenance.

Classifications are independent from visual review.  They may fill only the primary
and related knowledge-point fields during admission; they never mutate an approved
draft or weaken its visual-review provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

SAFE_CLASSIFICATION_ERROR = "知识点分类证据无效"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
NUMBER_PATTERN = re.compile(r"[1-9][0-9]{0,2}")
MAX_EVIDENCE_BYTES = 512 * 1024
TOP_KEYS = {
    "version", "import_job_id", "source_classifier", "reviewer", "scope",
    "question_count", "questions",
}
QUESTION_KEYS = {
    "source_question_no", "primary_code", "related_codes", "reason",
}


class KnowledgeClassificationError(RuntimeError):
    """Fixed-message classification failure safe for presentation."""


@dataclass(frozen=True)
class KnowledgeClassificationAdoption:
    question_count: int
    inserted: int


def _fail() -> NoReturn:
    raise KnowledgeClassificationError(SAFE_CLASSIFICATION_ERROR)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _parse(raw: str, job_id: int) -> dict:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        _fail()
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise KnowledgeClassificationError(SAFE_CLASSIFICATION_ERROR) from exc
    if raw[end:].strip() or not isinstance(value, dict) or set(value) != TOP_KEYS:
        _fail()
    if (
        value["version"] != 1
        or value["import_job_id"] != job_id
        or value["scope"] != "knowledge_only_no_solution"
        or not isinstance(value["source_classifier"], str)
        or not 1 <= len(value["source_classifier"].strip()) <= 100
        or not isinstance(value["reviewer"], str)
        or not 1 <= len(value["reviewer"].strip()) <= 100
        or not isinstance(value["question_count"], int)
        or value["question_count"] <= 0
        or not isinstance(value["questions"], list)
        or len(value["questions"]) != value["question_count"]
    ):
        _fail()
    seen = set()
    for item in value["questions"]:
        if not isinstance(item, dict) or set(item) != QUESTION_KEYS:
            _fail()
        number = item["source_question_no"]
        related = item["related_codes"]
        if (
            not isinstance(number, str) or not NUMBER_PATTERN.fullmatch(number)
            or number in seen
            or not isinstance(item["primary_code"], str)
            or not isinstance(related, list) or len(related) > 2
            or any(not isinstance(code, str) for code in related)
            or len(related) != len(set(related))
            or item["primary_code"] in related
            or not isinstance(item["reason"], str) or len(item["reason"]) > 200
        ):
            _fail()
        seen.add(number)
    return value


def adopt_knowledge_classifications(
    database_path: str | Path,
    job_id: int,
    raw: str,
    classifier_run_id: str,
) -> KnowledgeClassificationAdoption:
    """Validate and atomically bind a complete classification batch to drafts."""
    if not isinstance(job_id, int) or job_id <= 0 or not isinstance(classifier_run_id, str):
        _fail()
    if not RUN_ID_PATTERN.fullmatch(classifier_run_id):
        _fail()
    payload = _parse(raw, job_id)
    evidence_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    by_number = {item["source_question_no"]: item for item in payload["questions"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with sqlite3.connect(Path(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            valid_points = {
                row[0] for row in connection.execute(
                    "SELECT code FROM knowledge_points WHERE is_active=1"
                )
            }
            drafts = {
                row["source_question_no"]: row
                for row in connection.execute(
                    """SELECT source_question_no,edited_json,status,version,approval_source
                       FROM candidate_review_drafts
                       WHERE import_job_id=? AND deleted_at IS NULL""",
                    (job_id,),
                )
            }
            if set(drafts) != set(by_number):
                _fail()
            prepared = []
            for number, item in by_number.items():
                draft = drafts[number]
                if draft["status"] != "approved" or draft["approval_source"] not in {
                    "human", "ai_second_pass",
                }:
                    _fail()
                try:
                    edited = json.loads(draft["edited_json"])
                except (TypeError, json.JSONDecodeError):
                    _fail()
                primary = item["primary_code"]
                related = item["related_codes"]
                if primary not in valid_points or any(code not in valid_points for code in related):
                    _fail()
                prepared.append((
                    number, draft["version"], _canonical_sha(edited), primary,
                    json.dumps(related, ensure_ascii=False, separators=(",", ":")),
                    item["reason"],
                ))
            inserted = 0
            for number, version, edited_sha, primary, related_json, reason in prepared:
                existing = connection.execute(
                    """SELECT * FROM candidate_knowledge_classifications
                       WHERE import_job_id=? AND source_question_no=?
                         AND approved_draft_version=? AND edited_sha256=?""",
                    (job_id, number, version, edited_sha),
                ).fetchone()
                expected = (
                    primary, related_json, payload["source_classifier"], payload["reviewer"],
                    classifier_run_id, evidence_sha, reason,
                )
                if existing is not None:
                    actual = (
                        existing["primary_knowledge_point_code"],
                        existing["related_knowledge_point_codes_json"],
                        existing["classifier"], existing["reviewer"],
                        existing["classifier_run_id"], existing["evidence_sha256"],
                        existing["reason"],
                    )
                    if actual != expected:
                        _fail()
                    continue
                connection.execute(
                    """INSERT INTO candidate_knowledge_classifications
                       (import_job_id,source_question_no,approved_draft_version,
                        edited_sha256,primary_knowledge_point_code,
                        related_knowledge_point_codes_json,classifier,reviewer,
                        classifier_run_id,evidence_sha256,reason,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, number, version, edited_sha, primary, related_json,
                        payload["source_classifier"], payload["reviewer"],
                        classifier_run_id, evidence_sha, reason, now,
                    ),
                )
                inserted += 1
            connection.commit()
            return KnowledgeClassificationAdoption(payload["question_count"], inserted)
    except KnowledgeClassificationError:
        raise
    except sqlite3.Error as exc:
        raise KnowledgeClassificationError(SAFE_CLASSIFICATION_ERROR) from exc


def load_bound_knowledge_classification(
    connection: sqlite3.Connection,
    job_id: int,
    question_no: str,
    draft: dict,
) -> dict | None:
    """Return a classification only when it still binds the exact approved draft."""
    if (
        draft.get("status") != "approved"
        or draft.get("approval_source") not in {"human", "ai_second_pass"}
        or not isinstance(draft.get("version"), int)
    ):
        return None
    try:
        edited = json.loads(draft["edited_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    row = connection.execute(
        """SELECT * FROM candidate_knowledge_classifications
           WHERE import_job_id=? AND source_question_no=?
             AND approved_draft_version=? AND edited_sha256=?""",
        (job_id, question_no, draft["version"], _canonical_sha(edited)),
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    try:
        related = json.loads(row["related_knowledge_point_codes_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    valid = {
        item[0] for item in connection.execute(
            "SELECT code FROM knowledge_points WHERE is_active=1"
        )
    }
    primary = row["primary_knowledge_point_code"]
    if (
        primary not in valid or not isinstance(related, list) or len(related) > 2
        or len(related) != len(set(related)) or primary in related
        or any(not isinstance(code, str) or code not in valid for code in related)
    ):
        return None
    return {
        "primary_code": primary,
        "related_codes": related,
        "classification_id": row["id"],
        "classifier": row["classifier"],
        "reviewer": row["reviewer"],
    }
