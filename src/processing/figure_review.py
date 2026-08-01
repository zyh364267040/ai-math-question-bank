"""Signed independent review evidence for formal question figures."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    canonical_payload,
    load_hmac_key,
    locked_job,
    open_directory_at,
    read_file_at,
    sign_manifest,
    write_file_at,
)


SAFE_FIGURE_ERROR = "配图独立审核证据无效"


class FigureReviewError(ValueError):
    """A figure is not bound to valid independent review evidence."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def validate_figure_manifest(data, key, job_id):
    if (
        not isinstance(data, dict) or set(data) != {
            "version", "import_job_id", "generation_id", "assets", "signature",
        }
        or data["version"] != 2 or data["import_job_id"] != job_id
        or not isinstance(data["generation_id"], str) or len(data["generation_id"]) != 32
        or not isinstance(data["assets"], list)
    ):
        raise FigureReviewError(SAFE_FIGURE_ERROR)
    expected = hmac.new(key, canonical_payload(data), hashlib.sha256).hexdigest()
    if not isinstance(data["signature"], str) or not hmac.compare_digest(data["signature"], expected):
        raise FigureReviewError(SAFE_FIGURE_ERROR)
    return data


def _load_png(job_fd, relative, expected_sha):
    snapshot = read_file_at(job_fd, relative, max_bytes=64 * 1024 * 1024)
    if snapshot.sha256 != expected_sha:
        raise FigureReviewError(SAFE_FIGURE_ERROR)
    try:
        with Image.open(io.BytesIO(snapshot.data)) as opened:
            opened.load()
            if opened.format != "PNG":
                raise FigureReviewError(SAFE_FIGURE_ERROR)
            return snapshot, opened.convert("RGB").copy()
    except (OSError, UnidentifiedImageError) as error:
        raise FigureReviewError(SAFE_FIGURE_ERROR) from error


def _preview(source, asset, bbox):
    marked = source.copy()
    ImageDraw.Draw(marked).rectangle(tuple(bbox), outline="red", width=3)
    max_width = 800
    if marked.width > max_width:
        ratio = max_width / marked.width
        marked = marked.resize((max_width, max(1, round(marked.height * ratio))))
    canvas = Image.new("RGB", (marked.width + asset.width + 8, max(marked.height, asset.height)), "white")
    canvas.paste(marked, (0, 0))
    canvas.paste(asset, (marked.width + 8, 0))
    stream = io.BytesIO()
    canvas.save(stream, "PNG", optimize=False)
    return stream.getvalue()


def _candidate_for(snapshot, question_no):
    data = json.loads(snapshot.data)
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        raise FigureReviewError(SAFE_FIGURE_ERROR)
    candidate = next(
        (item for item in questions if str(item.get("source_question_no")) == str(question_no)),
        None,
    )
    if candidate is None or candidate.get("figure_required") is not True:
        raise FigureReviewError(SAFE_FIGURE_ERROR)
    return candidate


def _subject(asset, generation, candidate_sha):
    return {
        "generation_id": generation, "candidate_sha256": candidate_sha,
        "question_no": str(asset["question_no"]), "asset_sha256": asset["sha256"],
        "source_relative_path": asset["source_relative_path"],
        "source_sha256": asset["source_page_sha256"],
        "bbox": asset["crop_box_pixels"], "output_relative_path": asset["output_relative_path"],
    }


