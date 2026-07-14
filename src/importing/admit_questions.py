"""Safely admit independently audited import candidates into the question bank."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from src.database.initialize import DEFAULT_DATABASE_PATH, initialize_database
from src.reviewing.finalize import is_ai_second_pass_eligible
from src.web.app import AuditDataError, _load_valid_audit


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


def _json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"{label}缺失或损坏") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_png(job_dir: Path, relative: object, entry: dict) -> Path:
    if not isinstance(relative, str):
        raise AdmissionError("图片路径非法")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or "\\" in relative or rel.suffix.lower() != ".png":
        raise AdmissionError("图片路径非法")
    target = (job_dir / rel.as_posix()).resolve()
    if not target.is_relative_to(job_dir.resolve()) or not target.is_file():
        raise AdmissionError("图片不存在")
    try:
        with Image.open(target) as image:
            if image.format != "PNG" or image.size != (entry.get("width"), entry.get("height")):
                raise AdmissionError("图片尺寸或格式不一致")
    except (OSError, UnidentifiedImageError) as exc:
        raise AdmissionError("图片无法读取") from exc
    if entry.get("byte_size") != target.stat().st_size or entry.get("sha256") != _sha256(target):
        raise AdmissionError("图片哈希或大小不一致")
    return target


def _load_context(connection, private_root: Path, job_id: int):
    job = connection.execute(
        """SELECT j.id,j.source_paper_id,s.sha256,s.region_code,s.exam_year,
                  s.exam_type_code,s.paper_name,s.stored_path
           FROM import_jobs j JOIN source_papers s ON s.id=j.source_paper_id WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise AdmissionError("导入任务不存在")
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    candidate = _json(job_dir / "candidate_questions.json", "候选题")
    questions = candidate.get("questions")
    if (not isinstance(questions, list) or candidate.get("import_job_id") != job_id
            or candidate.get("source_paper_id") != job["source_paper_id"]
            or candidate.get("question_count") != len(questions)):
        raise AdmissionError("候选题与当前任务或试卷不匹配")
    numbers = [q.get("source_question_no") for q in questions if isinstance(q, dict)]
    if len(numbers) != len(questions) or any(not isinstance(n, str) or not n.isdigit() for n in numbers) or len(set(numbers)) != len(numbers):
        raise AdmissionError("候选题号非法或重复")
    try:
        _, audits = _load_valid_audit(job_dir / "ai_audit.json", job_id, questions)
    except AuditDataError as exc:
        raise AdmissionError("AI审核清单不完整") from exc

    crops_data = _json(job_dir / "question_crops.json", "完整题图清单")
    crop_entries = crops_data.get("questions")
    if (crops_data.get("import_job_id") != job_id or crops_data.get("question_count") != len(questions)
            or not isinstance(crop_entries, list) or len(crop_entries) != len(questions)):
        raise AdmissionError("完整题图清单不完整")
    crops = {}
    for entry in crop_entries:
        number = str(entry.get("question_no"))
        if number in crops or number not in numbers or entry.get("review_status") != "ai_review_passed" or entry.get("crop_status") != "generated":
            raise AdmissionError("完整题图清单非法")
        expected = f"question_crops/Q{int(number):03d}.png"
        if entry.get("output_relative_path") != expected:
            raise AdmissionError("完整题图路径非法")
        _safe_png(job_dir, expected, entry)
        crops[number] = entry
    if set(crops) != set(numbers):
        raise AdmissionError("完整题图清单不完整")

    figures_data = _json(job_dir / "figure_assets.json", "配图清单")
    figures = {}
    for entry in figures_data.get("assets", []):
        if not isinstance(entry, dict) or entry.get("kind") != "question_figure":
            continue
        number = entry.get("question_no")
        if number in figures or number not in numbers or entry.get("review_status") != "ai_review_passed":
            raise AdmissionError("必要配图未通过审核")
        _safe_png(job_dir, entry.get("output_relative_path"), entry)
        figures[number] = entry
    return job, job_dir, questions, audits, crops, figures


