"""Independent, signed authorization and application of proposed crop masks."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, UnidentifiedImageError

from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    canonical_payload,
    load_hmac_key,
    locked_job,
    open_directory_at,
    read_file_at,
    sign_manifest,
    validate_signed_manifest,
    write_file_at,
)


SAFE_MASK_ERROR = "遮罩独立审核证据无效"
CONTROLLED_REASONS = {"qr_code", "promotion_overlay"}
MAX_APPROVED_COVERAGE = 0.12
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_reason(value):
    if not isinstance(value, str):
        raise MaskReviewError(SAFE_MASK_ERROR)
    stripped = value.strip()
    for reason in CONTROLLED_REASONS:
        if stripped == reason or stripped.startswith(reason + ":") or stripped.startswith(reason + "："):
            return reason
    raise MaskReviewError(SAFE_MASK_ERROR)


class MaskReviewError(ValueError):
    """A mask proposal is not independently authorized for application."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _png_bytes(image):
    stream = io.BytesIO()
    image.save(stream, "PNG", optimize=False)
    return stream.getvalue()


def _current(database_path, job_id):
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            """SELECT status,question_count,crop_manifest_sha256,crop_generation_id,
                      crop_manifest_signature FROM import_question_split_runs
               WHERE import_job_id=?""", (job_id,),
        ).fetchone()


def _load_manifest(lock, database_path, job_id):
    row = _current(database_path, job_id)
    if row is None or row[0] != "completed" or any(value is None for value in row[1:]):
        raise MaskReviewError(SAFE_MASK_ERROR)
    snapshot = read_file_at(lock.descriptor, "question_crops.json", max_bytes=8 * 1024 * 1024)
    manifest = validate_signed_manifest(
        json.loads(snapshot.data), load_hmac_key(lock.path), expected_job_id=job_id,
        expected_question_nos=list(range(1, row[1] + 1)),
    )
    if (snapshot.sha256, manifest["generation_id"], manifest["signature"]) != tuple(row[2:]):
        raise MaskReviewError(SAFE_MASK_ERROR)
    return manifest, snapshot


def _page_images(lock, manifest):
    images = {}
    for page in manifest["source_pages"]:
        snapshot = read_file_at(lock.descriptor, page["relative_path"], max_bytes=64 * 1024 * 1024)
        if snapshot.sha256 != page["sha256"]:
            raise MaskReviewError(SAFE_MASK_ERROR)
        try:
            with Image.open(io.BytesIO(snapshot.data)) as opened:
                opened.load()
                if opened.format != "PNG" or opened.size != (page["pixel_width"], page["pixel_height"]):
                    raise MaskReviewError(SAFE_MASK_ERROR)
                images[page["page_number"]] = opened.convert("RGB").copy()
        except (OSError, UnidentifiedImageError) as error:
            raise MaskReviewError(SAFE_MASK_ERROR) from error
    return images


def _render(entry, images, masks=()):
    pieces = [images[region["page_number"]].crop(region["bbox"]) for region in entry["regions"]]
    if len(pieces) == 1:
        result = pieces[0]
        separator = 0
    else:
        separator = entry["composition"]["separator_height"]
        result = Image.new("RGB", (entry["width"], entry["height"]), "white")
        offset = 0
        for piece in pieces:
            result.paste(piece, (0, offset))
            offset += piece.height + separator
    offsets = []
    offset = 0
    for region in entry["regions"]:
        offsets.append(offset)
        offset += region["bbox"][3] - region["bbox"][1] + separator
    for mask in masks:
        matches = [
            (index, region) for index, region in enumerate(entry["regions"])
            if region["page_number"] == mask["page_number"]
            and region["bbox"][0] <= mask["bbox"][0]
            and region["bbox"][1] <= mask["bbox"][1]
            and mask["bbox"][2] <= region["bbox"][2]
            and mask["bbox"][3] <= region["bbox"][3]
        ]
        if len(matches) != 1:
            raise MaskReviewError(SAFE_MASK_ERROR)
        index, region = matches[0]
        left, top, right, bottom = mask["bbox"]
        result.paste("white", (
            left - region["bbox"][0], offsets[index] + top - region["bbox"][1],
            right - region["bbox"][0], offsets[index] + bottom - region["bbox"][1],
        ))
    return result


def _preview(original, masked):
    preview = Image.new("RGB", (original.width * 2 + 8, original.height), "#808080")
    preview.paste(original, (0, 0))
    preview.paste(masked, (original.width + 8, 0))
    return _png_bytes(preview)


def _subject(entry, mask, reason):
    return {
        "question_no": entry["question_no"], "regions": entry["regions"],
        "page_number": mask["page_number"], "bbox": mask["bbox"], "reason": reason,
    }


