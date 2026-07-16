"""Signed, resource-bounded, crash-recoverable generation of question crops."""

from __future__ import annotations

import io
import hashlib
import json
import os
import secrets
import shutil
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from src.processing.secure_crop_artifacts import (
    MAX_MANIFEST_BYTES,
    PinnedBytes,
    SecureCropArtifactError,
    bounded_directory_names,
    fsync_directory,
    load_hmac_key,
    locked_job,
    open_directory_at,
    open_pinned_file_at,
    read_file_at,
    read_pinned_descriptor,
    sign_manifest,
    validate_signed_manifest,
    write_file_at,
)


MAX_PAGES = 500
MAX_QUESTIONS = 1_000
MAX_TOTAL_REGIONS = 5_000
MAX_SOURCE_FILE_BYTES = 100 * 1024 * 1024
MAX_SOURCE_PIXELS_PER_PAGE = 100_000_000
MAX_TOTAL_SOURCE_PIXELS = 500_000_000
MAX_CROP_PIXELS_PER_QUESTION = 100_000_000
MAX_TOTAL_CROP_PIXELS = 500_000_000
MAX_TOTAL_OUTPUT_BYTES = 500 * 1024 * 1024
MIN_FREE_DISK_BYTES = 64 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 2_048
MAX_DIRECTORY_TOTAL_ENTRIES = 8_192
MAX_DIRECTORY_DEPTH = 32
FINAL_DIRECTORY = "question_crops"
FINAL_MANIFEST = "question_crops.json"
BACKUP_DIRECTORY = ".question_crops.previous"
BACKUP_MANIFEST = ".question_crops.previous.json"


class QuestionCropError(ValueError):
    """A question crop batch is unsafe, invalid, or could not be published."""


@dataclass(frozen=True)
class QuestionCropReport:
    """Result for an atomic-snapshot publication, not an in-place inode update."""

    manifest: dict[str, Any]
    recropped_question_nos: list[int]
    reused_question_nos: list[int]
    generation_id: str
    publication_mode: str = "atomic_snapshot"


@dataclass(frozen=True)
class _CropPair:
    manifest: dict[str, Any]
    files: dict[int, PinnedBytes]
    manifest_size: int


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QuestionCropError(f"{label}必须是正整数")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise QuestionCropError(f"{label}必须是非负整数")
    return value


def _exists_at(root_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _validate_removal_tree(root_fd: int, name: str, *, depth: int,
                           remaining: list[int]) -> None:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        if depth > MAX_DIRECTORY_DEPTH:
            raise QuestionCropError("目录递归深度超出预算")
        directory_fd = open_directory_at(root_fd, name)
        try:
            children = bounded_directory_names(
                directory_fd, max_entries=MAX_DIRECTORY_ENTRIES)
            remaining[0] -= len(children)
            if remaining[0] < 0:
                raise QuestionCropError("目录项总数超出预算")
            for child in children:
                _validate_removal_tree(
                    directory_fd, child, depth=depth + 1, remaining=remaining)
        finally:
            os.close(directory_fd)


def _remove_tree_unchecked(root_fd: int, name: str, *, depth: int,
                           remaining: list[int]) -> None:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        if depth > MAX_DIRECTORY_DEPTH:
            raise QuestionCropError("目录递归深度超出预算")
        directory_fd = open_directory_at(root_fd, name)
        try:
            children = bounded_directory_names(
                directory_fd, max_entries=MAX_DIRECTORY_ENTRIES)
            remaining[0] -= len(children)
            if remaining[0] < 0:
                raise QuestionCropError("目录项总数超出预算")
            for child in children:
                _remove_tree_unchecked(
                    directory_fd,
                    child,
                    depth=depth + 1,
                    remaining=remaining,
                )
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=root_fd)
    else:
        os.unlink(name, dir_fd=root_fd)


