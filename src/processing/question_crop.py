"""Transactional, validated generation of complete per-question PNG images."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError


class QuestionCropError(ValueError):
    """A question crop batch is unsafe, invalid, or could not be published."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_render_manifest(job_dir: Path) -> tuple[int, dict[int, dict[str, Any]]]:
    try:
        data = json.loads((job_dir / "render_manifest.json").read_text(encoding="utf-8"))
        job_id = data["import_job_id"]
        entries = data["pages"]
        if not isinstance(job_id, int) or isinstance(job_id, bool) or not isinstance(entries, list):
            raise TypeError
        pages: dict[int, dict[str, Any]] = {}
        for entry in entries:
            number = entry["page_number"]
            relative = entry["relative_path"]
            if (not isinstance(number, int) or isinstance(number, bool) or number < 1
                    or number in pages or not isinstance(relative, str)):
                raise TypeError
            rel = PurePosixPath(relative)
            if (rel.is_absolute() or ".." in rel.parts or "\\" in relative
                    or rel.suffix.lower() != ".png" or rel.parts[:1] != ("pages",)):
                raise TypeError
            source = (job_dir / rel.as_posix()).resolve()
            if not source.is_relative_to(job_dir) or not source.is_file():
                raise TypeError
            actual_hash = _sha256(source)
            if entry.get("sha256") != actual_hash:
                raise QuestionCropError(f"第{number}页源页面哈希与render_manifest不一致")
            try:
                with Image.open(source) as image:
                    image.verify()
                with Image.open(source) as image:
                    if image.format != "PNG":
                        raise TypeError
                    size = image.size
            except (OSError, UnidentifiedImageError) as error:
                raise QuestionCropError(f"第{number}页源PNG无法读取") from error
            if size != (entry.get("pixel_width"), entry.get("pixel_height")):
                raise QuestionCropError(f"第{number}页源页面尺寸与render_manifest不一致")
            pages[number] = {**entry, "source": source, "actual_sha256": actual_hash}
        if data.get("page_count") != len(pages):
            raise TypeError
        return job_id, pages
    except QuestionCropError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise QuestionCropError("render_manifest损坏或不完整") from error


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QuestionCropError(f"{label}必须是正整数")
    return value


def _safe_output(question_no: int, supplied: Any = None) -> str:
    expected = f"question_crops/Q{question_no:03d}.png"
    value = expected if supplied is None else supplied
    if not isinstance(value, str) or "\\" in value:
        raise QuestionCropError("输出路径无效")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".png"
            or path.parts[:1] != ("question_crops",) or path.as_posix() != expected):
        raise QuestionCropError("输出必须是对应题号的安全question_crops PNG路径")
    return expected


def _validate_box(value: Any, width: int, height: int) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise QuestionCropError("bbox必须包含四个像素坐标")
    if any(not isinstance(v, int) or isinstance(v, bool) for v in value):
        raise QuestionCropError("bbox像素坐标必须是整数")
    left, top, right, bottom = value
    if left < 0 or top < 0 or left >= right or top >= bottom or right > width or bottom > height:
        raise QuestionCropError("bbox顺序无效或超出源图边界")
    return [left, top, right, bottom]


def _save_verified_png(image: Image.Image, path: Path) -> None:
    try:
        image.save(path, format="PNG", optimize=False)
        with Image.open(path) as check:
            check.verify()
        with Image.open(path) as check:
            if check.format != "PNG" or check.size != image.size:
                raise QuestionCropError("生成PNG验证失败")
    except QuestionCropError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise QuestionCropError("无法写入或验证PNG") from error


def _validate_existing(job_dir: Path, manifest: dict[str, Any]) -> bool:
    try:
        for entry in manifest["questions"]:
            output = (job_dir / entry["output_relative_path"]).resolve()
            if (not output.is_relative_to(job_dir) or not output.is_file()
                    or output.stat().st_size != entry["byte_size"] or _sha256(output) != entry["sha256"]):
                return False
            with Image.open(output) as image:
                if image.format != "PNG" or image.size != (entry["width"], entry["height"]):
                    return False
        return True
    except (OSError, KeyError, TypeError, UnidentifiedImageError):
        return False


def _publish(job_dir: Path, temporary_dir: Path, temporary_manifest: Path) -> None:
    final_dir = job_dir / "question_crops"
    final_manifest = job_dir / "question_crops.json"
    backup_dir = job_dir / ".question_crops.previous"
    backup_manifest = job_dir / ".question_crops.previous.json"
    for stale in (backup_dir, backup_manifest):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    had_dir, had_manifest = final_dir.exists(), final_manifest.exists()
    try:
        if had_dir:
            os.replace(final_dir, backup_dir)
        if had_manifest:
            os.replace(final_manifest, backup_manifest)
        os.replace(temporary_dir, final_dir)
        os.replace(temporary_manifest, final_manifest)
    except OSError as error:
        if final_dir.exists() and not had_dir:
            shutil.rmtree(final_dir, ignore_errors=True)
        if final_manifest.exists() and not had_manifest:
            final_manifest.unlink(missing_ok=True)
        if backup_dir.exists():
            if final_dir.exists():
                shutil.rmtree(final_dir, ignore_errors=True)
            os.replace(backup_dir, final_dir)
        if backup_manifest.exists():
            final_manifest.unlink(missing_ok=True)
            os.replace(backup_manifest, final_manifest)
        raise QuestionCropError("无法原子发布整套单题图片，已回滚") from error
    finally:
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        backup_manifest.unlink(missing_ok=True)