def _validate_request(payload):
    keys = {
        "version", "import_job_id", "input_generation_id", "question_no",
        "page_number", "bbox", "reason", "reviewer", "decision",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise MaskReviewError(SAFE_MASK_ERROR)
    if (
        payload["version"] != 1 or type(payload["import_job_id"]) is not int
        or type(payload["question_no"]) is not int or type(payload["page_number"]) is not int
        or not isinstance(payload["input_generation_id"], str)
        or not isinstance(payload["bbox"], list) or len(payload["bbox"]) != 4
        or not all(type(value) is int for value in payload["bbox"])
        or payload["reason"] not in CONTROLLED_REASONS
        or payload["decision"] not in {"approved", "rejected"}
        or not isinstance(payload["reviewer"], str) or not 1 <= len(payload["reviewer"].strip()) <= 100
    ):
        raise MaskReviewError(SAFE_MASK_ERROR)
    return payload


def record_mask_review(database_path, private_root, payload):
    """Create independent evidence; callers cannot directly set crop review status."""
    payload = _validate_request(payload)
    job_id = payload["import_job_id"]
    job_dir = Path(private_root) / "processing" / f"import_job_{job_id}"
    try:
        with locked_job(job_dir) as lock:
            manifest, _ = _load_manifest(lock, database_path, job_id)
            if manifest["generation_id"] != payload["input_generation_id"]:
                raise MaskReviewError(SAFE_MASK_ERROR)
            entry = next(
                (item for item in manifest["questions"] if item["question_no"] == payload["question_no"]),
                None,
            )
            proposal = {
                "page_number": payload["page_number"], "bbox": payload["bbox"],
                "reason": payload["reason"],
            }
            if entry is None:
                raise MaskReviewError(SAFE_MASK_ERROR)
            matching_proposals = [
                item for item in entry.get("mask_regions", [])
                if item.get("page_number") == payload["page_number"]
                and item.get("bbox") == payload["bbox"]
                and _canonical_reason(item.get("reason")) == payload["reason"]
            ]
            if len(matching_proposals) != 1:
                raise MaskReviewError(SAFE_MASK_ERROR)
            region = next(
                (item for item in entry["regions"] if item["page_number"] == payload["page_number"]
                 and item["bbox"][0] <= payload["bbox"][0]
                 and item["bbox"][1] <= payload["bbox"][1]
                 and payload["bbox"][2] <= item["bbox"][2]
                 and payload["bbox"][3] <= item["bbox"][3]), None,
            )
            if region is None:
                raise MaskReviewError(SAFE_MASK_ERROR)
            mask_area = (payload["bbox"][2] - payload["bbox"][0]) * (payload["bbox"][3] - payload["bbox"][1])
            region_area = (region["bbox"][2] - region["bbox"][0]) * (region["bbox"][3] - region["bbox"][1])
            if payload["decision"] == "approved" and mask_area / region_area > MAX_APPROVED_COVERAGE:
                raise MaskReviewError(SAFE_MASK_ERROR)
            images = _page_images(lock, manifest)
            try:
                original = _render(entry, images)
                masked = _render(entry, images, [proposal])
                original_bytes = _png_bytes(original)
                preview_bytes = _preview(original, masked)
            finally:
                for image in images.values():
                    image.close()
            subject = _subject(entry, proposal, payload["reason"])
            subject_digest = _digest(subject)
            source_sha = next(
                page["sha256"] for page in manifest["source_pages"]
                if page["page_number"] == payload["page_number"]
            )
            evidence = sign_manifest(load_hmac_key(lock.path), {
                "version": 1, "kind": "mask", "import_job_id": job_id,
                "generation_id": manifest["generation_id"], "subject": subject,
                "subject_digest": subject_digest, "source_sha256": source_sha,
                "artifact_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "preview_sha256": hashlib.sha256(preview_bytes).hexdigest(),
                "reviewer": payload["reviewer"].strip(), "decision": payload["decision"],
                "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            evidence_bytes = _json_bytes(evidence)
            evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
            try:
                os.mkdir("mask_review", 0o700, dir_fd=lock.descriptor)
            except FileExistsError:
                pass
            review_fd = open_directory_at(lock.descriptor, "mask_review")
            try:
                preview_name = f"preview_{manifest['generation_id']}_{subject_digest}.png"
                evidence_name = f"evidence_{manifest['generation_id']}_{subject_digest}.json"
                for name, content in ((preview_name, preview_bytes), (evidence_name, evidence_bytes)):
                    try:
                        write_file_at(review_fd, name, content)
                    except FileExistsError:
                        if read_file_at(review_fd, name, max_bytes=16 * 1024 * 1024).data != content:
                            raise MaskReviewError(SAFE_MASK_ERROR)
            finally:
                os.close(review_fd)
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """INSERT INTO import_crop_security_reviews
                       (import_job_id,evidence_kind,question_no,generation_id,subject_digest,
                        source_sha256,artifact_sha256,preview_sha256,evidence_sha256,
                        evidence_signature,reviewer,decision,reason,bbox_json,reviewed_at)
                       VALUES (?,'mask',?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(import_job_id,evidence_kind,question_no,generation_id,subject_digest)
                       DO UPDATE SET source_sha256=excluded.source_sha256,
                         artifact_sha256=excluded.artifact_sha256,preview_sha256=excluded.preview_sha256,
                         evidence_sha256=excluded.evidence_sha256,evidence_signature=excluded.evidence_signature,
                         reviewer=excluded.reviewer,decision=excluded.decision,reason=excluded.reason,
                         bbox_json=excluded.bbox_json,reviewed_at=excluded.reviewed_at""",
                    (job_id, entry["question_no"], manifest["generation_id"], subject_digest,
                     source_sha, evidence["artifact_sha256"], evidence["preview_sha256"],
                     evidence_sha, evidence["signature"], evidence["reviewer"],
                     evidence["decision"], payload["reason"], json.dumps(payload["bbox"]),
                     evidence["reviewed_at"]),
                )
                connection.commit()
            return evidence
    except MaskReviewError:
        raise
    except (OSError, sqlite3.Error, SecureCropArtifactError, KeyError, ValueError) as error:
        raise MaskReviewError(SAFE_MASK_ERROR) from error


def _approved_evidence(connection, lock, manifest, entry, mask):
    canonical_mask = {**mask, "reason": _canonical_reason(mask.get("reason"))}
    subject = _subject(entry, canonical_mask, canonical_mask["reason"])
    subject_digest = _digest(subject)
    row = connection.execute(
        """SELECT source_sha256,artifact_sha256,preview_sha256,evidence_sha256,
                  evidence_signature,reviewer,decision,reason,bbox_json
           FROM import_crop_security_reviews
           WHERE import_job_id=? AND evidence_kind='mask' AND question_no=?
             AND generation_id=? AND subject_digest=?""",
        (manifest["import_job_id"], entry["question_no"], manifest["generation_id"], subject_digest),
    ).fetchone()
    if (row is None or row[6] != "approved" or row[7] != canonical_mask["reason"]
            or json.loads(row[8]) != canonical_mask["bbox"]):
        raise MaskReviewError(SAFE_MASK_ERROR)
    review_fd = open_directory_at(lock.descriptor, "mask_review")
    try:
        evidence_snapshot = read_file_at(
            review_fd, f"evidence_{manifest['generation_id']}_{subject_digest}.json",
            max_bytes=128 * 1024,
        )
        preview_snapshot = read_file_at(
            review_fd, f"preview_{manifest['generation_id']}_{subject_digest}.png",
            max_bytes=16 * 1024 * 1024,
        )
    finally:
        os.close(review_fd)
    evidence = json.loads(evidence_snapshot.data)
    expected_signature = hmac.new(load_hmac_key(lock.path), canonical_payload(evidence), hashlib.sha256).hexdigest()
    if (
        evidence_snapshot.sha256 != row[3] or preview_snapshot.sha256 != row[2]
        or evidence.get("signature") != row[4]
        or not hmac.compare_digest(evidence.get("signature", ""), expected_signature)
        or evidence.get("subject") != subject or evidence.get("subject_digest") != subject_digest
        or evidence.get("generation_id") != manifest["generation_id"]
        or evidence.get("source_sha256") != row[0] or evidence.get("artifact_sha256") != row[1]
        or evidence.get("preview_sha256") != row[2] or evidence.get("reviewer") != row[5]
        or evidence.get("decision") != "approved"
    ):
        raise MaskReviewError(SAFE_MASK_ERROR)
    return row, evidence, preview_snapshot


def apply_approved_masks(database_path, private_root, job_id):
    """Apply only a complete set of current-generation, DB-anchored approvals."""
    job_dir = Path(private_root) / "processing" / f"import_job_{job_id}"
    try:
        with locked_job(job_dir) as lock:
            manifest, snapshot = _load_manifest(lock, database_path, job_id)
            images = _page_images(lock, manifest)
            updated = json.loads(json.dumps(manifest))
            outputs = {}
            with closing(sqlite3.connect(database_path)) as evidence_db:
                for entry in updated["questions"]:
                    masks = entry.get("mask_regions", [])
                    if not masks:
                        continue
                    base = _render(entry, images)
                    base_bytes = _png_bytes(base)
                    evidence_rows = []
                    for mask in masks:
                        row, evidence, preview = _approved_evidence(
                            evidence_db, lock, manifest, entry, mask,
                        )
                        single = _render(entry, images, [mask])
                        if (
                            hashlib.sha256(base_bytes).hexdigest() != row[1]
                            or hashlib.sha256(_preview(base, single)).hexdigest() != row[2]
                        ):
                            raise MaskReviewError(SAFE_MASK_ERROR)
                        evidence_rows.append(row)
                    result = _render(entry, images, masks)
                    data = _png_bytes(result)
                    entry["sha256"] = hashlib.sha256(data).hexdigest()
                    entry["byte_size"] = len(data)
                    outputs[entry["question_no"]] = data
            for image in images.values():
                image.close()
            updated = sign_manifest(load_hmac_key(lock.path), {
                key: value for key, value in updated.items() if key != "signature"
            })
            manifest_bytes = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
            crops_fd = open_directory_at(lock.descriptor, "question_crops")
            try:
                for number, data in outputs.items():
                    temp = f".Q{number:03d}.mask.tmp"
                    write_file_at(crops_fd, temp, data)
                    os.replace(temp, f"Q{number:03d}.png", src_dir_fd=crops_fd, dst_dir_fd=crops_fd)
            finally:
                os.close(crops_fd)
            write_file_at(lock.descriptor, ".question_crops.mask.tmp", manifest_bytes)
            os.replace(
                ".question_crops.mask.tmp", "question_crops.json",
                src_dir_fd=lock.descriptor, dst_dir_fd=lock.descriptor,
            )
            digest = hashlib.sha256(manifest_bytes).hexdigest()
            with closing(sqlite3.connect(database_path)) as connection:
                cursor = connection.execute(
                    """UPDATE import_question_split_runs
                       SET crop_manifest_sha256=?,crop_manifest_signature=?,updated_at=CURRENT_TIMESTAMP
                       WHERE import_job_id=? AND status='completed' AND crop_manifest_sha256=?
                         AND crop_generation_id=? AND crop_manifest_signature=?""",
                    (digest, updated["signature"], job_id, snapshot.sha256,
                     manifest["generation_id"], manifest["signature"]),
                )
                if cursor.rowcount != 1:
                    raise MaskReviewError(SAFE_MASK_ERROR)
                connection.commit()
            return updated
    except MaskReviewError:
        raise
    except (OSError, sqlite3.Error, SecureCropArtifactError, KeyError, ValueError) as error:
        raise MaskReviewError(SAFE_MASK_ERROR) from error


def validate_applied_masks(connection, job_fd, job_dir, manifest):
    """Admission gate for every mask-bearing formal crop."""
    lock = SimpleNamespace(descriptor=job_fd, path=Path(job_dir))
    try:
        images = _page_images(lock, manifest)
        page_hashes = {
            page["page_number"]: page["sha256"] for page in manifest["source_pages"]
        }
        for entry in manifest["questions"]:
            masks = entry.get("mask_regions", [])
            if not masks:
                continue
            base = _render(entry, images)
            base_bytes = _png_bytes(base)
            for mask in masks:
                try:
                    row, _, _ = _approved_evidence(
                        connection, lock, manifest, entry, mask,
                    )
                except MaskReviewError as current_error:
                    entry_digest = hashlib.sha256(json.dumps(
                        entry, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode()).hexdigest()
                    generations = connection.execute(
                        """SELECT crop_generation_id FROM import_frozen_crop_reviews
                           WHERE import_job_id=? AND question_no=? AND crop_sha256=?
                             AND manifest_entry_sha256=? ORDER BY frozen_at DESC""",
                        (manifest["import_job_id"], entry["question_no"],
                         entry["sha256"], entry_digest),
                    ).fetchall()
                    row = None
                    for generation_row in generations:
                        frozen_manifest = dict(manifest)
                        frozen_manifest["generation_id"] = generation_row[0]
                        try:
                            row, _, _ = _approved_evidence(
                                connection, lock, frozen_manifest, entry, mask,
                            )
                            break
                        except MaskReviewError:
                            continue
                    if row is None:
                        raise current_error
                single = _render(entry, images, [mask])
                if (
                    row[0] != page_hashes[mask["page_number"]]
                    or row[1] != hashlib.sha256(base_bytes).hexdigest()
                    or row[2] != hashlib.sha256(_preview(base, single)).hexdigest()
                ):
                    raise MaskReviewError(SAFE_MASK_ERROR)
            expected = _png_bytes(_render(entry, images, masks))
            if (
                entry["sha256"] != hashlib.sha256(expected).hexdigest()
                or entry["byte_size"] != len(expected)
            ):
                raise MaskReviewError(SAFE_MASK_ERROR)
        for image in images.values():
            image.close()
    except MaskReviewError:
        raise
    except (OSError, sqlite3.Error, SecureCropArtifactError, KeyError, ValueError) as error:
        raise MaskReviewError(SAFE_MASK_ERROR) from error
