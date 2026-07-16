"""Safely admit independently audited import candidates into the question bank."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from src.database.initialize import DEFAULT_DATABASE_PATH, initialize_database
from src.reviewing.finalize import is_ai_second_pass_eligible
from src.processing.secure_crop_artifacts import (
    LOCK_FILENAME,
    SecureCropArtifactError,
    load_hmac_key,
    locked_job,
    read_file_at,
    validate_signed_manifest,
)
from src.web.app import (
    AuditDataError,
    CHOICE_TYPES,
    OPTION_CODE_PATTERN,
    _validate_audit_payload,
)


MAX_JSON_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_PNG_ARTIFACT_BYTES = 64 * 1024 * 1024


class AdmissionError(ValueError):
    """The batch cannot be admitted without weakening its safety guarantees."""


@dataclass(frozen=True)
class AssessmentItem:
    question_no: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssessmentReport:
    eligible: tuple[AssessmentItem, ...]
    ineligible: tuple[AssessmentItem, ...]


@dataclass(frozen=True)
class AdmissionResult:
    inserted: int
    already_present: int
    eligible: int
    ineligible: int
    question_codes: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactSnapshot:
    relative_path: str
    label: str
    max_bytes: int
    identity: tuple[int, int, int, int, int]
    sha256: str


def _read_stable_artifact(job_fd: int, relative_path: str,
                          label: str, max_bytes: int):
    try:
        pinned = read_file_at(job_fd, relative_path, max_bytes=max_bytes)
        return pinned.data, ArtifactSnapshot(
            relative_path, label, max_bytes, pinned.identity, pinned.sha256
        )
    except SecureCropArtifactError as exc:
        raise AdmissionError(f"输入文件{label}缺失、不安全或超出限制") from exc


def _read_artifact_json(job_fd: int, relative_path: str, label: str,
                        snapshots: list[ArtifactSnapshot]):
    content, snapshot = _read_stable_artifact(
        job_fd, relative_path, label, MAX_JSON_ARTIFACT_BYTES
    )
    snapshots.append(snapshot)
    try:
        return json.loads(content.decode("utf-8")), snapshot
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"{label}缺失或损坏") from exc


def _verify_artifact_snapshots(job_fd: int, snapshots) -> None:
    for expected in snapshots:
        try:
            _, current = _read_stable_artifact(
                job_fd, expected.relative_path, expected.label, expected.max_bytes
            )
        except AdmissionError as exc:
            raise AdmissionError(f"输入文件{expected.label}在准入期间发生变化") from exc
        if current.identity != expected.identity or current.sha256 != expected.sha256:
            raise AdmissionError(f"输入文件{expected.label}在准入期间发生变化")


@contextmanager
def _job_artifact_lock(job_dir: Path):
    descriptor = None
    try:
        supplied = Path(job_dir)
        if supplied.is_symlink():
            raise OSError("symbolic link job directory")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(supplied / LOCK_FILENAME, flags, 0o600)
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600):
            raise OSError("unsafe shared lock")
        os.close(descriptor)
        descriptor = None
        with locked_job(job_dir) as lock:
            yield lock
    except (OSError, SecureCropArtifactError) as exc:
        raise AdmissionError("导入任务文件锁不安全或不可用") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_png(job_dir: Path, job_fd: int, relative: object, entry: dict,
              snapshots: list[ArtifactSnapshot]) -> Path:
    if not isinstance(relative, str):
        raise AdmissionError("图片路径非法")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or "\\" in relative or rel.suffix.lower() != ".png":
        raise AdmissionError("图片路径非法")
    target = job_dir / rel.as_posix()
    content, snapshot = _read_stable_artifact(
        job_fd, rel.as_posix(), f"PNG {relative}", MAX_PNG_ARTIFACT_BYTES
    )
    snapshots.append(snapshot)
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG" or image.size != (entry.get("width"), entry.get("height")):
                raise AdmissionError("图片尺寸或格式不一致")
    except (OSError, UnidentifiedImageError) as exc:
        raise AdmissionError("图片无法读取") from exc
    if (entry.get("byte_size") != len(content)
            or entry.get("sha256") != snapshot.sha256):
        raise AdmissionError("图片哈希或大小不一致")
    return target


def _validate_optional_markdown_fields(questions):
    for question in questions:
        for field in ("answer_markdown", "analysis_markdown"):
            if field in question and not isinstance(question[field], str):
                raise AdmissionError(f"{field}必须为字符串")
        subquestions = question.get("subquestions", [])
        if not isinstance(subquestions, list):
            raise AdmissionError("subquestions必须为列表")
        for subquestion in subquestions:
            if not isinstance(subquestion, dict):
                raise AdmissionError("小题格式非法")
            for field in ("answer_markdown", "analysis_markdown"):
                if field in subquestion and not isinstance(subquestion[field], str):
                    raise AdmissionError(f"小题{field}必须为字符串")


def _has_markdown(question: dict, field: str) -> bool:
    parent = question.get(field, "")
    if isinstance(parent, str) and parent.strip():
        return True
    return any(
        isinstance(subquestion.get(field, ""), str) and subquestion.get(field, "").strip()
        for subquestion in question.get("subquestions", [])
    )


def _answer_analysis_sha256(question: dict) -> str:
    payload = {
        "source_question_no": question["source_question_no"],
        "answer_markdown": question.get("answer_markdown", ""),
        "analysis_markdown": question.get("analysis_markdown", ""),
        "subquestions": [
            {
                "label": subquestion.get("label", ""),
                "stem_markdown": subquestion.get("stem_markdown", ""),
                "answer_markdown": subquestion.get("answer_markdown", ""),
                "analysis_markdown": subquestion.get("analysis_markdown", ""),
            }
            for subquestion in question.get("subquestions", [])
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json_sha256(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_timezone_aware_iso8601(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_not_obviously_future_time(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc) + timedelta(
        minutes=5
    )


def _valid_edited_structure(question: dict) -> bool:
    options = question.get("options")
    related = question.get("related_knowledge_point_codes")
    subquestions = question.get("subquestions")
    if (not isinstance(options, list) or not isinstance(related, list)
            or not all(isinstance(code, str) for code in related)
            or not isinstance(subquestions, list)):
        return False
    option_codes = []
    for option in options:
        if (not isinstance(option, dict)
                or not isinstance(option.get("code"), str)
                or not option["code"].strip()
                or not OPTION_CODE_PATTERN.fullmatch(option["code"].strip())
                or not isinstance(option.get("content"), str)):
            return False
        option_codes.append(option["code"].strip().casefold())
    if len(option_codes) != len(set(option_codes)):
        return False
    if question.get("question_type_code") in CHOICE_TYPES and len(options) < 2:
        return False
    for subquestion in subquestions:
        if (not isinstance(subquestion, dict)
                or not isinstance(subquestion.get("label", ""), str)
                or not isinstance(subquestion.get("stem_markdown", ""), str)
                or any(field in subquestion and not isinstance(subquestion[field], str)
                       for field in ("answer_markdown", "analysis_markdown"))):
            return False
    return not any(
        field in question and not isinstance(question[field], str)
        for field in ("answer_markdown", "analysis_markdown")
    )


def _valid_question_number(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 3
        and value[0] in "123456789"
        and all("0" <= character <= "9" for character in value)
        and int(value) <= 999
    )


def _load_context(connection, private_root: Path, job_id: int, artifact_lock=None):
    job = connection.execute(
        """SELECT j.id,j.source_paper_id,s.sha256,s.region_code,s.exam_year,
                  s.exam_type_code,s.paper_name,s.stored_path
           FROM import_jobs j JOIN source_papers s ON s.id=j.source_paper_id WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise AdmissionError("导入任务不存在")
    if artifact_lock is None:
        job_dir = private_root / "processing" / f"import_job_{job_id}"
        with _job_artifact_lock(job_dir) as acquired:
            return _load_context(
                connection, private_root, job_id, artifact_lock=acquired
            )
    job_dir = artifact_lock.path
    job_fd = artifact_lock.descriptor
    snapshots = []
    candidate, candidate_snapshot = _read_artifact_json(
        job_fd, "candidate_questions.json", "候选题", snapshots
    )
    questions = candidate.get("questions")
    if (not isinstance(questions, list) or candidate.get("import_job_id") != job_id
            or candidate.get("source_paper_id") != job["source_paper_id"]
            or candidate.get("question_count") != len(questions)):
        raise AdmissionError("候选题与当前任务或试卷不匹配")
    numbers = [q.get("source_question_no") for q in questions if isinstance(q, dict)]
    if (len(numbers) != len(questions)
            or any(not _valid_question_number(number) for number in numbers)
            or len(set(numbers)) != len(numbers)):
        raise AdmissionError("候选题号非法或重复")
    _validate_optional_markdown_fields(questions)
    audit, _audit_snapshot = _read_artifact_json(
        job_fd, "ai_audit.json", "AI审核清单", snapshots
    )
    try:
        _, audits = _validate_audit_payload(audit, job_id, questions)
    except AuditDataError as exc:
        raise AdmissionError("AI审核清单不完整") from exc

    crops_data, _crops_snapshot = _read_artifact_json(
        job_fd, "question_crops.json", "完整题图清单", snapshots
    )
    crop_entries = crops_data.get("questions")
    if (crops_data.get("import_job_id") != job_id or crops_data.get("question_count") != len(questions)
            or not isinstance(crop_entries, list) or len(crop_entries) != len(questions)):
        raise AdmissionError("完整题图清单不完整")
    try:
        validate_signed_manifest(
            crops_data,
            load_hmac_key(job_dir),
            expected_job_id=job_id,
            expected_question_nos=[int(number) for number in numbers],
        )
    except SecureCropArtifactError as exc:
        raise AdmissionError(
            "完整题图清单签名或结构无效：必须使用v2签名工件，请先重新裁图迁移"
        ) from exc
    crops = {}
    for entry in crop_entries:
        number = str(entry.get("question_no"))
        if number in crops or number not in numbers or entry.get("review_status") != "ai_review_passed" or entry.get("crop_status") != "generated":
            raise AdmissionError("完整题图清单非法")
        expected = f"question_crops/Q{int(number):03d}.png"
        if entry.get("output_relative_path") != expected:
            raise AdmissionError("完整题图路径非法")
        _safe_png(job_dir, job_fd, expected, entry, snapshots)
        crops[number] = entry
    if set(crops) != set(numbers):
        raise AdmissionError("完整题图清单不完整")

    figures_data, _figures_snapshot = _read_artifact_json(
        job_fd, "figure_assets.json", "配图清单", snapshots
    )
    figures = {}
    figure_entries = figures_data.get("assets", [])
    if not isinstance(figure_entries, list):
        raise AdmissionError("配图清单缺失或损坏")
    for entry in figure_entries:
        if not isinstance(entry, dict) or entry.get("kind") != "question_figure":
            continue
        number = entry.get("question_no")
        if number in figures or number not in numbers or entry.get("review_status") != "ai_review_passed":
            raise AdmissionError("必要配图未通过审核")
        _safe_png(
            job_dir, job_fd, entry.get("output_relative_path"), entry, snapshots
        )
        figures[number] = entry
    return (
        job, job_dir, questions, audits, crops, figures,
        candidate_snapshot.sha256, tuple(snapshots),
    )