def generate_question_crops(*, job_dir, questions, expected_question_nos,
                            min_width=80, min_height=40, separator_height=12):
    """Generate and atomically publish a complete batch of question crops."""
    job_dir = Path(job_dir).resolve()
    if not job_dir.is_dir():
        raise QuestionCropError("job目录不存在")
    min_width = _positive_integer(min_width, "最小宽度")
    min_height = _positive_integer(min_height, "最小高度")
    if not isinstance(separator_height, int) or isinstance(separator_height, bool) or separator_height < 0:
        raise QuestionCropError("拼接分隔高度必须是非负整数")
    if not isinstance(questions, list) or not isinstance(expected_question_nos, list):
        raise QuestionCropError("题目计划与预期题号必须是列表")
    expected = [_positive_integer(number, "预期题号") for number in expected_question_nos]
    if len(expected) != len(set(expected)) or expected != sorted(expected):
        raise QuestionCropError("预期题号必须唯一且升序")
    job_id, pages = _load_render_manifest(job_dir)
    seen: set[int] = set()
    plans = []
    for item in questions:
        if not isinstance(item, dict):
            raise QuestionCropError("题目计划格式无效")
        number = _positive_integer(item.get("question_no"), "题号")
        if number in seen:
            raise QuestionCropError("题号重复")
        seen.add(number)
        output = _safe_output(number, item.get("output_relative_path"))
        regions = item.get("regions")
        warnings = item.get("warnings", [])
        if (not isinstance(regions, list) or not regions or not isinstance(warnings, list)
                or not all(isinstance(warning, str) for warning in warnings)):
            raise QuestionCropError("regions或warnings格式无效")
        validated = []
        for region in regions:
            if not isinstance(region, dict):
                raise QuestionCropError("region格式无效")
            page_number = _positive_integer(region.get("page_number"), "页码")
            if page_number not in pages:
                raise QuestionCropError("region页码不在render_manifest白名单")
            page = pages[page_number]
            bbox = _validate_box(region.get("bbox"), page["pixel_width"], page["pixel_height"])
            validated.append({"page_number": page_number, "bbox": bbox})
        plans.append({"question_no": number, "regions": validated,
                      "output_relative_path": output, "warnings": list(warnings)})
    if sorted(seen) != expected or len(plans) != len(expected):
        raise QuestionCropError("题号缺失、重复或超出预期范围")
    plans.sort(key=lambda item: item["question_no"])

    temp_dir = Path(tempfile.mkdtemp(prefix=".question_crops.", dir=job_dir))
    descriptor, temp_name = tempfile.mkstemp(prefix=".question_crops.manifest.", suffix=".tmp", dir=job_dir)
    os.close(descriptor)
    temp_manifest = Path(temp_name)
    try:
        entries = []
        for plan in plans:
            pieces = []
            for region in plan["regions"]:
                with Image.open(pages[region["page_number"]]["source"]) as source:
                    source.load()
                    pieces.append(source.convert("RGB").crop(region["bbox"]))
            width = max(piece.width for piece in pieces)
            height = sum(piece.height for piece in pieces) + separator_height * (len(pieces) - 1)
            if width < min_width or height < min_height:
                raise QuestionCropError(f"第{plan['question_no']}题裁切尺寸小于允许的最小值")
            if len(pieces) == 1:
                result = pieces[0]
                composition = {"mode": "single", "region_count": 1}
            else:
                result = Image.new("RGB", (width, height), "white")
                y = 0
                for piece in pieces:
                    result.paste(piece, (0, y))
                    y += piece.height + separator_height
                composition = {"mode": "vertical", "separator_height": separator_height,
                               "background": "white", "region_count": len(pieces)}
            output = temp_dir / Path(plan["output_relative_path"]).name
            _save_verified_png(result, output)
            entries.append({
                "question_no": plan["question_no"], "regions": plan["regions"],
                "composition": composition,
                "output_relative_path": plan["output_relative_path"],
                "width": result.width, "height": result.height,
                "byte_size": output.stat().st_size, "sha256": _sha256(output),
                "crop_status": "generated", "review_status": "pending_ai_review",
                "warnings": plan["warnings"],
            })
        manifest = {
            "version": 1, "import_job_id": job_id, "question_count": len(entries),
            "source_pages": [
                {"page_number": number, "relative_path": page["relative_path"],
                 "pixel_width": page["pixel_width"], "pixel_height": page["pixel_height"],
                 "sha256": page["actual_sha256"]}
                for number, page in sorted(pages.items())
            ],
            "questions": entries,
        }
        serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        existing_manifest = job_dir / "question_crops.json"
        if existing_manifest.is_file():
            try:
                old = json.loads(existing_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                old = None
            if old == manifest and _validate_existing(job_dir, manifest):
                return manifest
        temp_manifest.write_text(serialized, encoding="utf-8")
        if json.loads(temp_manifest.read_text(encoding="utf-8")) != manifest:
            raise QuestionCropError("临时清单验证失败")
        _publish(job_dir, temp_dir, temp_manifest)
        return manifest
    except QuestionCropError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise QuestionCropError("整套单题图片生成失败") from error
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_manifest.unlink(missing_ok=True)
