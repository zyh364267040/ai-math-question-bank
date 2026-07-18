"""Build deterministic, wholly synthetic import-job fixtures for tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image

from src.processing.secure_crop_artifacts import load_hmac_key, sign_manifest


QUESTION_COUNT = 23
SYNTHETIC_PAPER_NAME = "合成测试卷（非真实试卷）"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_png(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG", optimize=False)
    content = path.read_bytes()
    return {
        "width": size[0], "height": size[1], "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _source_page(question_no: int) -> int:
    if question_no <= 6:
        return 1
    if question_no <= 12:
        return 2
    if question_no <= 18:
        return 3
    return 4


def _choice_options(question_no: int) -> list[dict[str, str]]:
    if question_no == 3:
        return [{"code": code, "content": "见原页选项图"} for code in "ABCD"]
    contents = ("$S_1$", "$S_2$", "$S_3$", "$S_4$") if question_no == 1 else tuple(
        f"合成选项 {code}" for code in "ABCD"
    )
    return [{"code": code, "content": content} for code, content in zip("ABCD", contents)]


def _subquestions(question_no: int) -> list[dict[str, str]]:
    if question_no == 21:
        return [
            {"label": "（1）", "stem_markdown": "计算合成量 $u$；"},
            {"label": "（2）", "stem_markdown": "计算合成量 $v$；"},
            {"label": "（3）", "stem_markdown": "说明 $u$ 与 $v$ 的关系。"},
        ]
    if question_no == 22:
        return [
            {"label": "（1）", "stem_markdown": "若曲线 $y=f(x)$ 经过合成点，求参数 $a$；"},
            {"label": "（2）", "stem_markdown": "设函数 $f(x)$ 满足以下公共合成条件："},
            {"label": "（2）（i）", "stem_markdown": "若函数 $f(x)$ 有两个合成零点，求 $a$ 的取值范围；"},
            {"label": "（2）（ii）", "stem_markdown": "证明两个合成零点之积大于 $1$。"},
        ]
    if question_no == 23:
        return [
            {"label": "（1）", "stem_markdown": "讨论 $f(x)$ 的单调性；"},
            {"label": "（2）", "stem_markdown": "设 $a=1$，并沿用以下公共合成条件："},
            {"label": "（2）①", "stem_markdown": "在公共合成条件下，求参数 $m$ 的取值范围；"},
            {"label": "（2）②", "stem_markdown": "在同一公共条件下，比较两个合成式的大小。"},
        ]
    return []


def _candidate_question(question_no: int) -> dict:
    if question_no <= 12:
        question_type = "single_choice"
        stem = f"合成单选题 {question_no}：请选择唯一正确的合成结论。"
    elif question_no <= 20:
        question_type = "fill_blank"
        stem = f"合成填空题 {question_no}：填写合成计算结果。"
    else:
        question_type = "solution"
        stem = f"合成解答题 {question_no}：设 $f(x)=x^2+ax+{question_no}$。"
    return {
        "source_question_no": str(question_no),
        "question_type_code": question_type,
        "stem_markdown": stem,
        "options": _choice_options(question_no) if question_type == "single_choice" else [],
        "subquestions": _subquestions(question_no),
        "answer_markdown": "",
        "source_pages": [_source_page(question_no)],
        "primary_knowledge_point_code": "01.01.06",
        "related_knowledge_point_codes": ["01.01.07"],
        "figure_required": question_no in {3, 16},
        "figure_notes": "合成图像选项" if question_no == 3 else ("合成独立必要配图" if question_no == 16 else ""),
        "confidence": "medium" if question_no == 12 else "high",
        "review_notes": ["合成数据：需人工确认"] if question_no == 12 else [],
    }


def create_import_job_fixture(
    private_root: Path, *, job_id: int = 1, source_paper_id: int = 1,
) -> Path:
    """Create and return a safe synthetic ``import_job_<id>`` directory."""
    private_root = Path(private_root)
    job_dir = private_root / "processing" / f"import_job_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=False)

    page_entries = []
    page_metadata = {}
    for page_number in range(1, 5):
        relative = f"pages/page_{page_number:03d}.png"
        metadata = _write_png(job_dir / relative, (64, 80), (235 - page_number, 240, 245 + page_number))
        page_metadata[page_number] = metadata
        page_entries.append({
            "page_number": page_number, "relative_path": relative,
            "pixel_width": metadata["width"], "pixel_height": metadata["height"],
            "byte_size": metadata["byte_size"], "sha256": metadata["sha256"],
        })

    questions = [_candidate_question(number) for number in range(1, QUESTION_COUNT + 1)]
    _write_json(job_dir / "candidate_questions.json", {
        "import_job_id": job_id, "source_paper_id": source_paper_id,
        "paper_name": SYNTHETIC_PAPER_NAME, "page_range": [1, 4],
        "question_count": QUESTION_COUNT, "questions": questions,
        "global_review_notes": ["本文件仅含程序生成的合成测试内容。"],
    })

    audit_questions = []
    for number in range(1, QUESTION_COUNT + 1):
        human_required = number == 12
        audit_questions.append({
            "source_question_no": str(number),
            "audit_status": "human_required" if human_required else "auto_pass",
            "text_match": not human_required, "structure_match": True,
            "formula_match": not human_required,
            "figure_check": "passed" if number in {3, 16} else "not_applicable",
            "knowledge_check": "passed",
            "issues": ["合成的歧义标记需要人工确认"] if human_required else [],
            "suggested_corrections": ["人工确认后再决定是否入库"] if human_required else [],
            "evidence_page": _source_page(number),
            "audit_confidence": "medium" if human_required else "high",
        })
    _write_json(job_dir / "ai_audit.json", {
        "import_job_id": job_id, "auditor": "synthetic_fixture_auditor",
        "audit_scope": {"source_pages": [1, 2, 3, 4], "kind": "synthetic"},
        "question_count": QUESTION_COUNT,
        "counts": {"auto_pass": 22, "disputed": 0, "human_required": 1},
        "questions": audit_questions,
        "random_sample_recommendation": {
            "question_nos": ["3", "12", "16", "22", "23"],
            "reason": "覆盖合成图片、人工确认和嵌套小问。",
        },
        "global_findings": ["全部内容与图片均由测试生成器合成。"],
    })

    crop_entries = []
    for number in range(1, QUESTION_COUNT + 1):
        relative = f"question_crops/Q{number:03d}.png"
        metadata = _write_png(
            job_dir / relative, (48, 32),
            ((number * 17) % 256, (number * 29) % 256, (number * 43) % 256),
        )
        crop_entries.append({
            "question_no": number,
            "regions": [{"page_number": _source_page(number), "bbox": [1, 1, 47, 31]}],
            "composition": "single_region", "output_relative_path": relative,
            **metadata, "crop_status": "generated",
            "review_status": "ai_review_passed", "warnings": [],
        })
    crop_manifest = {
        "version": 2,
        "import_job_id": job_id,
        "generation_id": f"{job_id:032x}",
        "question_count": QUESTION_COUNT,
        "source_pages": [
            {
                key: entry[key]
                for key in (
                    "page_number", "relative_path", "pixel_width", "pixel_height",
                    "sha256",
                )
            }
            for entry in page_entries
        ],
        "questions": [
            {
                **entry,
                "composition": {"mode": "single", "region_count": 1},
            }
            for entry in crop_entries
        ],
    }
    _write_json(
        job_dir / "question_crops.json",
        sign_manifest(load_hmac_key(job_dir), crop_manifest),
    )

    figure_entries = []
    for number in (3, 16):
        page_number = _source_page(number)
        relative = f"assets/question_{number:03d}_figure_01.png"
        metadata = _write_png(job_dir / relative, (40, 24), (30 + number, 90, 150))
        figure_entries.append({
            "question_no": str(number), "kind": "question_figure",
            "source_page": page_number,
            "source_page_sha256": page_metadata[page_number]["sha256"],
            "crop_box_pixels": [2, 2, 42, 26],
            "crop_box_normalized": [0.03125, 0.025, 0.65625, 0.325],
            "output_relative_path": relative, **metadata, "processing": "synthetic",
            "review_status": "ai_review_passed",
            "review_notes": ["程序生成的合成必要配图。"],
        })
    _write_json(job_dir / "figure_assets.json", {"version": 1, "assets": figure_entries})

    _write_json(job_dir / "render_manifest.json", {
        "import_job_id": job_id, "source_paper_id": source_paper_id,
        "pdf_sha256": hashlib.sha256(b"synthetic-pdf-placeholder").hexdigest(),
        "dpi": 72, "page_count": 4, "pages": page_entries,
    })
    return job_dir


def anchor_synthetic_candidate_audit(database_path: Path, job_dir: Path) -> None:
    """Add the DB half of the synthetic fixture's completed batch audit."""
    candidate_raw = (job_dir / "candidate_questions.json").read_bytes()
    audit_raw = (job_dir / "ai_audit.json").read_bytes()
    crop_raw = (job_dir / "question_crops.json").read_bytes()
    candidate = json.loads(candidate_raw)
    crop = json.loads(crop_raw)
    count = candidate["question_count"]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO import_candidate_audit_runs
               (import_job_id,status,question_count,processed_questions,codex_run_id,
                input_candidate_sha256,input_candidate_byte_size,
                input_crop_generation_id,input_manifest_sha256,input_manifest_signature,
                output_sha256,output_byte_size,completed_at,updated_at)
               VALUES(?,'completed',?,?,'synthetic-admission-run',?,?,?,?,?,?,?,
                      '2026-07-16T00:00:00+00:00','2026-07-16T00:00:00+00:00')""",
            (candidate["import_job_id"], count, count,
             hashlib.sha256(candidate_raw).hexdigest(), len(candidate_raw),
             crop["generation_id"], hashlib.sha256(crop_raw).hexdigest(),
             crop["signature"], hashlib.sha256(audit_raw).hexdigest(), len(audit_raw)),
        )


def write_synthetic_crop_review_evidence(job_dir):
    """Create authenticated review evidence for an already-reviewed synthetic manifest."""
    manifest_bytes = (job_dir / "question_crops.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    questions = [
        {
            "question_no": item["question_no"],
            "status": item["review_status"],
            "warnings": item["warnings"],
        }
        for item in manifest["questions"]
    ]
    payload = {
        "version": 1,
        "import_job_id": manifest["import_job_id"],
        "input_generation_id": manifest["generation_id"],
        "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "reviewer_run_id": "synthetic-fixture-review",
        "questions": questions,
    }
    request_sha256 = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    evidence = sign_manifest(load_hmac_key(job_dir), {
        **payload,
        "reviewed_at": "2026-01-01T00:00:00+00:00",
        "output_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "output_manifest_signature": manifest["signature"],
        "request_sha256": request_sha256,
    })
    _write_json(job_dir / "crop_ai_review.json", evidence)
