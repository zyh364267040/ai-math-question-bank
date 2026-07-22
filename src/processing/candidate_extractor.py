"""Explicit, fail-closed Codex transcription of verified single-question PNGs."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from src.processing.crop_review import CropReviewError, validate_current_crop_review
from src.processing.pdf_page_renderer import (
    _open_child_directory,
    _open_safe_directory,
)
from src.processing.question_splitter import (
    CODEX_TIMEOUT_SECONDS,
    MAX_CODEX_OUTPUT_BYTES,
    MAX_CODEX_STDERR_BYTES,
    SAFE_CODEX_MISSING,
    _bounded_communicate,
    _resolve_codex_bin,
    _terminate_process_group,
)
from src.processing.secure_crop_artifacts import (
    SecureCropArtifactError,
    load_hmac_key,
    read_file_at,
    validate_signed_manifest,
)


MAX_QUESTIONS = 200
MAX_CROP_BYTES = 64 * 1024 * 1024
MAX_TOTAL_CROP_BYTES = 256 * 1024 * 1024
MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_STEM_LENGTH = 30_000
MAX_ITEM_LENGTH = 10_000
MAX_OPTIONS = 20
MAX_SUBQUESTIONS = 30
MAX_WARNINGS = 20
MAX_PAGES_PER_QUESTION = 20
MAX_IMAGE_DIMENSION = 20_000
SAFE_INPUT_REQUIRED = "已验证单题图片准备完成后才能调用 Codex 识别题目内容"
SAFE_INPUT_INVALID = "已验证单题图片校验失败，请重新完成切题审核"
SAFE_EXTRACTION_ERROR = "Codex 题目内容识别失败，请重试"
SAFE_EXISTING_ERROR = "现有候选题结果校验失败，请点击重试"


class CandidateExtractionError(ValueError):
    """A fixed, user-safe candidate extraction failure."""


class CandidateCodexExecutionError(CandidateExtractionError):
    pass


@dataclass(frozen=True)
class CandidateExtractionRunResult:
    final_message: str
    run_id: str


@dataclass
class CandidateExtractionClaim:
    database_path: Path
    private_root: Path
    job_id: int
    source_paper_id: int
    runner: Any
    image_paths: tuple[Path, ...]
    question_numbers: tuple[int, ...]
    source_pages: dict[int, list[int]]
    manifest_sha256: str
    generation_id: str
    manifest_signature: str
    temporary: Any
    lock_fd: int | None
    job_fd: int | None

    def close(self):
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.job_fd is not None:
            os.close(self.job_fd)
            self.job_fd = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_output_schema():
    option = {
        "type": "object", "additionalProperties": False,
        "required": ["code", "content"],
        "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": 16},
            "content": {"type": "string", "minLength": 1, "maxLength": MAX_ITEM_LENGTH},
        },
    }
    subquestion = {
        "type": "object", "additionalProperties": False,
        "required": ["label", "stem_markdown"],
        "properties": {
            "label": {"type": "string", "minLength": 1, "maxLength": 50},
            "stem_markdown": {
                "type": "string", "minLength": 1, "maxLength": MAX_ITEM_LENGTH,
            },
        },
    }
    question = {
        "type": "object", "additionalProperties": False,
        "required": [
            "source_question_no", "stem_markdown", "question_type_code",
            "primary_knowledge_point_code", "related_knowledge_point_codes",
            "options", "subquestions", "answer_markdown", "analysis_markdown",
            "figure_required", "source_pages", "extraction_confidence", "warnings",
        ],
        "properties": {
            "source_question_no": {
                "type": "string", "minLength": 1, "maxLength": 3,
                "pattern": "^[1-9][0-9]{0,2}$",
            },
            "stem_markdown": {
                "type": "string", "minLength": 1, "maxLength": MAX_STEM_LENGTH,
            },
            "question_type_code": {
                "type": "string",
                "enum": ["single_choice", "multiple_choice", "fill_blank", "solution"],
            },
            "primary_knowledge_point_code": {"type": "string", "maxLength": 0},
            "related_knowledge_point_codes": {
                "type": "array", "maxItems": 0, "items": {"type": "string"},
            },
            "options": {
                "type": "array", "maxItems": MAX_OPTIONS, "items": option,
            },
            "subquestions": {
                "type": "array", "maxItems": MAX_SUBQUESTIONS, "items": subquestion,
            },
            "answer_markdown": {"type": "string", "maxLength": 0},
            "analysis_markdown": {"type": "string", "maxLength": 0},
            "figure_required": {"type": "boolean"},
            "source_pages": {
                "type": "array", "minItems": 1, "maxItems": MAX_PAGES_PER_QUESTION,
                "items": {"type": "integer", "minimum": 1, "maximum": 10_000},
            },
            "extraction_confidence": {
                "type": "string", "enum": ["low", "medium", "high"],
            },
            "warnings": {
                "type": "array", "maxItems": MAX_WARNINGS,
                "items": {"type": "string", "maxLength": 500},
            },
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "version", "import_job_id", "source_paper_id", "question_count", "questions",
        ],
        "properties": {
            "version": {"type": "integer", "const": 1},
            "import_job_id": {"type": "integer", "minimum": 1},
            "source_paper_id": {"type": "integer", "minimum": 1},
            "question_count": {
                "type": "integer", "minimum": 1, "maximum": MAX_QUESTIONS,
            },
            "questions": {
                "type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS,
                "items": question,
            },
        },
    }


TOP_KEYS = {"version", "import_job_id", "source_paper_id", "question_count", "questions"}
QUESTION_KEYS = set(_candidate_output_schema()["properties"]["questions"]["items"]["required"])
OPTION_KEYS = {"code", "content"}
SUBQUESTION_KEYS = {"label", "stem_markdown"}
QUESTION_TYPES = {"single_choice", "multiple_choice", "fill_blank", "solution"}


def _strict_int(value, minimum=1, maximum=None):
    return (
        isinstance(value, int) and not isinstance(value, bool) and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _bounded_string(value, maximum, *, nonempty=False):
    return (
        isinstance(value, str) and len(value) <= maximum
        and (not nonempty or bool(value.strip()))
    )


def parse_candidate_output(
    raw, job_id, source_paper_id, expected_question_nos, expected_source_pages
):
    """Parse one exact JSON value and apply stricter local semantic checks."""
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise TypeError
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(raw)
        if raw[end:].strip() or not isinstance(value, dict) or set(value) != TOP_KEYS:
            raise TypeError
        expected = list(expected_question_nos)
        questions = value["questions"]
        if (
            value["version"] != 1 or isinstance(value["version"], bool)
            or value["import_job_id"] != job_id or isinstance(value["import_job_id"], bool)
            or value["source_paper_id"] != source_paper_id
            or isinstance(value["source_paper_id"], bool)
            or not _strict_int(value["question_count"], maximum=MAX_QUESTIONS)
            or not isinstance(questions, list)
            or value["question_count"] != len(questions) != 0
            or len(questions) != len(expected)
        ):
            raise TypeError
        found = []
        for index, question in enumerate(questions):
            if not isinstance(question, dict) or set(question) != QUESTION_KEYS:
                raise TypeError
            number = question["source_question_no"]
            if (
                not _bounded_string(number, 3, nonempty=True) or not number.isascii()
                or not number.isdigit() or number.startswith("0") or int(number) > 999
                or number != expected[index]
            ):
                raise TypeError
            found.append(number)
            if (
                not _bounded_string(question["stem_markdown"], MAX_STEM_LENGTH, nonempty=True)
                or question["question_type_code"] not in QUESTION_TYPES
                or question["primary_knowledge_point_code"] != ""
                or question["related_knowledge_point_codes"] != []
                or question["answer_markdown"] != ""
                or question["analysis_markdown"] != ""
                or not isinstance(question["figure_required"], bool)
                or question["extraction_confidence"] not in {"low", "medium", "high"}
            ):
                raise TypeError
            options = question["options"]
            if not isinstance(options, list) or len(options) > MAX_OPTIONS:
                raise TypeError
            option_codes = []
            for option in options:
                if (
                    not isinstance(option, dict) or set(option) != OPTION_KEYS
                    or not _bounded_string(option["code"], 16, nonempty=True)
                    or not _bounded_string(option["content"], MAX_ITEM_LENGTH, nonempty=True)
                ):
                    raise TypeError
                option_codes.append(option["code"].strip().casefold())
            if len(option_codes) != len(set(option_codes)):
                raise TypeError
            is_choice = question["question_type_code"] in {"single_choice", "multiple_choice"}
            if (is_choice and len(options) < 2) or (not is_choice and options):
                raise TypeError
            subquestions = question["subquestions"]
            if not isinstance(subquestions, list) or len(subquestions) > MAX_SUBQUESTIONS:
                raise TypeError
            labels = []
            for subquestion in subquestions:
                if (
                    not isinstance(subquestion, dict) or set(subquestion) != SUBQUESTION_KEYS
                    or not _bounded_string(subquestion["label"], 50, nonempty=True)
                    or not _bounded_string(
                        subquestion["stem_markdown"], MAX_ITEM_LENGTH, nonempty=True
                    )
                ):
                    raise TypeError
                labels.append(subquestion["label"].strip())
            if len(labels) != len(set(labels)):
                raise TypeError
            pages = question["source_pages"]
            manifest_pages = expected_source_pages.get(int(number))
            if (
                not isinstance(pages, list) or not pages or len(pages) > MAX_PAGES_PER_QUESTION
                or any(not _strict_int(page, maximum=10_000) for page in pages)
                or pages != sorted(set(pages)) or pages != manifest_pages
            ):
                raise TypeError
            warnings = question["warnings"]
            if (
                not isinstance(warnings, list) or len(warnings) > MAX_WARNINGS
                or any(not _bounded_string(item, 500) for item in warnings)
            ):
                raise TypeError
        if len(found) != len(set(found)):
            raise TypeError
        return value
    except CandidateExtractionError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeError) as error:
        raise CandidateExtractionError(SAFE_EXTRACTION_ERROR) from error


def _prompt(job_id, source_paper_id, question_numbers, source_pages):
    order = "、".join(f"Q{number:03d}" for number in question_numbers)
    page_mapping = json.dumps(
        {str(number): source_pages[number] for number in question_numbers},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"任务：只转录已验证的单题图片。import_job_id={job_id}，"
        f"source_paper_id={source_paper_id}。图片顺序及题号严格为：{order}。\n"
        f"source_pages 必须严格使用给定映射，不得自行推断或修改：{page_mapping}。\n"
        "只转录原图可见内容，不要猜，不要解题，不要补全缺失条件，不生成答案或解析。"
        "保留原有 LaTeX、公共条件、选项及小问层级；看不清的内容写入 warnings，"
        "并将 extraction_confidence 设为 low。题号必须与图片顺序一致；"
        "source_question_no 只写纯数字，不带 Q、不补零，例如图片 Q001 必须写为字符串 1。"
        "答案页不在输入中，answer_markdown 和 analysis_markdown 必须始终为空字符串。"
        "本阶段不分类知识点：primary_knowledge_point_code 必须为空字符串，"
        "related_knowledge_point_codes 必须为空数组。只输出符合给定 schema 的单一 JSON。"
    )


class CandidateCodexCliRunner:
    """Run Codex in a private read-only working directory with bounded I/O."""

    def __init__(self, executable=None, timeout=CODEX_TIMEOUT_SECONDS,
                 max_output_bytes=MAX_CODEX_OUTPUT_BYTES,
                 max_stderr_bytes=MAX_CODEX_STDERR_BYTES):
        self.executable = Path(executable).resolve() if executable else _resolve_codex_bin()
        details = self.executable.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise CandidateCodexExecutionError(SAFE_CODEX_MISSING)
        self.identity = (
            details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns,
        )
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.max_stderr_bytes = max_stderr_bytes

    def run(self, *, image_paths, prompt):
        details = self.executable.stat()
        identity = (
            details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns,
        )
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or identity != self.identity:
            raise CandidateCodexExecutionError(SAFE_CODEX_MISSING)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise CandidateCodexExecutionError(SAFE_EXTRACTION_ERROR)
        with tempfile.TemporaryDirectory(prefix="candidate-codex-") as temporary:
            root = Path(temporary)
            schema_path = root / "output-schema.json"
            message_path = root / "last-message.json"
            schema_path.write_text(json.dumps(_candidate_output_schema()), encoding="utf-8")
            command = [
                str(self.executable), "exec", "--sandbox", "read-only", "--ephemeral",
                "--ignore-user-config", "--ignore-rules", "--disable", "shell_tool",
                "--disable", "unified_exec", "--disable", "shell_snapshot",
                "--skip-git-repo-check", "--color", "never", "--cd", str(root),
                "--output-schema", str(schema_path), "--output-last-message",
                str(message_path), "--image",
                *(str(Path(path).resolve()) for path in image_paths), "--", prompt,
            ]
            environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
            for name in (
                "HOME", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            ):
                if os.environ.get(name):
                    environment[name] = os.environ[name]
            process = subprocess.Popen(
                command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                start_new_session=True,
            )
            try:
                try:
                    stdout, stderr = _bounded_communicate(
                        process, self.timeout, self.max_output_bytes, self.max_stderr_bytes
                    )
                except Exception:
                    _terminate_process_group(process)
                    raise
            finally:
                process.stdout.close()
                process.stderr.close()
            if process.returncode != 0:
                raise CandidateCodexExecutionError(SAFE_EXTRACTION_ERROR)
            try:
                content = message_path.read_bytes()
            except OSError as error:
                raise CandidateCodexExecutionError(SAFE_EXTRACTION_ERROR) from error
            if not content or len(content) > self.max_output_bytes:
                raise CandidateCodexExecutionError(SAFE_EXTRACTION_ERROR)
            try:
                final = content.decode("utf-8")
            except UnicodeError as error:
                raise CandidateCodexExecutionError(SAFE_EXTRACTION_ERROR) from error
            run_id = "codex-" + hashlib.sha256(stdout + stderr + content).hexdigest()[:24]
            return CandidateExtractionRunResult(final, run_id)


def _open_job_and_lock(private_root, job_id):
    descriptors = []
    job_fd = lock_fd = None
    try:
        root_fd = _open_safe_directory(Path(private_root))
        descriptors.append(root_fd)
        processing_fd = _open_child_directory(root_fd, "processing")
        descriptors.append(processing_fd)
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(".candidate_extraction.lock", flags, 0o600, dir_fd=job_fd)
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError("unsafe candidate lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            os.close(job_fd)
            job_fd = None
            return None, None
        return job_fd, lock_fd
    except OSError as error:
        for descriptor in (lock_fd, job_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise CandidateExtractionError(SAFE_INPUT_INVALID) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _database_input(database_path, job_id):
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            """SELECT j.status,j.source_paper_id,s.status,s.question_count,
                      s.crop_manifest_sha256,s.crop_generation_id,s.crop_manifest_signature
               FROM import_jobs j
               LEFT JOIN import_question_split_runs s ON s.import_job_id=j.id
               WHERE j.id=?""", (job_id,)
        ).fetchone()


def _read_valid_inputs(job_fd, job_dir, job_id, row, temporary_root):
    manifest_snapshot = read_file_at(
        job_fd, "question_crops.json", max_bytes=16 * 1024 * 1024
    )
    if manifest_snapshot.sha256 != row[4]:
        raise CandidateExtractionError(SAFE_INPUT_INVALID)
    manifest = validate_signed_manifest(
        json.loads(manifest_snapshot.data.decode("utf-8")), load_hmac_key(job_dir),
        expected_job_id=job_id,
        expected_question_nos=list(range(1, row[3] + 1)),
    )
    if manifest["generation_id"] != row[5] or manifest["signature"] != row[6]:
        raise CandidateExtractionError(SAFE_INPUT_INVALID)
    try:
        validate_current_crop_review(
            job_fd, load_hmac_key(job_dir), manifest, manifest_snapshot.sha256,
        )
    except (CropReviewError, SecureCropArtifactError) as error:
        raise CandidateExtractionError(SAFE_INPUT_INVALID) from error
    image_paths = []
    pages = {}
    total = 0
    for entry in manifest["questions"]:
        if entry["review_status"] != "ai_review_passed":
            raise CandidateExtractionError(SAFE_INPUT_REQUIRED)
        number = entry["question_no"]
        expected = f"question_crops/Q{number:03d}.png"
        if entry["output_relative_path"] != expected:
            raise CandidateExtractionError(SAFE_INPUT_INVALID)
        snapshot = read_file_at(job_fd, expected, max_bytes=MAX_CROP_BYTES)
        total += snapshot.size
        if (
            total > MAX_TOTAL_CROP_BYTES or snapshot.size != entry["byte_size"]
            or snapshot.sha256 != entry["sha256"]
        ):
            raise CandidateExtractionError(SAFE_INPUT_INVALID)
        try:
            with Image.open(io.BytesIO(snapshot.data)) as image:
                image.load()
                if (
                    image.format != "PNG" or image.size != (entry["width"], entry["height"])
                    or max(image.size) > MAX_IMAGE_DIMENSION
                ):
                    raise CandidateExtractionError(SAFE_INPUT_INVALID)
        except (UnidentifiedImageError, OSError) as error:
            raise CandidateExtractionError(SAFE_INPUT_INVALID) from error
        output = temporary_root / f"Q{number:03d}.png"
        with output.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(snapshot.data)
            stream.flush()
            os.fsync(stream.fileno())
        image_paths.append(output)
        pages[number] = sorted({region["page_number"] for region in entry["regions"]})
    return manifest_snapshot.sha256, manifest, tuple(image_paths), pages


def _valid_completed_output(job_fd, row, job_id, source_paper_id, numbers, pages, anchors):
    if (
        row is None or row[0] != "completed"
        or row[1] != len(numbers) or row[2] != len(numbers)
        or tuple(row[4:7]) != anchors
    ):
        return False
    try:
        snapshot = read_file_at(job_fd, "candidate_questions.json", max_bytes=MAX_CANDIDATE_BYTES)
        if snapshot.sha256 != row[7] or snapshot.size != row[8]:
            return False
        parse_candidate_output(
            snapshot.data.decode("utf-8"), job_id, source_paper_id,
            [str(number) for number in numbers], pages,
        )
        return True
    except (CandidateExtractionError, SecureCropArtifactError, UnicodeError):
        return False


def claim_candidate_extraction(database_path, private_root, job_id, runner=None):
    if not _strict_int(job_id):
        raise CandidateExtractionError("候选识别任务参数无效")
    database_path, private_root = Path(database_path), Path(private_root)
    row = _database_input(database_path, job_id)
    if row is None:
        raise CandidateExtractionError("未找到导入任务")
    if (
        row[0] != "pending" or row[2] != "completed"
        or not _strict_int(row[3], maximum=MAX_QUESTIONS)
        or any(value is None for value in row[4:7])
    ):
        raise CandidateExtractionError(SAFE_INPUT_REQUIRED)
    job_fd = lock_fd = None
    temporary = None
    try:
        job_fd, lock_fd = _open_job_and_lock(private_root, job_id)
        if job_fd is None:
            return None
        temporary = tempfile.TemporaryDirectory(prefix="candidate-input-")
        root = Path(temporary.name)
        digest, manifest, images, pages = _read_valid_inputs(
            job_fd, Path(private_root) / "processing" / f"import_job_{job_id}",
            job_id, row, root,
        )
        numbers = tuple(range(1, row[3] + 1))
        with closing(sqlite3.connect(database_path)) as connection:
            completed = connection.execute(
                """SELECT status,question_count,processed_questions,codex_run_id,
                          input_crop_generation_id,input_manifest_sha256,
                          input_manifest_signature,output_sha256,output_byte_size
                   FROM import_candidate_extraction_runs WHERE import_job_id=?""",
                (job_id,),
            ).fetchone()
        anchors = (manifest["generation_id"], digest, manifest["signature"])
        if _valid_completed_output(
            job_fd, completed, job_id, row[1], numbers, pages, anchors
        ):
            CandidateExtractionClaim(
                database_path, private_root, job_id, row[1], runner, images, numbers,
                pages, digest, manifest["generation_id"], manifest["signature"],
                temporary, lock_fd, job_fd,
            ).close()
            return None
        if runner is None:
            runner = CandidateCodexCliRunner()
        now = _now()
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT j.status,s.status,s.question_count,s.crop_manifest_sha256,
                          s.crop_generation_id,s.crop_manifest_signature
                   FROM import_jobs j JOIN import_question_split_runs s ON s.import_job_id=j.id
                   WHERE j.id=?""", (job_id,)
            ).fetchone()
            if current != (
                "pending", "completed", len(numbers), digest,
                manifest["generation_id"], manifest["signature"],
            ):
                raise CandidateExtractionError(SAFE_INPUT_INVALID)
            connection.execute(
                """INSERT INTO import_candidate_extraction_runs
                   (import_job_id,status,processed_questions,error_message,started_at,updated_at)
                   VALUES (?,'processing',0,NULL,?,?)
                   ON CONFLICT(import_job_id) DO UPDATE SET status='processing',
                     question_count=NULL,processed_questions=0,error_message=NULL,
                     codex_run_id=NULL,input_crop_generation_id=NULL,
                     input_manifest_sha256=NULL,input_manifest_signature=NULL,
                     output_sha256=NULL,output_byte_size=NULL,started_at=excluded.started_at,
                     completed_at=NULL,updated_at=excluded.updated_at""",
                (job_id, now, now),
            )
            connection.commit()
        return CandidateExtractionClaim(
            database_path, private_root, job_id, row[1], runner, images, numbers,
            pages, digest, manifest["generation_id"], manifest["signature"],
            temporary, lock_fd, job_fd,
        )
    except Exception:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        if job_fd is not None:
            os.close(job_fd)
        if temporary is not None:
            temporary.cleanup()
        raise