def record_figure_review(database_path, private_root, payload):
    keys = {
        "version", "import_job_id", "input_generation_id", "question_no",
        "output_relative_path", "reviewer", "decision",
    }
    if (
        not isinstance(payload, dict) or set(payload) != keys or payload["version"] != 1
        or type(payload["import_job_id"]) is not int
        or str(payload["question_no"]) == "" or not str(payload["question_no"]).isdigit()
        or not isinstance(payload["input_generation_id"], str)
        or not isinstance(payload["output_relative_path"], str)
        or not isinstance(payload["reviewer"], str) or not payload["reviewer"].strip()
        or payload["decision"] not in {"approved", "rejected"}
    ):
        raise FigureReviewError(SAFE_FIGURE_ERROR)
    job_id = payload["import_job_id"]
    job_dir = Path(private_root) / "processing" / f"import_job_{job_id}"
    try:
        with locked_job(job_dir) as lock:
            manifest_snapshot = read_file_at(lock.descriptor, "figure_assets.json", max_bytes=8 * 1024 * 1024)
            manifest = validate_figure_manifest(
                json.loads(manifest_snapshot.data), load_hmac_key(lock.path), job_id,
            )
            if manifest["generation_id"] != payload["input_generation_id"]:
                raise FigureReviewError(SAFE_FIGURE_ERROR)
            asset = next((item for item in manifest["assets"] if (
                item.get("kind") == "question_figure"
                and str(item.get("question_no")) == str(payload["question_no"])
                and item.get("output_relative_path") == payload["output_relative_path"]
            )), None)
            if asset is None or asset.get("review_status") != "pending_ai_review":
                raise FigureReviewError(SAFE_FIGURE_ERROR)
            candidate_snapshot = read_file_at(
                lock.descriptor, "candidate_questions.json", max_bytes=16 * 1024 * 1024,
            )
            _candidate_for(candidate_snapshot, payload["question_no"])
            asset_snapshot, asset_image = _load_png(
                lock.descriptor, asset["output_relative_path"], asset["sha256"],
            )
            source_snapshot, source_image = _load_png(
                lock.descriptor, asset["source_relative_path"], asset["source_page_sha256"],
            )
            try:
                preview_bytes = _preview(source_image, asset_image, asset["crop_box_pixels"])
            finally:
                source_image.close()
                asset_image.close()
            subject = _subject(asset, manifest["generation_id"], candidate_snapshot.sha256)
            subject_digest = _digest(subject)
            evidence = sign_manifest(load_hmac_key(lock.path), {
                "version": 1, "kind": "figure", "import_job_id": job_id,
                "subject": subject, "subject_digest": subject_digest,
                "preview_sha256": hashlib.sha256(preview_bytes).hexdigest(),
                "reviewer": payload["reviewer"].strip(), "decision": payload["decision"],
                "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            evidence_bytes = _json_bytes(evidence)
            evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
            try:
                os.mkdir("figure_review", 0o700, dir_fd=lock.descriptor)
            except FileExistsError:
                pass
            review_fd = open_directory_at(lock.descriptor, "figure_review")
            try:
                for name, content in (
                    (f"preview_{subject_digest}.png", preview_bytes),
                    (f"evidence_{subject_digest}.json", evidence_bytes),
                ):
                    try:
                        write_file_at(review_fd, name, content)
                    except FileExistsError:
                        if read_file_at(review_fd, name, max_bytes=16 * 1024 * 1024).data != content:
                            raise FigureReviewError(SAFE_FIGURE_ERROR)
            finally:
                os.close(review_fd)
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """INSERT INTO import_crop_security_reviews
                       (import_job_id,evidence_kind,question_no,generation_id,subject_digest,
                        source_sha256,artifact_sha256,preview_sha256,evidence_sha256,
                        evidence_signature,reviewer,decision,reason,bbox_json,reviewed_at)
                       VALUES (?,'figure',?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(import_job_id,evidence_kind,question_no,generation_id,subject_digest)
                       DO UPDATE SET source_sha256=excluded.source_sha256,
                         artifact_sha256=excluded.artifact_sha256,preview_sha256=excluded.preview_sha256,
                         evidence_sha256=excluded.evidence_sha256,evidence_signature=excluded.evidence_signature,
                         reviewer=excluded.reviewer,decision=excluded.decision,bbox_json=excluded.bbox_json,
                         reviewed_at=excluded.reviewed_at""",
                    (job_id, int(asset["question_no"]), manifest["generation_id"], subject_digest,
                     source_snapshot.sha256, asset_snapshot.sha256, evidence["preview_sha256"],
                     evidence_sha, evidence["signature"], evidence["reviewer"], evidence["decision"],
                     None, json.dumps(asset["crop_box_pixels"]), evidence["reviewed_at"]),
                )
                connection.commit()
            return evidence
    except FigureReviewError:
        raise
    except (OSError, sqlite3.Error, SecureCropArtifactError, KeyError, ValueError) as error:
        raise FigureReviewError(SAFE_FIGURE_ERROR) from error


def validate_figure_evidence(connection, job_fd, job_dir, job_id, manifest, candidate_sha):
    key = load_hmac_key(Path(job_dir))
    validate_figure_manifest(manifest, key, job_id)
    for asset in manifest["assets"]:
        if asset.get("kind") != "question_figure":
            continue
        subject = _subject(asset, manifest["generation_id"], candidate_sha)
        subject_digest = _digest(subject)
        row = connection.execute(
            """SELECT source_sha256,artifact_sha256,preview_sha256,evidence_sha256,
                      evidence_signature,reviewer,decision,bbox_json
               FROM import_crop_security_reviews WHERE import_job_id=?
                 AND evidence_kind='figure' AND question_no=? AND generation_id=?
                 AND subject_digest=?""",
            (job_id, int(asset["question_no"]), manifest["generation_id"], subject_digest),
        ).fetchone()
        if row is None or row[6] != "approved" or json.loads(row[7]) != asset["crop_box_pixels"]:
            raise FigureReviewError(SAFE_FIGURE_ERROR)
        source_snapshot, source_image = _load_png(
            job_fd, asset["source_relative_path"], asset["source_page_sha256"],
        )
        asset_snapshot, asset_image = _load_png(job_fd, asset["output_relative_path"], asset["sha256"])
        try:
            preview_bytes = _preview(source_image, asset_image, asset["crop_box_pixels"])
        finally:
            source_image.close()
            asset_image.close()
        review_fd = open_directory_at(job_fd, "figure_review")
        try:
            evidence_snapshot = read_file_at(review_fd, f"evidence_{subject_digest}.json", max_bytes=128 * 1024)
            preview_snapshot = read_file_at(review_fd, f"preview_{subject_digest}.png", max_bytes=16 * 1024 * 1024)
        finally:
            os.close(review_fd)
        evidence = json.loads(evidence_snapshot.data)
        signature = hmac.new(key, canonical_payload(evidence), hashlib.sha256).hexdigest()
        if (
            source_snapshot.sha256 != row[0] or asset_snapshot.sha256 != row[1]
            or preview_snapshot.sha256 != row[2]
            or hashlib.sha256(preview_bytes).hexdigest() != row[2]
            or evidence_snapshot.sha256 != row[3] or evidence.get("signature") != row[4]
            or not hmac.compare_digest(signature, row[4]) or evidence.get("subject") != subject
            or evidence.get("reviewer") != row[5] or evidence.get("decision") != "approved"
        ):
            raise FigureReviewError(SAFE_FIGURE_ERROR)
