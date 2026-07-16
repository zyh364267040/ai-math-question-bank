"""Inspect one import job and resume only the established page renderer."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from src.database.initialize import DEFAULT_DATABASE_PATH
from src.processing.pdf_page_renderer import (
    _open_child_directory,
    _open_safe_directory,
    _read_regular_at,
    claim_render_job,
    run_claimed_render,
)

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
FAILURE_STAGES = {"not_found", "unavailable", "failed"}


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _UsageError(message)


class _ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineResult:
    job_id: int
    stage: str
    next_action: str
    changed: bool = False
    eligible: int = 0
    ineligible: int = 0
    message: str = ""


def _result(job_id, stage, action, message, **values):
    return PipelineResult(job_id, stage, action, message=message, **values)


def _read_connection(path):
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _optional_child(parent_fd, name):
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _ArtifactError("unsafe directory") from error
    if not stat.S_ISDIR(details.st_mode):
        raise _ArtifactError("unsafe directory")
    try:
        return _open_child_directory(parent_fd, name)
    except OSError as error:
        raise _ArtifactError("unsafe directory") from error


def _object_at(parent_fd, name):
    if parent_fd is None:
        return None
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _ArtifactError("unsafe artifact") from error
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise _ArtifactError("unsafe artifact")
    try:
        raw = _read_regular_at(parent_fd, name, max_bytes=MAX_ARTIFACT_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@contextmanager
def _job_artifacts(private_root, job_id):
    descriptors = []
    try:
        try:
            root_fd = _open_safe_directory(Path(private_root))
        except FileNotFoundError:
            yield None
            return
        except OSError as error:
            raise _ArtifactError("unsafe root") from error
        descriptors.append(root_fd)
        processing_fd = _optional_child(root_fd, "processing")
        if processing_fd is None:
            yield None
            return
        descriptors.append(processing_fd)
        job_fd = _optional_child(processing_fd, f"import_job_{job_id}")
        if job_fd is not None:
            descriptors.append(job_fd)
        yield job_fd
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _candidate_numbers(candidate, job_id, source_id):
    questions = candidate.get("questions") if candidate else None
    if (
        candidate is None
        or candidate.get("import_job_id") != job_id
        or candidate.get("source_paper_id") != source_id
        or not isinstance(questions, list)
        or not questions
        or candidate.get("question_count") != len(questions)
    ):
        return None
    numbers = [
        item.get("source_question_no") if isinstance(item, dict) else None
        for item in questions
    ]
    if any(not isinstance(number, str) or not number.isdigit() for number in numbers):
        return None
    return set(numbers) if len(set(numbers)) == len(numbers) else None


def _matching_batch(value, job_id, numbers):
    entries = value.get("questions") if value else None
    if (
        value is None
        or value.get("import_job_id") != job_id
        or value.get("question_count") != len(numbers)
        or not isinstance(entries, list)
        or len(entries) != len(numbers)
    ):
        return False
    found = {
        str(item.get("question_no"))
        for item in entries
        if isinstance(item, dict)
    }
    return len(found) == len(entries) and found == numbers


def _database_snapshot(database_path, job_id):
    with _read_connection(database_path) as connection:
        job = connection.execute(
            "SELECT status,source_paper_id FROM import_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            return None
        render = connection.execute(
            "SELECT status FROM import_page_render_runs WHERE import_job_id=?",
            (job_id,),
        ).fetchone()
        admitted = {
            row[0]
            for row in connection.execute(
                "SELECT source_question_no FROM question_sources WHERE import_job_id=?",
                (job_id,),
            )
        }
    return job, render, admitted


def inspect_pipeline(database_path, private_root, job_id):
    """Derive one bounded, no-follow snapshot without writing database or files."""
    database_path, private_root = Path(database_path), Path(private_root)
    try:
        snapshot = _database_snapshot(database_path, job_id)
    except sqlite3.Error:
        return _result(job_id, "unavailable", "check_database", "题库数据库不可读取")
    if snapshot is None:
        return _result(job_id, "not_found", "check_job_id", "导入任务不存在")
    job, render, admitted = snapshot
    if job[0] == "completed":
        return _result(job_id, "completed", "none", "任务已经完成")
    if job[0] == "needs_review":
        return _result(job_id, "needs_review", "manual_review", "任务保持人工复核")
    if job[0] == "processing":
        return _result(
            job_id,
            "in_progress",
            "wait_or_recover",
            "任务正在处理中，请等待或使用原服务恢复",
        )
    if job[0] == "failed":
        return _result(
            job_id,
            "failed",
            "manual_review",
            "任务已失败，请检查后通过原服务重试",
        )
    if admitted:
        return _result(
            job_id,
            "needs_review",
            "manual_review",
            "任务已有正式题来源，不能根据可变工件继续推进",
        )

    try:
        with _job_artifacts(private_root, job_id) as job_fd:
            manifest = _object_at(job_fd, "render_manifest.json")
            if (
                render is None
                or render[0] != "completed"
                or manifest is None
                or manifest.get("import_job_id") != job_id
            ):
                return _result(job_id, "needs_render", "render_pages", "需要生成页面 PNG")

            candidate = _object_at(job_fd, "candidate_questions.json")
            numbers = _candidate_numbers(candidate, job_id, job[1])
            if numbers is None:
                return _result(
                    job_id,
                    "needs_candidates",
                    "provide_candidate_questions",
                    "需要由外部视觉识别流程提供候选题",
                )
            crops = _object_at(job_fd, "question_crops.json")
            if not _matching_batch(crops, job_id, numbers):
                return _result(
                    job_id,
                    "needs_crops",
                    "provide_crop_plan",
                    "区域计划和裁图由外部视觉流程处理",
                )
            crop_entries = (
                crops.get("questions") if isinstance(crops, dict) else []
            ) or []
            if any(
                item.get("crop_status") != "generated"
                or item.get("review_status") != "ai_review_passed"
                for item in crop_entries
            ):
                return _result(
                    job_id,
                    "needs_crop_review",
                    "review_crops",
                    "题目裁图需要视觉审核",
                )

            audit = _object_at(job_fd, "ai_audit.json")
            entries = audit.get("questions") if audit else None
            entry_list = entries if isinstance(entries, list) else []
            valid_entries = isinstance(entries, list) and all(
                isinstance(item, dict)
                and isinstance(item.get("source_question_no"), str)
                for item in entry_list
            )
            audit_numbers = (
                {item["source_question_no"] for item in entry_list}
                if valid_entries
                else set()
            )
            if (
                audit is None
                or audit.get("import_job_id") != job_id
                or audit.get("question_count") != len(numbers)
                or not valid_entries
                or len(entry_list) != len(numbers)
                or audit_numbers != numbers
            ):
                return _result(
                    job_id,
                    "needs_ai_review",
                    "provide_ai_audit",
                    "需要由外部流程提供 AI 审核清单",
                )
            return _result(
                job_id,
                "ready",
                "run_strict_admission",
                "视觉工件齐备，请调用现有严格入库服务",
            )
    except _ArtifactError:
        return _result(
            job_id,
            "unavailable",
            "check_artifacts",
            "任务工件不可安全读取",
        )


def run_pipeline(database_path, private_root, job_id, *, apply=False):
    """Inspect a job; --apply resumes only the established safe renderer."""
    database_path, private_root = Path(database_path), Path(private_root)
    current = inspect_pipeline(database_path, private_root, job_id)
    if not apply or current.next_action != "render_pages":
        return current
    try:
        claim = claim_render_job(database_path, private_root, job_id)
        if claim is None:
            return current
        claimed_state = inspect_pipeline(database_path, private_root, job_id)
        if claimed_state.next_action != "render_pages":
            close = getattr(claim, "close", None)
            if callable(close):
                close()
            return claimed_state
        rendered = run_claimed_render(claim)
        if rendered is None:
            return _result(job_id, "failed", "retry", "页面渲染执行失败")
        following = inspect_pipeline(database_path, private_root, job_id)
        return replace(following, changed=True)
    except Exception:
        return _result(job_id, "failed", "retry", "页面渲染执行失败")


def _parser():
    parser = _Parser(description="检查或推进可断点续跑导入流水线")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _print_result(result, as_json):
    if as_json:
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"job_id={result.job_id} stage={result.stage} "
            f"next_action={result.next_action} changed={str(result.changed).lower()} "
            f"eligible={result.eligible} ineligible={result.ineligible} "
            f"message={result.message}"
        )


def main(argv=None):
    raw = list(argv) if argv is not None else list(sys.argv[1:])
    try:
        args = _parser().parse_args(raw)
    except _UsageError:
        result = _result(0, "failed", "check_arguments", "命令参数无效")
        _print_result(result, "--json" in raw)
        return 2
    try:
        result = run_pipeline(
            args.database,
            args.private_root or args.database.parent,
            args.job_id,
            apply=args.apply,
        )
    except Exception:
        result = _result(args.job_id, "failed", "retry", "流水线检查失败")
    _print_result(result, args.json)
    return 1 if result.stage in FAILURE_STAGES else 0


if __name__ == "__main__":
    raise SystemExit(main())