def _assess(connection, context):
    job, _, questions, audits, crops, figures = context
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
    for question in questions:
        number = question["source_question_no"]
        reasons = []
        audit = audits[number]
        if not is_ai_second_pass_eligible(audit):
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
        item = AssessmentItem(number, tuple(reasons))
        (ineligible if reasons else eligible).append(item)
    return AssessmentReport(tuple(eligible), tuple(ineligible))


def assess_job(database_path=DEFAULT_DATABASE_PATH, private_root=None, job_id=1):
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        context = _load_context(connection, private_root, job_id)
        return _assess(connection, context)


def _code(source_sha: str, number: str) -> str:
    return f"Q-{source_sha[:16]}-{int(number):03d}"


def _insert_one(connection, context, question, code):
    job, _, _, audits, crops, figures = context
    number = question["source_question_no"]
    primary_id = connection.execute("SELECT id FROM knowledge_points WHERE code=?", (question["primary_knowledge_point_code"],)).fetchone()[0]
    content_hash = hashlib.sha256(json.dumps(question, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    qid = connection.execute(
        """INSERT INTO questions
           (question_code,stem_markdown,answer_markdown,answer_status,region_code,exam_year,
            exam_type_code,paper_name,source_question_no,source_page,source_file_path,
            question_type_code,primary_knowledge_point_id,ocr_review_status,formula_review_status,
            figure_review_status,answer_review_status,tag_review_status,usability_status,content_hash)
           VALUES (?,?,?,'missing',?,?,?,?,?,?,?,?,?,'passed','passed',?,'not_applicable','passed','pending_review',?)""",
        (code, question["stem_markdown"], "", job["region_code"], job["exam_year"], job["exam_type_code"],
         job["paper_name"], number, json.dumps(question.get("source_pages", [])), job["stored_path"],
         question["question_type_code"], primary_id, 'passed' if question.get("figure_required") else 'not_applicable', content_hash),
    ).lastrowid
    for index, option in enumerate(question.get("options", []), 1):
        content = option.get("content", "")
        if content == "见原页选项图":
            continue
        connection.execute("INSERT INTO question_options(question_id,option_code,content_markdown,display_order) VALUES(?,?,?,?)", (qid, option["code"], content, index))
    for index, sub in enumerate(question.get("subquestions", []), 1):
        stem = " ".join(filter(None, [sub.get("label", ""), sub.get("stem_markdown", "")])).strip()
        connection.execute("INSERT INTO subquestions(question_id,display_order,stem_markdown,answer_markdown,answer_status) VALUES(?,?,?,'','missing')", (qid, index, stem))
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
    connection.execute(
        """INSERT INTO question_reviews(question_id,review_item,previous_status,new_status,reviewer,reviewed_at,notes)
           VALUES(?,'usability','pending','passed',?,?,?)""",
        (qid, audits[number].get("auditor", "ai_audit"), datetime.now(timezone.utc).isoformat(), "AI审核auto_pass；原卷未提供答案"),
    )
    return qid


def admit_questions(database_path=DEFAULT_DATABASE_PATH, private_root=None, job_id=1):
    database_path = Path(database_path)
    private_root = Path(private_root or database_path.parent)
    initialize_database(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        context = _load_context(connection, private_root, job_id)
        assessment = _assess(connection, context)
        manifest_reasons = {
            "empty_stem", "invalid_question_type", "missing_knowledge_point",
            "missing_question_crop", "missing_approved_figure",
        }
        unsafe = [item for item in assessment.ineligible if manifest_reasons.intersection(item.reasons)]
        if unsafe:
            detail = "; ".join(f"Q{x.question_no}:{','.join(x.reasons)}" for x in unsafe)
            raise AdmissionError(f"批次不满足安全入库条件（{detail}）")
        by_no = {q["source_question_no"]: q for q in context[2]}
        codes, inserted, present = [], 0, 0
        with connection:
            for item in assessment.eligible:
                code = _code(context[0]["sha256"], item.question_no)
                codes.append(code)
                if connection.execute("SELECT 1 FROM questions WHERE question_code=?", (code,)).fetchone():
                    present += 1
                    continue
                _insert_one(connection, context, by_no[item.question_no], code)
                inserted += 1
        return AdmissionResult(inserted, present, len(assessment.eligible), len(assessment.ineligible), tuple(codes))
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