def _atomic_bytes(job_fd, name, content):
    temporary_name = f".{name}.{os.getpid()}.tmp"
    descriptor = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=job_fd)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_name, name, src_dir_fd=job_fd, dst_dir_fd=job_fd)
        os.fsync(job_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=job_fd)
        except FileNotFoundError:
            pass


def _mark_failed(database_path, job_id):
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                """UPDATE import_candidate_extraction_runs SET status='failed',
                          error_message=?,completed_at=NULL,updated_at=?
                   WHERE import_job_id=?""",
                (SAFE_EXTRACTION_ERROR, _now(), job_id),
            )
            connection.commit()
    except sqlite3.Error:
        pass


def _execute_claim(claim):
    response = claim.runner.run(
        image_paths=claim.image_paths,
        prompt=_prompt(
            claim.job_id, claim.source_paper_id, claim.question_numbers,
            claim.source_pages,
        ),
    )
    if not isinstance(response, CandidateExtractionRunResult):
        raise CandidateExtractionError(SAFE_EXTRACTION_ERROR)
    value = parse_candidate_output(
        response.final_message, claim.job_id, claim.source_paper_id,
        [str(number) for number in claim.question_numbers], claim.source_pages,
    )
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(content) > MAX_CANDIDATE_BYTES:
        raise CandidateExtractionError(SAFE_EXTRACTION_ERROR)
    previous = None
    try:
        previous = read_file_at(
            claim.job_fd, "candidate_questions.json", max_bytes=MAX_CANDIDATE_BYTES
        ).data
    except SecureCropArtifactError:
        try:
            os.stat("candidate_questions.json", dir_fd=claim.job_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CandidateExtractionError(SAFE_EXISTING_ERROR)
    _atomic_bytes(claim.job_fd, "candidate_questions.json", content)
    digest = hashlib.sha256(content).hexdigest()
    now = _now()
    try:
        with closing(sqlite3.connect(claim.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT status,question_count,crop_manifest_sha256,crop_generation_id,
                          crop_manifest_signature FROM import_question_split_runs
                   WHERE import_job_id=?""", (claim.job_id,)
            ).fetchone()
            if current != (
                "completed", len(claim.question_numbers), claim.manifest_sha256,
                claim.generation_id, claim.manifest_signature,
            ):
                raise CandidateExtractionError(SAFE_INPUT_INVALID)
            connection.execute(
                """UPDATE import_candidate_extraction_runs SET status='completed',
                          question_count=?,processed_questions=?,error_message=NULL,
                          codex_run_id=?,input_crop_generation_id=?,input_manifest_sha256=?,
                          input_manifest_signature=?,output_sha256=?,output_byte_size=?,
                          completed_at=?,updated_at=? WHERE import_job_id=?""",
                (
                    len(claim.question_numbers), len(claim.question_numbers), response.run_id,
                    claim.generation_id, claim.manifest_sha256, claim.manifest_signature,
                    digest, len(content), now, now, claim.job_id,
                ),
            )
            connection.commit()
    except Exception:
        if previous is None:
            try:
                os.unlink("candidate_questions.json", dir_fd=claim.job_fd)
                os.fsync(claim.job_fd)
            except FileNotFoundError:
                pass
        else:
            _atomic_bytes(claim.job_fd, "candidate_questions.json", previous)
        raise
    return value


def run_claimed_candidate_extraction(claim):
    if not isinstance(claim, CandidateExtractionClaim) or claim.lock_fd is None:
        raise CandidateExtractionError("候选识别任务无效")
    try:
        try:
            return _execute_claim(claim)
        except Exception:
            _mark_failed(claim.database_path, claim.job_id)
            return None
    finally:
        claim.close()


def load_completed_candidates(database_path, private_root, job_id):
    """Read a DB-anchored completed result without creating files or status rows."""
    database_path, private_root = Path(database_path), Path(private_root)
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            """SELECT j.source_paper_id,s.status,s.question_count,
                      s.crop_manifest_sha256,s.crop_generation_id,s.crop_manifest_signature,
                      c.status,c.input_crop_generation_id,c.input_manifest_sha256,
                      c.input_manifest_signature,c.output_sha256,c.output_byte_size
               FROM import_jobs j JOIN import_question_split_runs s ON s.import_job_id=j.id
               JOIN import_candidate_extraction_runs c ON c.import_job_id=j.id
               WHERE j.id=?""", (job_id,)
        ).fetchone()
    if (
        row is None or row[1] != "completed" or row[6] != "completed"
        or (row[4], row[3], row[5]) != (row[7], row[8], row[9])
    ):
        raise CandidateExtractionError(SAFE_EXISTING_ERROR)
    descriptors = []
    try:
        root_fd = _open_safe_directory(private_root)
        descriptors.append(root_fd)
        processing_fd = _open_child_directory(root_fd, "processing")
        descriptors.append(processing_fd)
        job_fd = _open_child_directory(processing_fd, f"import_job_{job_id}")
        descriptors.append(job_fd)
        manifest_snapshot = read_file_at(
            job_fd, "question_crops.json", max_bytes=16 * 1024 * 1024
        )
        if manifest_snapshot.sha256 != row[3]:
            raise CandidateExtractionError(SAFE_EXISTING_ERROR)
        manifest = validate_signed_manifest(
            json.loads(manifest_snapshot.data.decode("utf-8")),
            load_hmac_key(private_root / "processing" / f"import_job_{job_id}"),
            expected_job_id=job_id,
            expected_question_nos=list(range(1, row[2] + 1)),
        )
        if manifest["generation_id"] != row[4] or manifest["signature"] != row[5]:
            raise CandidateExtractionError(SAFE_EXISTING_ERROR)
        try:
            validate_current_crop_review(
                job_fd, load_hmac_key(private_root / "processing" / f"import_job_{job_id}"),
                manifest, manifest_snapshot.sha256,
            )
        except (CropReviewError, SecureCropArtifactError) as error:
            raise CandidateExtractionError(SAFE_EXISTING_ERROR) from error
        pages = {
            entry["question_no"]: sorted({region["page_number"] for region in entry["regions"]})
            for entry in manifest["questions"]
        }
        snapshot = read_file_at(
            job_fd, "candidate_questions.json", max_bytes=MAX_CANDIDATE_BYTES
        )
        if snapshot.sha256 != row[10] or snapshot.size != row[11]:
            raise CandidateExtractionError(SAFE_EXISTING_ERROR)
        return parse_candidate_output(
            snapshot.data.decode("utf-8"), job_id, row[0],
            [str(number) for number in range(1, row[2] + 1)], pages,
        )
    except CandidateExtractionError:
        raise
    except (OSError, SecureCropArtifactError, UnicodeError, json.JSONDecodeError) as error:
        raise CandidateExtractionError(SAFE_EXISTING_ERROR) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