def _assess(connection, context, effective=None):
    job, _, questions, audits, crops, figures = context[:6]
    types = {row[0] for row in connection.execute("SELECT code FROM question_types WHERE is_active=1")}
    knowledge = {row[0] for row in connection.execute("SELECT code FROM knowledge_points WHERE is_active=1")}
    deleted = {
        row[0] for row in connection.execute(
            """SELECT source_question_no FROM candidate_review_drafts
               WHERE import_job_id=? AND deleted_at IS NOT NULL""",
            (job["id"],),
        )
    }
    eligible, ineligible = [], []
    if effective is None:
        effective = _effective_questions(connection, context)
    for source_question in questions:
        number = source_question["source_question_no"]
        question = effective[number][0]
        reasons = list(effective[number][2])
        audit = audits[number]
        if not is_ai_second_pass_eligible(audit) and effective[number][1] is None:
            status = audit.get("audit_status")
            if status != "auto_pass": reasons.append(str(status or "audit_missing"))
            if audit.get("audit_confidence") != "high": reasons.append("audit_confidence_not_high")
            if audit.get("issues") != []: reasons.append("audit_issues_present")
            if audit.get("suggested_corrections") != []: reasons.append("audit_corrections_present")
        if number in deleted: reasons.append("candidate_deleted")
        if not isinstance(question.get("stem_markdown"), str) or not question["stem_markdown"].strip(): reasons.append("empty_stem")
        if question.get("question_type_code") not in types: reasons.append("invalid_question_type")
        codes = [question.get("primary_knowledge_point_code"), *question.get("related_knowledge_point_codes", [])]
        if any(code not in knowledge for code in codes): reasons.append("missing_knowledge_point")
        if number not in crops: reasons.append("missing_question_crop")
        if question.get("figure_required") is True and number not in figures: reasons.append("missing_approved_figure")
        answer_relevant = (
            _has_markdown(source_question, "answer_markdown")
            or _has_markdown(question, "answer_markdown")
        )
        analysis_relevant = (
            _has_markdown(source_question, "analysis_markdown")
            or _has_markdown(question, "analysis_markdown")
        )
        if answer_relevant and audit.get("answer_status") != "passed":
            reasons.append("answer_status_not_passed")
        if analysis_relevant and audit.get("analysis_status") != "passed":
            reasons.append("analysis_status_not_passed")
        if ((answer_relevant or analysis_relevant)
                and audit.get("answer_analysis_sha256") != _answer_analysis_sha256(question)):
            reasons.append("answer_analysis_sha256_mismatch")
        item = AssessmentItem(number, tuple(reasons))
        (ineligible if reasons else eligible).append(item)
    return AssessmentReport(tuple(eligible), tuple(ineligible))


