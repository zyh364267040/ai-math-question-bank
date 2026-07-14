"""Validated, atomic and auditable crops of private rendered exam pages."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, UnidentifiedImageError


KINDS = {"question_figure", "review_evidence"}


class CropError(ValueError):
    """A crop request is unsafe, invalid, or inconsistent with its manifest."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value:
        raise CropError("输出路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".png":
        raise CropError("输出必须是job目录内的安全PNG相对路径")
    if not path.parts or path.parts[0] not in {"assets", "review"}:
        raise CropError("输出仅允许位于assets或review目录")
    return path


def _processing(kind: str, value: dict[str, Any] | None) -> dict[str, Any]:
    processing = dict(value or {})
    if kind == "question_figure":
        if processing:
            raise CropError("正式配图不允许审核增强处理")
        return {"variant": "original", "scale": 1, "contrast": 1.0, "sharpen": False}
    allowed = {"variant", "scale", "contrast", "sharpen"}
    if set(processing) - allowed:
        raise CropError("含不支持的审核处理参数")
    variant = processing.get("variant", "original")
    scale = processing.get("scale", 1)
    contrast = processing.get("contrast", 1.0)
    sharpen = processing.get("sharpen", False)
    if variant not in {"original", "enhanced"}:
        raise CropError("审核证据版本无效")
    if scale not in {1, 2, 3} or not isinstance(contrast, (int, float)) or not 0.5 <= contrast <= 3:
        raise CropError("审核增强参数无效")
    if not isinstance(sharpen, bool):
        raise CropError("锐化参数无效")
    if variant == "original" and (scale != 1 or contrast != 1.0 or sharpen):
        raise CropError("原色证据不能应用增强")
    return {"variant": variant, "scale": scale, "contrast": float(contrast), "sharpen": sharpen}


def _pixel_box(box, normalized: bool, width: int, height: int, margin: int) -> tuple[int, int, int, int]:
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise CropError("裁切坐标必须包含四个数值")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
        raise CropError("裁切坐标必须是有限数值")
    if normalized:
        if any(v < 0 or v > 1 for v in box):
            raise CropError("归一化坐标必须位于0到1")
        left, top = math.floor(box[0] * width), math.floor(box[1] * height)
        right, bottom = math.ceil(box[2] * width), math.ceil(box[3] * height)
    else:
        if any(float(v) != int(v) for v in box):
            raise CropError("像素坐标必须是整数")
        left, top, right, bottom = map(int, box)
    if left >= right or top >= bottom or left < 0 or top < 0 or right > width or bottom > height:
        raise CropError("裁切坐标顺序无效或超出源图")
    if isinstance(margin, bool) or not isinstance(margin, int) or margin < 0:
        raise CropError("留白边距必须是非负整数")
    return max(0, left - margin), max(0, top - margin), min(width, right + margin), min(height, bottom + margin)


def _write_png_atomic(image: Image.Image, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temporary = Path(name)
        image.save(temporary, format="PNG", optimize=False)
        with Image.open(temporary) as check:
            check.verify()
        with Image.open(temporary) as check:
            if check.format != "PNG" or check.size != image.size:
                raise CropError("临时PNG验证失败")
        os.replace(temporary, output)
        temporary = None
    except CropError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise CropError("无法原子写入裁切图片") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_manifest(job_dir: Path, assets: list[dict[str, Any]]) -> None:
    path = job_dir / "figure_assets.json"
    descriptor, name = tempfile.mkstemp(prefix=".figure_assets.", suffix=".tmp", dir=job_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "assets": assets}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)
    except (OSError, json.JSONDecodeError) as error:
        raise CropError("无法原子写入配图清单") from error
    finally:
        temporary.unlink(missing_ok=True)


def crop_figure(*, job_dir, source_png, output_relative_path, crop_box, question_no,
                page_number, kind, normalized=False, margin=0, min_width=8,
                min_height=8, processing=None, review_status=None):
    """Crop one PNG and record a stable private asset manifest entry."""
    job_dir = Path(job_dir).resolve()
    source = Path(source_png).resolve()
    if kind not in KINDS:
        raise CropError("资源用途无效")
    if not job_dir.is_dir() or not source.is_file() or source.suffix.lower() != ".png":
        raise CropError("源图必须是存在的PNG")
    if not source.is_relative_to(job_dir):
        raise CropError("源图必须位于指定job目录")
    relative = _safe_relative_path(output_relative_path)
    output = (job_dir / relative.as_posix()).resolve()
    if not output.is_relative_to(job_dir):
        raise CropError("禁止路径穿越")
    process = _processing(kind, processing)
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise CropError("页码无效")
    question = str(question_no)
    if not question.isdigit() or int(question) < 1:
        raise CropError("题号无效")
    try:
        with Image.open(source) as opened:
            if opened.format != "PNG":
                raise CropError("源图实际格式不是PNG")
            opened.load()
            image = opened.convert("RGB") if opened.mode not in {"RGB", "RGBA", "L", "LA"} else opened.copy()
    except (OSError, UnidentifiedImageError) as error:
        raise CropError("源PNG无法读取") from error
    pixels = _pixel_box(crop_box, normalized, image.width, image.height, margin)
    width, height = pixels[2] - pixels[0], pixels[3] - pixels[1]
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (min_width, min_height)):
        raise CropError("最小尺寸无效")
    if width < min_width or height < min_height:
        raise CropError("裁切尺寸小于允许的最小值")
    source_hash = _sha256(source)
    normalized_box = [pixels[0] / image.width, pixels[1] / image.height,
                      pixels[2] / image.width, pixels[3] / image.height]
    identity = (question, kind, relative.as_posix(), process)
    manifest_path = job_dir / "figure_assets.json"
    assets = []
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            assets = payload["assets"]
            if not isinstance(assets, list):
                raise TypeError
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise CropError("现有配图清单损坏") from error
    for existing in assets:
        existing_identity = (str(existing.get("question_no")), existing.get("kind"),
                             existing.get("output_relative_path"), existing.get("processing"))
        if existing_identity == identity:
            if existing.get("source_page_sha256") != source_hash:
                raise CropError("源页面哈希已变化，拒绝静默复用")
            if (existing.get("crop_box_pixels") != list(pixels)
                    or not output.is_file() or _sha256(output) != existing.get("sha256")):
                raise CropError("幂等资源配置或文件不一致")
            return existing
        if existing.get("output_relative_path") == relative.as_posix():
            raise CropError("输出路径已被其他处理配置占用")
    result = image.crop(pixels)
    if process["scale"] > 1:
        result = result.resize((result.width * process["scale"], result.height * process["scale"]), Image.Resampling.LANCZOS)
    if process["contrast"] != 1.0:
        result = ImageEnhance.Contrast(result).enhance(process["contrast"])
    if process["sharpen"]:
        result = result.filter(ImageFilter.UnsharpMask(radius=1.2, percent=125, threshold=3))
    _write_png_atomic(result, output)
    status = review_status or ("pending_ai_review" if kind == "question_figure" else "review_evidence")
    asset = {
        "question_no": question, "kind": kind, "source_page": page_number,
        "source_page_sha256": source_hash, "crop_box_pixels": list(pixels),
        "crop_box_normalized": normalized_box, "output_relative_path": relative.as_posix(),
        "width": result.width, "height": result.height, "byte_size": output.stat().st_size,
        "sha256": _sha256(output), "processing": process, "review_status": status,
    }
    try:
        _write_manifest(job_dir, [*assets, asset])
    except CropError:
        output.unlink(missing_ok=True)
        raise
    return asset