def _remove_at(root_fd: int, name: str) -> None:
    try:
        _validate_removal_tree(
            root_fd, name, depth=0, remaining=[MAX_DIRECTORY_TOTAL_ENTRIES])
        _remove_tree_unchecked(
            root_fd,
            name,
            depth=0,
            remaining=[MAX_DIRECTORY_TOTAL_ENTRIES],
        )
    except SecureCropArtifactError as error:
        raise QuestionCropError("目录项超出安全预算或无法枚举") from error


def _remove_generated_temporary_directory(root_fd: int, name: str) -> None:
    """Remove a newly created empty directory even when its first open failed."""
    try:
        os.rmdir(name, dir_fd=root_fd)
    except FileNotFoundError:
        return
    except OSError:
        _remove_at(root_fd, name)


def _replace_at(root_fd: int, source: str, target: str) -> None:
    os.replace(source, target, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    fsync_directory(root_fd)


def _decode_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise QuestionCropError(f"{label}损坏或不完整") from error


def _verify_png_bytes(artifact: PinnedBytes, expected_size: tuple[int, int], label: str) -> None:
    try:
        with Image.open(io.BytesIO(artifact.data)) as image:
            image.load()
            if image.format != "PNG" or image.size != expected_size:
                raise QuestionCropError(f"{label}格式或尺寸无效")
    except QuestionCropError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise QuestionCropError(f"{label}无法读取") from error


def _validate_pair(job_fd: int, directory_name: str, manifest_name: str, key: bytes,
                   *, expected_job_id: int | None = None,
                   expected_question_nos: list[int] | None = None) -> _CropPair | None:
    if not _exists_at(job_fd, directory_name) or not _exists_at(job_fd, manifest_name):
        return None
    try:
        raw_manifest = read_file_at(job_fd, manifest_name, max_bytes=MAX_MANIFEST_BYTES)
        manifest = validate_signed_manifest(
            _decode_json(raw_manifest.data, "question_crops manifest"),
            key,
            expected_job_id=expected_job_id,
            expected_question_nos=expected_question_nos,
        )
        directory_fd = open_directory_at(job_fd, directory_name)
        try:
            expected_names = {f"Q{entry['question_no']:03d}.png" for entry in manifest["questions"]}
            actual_names = bounded_directory_names(
                directory_fd, max_entries=MAX_DIRECTORY_ENTRIES)
            if set(actual_names) != expected_names or len(actual_names) != len(expected_names):
                raise QuestionCropError("裁图目录内容与签名manifest不一致")
            files: dict[int, PinnedBytes] = {}
            total_bytes = 0
            for entry in manifest["questions"]:
                name = f"Q{entry['question_no']:03d}.png"
                artifact = read_file_at(directory_fd, name, max_bytes=MAX_TOTAL_OUTPUT_BYTES)
                total_bytes += artifact.size
                if (total_bytes > MAX_TOTAL_OUTPUT_BYTES or artifact.size != entry["byte_size"]
                        or artifact.sha256 != entry["sha256"]):
                    raise QuestionCropError("裁图大小或哈希与签名manifest不一致")
                _verify_png_bytes(artifact, (entry["width"], entry["height"]), "旧裁图PNG")
                files[entry["question_no"]] = artifact
        finally:
            os.close(directory_fd)
        return _CropPair(manifest, files, raw_manifest.size)
    except (SecureCropArtifactError, OSError, QuestionCropError):
        return None


def _is_legacy_pair(job_fd: int, directory_name: str = FINAL_DIRECTORY,
                    manifest_name: str = FINAL_MANIFEST) -> bool:
    if not _exists_at(job_fd, directory_name) or not _exists_at(job_fd, manifest_name):
        return False
    try:
        raw = read_file_at(job_fd, manifest_name, max_bytes=MAX_MANIFEST_BYTES)
        data = _decode_json(raw.data, "旧question_crops manifest")
        return ("signature" not in data and "generation_id" not in data
                and isinstance(data.get("questions"), list))
    except (SecureCropArtifactError, QuestionCropError):
        return False


def _artifact_components_exist(job_fd: int) -> bool:
    return any(_exists_at(job_fd, name) for name in (
        FINAL_DIRECTORY, FINAL_MANIFEST, BACKUP_DIRECTORY, BACKUP_MANIFEST,
    ))


def _restore_pair(job_fd: int, directory_source: str, manifest_source: str,
                  key: bytes) -> _CropPair:
    if directory_source != FINAL_DIRECTORY:
        _remove_at(job_fd, FINAL_DIRECTORY)
        _replace_at(job_fd, directory_source, FINAL_DIRECTORY)
    if manifest_source != FINAL_MANIFEST:
        _remove_at(job_fd, FINAL_MANIFEST)
        _replace_at(job_fd, manifest_source, FINAL_MANIFEST)
    restored = _validate_pair(job_fd, FINAL_DIRECTORY, FINAL_MANIFEST, key)
    if restored is None:
        raise QuestionCropError("裁图崩溃恢复后完整性验证失败")
    return restored


def _recover_crop_publication(job_fd: int, key: bytes) -> _CropPair | None:
    final = _validate_pair(job_fd, FINAL_DIRECTORY, FINAL_MANIFEST, key)
    if final is not None:
        _remove_at(job_fd, BACKUP_DIRECTORY)
        _remove_at(job_fd, BACKUP_MANIFEST)
        fsync_directory(job_fd)
        return final
    backup = _validate_pair(job_fd, BACKUP_DIRECTORY, BACKUP_MANIFEST, key)
    if backup is not None:
        return _restore_pair(job_fd, BACKUP_DIRECTORY, BACKUP_MANIFEST, key)
    split = _validate_pair(job_fd, BACKUP_DIRECTORY, FINAL_MANIFEST, key)
    if split is not None:
        return _restore_pair(job_fd, BACKUP_DIRECTORY, FINAL_MANIFEST, key)
    reverse_split = _validate_pair(job_fd, FINAL_DIRECTORY, BACKUP_MANIFEST, key)
    if reverse_split is not None:
        return _restore_pair(job_fd, FINAL_DIRECTORY, BACKUP_MANIFEST, key)
    legacy_candidates = (
        (FINAL_DIRECTORY, FINAL_MANIFEST),
        (BACKUP_DIRECTORY, BACKUP_MANIFEST),
        (BACKUP_DIRECTORY, FINAL_MANIFEST),
        (FINAL_DIRECTORY, BACKUP_MANIFEST),
    )
    for directory_name, manifest_name in legacy_candidates:
        if _is_legacy_pair(job_fd, directory_name, manifest_name):
            if directory_name != FINAL_DIRECTORY:
                _remove_at(job_fd, FINAL_DIRECTORY)
                _replace_at(job_fd, directory_name, FINAL_DIRECTORY)
            if manifest_name != FINAL_MANIFEST:
                _remove_at(job_fd, FINAL_MANIFEST)
                _replace_at(job_fd, manifest_name, FINAL_MANIFEST)
            return None
    if _artifact_components_exist(job_fd):
        raise QuestionCropError("裁图正式结果与备份均无有效签名，已拒绝继续")
    return None


def _publish(job_fd: int, temporary_dir: str, temporary_manifest: str, key: bytes) -> _CropPair:
    try:
        _remove_at(job_fd, BACKUP_DIRECTORY)
        _remove_at(job_fd, BACKUP_MANIFEST)
        if _exists_at(job_fd, FINAL_DIRECTORY):
            _replace_at(job_fd, FINAL_DIRECTORY, BACKUP_DIRECTORY)
        if _exists_at(job_fd, FINAL_MANIFEST):
            _replace_at(job_fd, FINAL_MANIFEST, BACKUP_MANIFEST)
        _replace_at(job_fd, temporary_dir, FINAL_DIRECTORY)
        _replace_at(job_fd, temporary_manifest, FINAL_MANIFEST)
        published = _validate_pair(job_fd, FINAL_DIRECTORY, FINAL_MANIFEST, key)
        if published is None:
            raise QuestionCropError("新裁图批次发布后验证失败")
        recovered = _recover_crop_publication(job_fd, key)
        if recovered is None:
            raise QuestionCropError("新裁图批次发布确认失败")
        return recovered
    except BaseException as original:
        try:
            _recover_crop_publication(job_fd, key)
        except BaseException as rollback_error:
            raise QuestionCropError("裁图发布失败且恢复未完成，已保留恢复工件") from rollback_error
        raise original


def _load_render_metadata(job_fd: int, *, max_pages: int,
                          max_source_pixels_per_page: int,
                          max_total_source_pixels: int) -> tuple[int, dict[int, dict[str, Any]]]:
    try:
        raw = read_file_at(job_fd, "render_manifest.json", max_bytes=MAX_MANIFEST_BYTES)
        data = _decode_json(raw.data, "render_manifest")
        job_id = data["import_job_id"]
        entries = data["pages"]
        if (not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1
                or not isinstance(entries, list) or len(entries) > max_pages
                or data.get("page_count") != len(entries)):
            raise TypeError
        pages: dict[int, dict[str, Any]] = {}
        total_pixels = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError
            number = entry["page_number"]
            relative = entry["relative_path"]
            width = entry["pixel_width"]
            height = entry["pixel_height"]
            digest = entry["sha256"]
            if (not isinstance(number, int) or isinstance(number, bool) or number < 1
                    or number in pages or not isinstance(width, int) or isinstance(width, bool)
                    or not isinstance(height, int) or isinstance(height, bool)
                    or width < 1 or height < 1 or not isinstance(digest, str)
                    or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise TypeError
            rel = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath()
            if (not isinstance(relative, str) or rel.is_absolute() or ".." in rel.parts
                    or "\\" in relative or rel.parts[:1] != ("pages",)
                    or rel.suffix.lower() != ".png"):
                raise TypeError
            pixels = width * height
            total_pixels += pixels
            if pixels > max_source_pixels_per_page or total_pixels > max_total_source_pixels:
                raise QuestionCropError("源页面像素超出资源预算")
            pages[number] = dict(entry)
        return job_id, pages
    except QuestionCropError:
        raise
    except (KeyError, TypeError, SecureCropArtifactError) as error:
        raise QuestionCropError("render_manifest损坏或不完整") from error


def _validate_box(value: Any, width: int, height: int) -> list[int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(not isinstance(item, int) or isinstance(item, bool) for item in value)):
        raise QuestionCropError("bbox必须包含四个整数像素坐标")
    left, top, right, bottom = value
    if left < 0 or top < 0 or left >= right or top >= bottom or right > width or bottom > height:
        raise QuestionCropError("bbox顺序无效或超出源图边界")
    return [left, top, right, bottom]


def _safe_output(question_no: int, supplied: Any = None) -> str:
    expected = f"question_crops/Q{question_no:03d}.png"
    value = expected if supplied is None else supplied
    if not isinstance(value, str) or value != expected:
        raise QuestionCropError("输出必须是对应题号的安全question_crops PNG路径")
    return expected


def _validate_plans(questions: Any, expected_question_nos: Any,
                    pages: dict[int, dict[str, Any]], *, max_questions: int,
                    max_total_regions: int, max_crop_pixels_per_question: int,
                    max_total_crop_pixels: int, separator_height: int) -> tuple[list[int], list[dict[str, Any]]]:
    if not isinstance(questions, list) or not isinstance(expected_question_nos, list):
        raise QuestionCropError("题目计划与预期题号必须是列表")
    expected = [_positive_integer(number, "预期题号") for number in expected_question_nos]
    if (len(expected) > max_questions or len(expected) != len(set(expected))
            or expected != sorted(expected)):
        raise QuestionCropError("预期题号无效或超出题目预算")
    plans = []
    seen: set[int] = set()
    total_regions = 0
    total_crop_pixels = 0
    for item in questions:
        if not isinstance(item, dict):
            raise QuestionCropError("题目计划格式无效")
        number = _positive_integer(item.get("question_no"), "题号")
        if number in seen:
            raise QuestionCropError("题号重复")
        seen.add(number)
        regions = item.get("regions")
        warnings = item.get("warnings", [])
        if (not isinstance(regions, list) or not regions or not isinstance(warnings, list)
                or not all(isinstance(warning, str) for warning in warnings)):
            raise QuestionCropError("regions或warnings格式无效")
        total_regions += len(regions)
        if total_regions > max_total_regions:
            raise QuestionCropError("区域数量超出资源预算")
        validated = []
        widths = []
        heights = []
        for region in regions:
            if not isinstance(region, dict):
                raise QuestionCropError("region格式无效")
            page_number = _positive_integer(region.get("page_number"), "页码")
            if page_number not in pages:
                raise QuestionCropError("region页码不在render_manifest白名单")
            page = pages[page_number]
            bbox = _validate_box(region.get("bbox"), page["pixel_width"], page["pixel_height"])
            validated.append({"page_number": page_number, "bbox": bbox})
            widths.append(bbox[2] - bbox[0])
            heights.append(bbox[3] - bbox[1])
        width = max(widths)
        height = sum(heights) + separator_height * (len(heights) - 1)
        pixels = width * height
        total_crop_pixels += pixels
        if pixels > max_crop_pixels_per_question or total_crop_pixels > max_total_crop_pixels:
            raise QuestionCropError("裁图像素超出资源预算")
        plans.append({
            "question_no": number,
            "regions": validated,
            "output_relative_path": _safe_output(number, item.get("output_relative_path")),
            "warnings": list(warnings),
            "expected_size": (width, height),
        })
    if sorted(seen) != expected or len(plans) != len(expected):
        raise QuestionCropError("题号缺失、重复或超出预期范围")
    plans.sort(key=lambda item: item["question_no"])
    return expected, plans


def _load_sources(job_fd: int, pages: dict[int, dict[str, Any]], *,
                  max_source_file_bytes: int,
                  source_page_bytes: dict[int, bytes] | None = None
                  ) -> tuple[dict[int, PinnedBytes], dict[int, Image.Image]]:
    descriptors: dict[int, int] = {}
    artifacts: dict[int, PinnedBytes] = {}
    images: dict[int, Image.Image] = {}
    succeeded = False
    try:
        if source_page_bytes is not None:
            if set(source_page_bytes) != set(pages) or not all(
                isinstance(value, bytes) and 0 < len(value) <= max_source_file_bytes
                for value in source_page_bytes.values()
            ):
                raise QuestionCropError("已验证源页面快照不完整或超出预算")
            for number, content in source_page_bytes.items():
                artifacts[number] = PinnedBytes(
                    content, hashlib.sha256(content).hexdigest(), len(content),
                    stat.S_IFREG | 0o600, 0, (0, number, len(content), 0, 0),
                )
        else:
            for number, page in pages.items():
                descriptors[number] = open_pinned_file_at(
                    job_fd, page["relative_path"], max_bytes=max_source_file_bytes)
            for number, descriptor in descriptors.items():
                artifacts[number] = read_pinned_descriptor(
                    descriptor, max_bytes=max_source_file_bytes
                )
        for number, artifact in artifacts.items():
            page = pages[number]
            if artifact.sha256 != page["sha256"]:
                raise QuestionCropError(f"第{number}页源页面哈希与render_manifest不一致")
            try:
                with Image.open(io.BytesIO(artifact.data)) as image:
                    image.load()
                    if (image.format != "PNG"
                            or image.size != (page["pixel_width"], page["pixel_height"])):
                        raise QuestionCropError(f"第{number}页源页面格式或尺寸不一致")
                    images[number] = image.convert("RGB").copy()
            except QuestionCropError:
                raise
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                raise QuestionCropError(f"第{number}页源PNG无法读取") from error
            artifacts[number] = artifact
        succeeded = True
        return artifacts, images
    except (SecureCropArtifactError, OSError) as error:
        raise QuestionCropError("源页面文件身份、链接或大小无效") from error
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)
        if not succeeded:
            for image in images.values():
                image.close()


def _composition(region_count: int, separator_height: int) -> dict[str, Any]:
    if region_count == 1:
        return {"mode": "single", "region_count": 1}
    return {"mode": "vertical", "separator_height": separator_height,
            "background": "white", "region_count": region_count}


def _can_reuse(old: dict[str, Any], plan: dict[str, Any], old_sources: dict[int, dict[str, Any]],
               sources: dict[int, PinnedBytes], min_width: int, min_height: int,
               separator_height: int) -> bool:
    width, height = plan["expected_size"]
    if (old.get("question_no") != plan["question_no"]
            or old.get("regions") != plan["regions"]
            or old.get("output_relative_path") != plan["output_relative_path"]
            or old.get("warnings") != plan["warnings"]
            or old.get("composition") != _composition(len(plan["regions"]), separator_height)
            or old.get("width") != width or old.get("height") != height
            or width < min_width or height < min_height):
        return False
    return all(
        old_sources[region["page_number"]]["sha256"]
        == sources[region["page_number"]].sha256
        for region in plan["regions"]
    )


def _png_bytes(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG", optimize=False)
    data = stream.getvalue()
    artifact = PinnedBytes(data, hashlib.sha256(data).hexdigest(), len(data), 0, 0,
                           (0, 0, len(data), 0, 0))
    _verify_png_bytes(artifact, image.size, "生成PNG")
    return data


def _build_manifest(job_id: int, generation_id: str, pages: dict[int, dict[str, Any]],
                    sources: dict[int, PinnedBytes], entries: list[dict[str, Any]],
                    key: bytes) -> dict[str, Any]:
    unsigned = {
        "version": 2,
        "import_job_id": job_id,
        "generation_id": generation_id,
        "question_count": len(entries),
        "source_pages": [
            {"page_number": number, "relative_path": page["relative_path"],
             "pixel_width": page["pixel_width"], "pixel_height": page["pixel_height"],
             "sha256": sources[number].sha256}
            for number, page in sorted(pages.items())
        ],
        "questions": entries,
    }
    return sign_manifest(key, unsigned)


def generate_question_crops_report(*, job_dir, questions, expected_question_nos,
                                   min_width=80, min_height=40, separator_height=12,
                                   max_pages=MAX_PAGES, max_questions=MAX_QUESTIONS,
                                   max_total_regions=MAX_TOTAL_REGIONS,
                                   max_source_file_bytes=MAX_SOURCE_FILE_BYTES,
                                   max_source_pixels_per_page=MAX_SOURCE_PIXELS_PER_PAGE,
                                   max_total_source_pixels=MAX_TOTAL_SOURCE_PIXELS,
                                   max_crop_pixels_per_question=MAX_CROP_PIXELS_PER_QUESTION,
                                   max_total_crop_pixels=MAX_TOTAL_CROP_PIXELS,
                                   max_total_output_bytes=MAX_TOTAL_OUTPUT_BYTES,
                                   min_free_disk_bytes=MIN_FREE_DISK_BYTES,
                                   source_page_bytes=None):
    """Generate a signed complete batch and report its published generation."""
    min_width = _positive_integer(min_width, "最小宽度")
    min_height = _positive_integer(min_height, "最小高度")
    separator_height = _nonnegative_integer(separator_height, "拼接分隔高度")
    limits = {
        "max_pages": _positive_integer(max_pages, "最大页数"),
        "max_questions": _positive_integer(max_questions, "最大题数"),
        "max_total_regions": _positive_integer(max_total_regions, "最大区域数"),
        "max_source_file_bytes": _positive_integer(max_source_file_bytes, "源文件大小预算"),
        "max_source_pixels_per_page": _positive_integer(
            max_source_pixels_per_page, "单页源像素预算"),
        "max_total_source_pixels": _positive_integer(max_total_source_pixels, "总源像素预算"),
        "max_crop_pixels_per_question": _positive_integer(
            max_crop_pixels_per_question, "单题裁图像素预算"),
        "max_total_crop_pixels": _positive_integer(max_total_crop_pixels, "总裁图像素预算"),
        "max_total_output_bytes": _positive_integer(max_total_output_bytes, "总输出字节预算"),
        "min_free_disk_bytes": _nonnegative_integer(min_free_disk_bytes, "最小磁盘裕量"),
    }
    try:
        with locked_job(job_dir) as lock:
            key = load_hmac_key(lock.path)
            recovered = _recover_crop_publication(lock.descriptor, key)
            job_id, pages = _load_render_metadata(
                lock.descriptor,
                max_pages=limits["max_pages"],
                max_source_pixels_per_page=limits["max_source_pixels_per_page"],
                max_total_source_pixels=limits["max_total_source_pixels"],
            )
            expected, plans = _validate_plans(
                questions, expected_question_nos, pages,
                max_questions=limits["max_questions"],
                max_total_regions=limits["max_total_regions"],
                max_crop_pixels_per_question=limits["max_crop_pixels_per_question"],
                max_total_crop_pixels=limits["max_total_crop_pixels"],
                separator_height=separator_height,
            )
            projected = sum(plan["expected_size"][0] * plan["expected_size"][1] * 4
                            for plan in plans)
            if shutil.disk_usage(lock.path).free - projected < limits["min_free_disk_bytes"]:
                raise QuestionCropError("磁盘剩余空间低于安全裕量")
            sources, source_images = _load_sources(
                lock.descriptor, pages,
                max_source_file_bytes=limits["max_source_file_bytes"],
                source_page_bytes=source_page_bytes,
            )
            with ExitStack() as resources:
                for image in source_images.values():
                    resources.callback(image.close)
                old_pair = recovered
                if old_pair is not None:
                    try:
                        validate_signed_manifest(
                            old_pair.manifest, key,
                            expected_job_id=job_id,
                            expected_question_nos=expected,
                        )
                    except SecureCropArtifactError:
                        old_pair = None
                old_entries = ({entry["question_no"]: entry
                                for entry in old_pair.manifest["questions"]} if old_pair else {})
                old_sources = ({entry["page_number"]: entry
                                for entry in old_pair.manifest["source_pages"]} if old_pair else {})
                reusable: dict[int, PinnedBytes] = {}
                for plan in plans:
                    number = plan["question_no"]
                    if (old_pair and number in old_entries
                            and _can_reuse(old_entries[number], plan, old_sources, sources,
                                           min_width, min_height, separator_height)):
                        reusable[number] = old_pair.files[number]
                reused_numbers = [plan["question_no"] for plan in plans
                                  if plan["question_no"] in reusable]
                if old_pair is not None and len(reused_numbers) == len(plans):
                    existing_bytes = (
                        sum(artifact.size for artifact in old_pair.files.values())
                        + old_pair.manifest_size
                    )
                    if existing_bytes > limits["max_total_output_bytes"]:
                        raise QuestionCropError(
                            "现有裁图PNG与manifest总输出字节超出资源预算")
                    return QuestionCropReport(
                        old_pair.manifest, [], reused_numbers,
                        old_pair.manifest["generation_id"])

                generation_id = secrets.token_hex(16)
                temporary_dir = f".question_crops.{generation_id}.tmp"
                temporary_manifest = f".question_crops.{generation_id}.json.tmp"
                os.mkdir(temporary_dir, 0o700, dir_fd=lock.descriptor)
                resources.callback(
                    _remove_generated_temporary_directory,
                    lock.descriptor,
                    temporary_dir,
                )
                resources.callback(_remove_at, lock.descriptor, temporary_manifest)
                fsync_directory(lock.descriptor)
                directory_fd = open_directory_at(lock.descriptor, temporary_dir)
                resources.callback(os.close, directory_fd)
                recropped: list[int] = []
                reused: list[int] = []
                entries: list[dict[str, Any]] = []
                output_bytes = 0
                for plan in plans:
                    number = plan["question_no"]
                    if number in reusable:
                        artifact = reusable[number]
                        data = artifact.data
                        entry = dict(old_entries[number])
                        reused.append(number)
                    else:
                        pieces = [
                            source_images[region["page_number"]].crop(region["bbox"])
                            for region in plan["regions"]
                        ]
                        width, height = plan["expected_size"]
                        if width < min_width or height < min_height:
                            raise QuestionCropError(f"第{number}题裁切尺寸小于允许的最小值")
                        if len(pieces) == 1:
                            result = pieces[0]
                        else:
                            result = Image.new("RGB", (width, height), "white")
                            y = 0
                            for piece in pieces:
                                result.paste(piece, (0, y))
                                y += piece.height + separator_height
                        data = _png_bytes(result)
                        digest = hashlib.sha256(data).hexdigest()
                        entry = {
                            "question_no": number,
                            "regions": plan["regions"],
                            "composition": _composition(len(plan["regions"]), separator_height),
                            "output_relative_path": plan["output_relative_path"],
                            "width": width,
                            "height": height,
                            "byte_size": len(data),
                            "sha256": digest,
                            "crop_status": "generated",
                            "review_status": "pending_ai_review",
                            "warnings": plan["warnings"],
                        }
                        recropped.append(number)
                    output_bytes += len(data)
                    if output_bytes > limits["max_total_output_bytes"]:
                        raise QuestionCropError("裁图输出字节超出资源预算")
                    write_file_at(directory_fd, f"Q{number:03d}.png", data)
                    if number in reusable:
                        os.utime(
                            f"Q{number:03d}.png",
                            ns=(artifact.mtime_ns, artifact.mtime_ns),
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    entries.append(entry)
                fsync_directory(directory_fd)
                manifest = _build_manifest(job_id, generation_id, pages, sources, entries, key)
                validate_signed_manifest(
                    manifest, key, expected_job_id=job_id, expected_question_nos=expected)
                manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    raise QuestionCropError("question_crops manifest超过大小预算")
                if output_bytes + len(manifest_bytes) > limits["max_total_output_bytes"]:
                    raise QuestionCropError("裁图PNG与manifest总输出字节超出资源预算")
                write_file_at(lock.descriptor, temporary_manifest, manifest_bytes)
                published = _publish(
                    lock.descriptor, temporary_dir, temporary_manifest, key)
                return QuestionCropReport(
                    published.manifest, recropped, reused, published.manifest["generation_id"])
    except QuestionCropError:
        raise
    except SecureCropArtifactError as error:
        raise QuestionCropError(str(error)) from error
    except (OSError, UnidentifiedImageError) as error:
        raise QuestionCropError("整套单题图片生成失败") from error


def generate_question_crops(*, job_dir, questions, expected_question_nos,
                            min_width=80, min_height=40, separator_height=12, **budgets):
    """Compatibility API for atomic-snapshot publication, returning its manifest."""
    return generate_question_crops_report(
        job_dir=job_dir,
        questions=questions,
        expected_question_nos=expected_question_nos,
        min_width=min_width,
        min_height=min_height,
        separator_height=separator_height,
        **budgets,
    ).manifest