def _effective_questions(connection, context):
    """Choose reviewed human content for candidates not eligible via AI review."""
    job, _, questions, audits, _, _ = context[:6]
    candidate_sha256 = context[6]
    drafts = {
        row["source_question_no"]: row
        for row in connection.execute(
            """SELECT source_question_no,source_candidate_sha256,source_snapshot_json,
                      edited_json,status,version,reviewed_at,approval_source,
                      approval_evidence_json,deleted_at
               FROM candidate_review_drafts WHERE import_job_id=?""",
            (job["id"],),
        )
    }
    selected = {}
    for question in questions:
        number = question["source_question_no"]
        draft = drafts.get(number)
        ai_eligible = is_ai_second_pass_eligible(audits[number])
        human_approved = bool(
            draft is not None
            and draft["status"] == "approved"
            and draft["approval_source"] == "human"
        )
        if draft is None or (ai_eligible and not human_approved):
            selected[number] = (question, None, (), False)
            continue
        if draft["status"] != "approved":
            selected[number] = (
                question, None, ("human_approval_status_invalid",), False
            )
            continue
        if draft["approval_source"] != "human":
            selected[number] = (
                question, None, ("human_approval_source_invalid",), False
            )
            continue
        try:
            evidence = json.loads(draft["approval_evidence_json"])
        except (TypeError, json.JSONDecodeError):
            evidence = None
        reviewed_at = draft["reviewed_at"]
        evidence_method = evidence.get("method") if isinstance(evidence, dict) else None
        workbench_evidence = evidence_method in {"workbench", "workbench_quick"}
        legacy_evidence = evidence_method == "existing_approval"
        valid_evidence = bool(
            _is_timezone_aware_iso8601(reviewed_at)
            and _is_not_obviously_future_time(reviewed_at)
            and isinstance(evidence, dict)
            and set(evidence) == {"method", "reviewed_at"}
            and (workbench_evidence or legacy_evidence)
            and isinstance(evidence.get("reviewed_at"), str)
            and evidence["reviewed_at"]
            and evidence["reviewed_at"] == reviewed_at
        )
        if not valid_evidence:
            selected[number] = (
                question, None, ("human_approval_evidence_invalid",), False
            )
            continue
        try:
            source_snapshot = json.loads(draft["source_snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            source_snapshot = None
        if (draft["source_candidate_sha256"] != candidate_sha256
                or source_snapshot != question):
            selected[number] = (
                question, None, ("human_approval_source_binding_invalid",), False
            )
            continue
        try:
            edited = json.loads(draft["edited_json"])
        except (TypeError, json.JSONDecodeError):
            edited = None
        if (isinstance(edited, dict)
                and edited.get("source_question_no") != number):
            selected[number] = (
                question, None, ("human_approval_question_identity_invalid",), False
            )
            continue
        if (isinstance(edited, dict)
                and any(edited.get(field) != question.get(field)
                        for field in ("source_pages", "figure_required"))):
            selected[number] = (
                question, None, ("human_approval_immutable_fields_invalid",), False
            )
            continue
        if isinstance(edited, dict) and not _valid_edited_structure(edited):
            selected[number] = (
                question, None, ("human_approval_edited_json_invalid",), False
            )
            continue
        legacy_present = legacy_evidence
        if legacy_present:
            formal = connection.execute(
                """SELECT q.question_code,q.deleted_at
                   FROM questions q JOIN question_sources s ON s.question_id=q.id
                   WHERE s.import_job_id=? AND s.source_question_no=?""",
                (job["id"], number),
            ).fetchall()
            expected_code = _code(job["sha256"], number)
            if (len(formal) != 1 or formal[0]["question_code"] != expected_code
                    or formal[0]["deleted_at"] is not None):
                selected[number] = (
                    question, None, ("legacy_existing_approval_invalid",), False
                )
                continue
        selected[number] = (
            (edited, draft, (), legacy_present) if isinstance(edited, dict)
            else (
                question, None, ("human_approval_edited_json_invalid",), False
            )
        )
    return selected


def assess_job(database_path=DEFAULT_DATABASE_PATH, private_root=None, job_id=1):
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    with _job_artifact_lock(job_dir) as artifact_lock:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            context = _load_context(
                connection, private_root, job_id, artifact_lock=artifact_lock
            )
            report = _assess(connection, context)
            _verify_artifact_snapshots(artifact_lock.descriptor, context[7])
            return report


def _code(source_sha: str, number: str) -> str:
    return f"Q-{source_sha[:16]}-{int(number):03d}"


def _insert_one(connection, context, question, code, human_approval=None):
    job, _, _, audits, crops, figures = context[:6]
    number = question["source_question_no"]
    primary_id = connection.execute("SELECT id FROM knowledge_points WHERE code=?", (question["primary_knowledge_point_code"],)).fetchone()[0]
    content_hash = hashlib.sha256(json.dumps(question, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    answer = question.get("answer_markdown", "")
    answer_provided = isinstance(answer, str) and bool(answer.strip())
    analysis = question.get("analysis_markdown")
    analysis_provided = isinstance(analysis, str) and bool(analysis.strip())
    qid = connection.execute(
        """INSERT INTO questions
           (question_code,stem_markdown,answer_markdown,answer_status,analysis_markdown,region_code,exam_year,
            exam_type_code,paper_name,source_question_no,source_page,source_file_path,
            question_type_code,primary_knowledge_point_id,ocr_review_status,formula_review_status,
            figure_review_status,answer_review_status,analysis_review_status,tag_review_status,
            usability_status,content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, question["stem_markdown"], answer if answer_provided else "",
         "provided" if answer_provided else "missing", analysis if analysis_provided else None,
         job["region_code"], job["exam_year"], job["exam_type_code"],
         job["paper_name"], number, json.dumps(question.get("source_pages", [])), job["stored_path"],
         question["question_type_code"], primary_id, "passed", "passed",
         "passed" if question.get("figure_required") else "not_applicable",
         "passed" if answer_provided else "not_applicable",
         "passed" if analysis_provided else "not_applicable", "passed", "pending_review", content_hash),
    ).lastrowid
    for index, option in enumerate(question.get("options", []), 1):
        content = option.get("content", "")
        if content == "见原页选项图":
            continue
        connection.execute("INSERT INTO question_options(question_id,option_code,content_markdown,display_order) VALUES(?,?,?,?)", (qid, option["code"], content, index))
    for index, sub in enumerate(question.get("subquestions", []), 1):
        stem = " ".join(filter(None, [sub.get("label", ""), sub.get("stem_markdown", "")])).strip()
        sub_answer = sub.get("answer_markdown", "")
        sub_answer_provided = isinstance(sub_answer, str) and bool(sub_answer.strip())
        sub_analysis = sub.get("analysis_markdown")
        sub_analysis_provided = isinstance(sub_analysis, str) and bool(sub_analysis.strip())
        connection.execute(
            """INSERT INTO subquestions
               (question_id,display_order,stem_markdown,answer_markdown,answer_status,analysis_markdown)
               VALUES(?,?,?,?,?,?)""",
            (qid, index, stem, sub_answer if sub_answer_provided else "",
             "provided" if sub_answer_provided else "missing",
             sub_analysis if sub_analysis_provided else None),
        )
    for related in question.get("related_knowledge_point_codes", []):
        kid = connection.execute("SELECT id FROM knowledge_points WHERE code=?", (related,)).fetchone()[0]
        connection.execute("INSERT OR IGNORE INTO question_related_knowledge_points VALUES(?,?)", (qid, kid))
    pages = json.dumps(question.get("source_pages", []), ensure_ascii=False)
    connection.execute("INSERT INTO question_sources VALUES(?,?,?,?,?)", (qid, job["source_paper_id"], job["id"], number, pages))
    assets = [("complete_question", crops[number])]
    if number in figures: assets.append(("question_figure", figures[number]))
    for kind, asset in assets:
        connection.execute(
            """INSERT INTO question_assets
               (question_id,asset_kind,relative_path,width,height,byte_size,sha256,review_status,display_order,import_job_id)
               VALUES(?,?,?,?,?,?,?,'ai_review_passed',1,?)""",
            (qid, kind, asset["output_relative_path"], asset["width"], asset["height"], asset["byte_size"], asset["sha256"], job["id"]),
        )
    any_answer_provided = _has_markdown(question, "answer_markdown")
    answer_note = (
        "原卷答案已通过审核" if any_answer_provided else "原卷未提供答案"
    )
    if human_approval is not None:
        evidence = json.dumps(
            json.loads(human_approval["approval_evidence_json"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        reviewer = "human"
        reviewed_at = human_approval["reviewed_at"]
        review_note = (
            f"人工审核通过；草稿版本={human_approval['version']}；"
            f"候选源SHA256={human_approval['source_candidate_sha256']}；"
            f"获批内容SHA256={_canonical_json_sha256(question)}；"
            f"批准证据={evidence}；{answer_note}"
        )
    else:
        reviewer = audits[number].get("auditor", "ai_audit")
        reviewed_at = datetime.now(timezone.utc).isoformat()
        review_note = f"AI二审通过（auto_pass）；{answer_note}"
    connection.execute(
        """INSERT INTO question_reviews(question_id,review_item,previous_status,new_status,reviewer,reviewed_at,notes)
           VALUES(?,'usability','pending','passed',?,?,?)""",
        (qid, reviewer, reviewed_at, review_note),
    )
    return qid


def admit_questions(database_path=DEFAULT_DATABASE_PATH, private_root=None, job_id=1):
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    with _job_artifact_lock(job_dir) as artifact_lock:
        initialize_database(database_path).close()
        connection = sqlite3.connect(database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            context = _load_context(
                connection, private_root, job_id, artifact_lock=artifact_lock
            )
            effective = _effective_questions(connection, context)
            assessment = _assess(connection, context, effective)
            manifest_reasons = {
                "empty_stem", "invalid_question_type", "missing_knowledge_point",
                "missing_question_crop", "missing_approved_figure",
                "answer_status_not_passed", "analysis_status_not_passed",
                "answer_analysis_sha256_mismatch",
            }
            unsafe = [
                item for item in assessment.ineligible
                if manifest_reasons.intersection(item.reasons)
            ]
            if unsafe:
                detail = "; ".join(
                    f"Q{x.question_no}:{','.join(x.reasons)}" for x in unsafe
                )
                raise AdmissionError(f"批次不满足安全入库条件（{detail}）")
            codes, inserted, present = [], 0, 0
            for item in assessment.eligible:
                code = _code(context[0]["sha256"], item.question_no)
                codes.append(code)
                if connection.execute(
                    "SELECT 1 FROM questions WHERE question_code=?", (code,)
                ).fetchone():
                    present += 1
                    continue
                if effective[item.question_no][3]:
                    raise AdmissionError(
                        f"Q{item.question_no} 的历史批准不得用于首次入库"
                    )
                _insert_one(
                    connection, context, effective[item.question_no][0], code,
                    effective[item.question_no][1],
                )
                inserted += 1
            _verify_artifact_snapshots(artifact_lock.descriptor, context[7])
            result = AdmissionResult(
                inserted, present, len(assessment.eligible), len(assessment.ineligible),
                tuple(codes),
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def backup_database(database_path=DEFAULT_DATABASE_PATH, backup_dir=None):
    source = Path(database_path)
    target_dir = Path(backup_dir or source.parent / "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = target_dir / f"question-bank-{stamp}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target, _sha256(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--job-id", type=int, default=1)
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()
    if args.backup:
        path, digest = backup_database(args.database)
        print(json.dumps({"backup": str(path), "sha256": digest}, ensure_ascii=False))
    result = admit_questions(args.database, args.private_root, args.job_id)
    print(json.dumps(result.__dict__, ensure_ascii=False))


if __name__ == "__main__":
    main()
