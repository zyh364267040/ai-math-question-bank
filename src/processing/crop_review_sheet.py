"""Signed-input, journaled generation of fixed four-question review sheets."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import stat
import re
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import Any

from PIL import Image, ImageDraw, UnidentifiedImageError

from src.processing.question_crop import QuestionCropError, _exists_at, _remove_at
from src.processing.secure_crop_artifacts import (
    MAX_MANIFEST_BYTES,
    PinnedBytes,
    SecureCropArtifactError,
    canonical_payload,
    fsync_directory,
    load_hmac_key,
    locked_job,
    open_directory_at,
    read_file_at,
    sign_manifest,
    validate_signed_manifest,
    write_file_at,
)


GROUP_SIZE = 4
SHEET_DIRECTORY = "review"
JOURNAL_FILENAME = ".crop_review_sheet_journal.json"
MAX_CANVAS_DIMENSION = 16_000
MAX_CANVAS_PIXELS = 48_000_000
MAX_INPUT_PIXELS = 100_000_000
MAX_INPUT_BYTES = 500 * 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 100 * 1024 * 1024
MIN_FREE_DISK_BYTES = 64 * 1024 * 1024
JOURNAL_KEYS = {
    "generation_id", "source_generation_id", "source_manifest_signature",
    "stage_directory", "backup_directory", "items", "signature",
}
JOURNAL_ITEM_KEYS = {"name", "new_sha256", "new_size", "old_present", "old_sha256", "old_size"}
SHEET_NAME_PATTERN = re.compile(r"crops_(\d+)_([0-9]+)\.jpg\Z")


class CropReviewSheetError(ValueError):
    """Review sheet inputs or journaled publication are unsafe."""


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CropReviewSheetError(f"{label}必须是正整数")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CropReviewSheetError(f"{label}必须是非负整数")
    return value


def _sheet_name(group_start: int) -> str:
    return f"crops_{group_start:02d}_{group_start + GROUP_SIZE - 1:02d}.jpg"


def _sheet_start(name: str) -> int:
    match = SHEET_NAME_PATTERN.fullmatch(name)
    if match is None:
        raise CropReviewSheetError("联系表文件名无效")
    start, end = (int(value) for value in match.groups())
    if start < 1 or end != start + GROUP_SIZE - 1:
        raise CropReviewSheetError("联系表固定分组文件名无效")
    return start


def _safe_transaction_directory(value: Any, prefix: str) -> bool:
    return (isinstance(value, str) and value.startswith(prefix)
            and PurePosixPath(value).name == value and "/" not in value and "\\" not in value)


def _open_review(job_fd: int, *, create: bool) -> int:
    try:
        info = os.stat(SHEET_DIRECTORY, dir_fd=job_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("unsafe review directory")
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(SHEET_DIRECTORY, 0o700, dir_fd=job_fd)
        fsync_directory(job_fd)
    return open_directory_at(job_fd, SHEET_DIRECTORY)


def _replace_at(source_fd: int, source: str, target_fd: int, target: str) -> None:
    os.replace(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd)
    fsync_directory(source_fd)
    if target_fd != source_fd:
        fsync_directory(target_fd)


def _read_manifest(job_fd: int, key: bytes) -> dict[str, Any]:
    try:
        raw = read_file_at(job_fd, "question_crops.json", max_bytes=MAX_MANIFEST_BYTES)
        data = json.loads(raw.data.decode("utf-8"))
        return validate_signed_manifest(data, key)
    except SecureCropArtifactError as error:
        raise CropReviewSheetError(str(error)) from error
    except (UnicodeError, json.JSONDecodeError, OSError) as error:
        raise CropReviewSheetError("question_crops manifest损坏或完整性无效") from error


def _verify_input_png(artifact: PinnedBytes, entry: dict[str, Any]) -> None:
    if artifact.size != entry["byte_size"] or artifact.sha256 != entry["sha256"]:
        raise CropReviewSheetError("联系表输入PNG大小或哈希无效")
    try:
        with Image.open(io.BytesIO(artifact.data)) as image:
            image.load()
            if image.format != "PNG" or image.size != (entry["width"], entry["height"]):
                raise CropReviewSheetError("联系表输入PNG格式或尺寸无效")
    except CropReviewSheetError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise CropReviewSheetError("联系表输入PNG损坏") from error


def _build_sheet(items: list[tuple[int, PinnedBytes, dict[str, Any]]],
                 max_canvas_width: int, max_canvas_height: int,
                 jpeg_quality: int) -> bytes:
    margin = 24
    label_height = 30
    gap = 18
    available_width = max_canvas_width - 2 * margin
    layouts = []
    total_height = margin
    widest = 0
    for number, artifact, entry in items:
        width, height = entry["width"], entry["height"]
        scale = min(1.0, available_width / width)
        rendered = (max(1, round(width * scale)), max(1, round(height * scale)))
        layouts.append((number, artifact, rendered))
        widest = max(widest, rendered[0])
        total_height += label_height + rendered[1] + gap
    total_height += margin - gap
    canvas_width = max(320, widest + 2 * margin)
    if (canvas_width > max_canvas_width or total_height > max_canvas_height
            or canvas_width * total_height > MAX_CANVAS_PIXELS):
        raise CropReviewSheetError("联系表画布尺寸超出预算")
    canvas = Image.new("RGB", (canvas_width, total_height), "white")
    draw = ImageDraw.Draw(canvas)
    y = margin
    for number, artifact, rendered in layouts:
        draw.rectangle((margin, y, canvas_width - margin, y + label_height - 6), fill="#18324a")
        draw.text((margin + 10, y + 5), f"Question {number} / Q{number:03d}", fill="white")
        y += label_height
        with Image.open(io.BytesIO(artifact.data)) as image:
            image.load()
            rgb = image.convert("RGB")
            if rgb.size != rendered:
                rgb = rgb.resize(rendered, Image.Resampling.LANCZOS)
            canvas.paste(rgb, (margin, y))
        y += rendered[1] + gap
    output = io.BytesIO()
    try:
        canvas.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
        data = output.getvalue()
        with Image.open(io.BytesIO(data)) as check:
            check.load()
            if check.format != "JPEG" or check.size != canvas.size:
                raise CropReviewSheetError("生成联系表验证失败")
        return data
    except CropReviewSheetError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise CropReviewSheetError("无法写入或验证联系表") from error


def _validate_journal(data: Any, key: bytes) -> dict[str, Any]:
    try:
        if not isinstance(data, dict) or set(data) != JOURNAL_KEYS:
            raise TypeError
        signature = data["signature"]
        expected = hmac.new(key, canonical_payload(data), hashlib.sha256).hexdigest()
        if (not isinstance(signature, str) or len(signature) != 64
                or not hmac.compare_digest(signature, expected)):
            raise CropReviewSheetError("联系表恢复journal签名无效")
        generation = data["generation_id"]
        if (not isinstance(generation, str) or len(generation) != 32
                or any(char not in "0123456789abcdef" for char in generation)
                or not isinstance(data["source_generation_id"], str)
                or len(data["source_generation_id"]) != 32
                or any(char not in "0123456789abcdef"
                       for char in data["source_generation_id"])
                or not isinstance(data["source_manifest_signature"], str)
                or len(data["source_manifest_signature"]) != 64
                or any(char not in "0123456789abcdef"
                       for char in data["source_manifest_signature"])
                or not _safe_transaction_directory(
                    data["stage_directory"], ".crop_review_sheets.stage.")
                or not _safe_transaction_directory(
                    data["backup_directory"], ".crop_review_sheets.backup.")
                or not isinstance(data["items"], list) or not data["items"]):
            raise TypeError
        names = []
        for item in data["items"]:
            if not isinstance(item, dict) or set(item) != JOURNAL_ITEM_KEYS:
                raise TypeError
            name = item["name"]
            if (not isinstance(name, str) or PurePosixPath(name).name != name
                    or name in names or not isinstance(item["old_present"], bool)
                    or not isinstance(item["new_size"], int) or isinstance(item["new_size"], bool)
                    or item["new_size"] < 1 or not isinstance(item["new_sha256"], str)
                    or len(item["new_sha256"]) != 64):
                raise TypeError
            _sheet_start(name)
            if item["old_present"]:
                if (not isinstance(item["old_size"], int) or isinstance(item["old_size"], bool)
                        or item["old_size"] < 1 or not isinstance(item["old_sha256"], str)
                        or len(item["old_sha256"]) != 64):
                    raise TypeError
            elif item["old_size"] is not None or item["old_sha256"] is not None:
                raise TypeError
            names.append(name)
        return data
    except CropReviewSheetError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise CropReviewSheetError("联系表恢复journal schema无效") from error


def _artifact_matches(directory_fd: int | None, name: str, digest: str | None,
                      size: int | None) -> bool:
    if directory_fd is None or digest is None or size is None:
        return False
    try:
        artifact = read_file_at(directory_fd, name, max_bytes=MAX_TOTAL_OUTPUT_BYTES)
        return artifact.size == size and artifact.sha256 == digest
    except SecureCropArtifactError:
        return False


def _cleanup_transaction(job_fd: int, journal: dict[str, Any], *,
                         remove_journal: bool = True) -> None:
    _remove_at(job_fd, journal["stage_directory"])
    _remove_at(job_fd, journal["backup_directory"])
    if remove_journal:
        _remove_at(job_fd, JOURNAL_FILENAME)
    fsync_directory(job_fd)


def _journal_owns_transaction(job_fd: int, key: bytes, generation: str) -> bool:
    """Keep recovery artifacts if the durable journal owns them or is unreadable."""
    if not _exists_at(job_fd, JOURNAL_FILENAME):
        return False
    try:
        raw = read_file_at(job_fd, JOURNAL_FILENAME, max_bytes=MAX_MANIFEST_BYTES)
        journal = _validate_journal(json.loads(raw.data.decode("utf-8")), key)
        return journal["generation_id"] == generation
    except (CropReviewSheetError, SecureCropArtifactError, UnicodeError, json.JSONDecodeError):
        return True


def _recover_journal(job_fd: int, key: bytes,
                     current_manifest: dict[str, Any]) -> set[str]:
    if not _exists_at(job_fd, JOURNAL_FILENAME):
        return set()
    try:
        raw = read_file_at(job_fd, JOURNAL_FILENAME, max_bytes=MAX_MANIFEST_BYTES)
        journal = _validate_journal(json.loads(raw.data.decode("utf-8")), key)
        source_changed = (
            journal["source_generation_id"] != current_manifest["generation_id"]
            or journal["source_manifest_signature"] != current_manifest["signature"]
        )
        with ExitStack() as resources:
            review_fd = _open_review(job_fd, create=True)
            resources.callback(os.close, review_fd)
            backup_fd = None
            if _exists_at(job_fd, journal["backup_directory"]):
                backup_fd = open_directory_at(job_fd, journal["backup_directory"])
                resources.callback(os.close, backup_fd)
            if (not source_changed and all(
                    _artifact_matches(
                        review_fd, item["name"], item["new_sha256"], item["new_size"])
                    for item in journal["items"]
            )):
                _cleanup_transaction(job_fd, journal)
                return set()
            for item in journal["items"]:
                if item["old_present"] and not (
                    _artifact_matches(review_fd, item["name"], item["old_sha256"], item["old_size"])
                    or _artifact_matches(
                        backup_fd, item["name"], item["old_sha256"], item["old_size"])
                ):
                    raise CropReviewSheetError("联系表journal缺少可恢复的旧文件")
            for item in journal["items"]:
                name = item["name"]
                if item["old_present"]:
                    if not _artifact_matches(review_fd, name, item["old_sha256"], item["old_size"]):
                        _remove_at(review_fd, name)
                        if backup_fd is None:
                            raise CropReviewSheetError("联系表备份目录缺失")
                        _replace_at(backup_fd, name, review_fd, name)
                else:
                    _remove_at(review_fd, name)
            fsync_directory(review_fd)
            if not all(
                (_artifact_matches(review_fd, item["name"], item["old_sha256"], item["old_size"])
                 if item["old_present"] else not _exists_at(review_fd, item["name"]))
                for item in journal["items"]
            ):
                raise CropReviewSheetError("联系表journal回滚验证失败")
            _cleanup_transaction(
                job_fd, journal, remove_journal=not source_changed)
            return ({item["name"] for item in journal["items"]}
                    if source_changed else set())
    except CropReviewSheetError:
        raise
    except (SecureCropArtifactError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CropReviewSheetError("联系表journal恢复失败，恢复工件已保留") from error


def _write_journal(job_fd: int, journal: dict[str, Any]) -> None:
    temporary = f".{JOURNAL_FILENAME}.{journal['generation_id']}.tmp"
    data = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode()
    try:
        write_file_at(job_fd, temporary, data)
        _replace_at(job_fd, temporary, job_fd, JOURNAL_FILENAME)
    finally:
        _remove_at(job_fd, temporary)


def _publish(job_fd: int, review_fd: int, stage_fd: int, backup_fd: int,
             journal: dict[str, Any], key: bytes,
             source_manifest: dict[str, Any]) -> None:
    _write_journal(job_fd, journal)
    try:
        for item in journal["items"]:
            name = item["name"]
            if item["old_present"]:
                _replace_at(review_fd, name, backup_fd, name)
            _replace_at(stage_fd, name, review_fd, name)
        fsync_directory(review_fd)
        if not all(_artifact_matches(review_fd, item["name"], item["new_sha256"], item["new_size"])
                   for item in journal["items"]):
            raise CropReviewSheetError("联系表发布后验证失败")
        _cleanup_transaction(job_fd, journal)
    except BaseException as original:
        if isinstance(original, Exception):
            try:
                _recover_journal(job_fd, key, source_manifest)
            except BaseException as rollback_error:
                raise CropReviewSheetError("联系表发布失败且journal恢复未完成") from rollback_error
        raise original


def generate_crop_review_sheets(*, job_dir, recropped_question_nos,
                                max_canvas_width=2400, max_canvas_height=12000,
                                max_input_pixels=MAX_INPUT_PIXELS,
                                max_input_bytes=MAX_INPUT_BYTES,
                                max_total_output_bytes=MAX_TOTAL_OUTPUT_BYTES,
                                min_free_disk_bytes=MIN_FREE_DISK_BYTES,
                                jpeg_quality=88):
    """Rebuild only affected four-question sheets under one recoverable journal."""
    if not isinstance(recropped_question_nos, list):
        raise CropReviewSheetError("变化题号必须是列表")
    changed = [_positive_integer(number, "变化题号") for number in recropped_question_nos]
    if len(changed) != len(set(changed)):
        raise CropReviewSheetError("变化题号不能重复")
    max_canvas_width = _positive_integer(max_canvas_width, "画布最大宽度")
    max_canvas_height = _positive_integer(max_canvas_height, "画布最大高度")
    max_input_pixels = _positive_integer(max_input_pixels, "输入像素预算")
    max_input_bytes = _positive_integer(max_input_bytes, "输入字节预算")
    max_total_output_bytes = _positive_integer(max_total_output_bytes, "输出字节预算")
    min_free_disk_bytes = _nonnegative_integer(min_free_disk_bytes, "最小磁盘裕量")
    jpeg_quality = _positive_integer(jpeg_quality, "JPEG质量")
    if (max_canvas_width < 320 or max_canvas_width > MAX_CANVAS_DIMENSION
            or max_canvas_height > MAX_CANVAS_DIMENSION or jpeg_quality > 95):
        raise CropReviewSheetError("联系表尺寸或JPEG质量参数超出安全范围")
    try:
        with locked_job(job_dir) as lock:
            key = load_hmac_key(lock.path)
            manifest = _read_manifest(lock.descriptor, key)
            forced_names = _recover_journal(lock.descriptor, key, manifest)
            if not changed and not forced_names:
                return []
            entries = {entry["question_no"]: entry for entry in manifest["questions"]}
            if any(number not in entries for number in changed):
                raise CropReviewSheetError("变化题号不在签名question_crops manifest中")
            starts = sorted(
                {((number - 1) // GROUP_SIZE) * GROUP_SIZE + 1 for number in changed}
                | {_sheet_start(name) for name in forced_names}
            )
            groups = [
                (start, [(number, entries[number]) for number in range(start, start + GROUP_SIZE)
                         if number in entries])
                for start in starts
            ]
            all_entries = [entry for _start, group in groups for _number, entry in group]
            total_pixels = sum(entry["width"] * entry["height"] for entry in all_entries)
            total_bytes = sum(entry["byte_size"] for entry in all_entries)
            if total_pixels > max_input_pixels or total_bytes > max_input_bytes:
                raise CropReviewSheetError("联系表总输入像素或字节超出预算")
            projected = total_pixels * 3
            if shutil.disk_usage(lock.path).free - projected < min_free_disk_bytes:
                raise CropReviewSheetError("磁盘剩余空间低于安全裕量")
            crop_fd = open_directory_at(lock.descriptor, "question_crops")
            try:
                artifacts: dict[int, PinnedBytes] = {}
                for _start, group in groups:
                    for number, entry in group:
                        artifact = read_file_at(
                            crop_fd, f"Q{number:03d}.png", max_bytes=max_input_bytes)
                        _verify_input_png(artifact, entry)
                        artifacts[number] = artifact
            finally:
                os.close(crop_fd)
            generation = secrets.token_hex(16)
            stage_name = f".crop_review_sheets.stage.{generation}"
            backup_name = f".crop_review_sheets.backup.{generation}"
            with ExitStack() as resources:
                os.mkdir(stage_name, 0o700, dir_fd=lock.descriptor)

                def cleanup_unjournaled_transaction():
                    if not _journal_owns_transaction(
                            lock.descriptor, key, generation):
                        _remove_at(lock.descriptor, stage_name)
                        _remove_at(lock.descriptor, backup_name)

                resources.callback(cleanup_unjournaled_transaction)
                os.mkdir(backup_name, 0o700, dir_fd=lock.descriptor)
                fsync_directory(lock.descriptor)
                stage_fd = open_directory_at(lock.descriptor, stage_name)
                resources.callback(os.close, stage_fd)
                backup_fd = open_directory_at(lock.descriptor, backup_name)
                resources.callback(os.close, backup_fd)
                review_fd = _open_review(lock.descriptor, create=True)
                resources.callback(os.close, review_fd)
                names = [_sheet_name(start) for start, _group in groups]
                new_files: dict[str, bytes] = {}
                total_output = 0
                for (start, group), name in zip(groups, names, strict=True):
                    items = [(number, artifacts[number], entry) for number, entry in group]
                    data = _build_sheet(items, max_canvas_width, max_canvas_height, jpeg_quality)
                    total_output += len(data)
                    if total_output > max_total_output_bytes:
                        raise CropReviewSheetError("联系表JPEG总输出字节超出预算")
                    write_file_at(stage_fd, name, data)
                    new_files[name] = data
                journal_items = []
                for name in names:
                    old = None
                    if _exists_at(review_fd, name):
                        old = read_file_at(review_fd, name, max_bytes=max_total_output_bytes)
                    new = new_files[name]
                    journal_items.append({
                        "name": name,
                        "new_sha256": hashlib.sha256(new).hexdigest(),
                        "new_size": len(new),
                        "old_present": old is not None,
                        "old_sha256": old.sha256 if old else None,
                        "old_size": old.size if old else None,
                    })
                journal = sign_manifest(key, {
                    "generation_id": generation,
                    "source_generation_id": manifest["generation_id"],
                    "source_manifest_signature": manifest["signature"],
                    "stage_directory": stage_name,
                    "backup_directory": backup_name,
                    "items": journal_items,
                })
                _validate_journal(journal, key)
                _publish(
                    lock.descriptor, review_fd, stage_fd, backup_fd,
                    journal, key, manifest)
                return names
    except CropReviewSheetError:
        raise
    except SecureCropArtifactError as error:
        raise CropReviewSheetError(str(error)) from error
    except QuestionCropError as error:
        raise CropReviewSheetError(str(error)) from error
    except (OSError, UnidentifiedImageError) as error:
        raise CropReviewSheetError("联系表生成或发布失败") from error
