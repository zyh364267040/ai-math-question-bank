"""Source-bound extraction, independent review, and batch application of official answers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from src.processing.question_splitter import (
    CODEX_TIMEOUT_SECONDS,
    MAX_CODEX_OUTPUT_BYTES,
    MAX_CODEX_STDERR_BYTES,
    _bounded_communicate,
    _resolve_codex_bin,
    _terminate_process_group,
)
from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    locked_job,
    read_file_at,
)
from src.reviewing.local_knowledge_classification import (
    KnowledgeClassificationRunError,
    _authoritative_input,
    _classification_generation_rows,
    _validate_complete_classification_generation,
)
from src.reviewing.candidate_review_ai import (
    classification_scope_sha256,
    visual_question_scope_sha256,
)


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_PAGE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_PAGE_BYTES = 256 * 1024 * 1024
MAX_QUESTIONS = 200
MAX_SUBQUESTIONS = 30
MAX_MARKDOWN = 100_000
MAX_FAILED_RAW_BYTES = 1024 * 1024
SAFE_INPUT = "官方答案来源或当前候选已变化，请重新登记答案页"
SAFE_MODEL = "官方答案转写结果格式无效或不完整"
SAFE_REVIEW = "官方答案独立复核未能逐题通过"
SAFE_APPLY = "官方答案尚未完整独立复核，不能整批应用"


class OfficialAnswerError(ValueError):
    """A fail-closed, user-safe official-answer workflow error."""


@contextmanager
def _safe_job_lock(job_dir: Path):
    try:
        with locked_job(job_dir) as lock:
            yield lock
    except SecureCropArtifactError as exc:
        raise OfficialAnswerError(SAFE_INPUT) from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lease_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def _canonical_sha(value: object) -> str:
    return _sha(_canonical_bytes(value).rstrip(b"\n"))


def _read_json_at(job_fd: int, relative: str) -> tuple[dict, Any]:
    try:
        snapshot = read_file_at(job_fd, relative, max_bytes=MAX_ARTIFACT_BYTES)
        value = json.loads(snapshot.data.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError
        return value, snapshot
    except (SecureCropArtifactError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise OfficialAnswerError(SAFE_INPUT) from exc


def _question_shape(candidate: dict) -> tuple[list[str], dict[str, list[str]]]:
    questions = candidate.get("questions")
    if (
        not isinstance(questions, list) or not questions
        or len(questions) > MAX_QUESTIONS
        or candidate.get("question_count") != len(questions)
    ):
        raise OfficialAnswerError(SAFE_INPUT)
    numbers: list[str] = []
    labels: dict[str, list[str]] = {}
    for question in questions:
        number = question.get("source_question_no") if isinstance(question, dict) else None
        subquestions = question.get("subquestions") if isinstance(question, dict) else None
        if (
            not isinstance(number, str) or not number.isascii() or not number.isdigit()
            or number.startswith("0") or len(number) > 3 or int(number) > 999
            or not isinstance(subquestions, list) or len(subquestions) > MAX_SUBQUESTIONS
        ):
            raise OfficialAnswerError(SAFE_INPUT)
        found_labels = []
        for subquestion in subquestions:
            label = subquestion.get("label") if isinstance(subquestion, dict) else None
            if not isinstance(label, str) or not label.strip() or len(label) > 50:
                raise OfficialAnswerError(SAFE_INPUT)
            found_labels.append(label)
        if len(found_labels) != len(set(found_labels)):
            raise OfficialAnswerError(SAFE_INPUT)
        numbers.append(number)
        labels[number] = found_labels
    if len(numbers) != len(set(numbers)):
        raise OfficialAnswerError(SAFE_INPUT)
    return numbers, labels


def _current_draft_batch(connection, candidate: dict, numbers: list[str]):
    """Return the complete current approved draft batch in candidate order."""
    connection.row_factory = sqlite3.Row
    job_id = candidate.get("import_job_id")
    rows = list(connection.execute(
        """SELECT source_question_no,edited_json,status,deleted_at
           FROM candidate_review_drafts WHERE import_job_id=?""",
        (job_id,),
    ))
    indexed = {row["source_question_no"]: row for row in rows}
    if set(indexed) != set(numbers) or len(rows) != len(numbers):
        raise OfficialAnswerError(SAFE_INPUT)
    questions = []
    for number in numbers:
        row = indexed[number]
        if row["deleted_at"] is not None or row["status"] != "approved":
            raise OfficialAnswerError(SAFE_INPUT)
        try:
            edited = json.loads(row["edited_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise OfficialAnswerError(SAFE_INPUT) from exc
        if not isinstance(edited, dict) or edited.get("source_question_no") != number:
            raise OfficialAnswerError(SAFE_INPUT)
        questions.append(edited)
    draft_candidate = dict(candidate)
    draft_candidate["questions"] = questions
    draft_candidate["question_count"] = len(questions)
    draft_numbers, labels = _question_shape(draft_candidate)
    if draft_numbers != numbers:
        raise OfficialAnswerError(SAFE_INPUT)
    digest = _canonical_sha({
        "version": 1,
        "import_job_id": job_id,
        "questions": questions,
    })
    return draft_candidate, labels, digest


def _page_digest(pages: list[dict]) -> str:
    return _canonical_sha([
        {"page_number": page["page_number"], "relative_path": page["relative_path"],
         "png_sha256": page["png_sha256"], "byte_size": page["byte_size"]}
        for page in pages
    ])


def _verify_locked_inputs(lock, inputs) -> None:
    candidate = read_file_at(
        lock.descriptor, "candidate_questions.json", max_bytes=MAX_ARTIFACT_BYTES
    )
    if candidate.sha256 != inputs["candidate_sha"]:
        raise OfficialAnswerError(SAFE_INPUT)
    for page in inputs["pages"]:
        snapshot = read_file_at(
            lock.descriptor, page["relative_path"], max_bytes=MAX_PAGE_BYTES
        )
        if snapshot.sha256 != page["png_sha256"] or snapshot.size != page["byte_size"]:
            raise OfficialAnswerError(SAFE_INPUT)


def _load_registered_inputs(database_path: Path, private_root: Path, job_id: int):
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    try:
        with locked_job(job_dir) as lock:
            candidate, candidate_snapshot = _read_json_at(
                lock.descriptor, "candidate_questions.json"
            )
            numbers, labels = _question_shape(candidate)
            with closing(sqlite3.connect(database_path)) as connection:
                connection.row_factory = sqlite3.Row
                source = connection.execute(
                    "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
                ).fetchone()
                pages = [dict(row) for row in connection.execute(
                    "SELECT * FROM import_answer_pages WHERE import_job_id=? ORDER BY page_number",
                    (job_id,),
                )]
                draft_candidate, labels, draft_batch_sha = _current_draft_batch(
                    connection, candidate, numbers
                )
            if (
                source is None or source["source_answer_state"] == "source_has_no_answer"
                or source["candidate_sha256"] != candidate_snapshot.sha256
                or source["draft_batch_sha256"] != draft_batch_sha
                or source["expected_question_count"] != len(numbers) or not pages
            ):
                raise OfficialAnswerError(SAFE_INPUT)
            copied = tempfile.TemporaryDirectory(prefix="official-answer-pages-")
            image_paths = []
            total = 0
            try:
                for page in pages:
                    expected = f"pages/page_{page['page_number']:03d}.png"
                    if page["relative_path"] != expected:
                        raise OfficialAnswerError(SAFE_INPUT)
                    snapshot = read_file_at(
                        lock.descriptor, expected, max_bytes=MAX_PAGE_BYTES
                    )
                    total += snapshot.size
                    if (
                        total > MAX_TOTAL_PAGE_BYTES or snapshot.sha256 != page["png_sha256"]
                        or snapshot.size != page["byte_size"]
                    ):
                        raise OfficialAnswerError(SAFE_INPUT)
                    try:
                        with Image.open(io.BytesIO(snapshot.data)) as image:
                            image.load()
                            if image.format != "PNG" or image.size != (
                                page["pixel_width"], page["pixel_height"]
                            ):
                                raise OfficialAnswerError(SAFE_INPUT)
                    except (UnidentifiedImageError, OSError) as exc:
                        raise OfficialAnswerError(SAFE_INPUT) from exc
                    target = Path(copied.name) / Path(expected).name
                    target.write_bytes(snapshot.data)
                    target.chmod(0o600)
                    image_paths.append(target)
                return {
                    "candidate": draft_candidate, "immutable_candidate": candidate,
                    "candidate_sha": candidate_snapshot.sha256,
                    "draft_batch_sha": draft_batch_sha,
                    "numbers": numbers, "labels": labels, "pages": pages,
                    "page_digest": _page_digest(pages), "images": tuple(image_paths),
                    "temporary": copied, "job_dir": job_dir,
                }
            except Exception:
                copied.cleanup()
                raise
    except SecureCropArtifactError as exc:
        raise OfficialAnswerError(SAFE_INPUT) from exc


def register_answer_source(
    database_path, private_root, job_id: int, source_answer_state: str,
    page_start: int | None = None, page_end: int | None = None,
) -> None:
    """Record an explicit source classification and pin verified answer PNGs."""
    if source_answer_state not in {
        "source_has_no_answer", "source_has_answer_unprocessed"
    } or type(job_id) is not int or job_id <= 0:
        raise OfficialAnswerError("官方答案来源登记参数无效")
    if source_answer_state == "source_has_no_answer":
        if page_start is not None or page_end is not None:
            raise OfficialAnswerError("无答案来源不能登记答案页")
    elif (
        type(page_start) is not int or type(page_end) is not int
        or page_start <= 0 or page_end < page_start or page_end - page_start >= 100
    ):
        raise OfficialAnswerError("答案页范围无效")
    database_path, private_root = Path(database_path), Path(private_root)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    with closing(sqlite3.connect(database_path)) as connection:
        anchor = connection.execute(
            """SELECT j.source_paper_id,s.sha256,r.status,r.manifest_sha256,
                      r.manifest_byte_size,r.source_pdf_sha256
               FROM import_jobs j JOIN source_papers s ON s.id=j.source_paper_id
               LEFT JOIN import_page_render_runs r ON r.import_job_id=j.id
               WHERE j.id=?""", (job_id,),
        ).fetchone()
    if anchor is None:
        raise OfficialAnswerError("未找到导入任务")
    try:
        with locked_job(job_dir) as lock:
            candidate, candidate_snapshot = _read_json_at(
                lock.descriptor, "candidate_questions.json"
            )
            numbers, _ = _question_shape(candidate)
            if (
                candidate.get("import_job_id") != job_id
                or candidate.get("source_paper_id") != anchor[0]
            ):
                raise OfficialAnswerError(SAFE_INPUT)
            pages = []
            manifest_sha = None
            if source_answer_state != "source_has_no_answer":
                manifest, snapshot = _read_json_at(lock.descriptor, "render_manifest.json")
                manifest_sha = snapshot.sha256
                if (
                    anchor[2] != "completed" or anchor[3] != snapshot.sha256
                    or anchor[4] != snapshot.size or anchor[5] != anchor[1]
                    or manifest.get("import_job_id") != job_id
                    or manifest.get("source_pdf_sha256") != anchor[1]
                ):
                    raise OfficialAnswerError(SAFE_INPUT)
                manifest_pages = manifest.get("pages")
                if not isinstance(manifest_pages, list):
                    raise OfficialAnswerError(SAFE_INPUT)
                indexed = {
                    entry.get("page_number"): entry for entry in manifest_pages
                    if isinstance(entry, dict)
                }
                for number in range(page_start, page_end + 1):
                    entry = indexed.get(number)
                    expected = f"pages/page_{number:03d}.png"
                    if not entry or entry.get("relative_path") != expected:
                        raise OfficialAnswerError(SAFE_INPUT)
                    page = read_file_at(lock.descriptor, expected, max_bytes=MAX_PAGE_BYTES)
                    try:
                        with Image.open(io.BytesIO(page.data)) as image:
                            image.load()
                            valid_image = image.format == "PNG" and image.size == (
                                entry.get("pixel_width"), entry.get("pixel_height")
                            )
                    except (UnidentifiedImageError, OSError):
                        valid_image = False
                    if (
                        not valid_image or page.sha256 != entry.get("sha256")
                        or page.size != entry.get("byte_size")
                    ):
                        raise OfficialAnswerError(SAFE_INPUT)
                    pages.append({
                        "page_number": number, "relative_path": expected,
                        "png_sha256": page.sha256, "byte_size": page.size,
                        "pixel_width": entry["pixel_width"],
                        "pixel_height": entry["pixel_height"],
                    })
            now = _now()
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    job = connection.execute(
                        "SELECT 1 FROM import_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if job is None:
                        raise OfficialAnswerError("未找到导入任务")
                    _, _, draft_batch_sha = _current_draft_batch(
                        connection, candidate, numbers
                    )
                    classification = {
                        "import_job_id": job_id, "state": source_answer_state,
                        "page_start": page_start, "page_end": page_end,
                        "candidate_sha256": candidate_snapshot.sha256,
                        "draft_batch_sha256": draft_batch_sha,
                        "pages": pages,
                    }
                    active = connection.execute(
                        "SELECT 1 FROM import_answer_extraction_runs WHERE import_job_id=?",
                        (job_id,),
                    ).fetchone()
                    if active:
                        raise OfficialAnswerError("答案来源已有处理记录，不能覆盖")
                    connection.execute(
                        "DELETE FROM import_answer_sources WHERE import_job_id=?", (job_id,)
                    )
                    connection.execute(
                        """INSERT INTO import_answer_sources
                           (import_job_id,source_answer_state,answer_page_start,
                            answer_page_end,render_manifest_sha256,candidate_sha256,
                            draft_batch_sha256,expected_question_count,
                            classification_evidence_sha256,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (job_id, source_answer_state, page_start, page_end, manifest_sha,
                         candidate_snapshot.sha256, draft_batch_sha, len(numbers),
                         _canonical_sha(classification), now, now),
                    )
                    for page in pages:
                        connection.execute(
                            """INSERT INTO import_answer_pages
                               (import_job_id,page_number,relative_path,png_sha256,
                                byte_size,pixel_width,pixel_height)
                               VALUES(?,?,?,?,?,?,?)""",
                            (job_id, page["page_number"], page["relative_path"],
                             page["png_sha256"], page["byte_size"],
                             page["pixel_width"], page["pixel_height"]),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
    except (sqlite3.Error, SecureCropArtifactError) as exc:
        raise OfficialAnswerError(SAFE_INPUT) from exc


def _replacement_artifact(job_dir: Path, name: str, expected_sha, expected_size=None):
    try:
        raw = (job_dir / name).read_bytes()
    except OSError as exc:
        raise OfficialAnswerError(SAFE_INPUT) from exc
    if (
        not raw or len(raw) > MAX_ARTIFACT_BYTES or _sha(raw) != expected_sha
        or (expected_size is not None and len(raw) != expected_size)
    ):
        raise OfficialAnswerError(SAFE_INPUT)
    return raw


def replace_answer_source(
    database_path, private_root, job_id: int, page_start: int, page_end: int,
) -> None:
    """Safely replace one complete, unapplied official-answer batch in place."""
    if (
        type(job_id) is not int or job_id <= 0
        or type(page_start) is not int or type(page_end) is not int
        or page_start <= 0 or page_end < page_start or page_end - page_start >= 100
    ):
        raise OfficialAnswerError("答案页范围无效")
    database_path, private_root = Path(database_path), Path(private_root)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    moved: list[tuple[Path, Path]] = []
    archive_dir = job_dir / "official_answer_archive" / (
        "replaced_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_")
        + secrets.token_hex(4)
    )
    with _safe_job_lock(job_dir) as lock:
        candidate, candidate_snapshot = _read_json_at(
            lock.descriptor, "candidate_questions.json"
        )
        numbers, _ = _question_shape(candidate)
        with closing(sqlite3.connect(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
                ).fetchone()
                pages = [dict(row) for row in connection.execute(
                    "SELECT * FROM import_answer_pages WHERE import_job_id=? ORDER BY page_number",
                    (job_id,),
                )]
                extraction = connection.execute(
                    "SELECT * FROM import_answer_extraction_runs WHERE import_job_id=?", (job_id,)
                ).fetchone()
                review_run = connection.execute(
                    "SELECT * FROM import_answer_review_runs WHERE import_job_id=?", (job_id,)
                ).fetchone()
                answers = {row["source_question_no"]: row for row in connection.execute(
                    "SELECT * FROM candidate_official_answers WHERE import_job_id=?", (job_id,)
                )}
                reviews = {row["source_question_no"]: row for row in connection.execute(
                    "SELECT * FROM candidate_official_answer_reviews WHERE import_job_id=?", (job_id,)
                )}
                draft_candidate, _, current_draft_sha = _current_draft_batch(
                    connection, candidate, numbers
                )
                drafts = {row["source_question_no"]: row for row in connection.execute(
                    """SELECT source_question_no,source_snapshot_json,edited_json
                       FROM candidate_review_drafts WHERE import_job_id=?""", (job_id,)
                )}
                review_completed = (
                    review_run is not None and review_run["status"] == "completed"
                )
                review_failed = (
                    review_run is not None and review_run["status"] == "failed"
                )
                review_records_safe = (
                    review_completed and set(reviews) == set(numbers)
                    or review_failed and not reviews
                )
                if (
                    source is None
                    or source["source_answer_state"] != "source_has_answer_unprocessed"
                    or source["applied_at"] is not None
                    or source["answer_page_start"] != page_start
                    or source["answer_page_end"] != page_end
                    or source["candidate_sha256"] != candidate_snapshot.sha256
                    or source["expected_question_count"] != len(numbers)
                    or extraction is None or extraction["status"] != "completed"
                    or not (review_completed or review_failed)
                    or set(answers) != set(numbers) or not review_records_safe
                    or connection.execute(
                        "SELECT 1 FROM question_sources WHERE import_job_id=? LIMIT 1", (job_id,)
                    ).fetchone() is not None
                ):
                    raise OfficialAnswerError("现有答案批次不满足安全替换条件")
                if review_run is None:
                    raise OfficialAnswerError("现有答案批次不满足安全替换条件")
                old_draft_sha = source["draft_batch_sha256"]
                anchors = [
                    extraction["candidate_sha256"] == candidate_snapshot.sha256,
                    extraction["draft_batch_sha256"] == old_draft_sha,
                    extraction["answer_pages_sha256"] == _page_digest(pages),
                    review_run["candidate_sha256"] == candidate_snapshot.sha256,
                    review_run["draft_batch_sha256"] == old_draft_sha,
                    review_run["answer_pages_sha256"] == _page_digest(pages),
                    review_run["extraction_artifact_sha256"] == extraction["output_sha256"],
                    all(
                        row["candidate_sha256"] == candidate_snapshot.sha256
                        and row["draft_batch_sha256"] == old_draft_sha
                        and row["extraction_artifact_sha256"] == extraction["output_sha256"]
                        for row in answers.values()
                    ),
                    all(
                        row["decision"] == "passed"
                        and row["candidate_sha256"] == candidate_snapshot.sha256
                        and row["draft_batch_sha256"] == old_draft_sha
                        and row["answer_pages_sha256"] == extraction["answer_pages_sha256"]
                        and row["extraction_artifact_sha256"] == extraction["output_sha256"]
                        for row in reviews.values()
                    ),
                ]
                for page in pages:
                    snapshot = read_file_at(
                        lock.descriptor, page["relative_path"], max_bytes=MAX_PAGE_BYTES
                    )
                    anchors.append(
                        snapshot.sha256 == page["png_sha256"]
                        and snapshot.size == page["byte_size"]
                    )
                for number in numbers:
                    try:
                        source_snapshot = json.loads(drafts[number]["source_snapshot_json"])
                        edited = json.loads(drafts[number]["edited_json"])
                    except (json.JSONDecodeError, TypeError, KeyError) as exc:
                        raise OfficialAnswerError(SAFE_INPUT) from exc
                    if not _answer_fields_equal(source_snapshot, edited):
                        raise OfficialAnswerError("检测到人工编辑的答案或解析，安全替换已拒绝")
                if not all(anchors):
                    raise OfficialAnswerError(SAFE_INPUT)
                artifacts = [
                    ("official_answers_raw.json", extraction["raw_artifact_sha256"],
                     extraction["raw_artifact_byte_size"]),
                    ("official_answers.json", extraction["output_sha256"],
                     extraction["output_byte_size"]),
                ]
                if review_completed:
                    artifacts.extend([
                        ("official_answer_review_raw.json", review_run["raw_artifact_sha256"], None),
                        ("official_answer_review.json", review_run["output_sha256"], None),
                    ])
                for name, digest, size in artifacts:
                    _replacement_artifact(job_dir, name, digest, size)
                archive_dir.mkdir(parents=True, mode=0o700)
                for name, _, _ in artifacts:
                    source_path, target = job_dir / name, archive_dir / name
                    if target.exists():
                        raise OfficialAnswerError(SAFE_INPUT)
                    os.replace(source_path, target)
                    moved.append((source_path, target))
                connection.execute(
                    "DELETE FROM candidate_official_answer_reviews WHERE import_job_id=?", (job_id,)
                )
                connection.execute(
                    "DELETE FROM import_answer_review_runs WHERE import_job_id=?", (job_id,)
                )
                connection.execute(
                    "DELETE FROM candidate_official_answers WHERE import_job_id=?", (job_id,)
                )
                connection.execute(
                    "DELETE FROM import_answer_extraction_runs WHERE import_job_id=?", (job_id,)
                )
                classification = {
                    "import_job_id": job_id,
                    "state": "source_has_answer_unprocessed",
                    "page_start": page_start,
                    "page_end": page_end,
                    "candidate_sha256": candidate_snapshot.sha256,
                    "draft_batch_sha256": current_draft_sha,
                    "pages": pages,
                }
                now = _now()
                connection.execute(
                    """UPDATE import_answer_sources SET draft_batch_sha256=?,
                       classification_evidence_sha256=?,updated_at=? WHERE import_job_id=?""",
                    (current_draft_sha, _canonical_sha(classification), now, job_id),
                )
                del draft_candidate
                connection.commit()
            except Exception:
                connection.rollback()
                for original, archived in reversed(moved):
                    try:
                        os.replace(archived, original)
                    except FileNotFoundError:
                        pass
                raise


def _bounded_text(value: object, *, nonempty: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= MAX_MARKDOWN and (
        not nonempty or bool(value.strip())
    )


def _formula_complete(value: str) -> bool:
    def preceding_backslashes(index: int) -> int:
        count = 0
        index -= 1
        while index >= 0 and value[index] == "\\":
            count += 1
            index -= 1
        return count

    dollars = sum(
        1 for index, character in enumerate(value)
        if character == "$" and preceding_backslashes(index) % 2 == 0
    )
    opened = sum(
        1 for index in range(len(value) - 1)
        if value[index:index + 2] == "\\(" and preceding_backslashes(index) % 2 == 0
    )
    closed = sum(
        1 for index in range(len(value) - 1)
        if value[index:index + 2] == "\\)" and preceding_backslashes(index) % 2 == 0
    )
    return dollars % 2 == 0 and opened == closed


def answer_extraction_schema() -> dict:
    sub = {"type": "object", "additionalProperties": False,
           "required": ["label", "answer_markdown", "analysis_markdown"],
           "properties": {
               "label": {"type": "string", "minLength": 1, "maxLength": 50},
               "answer_markdown": {"type": "string", "maxLength": MAX_MARKDOWN},
               "analysis_markdown": {"type": "string", "maxLength": MAX_MARKDOWN},
           }}
    question = {"type": "object", "additionalProperties": False,
                "required": ["source_question_no", "content_kind", "answer_markdown",
                             "analysis_markdown", "subquestions", "source_pages"],
                "properties": {
                    "source_question_no": {"type": "string", "pattern": "^[1-9][0-9]{0,2}$"},
                    "content_kind": {"type": "string", "enum": ["short_answer", "worked_solution"]},
                    "answer_markdown": {"type": "string", "maxLength": MAX_MARKDOWN},
                    "analysis_markdown": {"type": "string", "maxLength": MAX_MARKDOWN},
                    "subquestions": {"type": "array", "maxItems": MAX_SUBQUESTIONS, "items": sub},
                    "source_pages": {"type": "array", "minItems": 1, "maxItems": 100,
                                     "items": {"type": "integer", "minimum": 1, "maximum": 10000}},
                }}
    return {"type": "object", "additionalProperties": False,
            "required": ["version", "import_job_id", "candidate_sha256",
                         "draft_batch_sha256", "question_count", "questions"],
            "properties": {
                "version": {"type": "integer", "const": 1},
                "import_job_id": {"type": "integer", "minimum": 1},
                "candidate_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "draft_batch_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "question_count": {"type": "integer", "minimum": 1, "maximum": MAX_QUESTIONS},
                "questions": {"type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS, "items": question},
            }}


def parse_answer_extraction_output(
    raw, job_id, candidate_sha, candidate, page_hashes, draft_batch_sha=None
):
    """Parse one exact provider-compatible JSON value and enforce semantic coverage."""
    try:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_ARTIFACT_BYTES:
            raise TypeError
        decoder = json.JSONDecoder(); value, end = decoder.raw_decode(raw)
        top = {"version", "import_job_id", "candidate_sha256", "draft_batch_sha256",
               "question_count", "questions"}
        if raw[end:].strip() or not isinstance(value, dict) or set(value) != top:
            raise TypeError
        numbers, labels = _question_shape(candidate)
        questions = value["questions"]
        if (
            value["version"] != 1 or value["import_job_id"] != job_id
            or value["candidate_sha256"] != candidate_sha
            or not isinstance(draft_batch_sha, str)
            or value["draft_batch_sha256"] != draft_batch_sha
            or value["question_count"] != len(numbers)
            or not isinstance(questions, list) or len(questions) != len(numbers)
        ):
            raise TypeError
        question_keys = {"source_question_no", "content_kind", "answer_markdown",
                         "analysis_markdown", "subquestions", "source_pages"}
        sub_keys = {"label", "answer_markdown", "analysis_markdown"}
        for expected, question in zip(numbers, questions):
            if not isinstance(question, dict) or set(question) != question_keys:
                raise TypeError
            if question["source_question_no"] != expected or question["content_kind"] not in {
                "short_answer", "worked_solution"
            }:
                raise TypeError
            answer, analysis = question["answer_markdown"], question["analysis_markdown"]
            if not _bounded_text(answer) or not _bounded_text(analysis):
                raise TypeError
            subquestions = question["subquestions"]
            if not isinstance(subquestions, list) or len(subquestions) != len(labels[expected]):
                raise TypeError
            for expected_label, subquestion in zip(labels[expected], subquestions):
                if (
                    not isinstance(subquestion, dict) or set(subquestion) != sub_keys
                    or subquestion["label"] != expected_label
                    or not _bounded_text(subquestion["answer_markdown"])
                    or not _bounded_text(subquestion["analysis_markdown"])
                ):
                    raise TypeError
            content = [answer, analysis, *(
                text for sub in subquestions
                for text in (sub["answer_markdown"], sub["analysis_markdown"])
            )]
            if not any(text.strip() for text in content) or not all(
                _formula_complete(text) for text in content
            ):
                raise TypeError
            pages = question["source_pages"]
            if (
                not isinstance(pages, list) or pages != sorted(set(pages))
                or not pages or any(type(page) is not int or page not in page_hashes for page in pages)
            ):
                raise TypeError
            if question["content_kind"] == "short_answer" and (
                analysis.strip() or any(sub["analysis_markdown"].strip() for sub in subquestions)
            ):
                raise TypeError
        return value
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise OfficialAnswerError(SAFE_MODEL) from exc


def answer_review_schema() -> dict:
    question = {"type": "object", "additionalProperties": False,
                "required": ["source_question_no", "decision", "question_number_match",
                             "final_answer_match", "all_subquestions_match",
                             "page_boundaries_match", "formula_complete", "source_pages", "issues"],
                "properties": {
                    "source_question_no": {"type": "string", "pattern": "^[1-9][0-9]{0,2}$"},
                    "decision": {"type": "string", "enum": ["passed", "failed"]},
                    **{name: {"type": "boolean"} for name in (
                        "question_number_match", "final_answer_match", "all_subquestions_match",
                        "page_boundaries_match", "formula_complete")},
                    "source_pages": {"type": "array", "minItems": 1, "maxItems": 100,
                                     "items": {"type": "integer", "minimum": 1, "maximum": 10000}},
                    "issues": {"type": "array", "maxItems": 30,
                               "items": {"type": "string", "maxLength": 500}},
                }}
    return {"type": "object", "additionalProperties": False,
            "required": ["version", "import_job_id", "candidate_sha256",
                         "draft_batch_sha256",
                         "extraction_artifact_sha256", "question_count", "questions"],
            "properties": {
                "version": {"type": "integer", "const": 1},
                "import_job_id": {"type": "integer", "minimum": 1},
                "candidate_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "draft_batch_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "extraction_artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "question_count": {"type": "integer", "minimum": 1, "maximum": MAX_QUESTIONS},
                "questions": {"type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS, "items": question},
            }}


def parse_answer_review_output(
    raw, job_id, candidate_sha, extraction_sha, extraction, draft_batch_sha=None
):
    try:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_ARTIFACT_BYTES:
            raise TypeError
        decoder = json.JSONDecoder(); value, end = decoder.raw_decode(raw)
        top = {"version", "import_job_id", "candidate_sha256", "draft_batch_sha256",
               "extraction_artifact_sha256", "question_count", "questions"}
        if raw[end:].strip() or not isinstance(value, dict) or set(value) != top:
            raise TypeError
        expected = extraction["questions"]
        questions = value["questions"]
        if (
            value["version"] != 1 or value["import_job_id"] != job_id
            or value["candidate_sha256"] != candidate_sha
            or not isinstance(draft_batch_sha, str)
            or value["draft_batch_sha256"] != draft_batch_sha
            or extraction.get("draft_batch_sha256") != draft_batch_sha
            or value["extraction_artifact_sha256"] != extraction_sha
            or value["question_count"] != len(expected)
            or not isinstance(questions, list) or len(questions) != len(expected)
        ):
            raise TypeError
        keys = {"source_question_no", "decision", "question_number_match",
                "final_answer_match", "all_subquestions_match", "page_boundaries_match",
                "formula_complete", "source_pages", "issues"}
        checks = {"question_number_match", "final_answer_match", "all_subquestions_match",
                  "page_boundaries_match", "formula_complete"}
        for source, review in zip(expected, questions):
            if not isinstance(review, dict) or set(review) != keys:
                raise TypeError
            if (
                review["source_question_no"] != source["source_question_no"]
                or review["source_pages"] != source["source_pages"]
                or review["decision"] != "passed" or review["issues"] != []
                or any(review[name] is not True for name in checks)
            ):
                raise TypeError
        return value
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise OfficialAnswerError(SAFE_REVIEW) from exc


class OfficialAnswerCodexRunner:
    """Run one fresh, read-only, shell-less Codex session per invocation."""

    def __init__(self, executable=None, timeout=CODEX_TIMEOUT_SECONDS):
        self.executable = Path(executable).resolve() if executable else _resolve_codex_bin()
        details = self.executable.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OfficialAnswerError("Codex CLI 不可用")
        self.identity = (details.st_dev, details.st_ino, details.st_size,
                         details.st_mtime_ns, details.st_ctime_ns)
        self.timeout = timeout

    def run(self, *, image_paths, prompt, schema):
        details = self.executable.stat()
        identity = (details.st_dev, details.st_ino, details.st_size,
                    details.st_mtime_ns, details.st_ctime_ns)
        if identity != self.identity or not stat.S_ISREG(details.st_mode):
            raise OfficialAnswerError("Codex CLI 不可用")
        with tempfile.TemporaryDirectory(prefix="official-answer-codex-") as temporary:
            root = Path(temporary); schema_path = root / "schema.json"; output = root / "output.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [str(self.executable), "exec", "--sandbox", "read-only", "--ephemeral",
                       "--ignore-user-config", "--ignore-rules", "--disable", "shell_tool",
                       "--disable", "unified_exec", "--disable", "shell_snapshot",
                       "--skip-git-repo-check", "--color", "never", "--cd", str(root),
                       "--output-schema", str(schema_path), "--output-last-message", str(output),
                       "--image", *(str(Path(path).resolve()) for path in image_paths), "--", prompt]
            environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
            for name in ("HOME", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
                         "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                if os.environ.get(name):
                    environment[name] = os.environ[name]
            process = subprocess.Popen(command, cwd=root, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False, start_new_session=True)
            try:
                try:
                    stdout, stderr = _bounded_communicate(
                        process, self.timeout, MAX_CODEX_OUTPUT_BYTES, MAX_CODEX_STDERR_BYTES
                    )
                except Exception:
                    _terminate_process_group(process); raise
            finally:
                process.stdout.close(); process.stderr.close()
            if process.returncode != 0:
                raise OfficialAnswerError(SAFE_MODEL)
            raw = output.read_bytes()
            if not raw or len(raw) > MAX_ARTIFACT_BYTES:
                raise OfficialAnswerError(SAFE_MODEL)
            return raw.decode("utf-8"), "codex-" + _sha(stdout + stderr + raw)[:24]


def _runner_result(result) -> tuple[str, str]:
    try:
        if isinstance(result, tuple):
            raw, run_id = result
        else:
            raw, run_id = result.final_message, result.run_id
        if not isinstance(raw, str) or not isinstance(run_id, str) or not (1 <= len(run_id) <= 200):
            raise TypeError
        return raw, run_id
    except (AttributeError, TypeError, ValueError) as exc:
        raise OfficialAnswerError(SAFE_MODEL) from exc


def _publish_pair(job_dir: Path, pairs: list[tuple[str, bytes]]) -> None:
    staged = []
    published = []
    try:
        for name, content in pairs:
            fd, path = tempfile.mkstemp(prefix=f".{name}.", dir=job_dir)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600); stream.write(content); stream.flush(); os.fsync(stream.fileno())
            staged.append((Path(path), job_dir / name))
        if any(target.exists() for _, target in staged):
            raise OfficialAnswerError("官方答案工件已存在，拒绝覆盖")
        for source, target in staged:
            os.replace(source, target)
            published.append(target)
    except Exception:
        for target in published:
            try: target.unlink()
            except FileNotFoundError: pass
        raise
    finally:
        for source, _ in staged:
                try: source.unlink()
                except FileNotFoundError: pass


def _store_failed_raw_diagnostic(
    database_path: Path, job_dir: Path, job_id: int, stage: str,
    model_run_id: str, raw: str,
) -> None:
    """Atomically retain bounded, untrusted parser input and anchor it in SQLite."""
    raw_bytes = raw.encode("utf-8")[:MAX_FAILED_RAW_BYTES]
    if not raw_bytes:
        return
    digest = _sha(raw_bytes)
    with closing(sqlite3.connect(database_path)) as connection:
        existing = connection.execute(
            """SELECT 1 FROM import_answer_raw_diagnostics
               WHERE import_job_id=? AND stage=? AND model_run_id=? AND raw_sha256=?""",
            (job_id, stage, model_run_id, digest),
        ).fetchone()
    if existing:
        return
    relative = (
        f"official_answer_diagnostics/{stage}_{_sha(model_run_id.encode())[:16]}_"
        f"{digest[:16]}_{secrets.token_hex(4)}.json"
    )
    target = job_dir / relative
    with _safe_job_lock(job_dir):
        directory = target.parent
        directory.mkdir(mode=0o700, exist_ok=True)
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise OfficialAnswerError(SAFE_INPUT)
        directory.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".failed-raw-", dir=directory)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                raise OfficialAnswerError(SAFE_INPUT)
            os.replace(temporary_path, target)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    try:
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute(
                """INSERT INTO import_answer_raw_diagnostics
                   (import_job_id,stage,model_run_id,artifact_relative_path,
                    raw_sha256,byte_size,trusted,created_at)
                   VALUES(?,?,?,?,?,?,0,?)""",
                (job_id, stage, model_run_id, relative, digest, len(raw_bytes), _now()),
            )
    except Exception:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise


def _extraction_prompt(inputs) -> str:
    structure = [{"source_question_no": q["source_question_no"],
                  "subquestion_labels": inputs["labels"][q["source_question_no"]]}
                 for q in inputs["candidate"]["questions"]]
    page_range = [inputs["pages"][0]["page_number"], inputs["pages"][-1]["page_number"]]
    return (
        f"只转写官方答案页，禁止解题、推导、补写或猜测。import_job_id={inputs['candidate']['import_job_id']}；"
        f"候选SHA={inputs['candidate_sha']}；显式答案页范围={page_range}；"
        f"当前草稿批次SHA={inputs['draft_batch_sha']}；"
        f"权威题号与小问结构={json.dumps(structure, ensure_ascii=False, separators=(',', ':'))}。"
        "short_answer 只保存原卷短答案；worked_solution 必须逐行完整转写原卷官方解答，"
        "不得概括、缩写、合并步骤或省略公式，也不得把同一解答同时复制到题级与小问级。"
        "有小问时，题级 analysis_markdown 仅保存答案页在第一小问前明确出现的公共解答前言；"
        "各小问只保存答案页对应编号下的原文。"
        "只有题目结构中的父级条件行而答案页没有独立作答段时，该父级行答案与解析必须均为空。"
        "长解答可绑定连续多页，但 source_pages 只能使用输入图片页。"
        "题号、小问或公式看不清时不要补全，输出将被拒绝。"
        "只输出符合 schema 的单一 JSON。"
    )


def claim_answer_extraction(database_path, private_root, job_id: int) -> str | None:
    inputs = _load_registered_inputs(Path(database_path), Path(private_root), job_id)
    inputs["temporary"].cleanup()
    token = secrets.token_hex(32); now = _now()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status,lease_expires_at FROM import_answer_extraction_runs WHERE import_job_id=?",
            (job_id,),
        ).fetchone()
        if row and (row[0] == "completed" or (
            row[0] == "processing" and isinstance(row[1], str) and row[1] > now
        )):
            connection.rollback(); return None
        values = (inputs["candidate_sha"], inputs["draft_batch_sha"],
                  inputs["page_digest"], len(inputs["numbers"]),
                  token, _lease_time(), now, now, job_id)
        if row:
            connection.execute(
                """UPDATE import_answer_extraction_runs SET status='processing',
                   candidate_sha256=?,draft_batch_sha256=?,answer_pages_sha256=?,
                   question_count=?,claim_token=?,
                   lease_expires_at=?,error_message=NULL,started_at=?,updated_at=?
                   WHERE import_job_id=?""", values,
            )
        else:
            connection.execute(
                """INSERT INTO import_answer_extraction_runs
                   (candidate_sha256,draft_batch_sha256,answer_pages_sha256,
                    question_count,claim_token,
                    lease_expires_at,started_at,updated_at,import_job_id,status)
                   VALUES(?,?,?,?,?,?,?,?,?, 'processing')""", values,
            )
        connection.commit(); return token


def run_answer_extraction(
    database_path, private_root, job_id: int, runner=None, *, _claim_token=None,
) -> int:
    claim_token = _claim_token or claim_answer_extraction(
        database_path, private_root, job_id
    )
    if claim_token is None:
        return 0
    inputs = _load_registered_inputs(Path(database_path), Path(private_root), job_id)
    runner = runner or OfficialAnswerCodexRunner()
    published = False
    raw = None
    run_id = None
    parser_rejected = False
    try:
        raw, run_id = _runner_result(runner.run(
            image_paths=inputs["images"], prompt=_extraction_prompt(inputs),
            schema=answer_extraction_schema(),
        ))
        page_hashes = {page["page_number"]: page["png_sha256"] for page in inputs["pages"]}
        try:
            parsed = parse_answer_extraction_output(
                raw, job_id, inputs["candidate_sha"], inputs["candidate"], page_hashes,
                inputs["draft_batch_sha"],
            )
        except OfficialAnswerError:
            parser_rejected = True
            raise
        normalized = _canonical_bytes(parsed); raw_bytes = raw.encode()
        current = _load_registered_inputs(Path(database_path), Path(private_root), job_id)
        try:
            if (
                current["candidate_sha"] != inputs["candidate_sha"]
                or current["draft_batch_sha"] != inputs["draft_batch_sha"]
                or current["page_digest"] != inputs["page_digest"]
            ):
                raise OfficialAnswerError(SAFE_INPUT)
        finally:
            current["temporary"].cleanup()
        with locked_job(inputs["job_dir"]) as lock:
            _verify_locked_inputs(lock, inputs)
            _publish_pair(inputs["job_dir"], [
                ("official_answers_raw.json", raw_bytes), ("official_answers.json", normalized)
            ])
            published = True
        completed = _now(); output_sha = _sha(normalized)
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON"); connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM candidate_official_answers WHERE import_job_id=?", (job_id,))
                for question in parsed["questions"]:
                    pages = question["source_pages"]
                    page_map = {str(page): page_hashes[page] for page in pages}
                    content_sha = _canonical_sha(question)
                    connection.execute(
                        """INSERT INTO candidate_official_answers
                           (import_job_id,source_question_no,candidate_sha256,
                            draft_batch_sha256,content_kind,
                            answer_markdown,analysis_markdown,subquestions_json,
                            source_pages_json,source_page_hashes_json,content_sha256,
                            extraction_artifact_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (job_id, question["source_question_no"], inputs["candidate_sha"],
                         inputs["draft_batch_sha"], question["content_kind"], question["answer_markdown"],
                         question["analysis_markdown"], json.dumps(question["subquestions"], ensure_ascii=False),
                         json.dumps(pages), json.dumps(page_map, sort_keys=True), content_sha, output_sha),
                    )
                cursor = connection.execute(
                    """UPDATE import_answer_extraction_runs SET status='completed',model_run_id=?,
                       raw_artifact_sha256=?,raw_artifact_byte_size=?,output_sha256=?,output_byte_size=?,
                       completed_at=?,updated_at=?,claim_token=NULL,lease_expires_at=NULL
                       WHERE import_job_id=? AND status='processing' AND claim_token=?
                       AND candidate_sha256=? AND draft_batch_sha256=?
                       AND answer_pages_sha256=?""",
                    (run_id, _sha(raw_bytes), len(raw_bytes), output_sha, len(normalized),
                     completed, completed, job_id, claim_token,
                     inputs["candidate_sha"], inputs["draft_batch_sha"],
                     inputs["page_digest"]),
                )
                if cursor.rowcount != 1: raise OfficialAnswerError(SAFE_INPUT)
                connection.commit()
            except Exception:
                connection.rollback(); raise
        return len(parsed["questions"])
    except Exception:
        if parser_rejected and raw is not None and run_id is not None:
            _store_failed_raw_diagnostic(
                Path(database_path), inputs["job_dir"], job_id,
                "extraction", run_id, raw,
            )
        if published:
            for name in ("official_answers_raw.json", "official_answers.json"):
                try: (inputs["job_dir"] / name).unlink()
                except FileNotFoundError: pass
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute(
                "UPDATE import_answer_extraction_runs SET status='failed',error_message=?,updated_at=?,"
                "claim_token=NULL,lease_expires_at=NULL WHERE import_job_id=? AND status='processing' "
                "AND claim_token=?", (SAFE_MODEL, _now(), job_id, claim_token)
            )
        raise
    finally:
        inputs["temporary"].cleanup()


def _load_extraction(job_dir: Path, database_path: Path, job_id: int, inputs):
    try:
        raw = (job_dir / "official_answers.json").read_bytes()
    except OSError as exc:
        raise OfficialAnswerError(SAFE_REVIEW) from exc
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise OfficialAnswerError(SAFE_REVIEW)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT * FROM import_answer_extraction_runs WHERE import_job_id=?", (job_id,)
        ).fetchone()
    if (
        not run or run["status"] != "completed"
        or run["candidate_sha256"] != inputs["candidate_sha"]
        or run["draft_batch_sha256"] != inputs["draft_batch_sha"]
        or run["answer_pages_sha256"] != inputs["page_digest"]
        or _sha(raw) != run["output_sha256"]
    ):
        raise OfficialAnswerError(SAFE_REVIEW)
    page_hashes = {page["page_number"]: page["png_sha256"] for page in inputs["pages"]}
    parsed = parse_answer_extraction_output(
        raw.decode(), job_id, inputs["candidate_sha"], inputs["candidate"], page_hashes,
        inputs["draft_batch_sha"],
    )
    return parsed, _sha(raw), run["model_run_id"]


def claim_answer_review(database_path, private_root, job_id: int) -> str | None:
    database_path, private_root = Path(database_path), Path(private_root)
    inputs = _load_registered_inputs(database_path, private_root, job_id)
    try:
        extraction, extraction_sha, producer_run_id = _load_extraction(
            inputs["job_dir"], database_path, job_id, inputs
        )
        del extraction
        token = secrets.token_hex(32); now = _now()
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,lease_expires_at FROM import_answer_review_runs WHERE import_job_id=?",
                (job_id,),
            ).fetchone()
            if row and (row[0] == "completed" or (
                row[0] == "processing" and isinstance(row[1], str) and row[1] > now
            )):
                connection.rollback(); return None
            values = (inputs["candidate_sha"], inputs["draft_batch_sha"],
                      inputs["page_digest"], extraction_sha,
                      producer_run_id, len(inputs["numbers"]), token, _lease_time(),
                      now, now, job_id)
            if row:
                connection.execute(
                    """UPDATE import_answer_review_runs SET status='processing',
                       candidate_sha256=?,draft_batch_sha256=?,answer_pages_sha256=?,
                       extraction_artifact_sha256=?,
                       producer_model_run_id=?,question_count=?,claim_token=?,lease_expires_at=?,
                       error_message=NULL,started_at=?,updated_at=? WHERE import_job_id=?""", values,
                )
            else:
                connection.execute(
                    """INSERT INTO import_answer_review_runs
                       (candidate_sha256,draft_batch_sha256,answer_pages_sha256,
                        extraction_artifact_sha256,
                        producer_model_run_id,question_count,claim_token,lease_expires_at,
                        started_at,updated_at,import_job_id,status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?, 'processing')""", values,
                )
            connection.commit(); return token
    finally:
        inputs["temporary"].cleanup()


def run_answer_review(
    database_path, private_root, job_id: int, runner=None, *, _claim_token=None,
) -> int:
    claim_token = _claim_token or claim_answer_review(database_path, private_root, job_id)
    if claim_token is None:
        return 0
    database_path, private_root = Path(database_path), Path(private_root)
    inputs = _load_registered_inputs(database_path, private_root, job_id)
    published = False
    raw = None
    reviewer_run_id = None
    parser_rejected = False
    try:
        extraction, extraction_sha, producer_run_id = _load_extraction(
            inputs["job_dir"], database_path, job_id, inputs
        )
        runner = runner or OfficialAnswerCodexRunner()
        review_input = json.dumps(extraction, ensure_ascii=False, separators=(",", ":"))
        prompt = (
            "你是独立官方答案复核者。只比较输入原始答案页、候选题号/小问结构与待审转写；"
            "不得求解题目，不得推断生产者思路。逐题核对题号、最终答案、全部小问、公式和跨页边界。"
            "待审转写的小问标签严格来自当前已审核草稿；若其中有答案与解析均为空、且答案页没有独立作答段的父级条件行，"
            "当前草稿结构中的空父级条件行不算额外小问，也不得因此判为结构不匹配。"
            f"候选SHA={inputs['candidate_sha']}；提取artifact SHA={extraction_sha}；"
            f"当前草稿批次SHA={inputs['draft_batch_sha']}；"
            f"待审转写={review_input}。任何缺题、多题、公式缺失或不确定均 decision=failed。"
            "只输出符合 schema 的单一 JSON。"
        )
        raw, reviewer_run_id = _runner_result(runner.run(
            image_paths=inputs["images"], prompt=prompt, schema=answer_review_schema()
        ))
        if reviewer_run_id == producer_run_id:
            raise OfficialAnswerError(SAFE_REVIEW)
        try:
            parsed = parse_answer_review_output(
                raw, job_id, inputs["candidate_sha"], extraction_sha, extraction,
                inputs["draft_batch_sha"],
            )
        except OfficialAnswerError:
            parser_rejected = True
            raise
        normalized = _canonical_bytes(parsed); raw_bytes = raw.encode(); completed = _now()
        with locked_job(inputs["job_dir"]) as lock:
            _verify_locked_inputs(lock, inputs)
            if _sha((inputs["job_dir"] / "official_answers.json").read_bytes()) != extraction_sha:
                raise OfficialAnswerError(SAFE_REVIEW)
            _publish_pair(inputs["job_dir"], [
                ("official_answer_review_raw.json", raw_bytes),
                ("official_answer_review.json", normalized),
            ])
            published = True
        answers = {q["source_question_no"]: q for q in extraction["questions"]}
        candidates = {
            q["source_question_no"]: q for q in inputs["candidate"]["questions"]
        }
        page_hashes = {str(page["page_number"]): page["png_sha256"] for page in inputs["pages"]}
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON"); connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM candidate_official_answer_reviews WHERE import_job_id=?", (job_id,))
                for review in parsed["questions"]:
                    answer = answers[review["source_question_no"]]
                    effective = json.loads(json.dumps(
                        candidates[review["source_question_no"]], ensure_ascii=False
                    ))
                    effective["answer_markdown"] = answer["answer_markdown"]
                    effective["analysis_markdown"] = answer["analysis_markdown"]
                    for target, official in zip(
                        effective.get("subquestions", []), answer["subquestions"]
                    ):
                        target["answer_markdown"] = official["answer_markdown"]
                        target["analysis_markdown"] = official["analysis_markdown"]
                    hashes = {str(page): page_hashes[str(page)] for page in review["source_pages"]}
                    connection.execute(
                        """INSERT INTO candidate_official_answer_reviews
                           (import_job_id,source_question_no,decision,candidate_sha256,
                            draft_batch_sha256,answer_pages_sha256,
                            extraction_artifact_sha256,
                            answer_content_sha256,answer_analysis_sha256,source_pages_json,
                            source_page_hashes_json,review_evidence_json,reviewed_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (job_id, review["source_question_no"], "passed", inputs["candidate_sha"],
                         inputs["draft_batch_sha"], inputs["page_digest"], extraction_sha,
                         _canonical_sha(answer),
                         _answer_analysis_sha256(effective), json.dumps(review["source_pages"]),
                         json.dumps(hashes, sort_keys=True),
                         json.dumps(review, ensure_ascii=False, sort_keys=True), completed),
                    )
                connection.execute(
                    """UPDATE import_answer_review_runs SET status='completed',reviewer_model_run_id=?,
                       raw_artifact_sha256=?,output_sha256=?,completed_at=?,updated_at=?,
                       claim_token=NULL,lease_expires_at=NULL
                       WHERE import_job_id=? AND status='processing' AND claim_token=?""",
                    (reviewer_run_id, _sha(raw_bytes), _sha(normalized), completed,
                     completed, job_id, claim_token),
                )
                connection.commit()
            except Exception:
                connection.rollback(); raise
        return len(parsed["questions"])
    except Exception:
        if parser_rejected and raw is not None and reviewer_run_id is not None:
            _store_failed_raw_diagnostic(
                database_path, inputs["job_dir"], job_id,
                "review", reviewer_run_id, raw,
            )
        if published:
            for name in ("official_answer_review_raw.json", "official_answer_review.json"):
                try: (inputs["job_dir"] / name).unlink()
                except FileNotFoundError: pass
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute(
                "UPDATE import_answer_review_runs SET status='failed',error_message=?,updated_at=?,"
                "claim_token=NULL,lease_expires_at=NULL WHERE import_job_id=? AND status='processing' "
                "AND claim_token=?", (SAFE_REVIEW, _now(), job_id, claim_token)
            )
        raise
    finally:
        inputs["temporary"].cleanup()


def _answer_fields_equal(left: dict, right: dict) -> bool:
    if (
        left.get("answer_markdown", "") != right.get("answer_markdown", "")
        or left.get("analysis_markdown", "") != right.get("analysis_markdown", "")
    ):
        return False
    left_subs = left.get("subquestions", [])
    right_subs = right.get("subquestions", [])
    if not isinstance(left_subs, list) or not isinstance(right_subs, list):
        return False
    count = max(len(left_subs), len(right_subs))
    for index in range(count):
        old = left_subs[index] if index < len(left_subs) else {}
        new = right_subs[index] if index < len(right_subs) else {}
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        if (
            old.get("answer_markdown", "") != new.get("answer_markdown", "")
            or old.get("analysis_markdown", "") != new.get("analysis_markdown", "")
        ):
            return False
    return True


def _answer_analysis_sha256(question: dict) -> str:
    payload = {
        "source_question_no": question["source_question_no"],
        "answer_markdown": question.get("answer_markdown", ""),
        "analysis_markdown": question.get("analysis_markdown", ""),
        "subquestions": [{
            "label": subquestion.get("label", ""),
            "stem_markdown": subquestion.get("stem_markdown", ""),
            "answer_markdown": subquestion.get("answer_markdown", ""),
            "analysis_markdown": subquestion.get("analysis_markdown", ""),
        } for subquestion in question.get("subquestions", [])],
    }
    return _canonical_sha(payload)


def apply_reviewed_official_answers(database_path, private_root, job_id: int) -> int:
    """CAS-update the authoritative candidate set as one idempotent transaction."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        linked = connection.execute(
            """SELECT 1 FROM import_answer_sources WHERE import_job_id=?
               AND source_answer_state='source_answer_linked' AND applied_at IS NOT NULL""",
            (job_id,),
        ).fetchone()
        draft_rows = list(connection.execute(
            """SELECT source_snapshot_json,edited_json FROM candidate_review_drafts
               WHERE import_job_id=? AND deleted_at IS NULL""", (job_id,)
        ))
    if linked:
        return 0
    try:
        human_answer_edit = any(
            not _answer_fields_equal(
                json.loads(row["source_snapshot_json"]), json.loads(row["edited_json"])
            )
            for row in draft_rows
        )
    except (json.JSONDecodeError, TypeError):
        human_answer_edit = False
    if human_answer_edit:
        raise OfficialAnswerError("检测到人工编辑的答案或解析，整批应用已拒绝")
    inputs = _load_registered_inputs(Path(database_path), Path(private_root), job_id)
    inputs["temporary"].cleanup()
    with _safe_job_lock(inputs["job_dir"]) as lock:
        _verify_locked_inputs(lock, inputs)
        with closing(sqlite3.connect(database_path)) as connection:
            connection.row_factory = sqlite3.Row; connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
                ).fetchone()
                if source["source_answer_state"] == "source_answer_linked" and source["applied_at"]:
                    connection.rollback(); return 0
                extraction = connection.execute(
                    "SELECT * FROM import_answer_extraction_runs WHERE import_job_id=?", (job_id,)
                ).fetchone()
                review_run = connection.execute(
                    "SELECT * FROM import_answer_review_runs WHERE import_job_id=?", (job_id,)
                ).fetchone()
                answers = {row["source_question_no"]: row for row in connection.execute(
                    "SELECT * FROM candidate_official_answers WHERE import_job_id=?", (job_id,)
                )}
                reviews = {row["source_question_no"]: row for row in connection.execute(
                    "SELECT * FROM candidate_official_answer_reviews WHERE import_job_id=?", (job_id,)
                )}
                drafts = {row["source_question_no"]: row for row in connection.execute(
                    "SELECT * FROM candidate_review_drafts WHERE import_job_id=? AND deleted_at IS NULL", (job_id,)
                )}
                expected = set(inputs["numbers"])
                if (
                    not extraction or extraction["status"] != "completed"
                    or not review_run or review_run["status"] != "completed"
                    or extraction["candidate_sha256"] != inputs["candidate_sha"]
                    or extraction["draft_batch_sha256"] != inputs["draft_batch_sha"]
                    or extraction["answer_pages_sha256"] != inputs["page_digest"]
                    or review_run["candidate_sha256"] != inputs["candidate_sha"]
                    or review_run["draft_batch_sha256"] != inputs["draft_batch_sha"]
                    or review_run["answer_pages_sha256"] != inputs["page_digest"]
                    or set(answers) != expected or set(reviews) != expected or set(drafts) != expected
                    or any(review["decision"] != "passed" for review in reviews.values())
                    or any(
                        answer["candidate_sha256"] != inputs["candidate_sha"]
                        or answer["draft_batch_sha256"] != inputs["draft_batch_sha"]
                        or answer["extraction_artifact_sha256"] != extraction["output_sha256"]
                        for answer in answers.values()
                    )
                    or any(
                        review["candidate_sha256"] != inputs["candidate_sha"]
                        or review["draft_batch_sha256"] != inputs["draft_batch_sha"]
                        or review["answer_pages_sha256"] != inputs["page_digest"]
                        or review["extraction_artifact_sha256"] != extraction["output_sha256"]
                        for review in reviews.values()
                    )
                ):
                    raise OfficialAnswerError(SAFE_APPLY)
                if any(row["approval_source"] == "ai_second_pass" for row in drafts.values()):
                    # Reuse the visual-review verifier rather than trusting the
                    # mutable draft row's approval label.
                    _authoritative_input(connection, Path(private_root), job_id)
                classification_run, classification_drafts, classification_evidence = (
                    _classification_generation_rows(connection, job_id)
                )
                if classification_run is not None:
                    _validate_complete_classification_generation(
                        classification_run, classification_drafts,
                        classification_evidence, expected,
                        require_final_evidence=True,
                    )
                    if classification_run.get("applied_at") is None:
                        raise OfficialAnswerError(SAFE_APPLY)
                elif classification_drafts or classification_evidence:
                    raise OfficialAnswerError(SAFE_APPLY)
                classification_drafts_by_number = {
                    row["source_question_no"]: row for row in classification_drafts
                }
                classification_evidence_by_number = {
                    row["source_question_no"]: row for row in classification_evidence
                }
                updates = []
                for number in inputs["numbers"]:
                    draft = drafts[number]
                    try:
                        source_snapshot = json.loads(draft["source_snapshot_json"])
                        edited = json.loads(draft["edited_json"])
                        subanswers = json.loads(answers[number]["subquestions_json"])
                        answer_source_pages = json.loads(answers[number]["source_pages_json"])
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise OfficialAnswerError(SAFE_APPLY) from exc
                    if not _answer_fields_equal(source_snapshot, edited):
                        raise OfficialAnswerError("检测到人工编辑的答案或解析，整批应用已拒绝")
                    try:
                        approval_evidence = json.loads(draft["approval_evidence_json"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise OfficialAnswerError(SAFE_APPLY) from exc
                    if (
                        draft["status"] != "approved"
                        or draft["approval_source"] not in {"human", "ai_second_pass"}
                        or not draft["reviewed_at"]
                        or not isinstance(approval_evidence, dict)
                        or (
                            draft["approval_source"] == "human"
                            and not (
                                set(approval_evidence) == {"method", "reviewed_at"}
                                and approval_evidence.get("method") in {
                                    "workbench", "workbench_quick", "existing_approval",
                                }
                                and approval_evidence.get("reviewed_at") == draft["reviewed_at"]
                            )
                        )
                    ):
                        raise OfficialAnswerError(SAFE_APPLY)
                    if [sub.get("label") for sub in edited.get("subquestions", [])] != [
                        sub.get("label") for sub in subanswers
                    ]:
                        raise OfficialAnswerError(SAFE_APPLY)
                    prior_sha = _canonical_sha(edited)
                    visual_before = visual_question_scope_sha256(edited)
                    classification_before = classification_scope_sha256(edited)
                    classification_draft = classification_drafts_by_number.get(number)
                    classification_final = classification_evidence_by_number.get(number)
                    if classification_run is not None and (
                        classification_draft is None or classification_final is None
                        or classification_draft["approved_draft_version"] != draft["version"]
                        or classification_draft["edited_sha256"] != prior_sha
                        or classification_final["approved_draft_version"] != draft["version"]
                        or classification_final["edited_sha256"] != prior_sha
                        or classification_draft.get("classification_scope_sha256")
                            not in {None, classification_before}
                        or classification_final.get("classification_scope_sha256")
                            not in {None, classification_before}
                    ):
                        raise OfficialAnswerError(SAFE_APPLY)
                    edited["answer_markdown"] = answers[number]["answer_markdown"]
                    edited["analysis_markdown"] = answers[number]["analysis_markdown"]
                    for target, official in zip(edited.get("subquestions", []), subanswers):
                        target["answer_markdown"] = official["answer_markdown"]
                        target["analysis_markdown"] = official["analysis_markdown"]
                    answer_payload = {
                        "source_question_no": number,
                        "content_kind": answers[number]["content_kind"],
                        "answer_markdown": answers[number]["answer_markdown"],
                        "analysis_markdown": answers[number]["analysis_markdown"],
                        "subquestions": subanswers,
                        "source_pages": answer_source_pages,
                    }
                    if (
                        answers[number]["content_sha256"] != _canonical_sha(answer_payload)
                        or reviews[number]["answer_content_sha256"]
                            != answers[number]["content_sha256"]
                        or reviews[number]["answer_analysis_sha256"]
                            != _answer_analysis_sha256(edited)
                        or reviews[number]["source_pages_json"]
                            != answers[number]["source_pages_json"]
                        or reviews[number]["source_page_hashes_json"]
                            != answers[number]["source_page_hashes_json"]
                    ):
                        raise OfficialAnswerError(SAFE_APPLY)
                    edited_json = json.dumps(
                        edited, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    visual_after = visual_question_scope_sha256(edited)
                    classification_after = classification_scope_sha256(edited)
                    if (
                        visual_before != visual_after
                        or classification_before != classification_after
                    ):
                        raise OfficialAnswerError(SAFE_APPLY)
                    updates.append({
                        "number": number, "version": draft["version"],
                        "edited_json": edited_json, "prior_sha": prior_sha,
                        "new_sha": _canonical_sha(edited),
                        "visual_before": visual_before, "visual_after": visual_after,
                        "classification_before": classification_before,
                        "classification_after": classification_after,
                        "approval_source": draft["approval_source"],
                        "approval_evidence_sha": _sha(
                            draft["approval_evidence_json"].encode("utf-8")
                        ),
                        "answer_content_sha": answers[number]["content_sha256"],
                        "answer_analysis_sha": reviews[number]["answer_analysis_sha256"],
                    })
                now = _now()
                if classification_run is not None:
                    connection.execute(
                        """INSERT INTO official_answer_overlay_authorizations
                           (import_job_id,authorization_token,created_at) VALUES(?,?,?)""",
                        (job_id, secrets.token_hex(32), now),
                    )
                for update in updates:
                    cursor = connection.execute(
                        """UPDATE candidate_review_drafts SET edited_json=?,status='approved',
                           version=version+1,updated_at=?
                           WHERE import_job_id=? AND source_question_no=? AND version=?""",
                        (update["edited_json"], now, job_id, update["number"], update["version"]),
                    )
                    if cursor.rowcount != 1:
                        raise OfficialAnswerError("候选草稿版本冲突，整批应用已回滚")
                    connection.execute(
                        """INSERT INTO candidate_official_answer_overlays
                           (import_job_id,source_question_no,prior_draft_version,
                            prior_edited_sha256,new_draft_version,new_edited_sha256,
                            visual_scope_before_sha256,visual_scope_after_sha256,
                            classification_scope_before_sha256,
                            classification_scope_after_sha256,approval_source,
                            approval_evidence_sha256,extraction_artifact_sha256,
                            review_artifact_sha256,answer_content_sha256,
                            answer_analysis_sha256,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (job_id, update["number"], update["version"], update["prior_sha"],
                         update["version"] + 1, update["new_sha"],
                         update["visual_before"], update["visual_after"],
                         update["classification_before"], update["classification_after"],
                         update["approval_source"], update["approval_evidence_sha"],
                         extraction["output_sha256"], review_run["output_sha256"],
                         update["answer_content_sha"], update["answer_analysis_sha"], now),
                    )
                    if classification_run is not None:
                        parameters = (
                            update["version"] + 1, update["new_sha"],
                            update["classification_before"], job_id, update["number"],
                            update["version"], update["prior_sha"],
                        )
                        for table in (
                            "candidate_knowledge_classification_drafts",
                            "candidate_knowledge_classifications",
                        ):
                            rebound = connection.execute(
                                f"""UPDATE {table}
                                    SET approved_draft_version=?,edited_sha256=?,
                                        classification_scope_sha256=?
                                    WHERE import_job_id=? AND source_question_no=?
                                      AND approved_draft_version=? AND edited_sha256=?""",
                                parameters,
                            )
                            if rebound.rowcount != 1:
                                raise OfficialAnswerError(SAFE_APPLY)
                if classification_run is not None:
                    connection.execute(
                        "DELETE FROM official_answer_overlay_authorizations WHERE import_job_id=?",
                        (job_id,),
                    )
                connection.execute(
                    """UPDATE import_answer_sources SET source_answer_state='source_answer_linked',
                       applied_at=?,updated_at=? WHERE import_job_id=?
                       AND source_answer_state='source_has_answer_unprocessed'""",
                    (now, now, job_id),
                )
                _verify_locked_inputs(lock, inputs)
                connection.commit()
                return len(updates)
            except Exception as exc:
                connection.rollback()
                if isinstance(exc, KnowledgeClassificationRunError):
                    raise OfficialAnswerError(SAFE_APPLY) from exc
                raise


def answer_coverage(database_path, job_id: int) -> dict:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute(
            "SELECT * FROM import_answer_sources WHERE import_job_id=?", (job_id,)
        ).fetchone()
        if source is None:
            return {"state": None, "expected": 0, "linked": 0, "unresolved": 0,
                    "label": "原卷答案状态未登记"}
        expected = source["expected_question_count"]
        linked = connection.execute(
            "SELECT COUNT(*) FROM candidate_official_answer_reviews WHERE import_job_id=? AND decision='passed'",
            (job_id,),
        ).fetchone()[0]
        labels = {
            "source_has_no_answer": "原卷未提供答案",
            "source_has_answer_unprocessed": "原卷答案待处理",
            "source_answer_linked": "原卷答案已审核",
        }
        return {"state": source["source_answer_state"], "expected": expected,
                "linked": linked, "unresolved": max(0, expected - linked),
                "label": labels[source["source_answer_state"]]}
