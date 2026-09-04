"""Strict, transactional ingestion for independently stored AI reference answers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path


MIN_BATCH_QUESTION_COUNT = 1
MAX_BATCH_QUESTION_COUNT = 10
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ANSWER_LENGTH = 50_000
MAX_ANALYSIS_LENGTH = 100_000
MAX_MODEL_LENGTH = 200
MAX_NOTES_LENGTH = 2_000
HEX = frozenset("0123456789abcdef")


class AiReferenceAnswerError(ValueError):
    """An evidence bundle is unsafe to bind to formal questions."""


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AiReferenceAnswerError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_json(path):
    candidate = Path(path)
    try:
        content = candidate.read_bytes()
    except OSError as error:
        raise AiReferenceAnswerError("JSON file cannot be read") from error
    if not content or len(content) > MAX_JSON_BYTES:
        raise AiReferenceAnswerError("JSON file length is invalid")
    digest = hashlib.sha256(content).hexdigest()
    try:
        payload = json.loads(
            content.decode("utf-8"), object_pairs_hook=_object_no_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiReferenceAnswerError("invalid JSON structure") from error
    if not isinstance(payload, dict):
        raise AiReferenceAnswerError("invalid JSON structure")
    return payload, digest


def _exact_fields(value, expected, location):
    if not isinstance(value, dict):
        raise AiReferenceAnswerError(f"invalid JSON structure at {location}")
    actual = set(value)
    expected = set(expected)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise AiReferenceAnswerError(
            f"unknown field at {location}: {unknown[0]}"
        )
    if missing:
        raise AiReferenceAnswerError(
            f"missing field at {location}: {missing[0]}"
        )


def _string(value, location, maximum, *, nonempty=True):
    if not isinstance(value, str):
        raise AiReferenceAnswerError(f"invalid string at {location}")
    if len(value) > maximum:
        raise AiReferenceAnswerError(f"length limit exceeded at {location}")
    if nonempty and not value.strip():
        raise AiReferenceAnswerError(f"non-empty value required at {location}")
    return value


def _model(value, location):
    value = _string(value, location, MAX_MODEL_LENGTH)
    if value != value.strip():
        raise AiReferenceAnswerError(f"invalid model at {location}")
    return value


def _hash(value, location):
    value = _string(value, location, 64)
    if len(value) != 64 or any(character not in HEX for character in value):
        raise AiReferenceAnswerError(f"invalid content_hash at {location}")
    return value


def _identity(item, location):
    code = _string(item["question_code"], f"{location}.question_code", 100)
    digest = _hash(
        item["question_content_hash"], f"{location}.question_content_hash"
    )
    return code, digest


def _subquestions(value, location):
    if not isinstance(value, list) or len(value) > 100:
        raise AiReferenceAnswerError(f"invalid subquestion structure at {location}")
    result = []
    for index, item in enumerate(value):
        item_location = f"{location}[{index}]"
        _exact_fields(
            item,
            {"display_order", "answer_markdown", "analysis_markdown"},
            item_location,
        )
        order = item["display_order"]
        if type(order) is not int or order < 1:
            raise AiReferenceAnswerError(
                f"invalid subquestion display_order at {item_location}"
            )
        result.append({
            "display_order": order,
            "answer_markdown": _string(
                item["answer_markdown"], f"{item_location}.answer_markdown",
                MAX_ANSWER_LENGTH,
            ),
            "analysis_markdown": _string(
                item["analysis_markdown"], f"{item_location}.analysis_markdown",
                MAX_ANALYSIS_LENGTH,
            ),
        })
    if [item["display_order"] for item in result] != sorted(
        {item["display_order"] for item in result}
    ):
        raise AiReferenceAnswerError(f"invalid subquestion order at {location}")
    return result


def _parse_source(payload):
    _exact_fields(payload, {"schema_version", "questions"}, "source")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise AiReferenceAnswerError("unsupported source schema_version")
    questions = payload["questions"]
    if (
        not isinstance(questions, list)
        or not MIN_BATCH_QUESTION_COUNT <= len(questions) <= MAX_BATCH_QUESTION_COUNT
    ):
        raise AiReferenceAnswerError("source must contain between 1 and 10 questions")
    parsed = []
    for index, item in enumerate(questions):
        location = f"source.questions[{index}]"
        _exact_fields(item, {"question_code", "question_content_hash"}, location)
        parsed.append(_identity(item, location))
    if len({code for code, _digest in parsed}) != len(parsed):
        raise AiReferenceAnswerError("source coverage contains duplicate question_code")
    return parsed


def _parse_solution(payload, kind):
    _exact_fields(payload, {"schema_version", "model", "questions"}, kind)
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise AiReferenceAnswerError(f"unsupported {kind} schema_version")
    model = _model(payload["model"], f"{kind}.model")
    questions = payload["questions"]
    if (
        not isinstance(questions, list)
        or not MIN_BATCH_QUESTION_COUNT <= len(questions) <= MAX_BATCH_QUESTION_COUNT
    ):
        raise AiReferenceAnswerError(
            f"{kind} coverage must contain between 1 and 10 questions"
        )
    parsed = []
    for index, item in enumerate(questions):
        location = f"{kind}.questions[{index}]"
        _exact_fields(item, {
            "question_code", "question_content_hash", "answer_markdown",
            "analysis_markdown", "subquestions",
        }, location)
        code, digest = _identity(item, location)
        parsed.append({
            "question_code": code,
            "question_content_hash": digest,
            "answer_markdown": _string(
                item["answer_markdown"], f"{location}.answer_markdown",
                MAX_ANSWER_LENGTH,
            ),
            "analysis_markdown": _string(
                item["analysis_markdown"], f"{location}.analysis_markdown",
                MAX_ANALYSIS_LENGTH,
            ),
            "subquestions": _subquestions(
                item["subquestions"], f"{location}.subquestions"
            ),
        })
    return model, parsed


def _parse_review(payload):
    kind = "final_review"
    _exact_fields(payload, {"schema_version", "model", "questions"}, kind)
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise AiReferenceAnswerError("unsupported final_review schema_version")
    model = _model(payload["model"], "final_review.model")
    questions = payload["questions"]
    if (
        not isinstance(questions, list)
        or not MIN_BATCH_QUESTION_COUNT <= len(questions) <= MAX_BATCH_QUESTION_COUNT
    ):
        raise AiReferenceAnswerError(
            "final_review coverage must contain between 1 and 10 questions"
        )
    parsed = []
    for index, item in enumerate(questions):
        location = f"final_review.questions[{index}]"
        _exact_fields(
            item,
            {
                "question_code", "question_content_hash", "decision", "notes",
                "answer_markdown", "analysis_markdown", "subquestions",
            },
            location,
        )
        code, digest = _identity(item, location)
        decision = item["decision"]
        if decision not in {"passed", "unresolved", "failed"}:
            raise AiReferenceAnswerError(f"invalid review decision at {location}")
        if decision == "passed":
            answer_markdown = _string(
                item["answer_markdown"], f"{location}.answer_markdown",
                MAX_ANSWER_LENGTH,
            )
            analysis_markdown = _string(
                item["analysis_markdown"], f"{location}.analysis_markdown",
                MAX_ANALYSIS_LENGTH,
            )
            subquestions = _subquestions(
                item["subquestions"], f"{location}.subquestions"
            )
        else:
            if (
                item["answer_markdown"] != ""
                or item["analysis_markdown"] != ""
                or item["subquestions"] != []
            ):
                raise AiReferenceAnswerError(
                    f"final review content must be empty at {location}"
                )
            answer_markdown = ""
            analysis_markdown = ""
            subquestions = []
        parsed.append({
            "question_code": code,
            "question_content_hash": digest,
            "decision": decision,
            "notes": _string(
                item["notes"], f"{location}.notes", MAX_NOTES_LENGTH,
                nonempty=False,
            ),
            "answer_markdown": answer_markdown,
            "analysis_markdown": analysis_markdown,
            "subquestions": subquestions,
        })
    return model, parsed


def _require_matching_coverage(source, records, kind):
    identities = [
        (item["question_code"], item["question_content_hash"])
        for item in records
    ]
    if identities != source:
        marker = "order" if kind == "final_review" else "coverage"
        raise AiReferenceAnswerError(f"{kind} {marker} does not match source")


def _formal_question(connection, code, expected_hash):
    row = connection.execute(
        """SELECT q.id,q.content_hash,q.deleted_at,ans.source_answer_state,
                  (qs.question_id IS NOT NULL) AS is_formal
           FROM questions q
           LEFT JOIN question_sources qs ON qs.question_id=q.id
           LEFT JOIN import_answer_sources ans ON ans.import_job_id=qs.import_job_id
           WHERE q.question_code=?""",
        (code,),
    ).fetchone()
    if row is None:
        raise AiReferenceAnswerError(f"question is not formal: {code}")
    if row[2] is not None:
        raise AiReferenceAnswerError(f"question is deleted: {code}")
    if not row[4]:
        raise AiReferenceAnswerError(f"question is not formal: {code}")
    if row[3] != "source_has_no_answer":
        raise AiReferenceAnswerError(
            f"question source is not source_has_no_answer: {code}"
        )
    if row[1] != expected_hash:
        raise AiReferenceAnswerError(f"question content_hash mismatch: {code}")
    subquestions = connection.execute(
        "SELECT id,display_order FROM subquestions WHERE question_id=? ORDER BY display_order",
        (row[0],),
    ).fetchall()
    return row[0], subquestions


def _existing_matches(row, expected):
    return tuple(row) == tuple(expected)


def import_ai_reference_answers(
    database_path, source_path, generator_path, independent_path,
    final_review_path, *, apply=False,
):
    """Validate four evidence files and optionally insert approved answers."""
    try:
        database = Path(database_path).expanduser().resolve(strict=True)
    except OSError as error:
        raise AiReferenceAnswerError("database file does not exist") from error
    if not database.is_file():
        raise AiReferenceAnswerError("database path is not a file")
    source_payload, source_sha = _read_json(source_path)
    generator_payload, generator_sha = _read_json(generator_path)
    independent_payload, independent_sha = _read_json(independent_path)
    review_payload, review_sha = _read_json(final_review_path)
    evidence = (source_sha, generator_sha, independent_sha, review_sha)
    if len(set(evidence)) != len(evidence):
        raise AiReferenceAnswerError("input files have duplicate SHA-256")
    source = _parse_source(source_payload)
    generator_model, generated = _parse_solution(generator_payload, "generator")
    independent_model, independent = _parse_solution(
        independent_payload, "independent"
    )
    if generator_model == independent_model:
        raise AiReferenceAnswerError(
            "generator and independent models must differ"
        )
    review_model, reviewed = _parse_review(review_payload)
    _require_matching_coverage(source, generated, "generator")
    _require_matching_coverage(source, independent, "independent")
    _require_matching_coverage(source, reviewed, "final_review")

    connection = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    inserted = unchanged = skipped = 0
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for identity, generated_item, independent_item, review_item in zip(
            source, generated, independent, reviewed
        ):
            code, content_hash = identity
            question_id, formal_subquestions = _formal_question(
                connection, code, content_hash
            )
            expected_orders = [row[1] for row in formal_subquestions]
            for kind, item in (
                ("generator", generated_item),
                ("independent", independent_item),
            ):
                actual_orders = [sub["display_order"] for sub in item["subquestions"]]
                if actual_orders != expected_orders:
                    raise AiReferenceAnswerError(
                        f"{kind} subquestion coverage mismatch: {code}"
                    )
            if review_item["decision"] != "passed":
                if connection.execute(
                    "SELECT 1 FROM ai_reference_answers WHERE question_id=?",
                    (question_id,),
                ).fetchone() is not None:
                    raise AiReferenceAnswerError(
                        f"AI reference answer conflict: {code}"
                    )
                skipped += 1
                continue
            review_orders = [
                sub["display_order"] for sub in review_item["subquestions"]
            ]
            if review_orders != expected_orders:
                raise AiReferenceAnswerError(
                    f"final_review subquestion coverage mismatch: {code}"
                )
            expected = (
                content_hash, review_item["answer_markdown"],
                review_item["analysis_markdown"], generator_model,
                independent_model, review_model, "passed", review_item["notes"],
                *evidence,
            )
            subquestion_ids = {row[1]: row[0] for row in formal_subquestions}
            expected_subquestions = [
                (subquestion_ids[sub["display_order"]], sub["display_order"],
                 sub["answer_markdown"], sub["analysis_markdown"])
                for sub in review_item["subquestions"]
            ]
            existing = connection.execute(
                """SELECT id,question_content_hash,answer_markdown,analysis_markdown,
                          generator_model,independent_model,final_review_model,
                          review_decision,review_notes,source_sha256,generator_sha256,
                          independent_sha256,final_review_sha256
                   FROM ai_reference_answers WHERE question_id=?""",
                (question_id,),
            ).fetchone()
            if existing is not None:
                stored_subquestions = connection.execute(
                    """SELECT subquestion_id,display_order,answer_markdown,
                              analysis_markdown
                       FROM ai_reference_subquestion_answers
                       WHERE ai_reference_answer_id=? ORDER BY display_order""",
                    (existing[0],),
                ).fetchall()
                if (
                    not _existing_matches(existing[1:], expected)
                    or [tuple(row) for row in stored_subquestions]
                    != expected_subquestions
                ):
                    raise AiReferenceAnswerError(
                        f"AI reference answer conflict: {code}"
                    )
                unchanged += 1
                continue
            if not apply:
                inserted += 1
                continue
            answer_id = connection.execute(
                """INSERT INTO ai_reference_answers
                   (question_id,question_content_hash,answer_markdown,
                    analysis_markdown,generator_model,independent_model,
                    final_review_model,review_decision,review_notes,source_sha256,
                    generator_sha256,independent_sha256,final_review_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (question_id, *expected, created_at),
            ).lastrowid
            connection.executemany(
                """INSERT INTO ai_reference_subquestion_answers
                   (ai_reference_answer_id,subquestion_id,display_order,
                    answer_markdown,analysis_markdown) VALUES(?,?,?,?,?)""",
                [(answer_id, *values) for values in expected_subquestions],
            )
            inserted += 1
        if apply:
            connection.commit()
        else:
            connection.rollback()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "inserted": inserted, "unchanged": unchanged, "skipped": skipped,
        "applied": bool(apply),
    }
