"""Durable Web coordination for strict whole-batch admission and finalization."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from src.importing.admit_questions import (
    AdmissionError,
    admit_questions,
    assess_job,
    backup_database,
)
from src.reviewing.finalize import finalize_review
from src.reviewing.knowledge_classification import load_bound_knowledge_classification


SAFE_APPLY_FAILED = "严格整批入库未完成，可安全重试"
SAFE_BACKUP_STALE = "数据库已发生其他业务变化，旧备份不再适用于当前状态"
SAFE_FINALIZE_FAILED = "正式题已安全入库，任务收口未完成，可安全继续"
SAFE_COMPLETED_DRIFT = "正式题内容与完成时锚点不一致"
SAFE_BUSY = "任务正由另一个请求处理，请稍后刷新"
SAFE_NOT_READY = "当前任务不满足严格整批入库条件"
LEASE_SECONDS = 300

REASON_NAMES = {
    "human_required": "需要人工确认",
    "disputed": "视觉审核存在争议",
    "audit_missing": "视觉审核缺失",
    "audit_confidence_not_high": "视觉审核置信度不足",
    "audit_issues_present": "视觉审核仍有问题",
    "audit_corrections_present": "视觉审核仍有修正建议",
    "candidate_deleted": "候选题已删除",
    "empty_stem": "题干缺失",
    "invalid_question_type": "题型无效",
    "missing_knowledge_point": "知识点无效或缺失",
    "knowledge_classification_missing": "缺少与当前草稿绑定的分类证据",
    "missing_question_crop": "缺少完整题图",
    "missing_approved_figure": "缺少已通过审核的必要配图",
    "answer_status_not_passed": "已提供答案未通过审核",
    "analysis_status_not_passed": "已提供解析未通过审核",
    "answer_analysis_sha256_mismatch": "答案或解析与审核证据不匹配",
    "batch_ai_approval_not_anchored": "整批视觉审核证据未锚定",
    "human_approval_status_invalid": "人工审核草稿未批准",
    "human_approval_source_invalid": "人工批准来源无效",
    "human_approval_evidence_invalid": "人工批准证据无效",
    "human_approval_source_binding_invalid": "人工批准未绑定当前候选版本",
    "human_approval_edited_json_invalid": "已批准草稿结构无效",
    "human_approval_question_identity_invalid": "已批准草稿题号不匹配",
    "human_approval_immutable_fields_invalid": "已批准草稿改变了受保护来源字段",
    "ai_approval_provenance_invalid": "AI二审批准证据无效",
}


class WebAdmissionError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AdmissionPage:
    job_id: int
    paper_name: str
    job_status: str
    candidate_count: int
    evidence_count: int
    formal_count: int
    eligible_count: int
    ineligible_count: int
    ineligible: tuple[dict, ...]
    stage: str
    safe_error: str | None
    can_apply: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _digest(values) -> str:
    raw = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_page_error(run) -> str | None:
    if run is None:
        return None
    if run["stage"] == "admitted_pending_finalize":
        return SAFE_FINALIZE_FAILED
    if run["status"] == "failed":
        return SAFE_APPLY_FAILED
    return None


def _snapshot_value(value):
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise TypeError("unsupported SQLite value")


def _business_snapshot_digest(connection: sqlite3.Connection) -> str:
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name!='import_web_admission_runs' ORDER BY name"
    )]
    core = {"import_jobs", "questions", "source_papers"}
    if not core.issubset(tables):
        raise sqlite3.DatabaseError("missing core schema")
    snapshot = []
    for table in tables:
        quoted_table = '"' + table.replace('"', '""') + '"'
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
        if not columns:
            raise sqlite3.DatabaseError("unreadable table")
        rows = [
            [_snapshot_value(value) for value in row]
            for row in connection.execute(f"SELECT * FROM {quoted_table}")
        ]
        rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
        snapshot.append([table, columns, rows])
    return hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _formal_batch_digest(connection: sqlite3.Connection, job_id: int) -> str:
    """Hash protected admission values while excluding soft-delete lifecycle state."""
    question_ids_sql = (
        "SELECT question_id FROM question_sources WHERE import_job_id=?"
    )
    lifecycle_columns = {"deleted_at", "deletion_reason", "deletion_note", "updated_at"}
    question_columns = [
        row[1] for row in connection.execute('PRAGMA table_info("questions")')
        if row[1] not in lifecycle_columns
    ]
    quoted_question_columns = ",".join(
        '"' + column.replace('"', '""') + '"' for column in question_columns
    )
    table_queries = (
        (
            "questions",
            f"SELECT {quoted_question_columns} FROM questions "
            f"WHERE id IN ({question_ids_sql})",
            (job_id,),
            question_columns,
        ),
        ("question_options", f"SELECT * FROM question_options WHERE question_id IN ({question_ids_sql})", (job_id,), None),
        ("subquestions", f"SELECT * FROM subquestions WHERE question_id IN ({question_ids_sql})", (job_id,), None),
        ("question_formulas", f"SELECT * FROM question_formulas WHERE question_id IN ({question_ids_sql})", (job_id,), None),
        ("question_figures", f"SELECT * FROM question_figures WHERE question_id IN ({question_ids_sql})", (job_id,), None),
        ("question_tags", f"SELECT * FROM question_tags WHERE question_id IN ({question_ids_sql})", (job_id,), None),
        (
            "question_related_knowledge_points",
            f"SELECT * FROM question_related_knowledge_points WHERE question_id IN ({question_ids_sql})",
            (job_id,),
            None,
        ),
        ("question_assets", "SELECT * FROM question_assets WHERE import_job_id=?", (job_id,), None),
        ("question_sources", "SELECT * FROM question_sources WHERE import_job_id=?", (job_id,), None),
        (
            "question_reviews",
            f"SELECT * FROM question_reviews WHERE question_id IN ({question_ids_sql})",
            (job_id,),
            None,
        ),
        ("question_versions", f"SELECT * FROM question_versions WHERE question_id IN ({question_ids_sql})", (job_id,), None),
    )
    snapshot = []
    for table, sql, params, selected_columns in table_queries:
        columns = selected_columns or [
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
        rows = [
            [_snapshot_value(value) for value in row]
            for row in connection.execute(sql, params)
        ]
        rows.sort(key=lambda row: json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        snapshot.append([table, columns, rows])
    raw = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _verify_formal_batch(connection, run, job_id: int) -> None:
    expected = (
        run["formal_batch_digest"]
        if "formal_batch_digest" in run.keys() else None
    )
    actual = _formal_batch_digest(connection, job_id)
    if not isinstance(expected, str) or not secrets.compare_digest(expected, actual):
        raise WebAdmissionError(SAFE_COMPLETED_DRIFT, status_code=409)


def _database_snapshot_digest(database_path: Path) -> str:
    uri = f"file:{quote(str(database_path), safe='/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
        connection.execute("BEGIN")
        try:
            digest = _business_snapshot_digest(connection)
            connection.commit()
            return digest
        except Exception:
            connection.rollback()
            raise


def _validate_backup(
    database_path: Path, result, *, error_message: str,
    expected_snapshot_digest: str,
) -> tuple[Path, str, str, str]:
    """Validate an untrusted backup result before persisting its anchor."""
    try:
        backup_path, supplied_digest = result
        backup_path = Path(backup_path)
    except (TypeError, ValueError) as exc:
        raise WebAdmissionError(error_message, status_code=500) from exc
    backup_root = database_path.parent / "backups"
    try:
        root = backup_root.resolve(strict=True)
        resolved = backup_path.resolve(strict=True)
        relative_to_root = resolved.relative_to(root)
        details = os.lstat(backup_path)
    except (OSError, ValueError) as exc:
        raise WebAdmissionError(error_message, status_code=500) from exc
    valid_digest = (
        isinstance(supplied_digest, str) and len(supplied_digest) == 64
        and all(character in "0123456789abcdef" for character in supplied_digest)
    )
    if (
        not relative_to_root.parts or ".." in relative_to_root.parts
        or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
        or not valid_digest or _sha256_file(resolved) != supplied_digest
    ):
        raise WebAdmissionError(error_message, status_code=500)
    try:
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise sqlite3.DatabaseError("backup quick_check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.DatabaseError("backup foreign keys failed")
            snapshot_digest = _business_snapshot_digest(connection)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise WebAdmissionError(error_message, status_code=500) from exc
    if snapshot_digest != expected_snapshot_digest:
        raise WebAdmissionError(error_message, status_code=500)
    relative = resolved.relative_to(database_path.parent.resolve()).as_posix()
    return resolved, relative, supplied_digest, snapshot_digest


def _backup_snapshot(database_path: Path) -> set[Path]:
    root = database_path.parent / "backups"
    try:
        return {path.absolute() for path in root.iterdir()}
    except FileNotFoundError:
        return set()


def _anchored_backups(database_path: Path) -> set[Path]:
    with closing(_connect(database_path)) as connection:
        rows = connection.execute(
            """SELECT backup_relative_path FROM import_web_admission_runs
               WHERE backup_relative_path IS NOT NULL
               UNION SELECT finalize_backup_relative_path FROM import_web_admission_runs
               WHERE finalize_backup_relative_path IS NOT NULL"""
        ).fetchall()
    return {(database_path.parent / row[0]).absolute() for row in rows}


def _cleanup_new_backup(database_path: Path, candidate, before: set[Path]) -> None:
    """Best-effort unlink only a newly returned, unanchored entry in backups."""
    try:
        path = Path(candidate[0]).absolute()
        root = (database_path.parent / "backups").absolute()
        relative = path.relative_to(root)
        if ".." in relative.parts:
            return
        resolved_root = root.resolve(strict=True)
        resolved_parent = path.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
        if path in before or path in _anchored_backups(database_path):
            return
        path.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError, sqlite3.Error):
        pass


def _job_row(connection, job_id: int):
    return connection.execute(
        """SELECT j.id,j.status,s.paper_name,s.sha256 AS source_sha256 FROM import_jobs j
           JOIN source_papers s ON s.id=j.source_paper_id WHERE j.id=?""",
        (job_id,),
    ).fetchone()


def _evidence_numbers(connection, job_id: int) -> set[str]:
    return {
        row[0] for row in connection.execute(
            "SELECT source_question_no FROM candidate_knowledge_classifications "
            "WHERE import_job_id=?",
            (job_id,),
        )
    }


def _bound_evidence_numbers(connection, job_id: int) -> set[str]:
    numbers = set()
    for row in connection.execute(
        """SELECT source_question_no,edited_json,status,version,approval_source
           FROM candidate_review_drafts
           WHERE import_job_id=? AND deleted_at IS NULL""",
        (job_id,),
    ):
        draft = dict(row)
        if load_bound_knowledge_classification(
            connection, job_id, row["source_question_no"], draft
        ) is not None:
            numbers.add(row["source_question_no"])
    return numbers


def _is_exact_completed_batch(connection, job, expected_numbers: set[str]) -> bool:
    rows = connection.execute(
        """SELECT q.question_code,s.source_question_no
           FROM question_sources s JOIN questions q ON q.id=s.question_id
           WHERE s.import_job_id=?""",
        (job["id"],),
    ).fetchall()
    actual_numbers = {row["source_question_no"] for row in rows}
    return bool(
        expected_numbers and len(rows) == len(expected_numbers)
        and actual_numbers == expected_numbers
        and all(
            row["question_code"] == (
                f"Q-{job['source_sha256'][:16]}-{int(row['source_question_no']):03d}"
            )
            for row in rows
        )
        and not connection.execute("PRAGMA foreign_key_check").fetchall()
    )


def load_admission_page(database_path, private_root, job_id: int) -> AdmissionPage:
    """Read a page model without creating rows, backups, or artifacts."""
    database_path = Path(database_path)
    with closing(_connect(database_path)) as connection:
        job = _job_row(connection, job_id)
        if job is None:
            raise WebAdmissionError("未找到导入任务", status_code=404)
        run = connection.execute(
            "SELECT * FROM import_web_admission_runs WHERE import_job_id=?",
            (job_id,),
        ).fetchone()
        formal_count = connection.execute(
            "SELECT COUNT(*) FROM question_sources WHERE import_job_id=?", (job_id,)
        ).fetchone()[0]
        evidence = _evidence_numbers(connection, job_id)
        formal_numbers = {
            row[0] for row in connection.execute(
                "SELECT source_question_no FROM question_sources WHERE import_job_id=?",
                (job_id,),
            )
        }
        if run is not None and run["status"] == "completed" and (
            job["status"] != "completed" or run["stage"] != "completed"
        ):
            raise WebAdmissionError(SAFE_COMPLETED_DRIFT, status_code=409)
        if job["status"] == "completed" and run is not None:
            if (run["status"], run["stage"]) != ("completed", "completed"):
                raise WebAdmissionError(SAFE_COMPLETED_DRIFT, status_code=409)
            _verify_formal_batch(connection, run, job_id)
            if not _is_exact_completed_batch(connection, job, formal_numbers):
                raise WebAdmissionError(SAFE_COMPLETED_DRIFT, status_code=409)
            return AdmissionPage(
                job_id, job["paper_name"], job["status"], len(formal_numbers),
                len(evidence), formal_count, formal_count, 0, (), "completed",
                None, False,
            )
        if (
            job["status"] == "completed" and run is None
            and _is_exact_completed_batch(connection, job, formal_numbers)
        ):
            return AdmissionPage(
                job_id, job["paper_name"], job["status"], len(formal_numbers),
                len(evidence), formal_count, formal_count, 0, (), "completed",
                None, False,
            )

    try:
        report = assess_job(database_path, private_root, job_id)
    except (AdmissionError, sqlite3.Error, OSError) as exc:
        raise WebAdmissionError("严格准入评估暂时无法完成", status_code=409) from exc
    numbers = {item.question_no for item in (*report.eligible, *report.ineligible)}
    blocked = tuple({
        "question_no": item.question_no,
        "reasons": tuple(REASON_NAMES.get(reason, "未通过受保护的准入校验") for reason in item.reasons),
    } for item in report.ineligible)
    with closing(_connect(database_path)) as connection:
        job = _job_row(connection, job_id)
        run = connection.execute(
            "SELECT stage,status,safe_error FROM import_web_admission_runs WHERE import_job_id=?",
            (job_id,),
        ).fetchone()
        evidence = _bound_evidence_numbers(connection, job_id)
        all_evidence = _evidence_numbers(connection, job_id)
        formal_count = connection.execute(
            "SELECT COUNT(*) FROM question_sources WHERE import_job_id=?", (job_id,)
        ).fetchone()[0]
        authoritative_completed = bool(
            job and job["status"] == "completed"
            and _is_exact_completed_batch(connection, job, numbers)
        )
    if authoritative_completed:
        return AdmissionPage(
            job_id, job["paper_name"], job["status"], len(numbers), len(evidence),
            formal_count, len(numbers), 0, (), "completed", None, False,
        )
    stage = run["stage"] if run else "pending"
    can_apply = bool(
        job and job["status"] == "needs_review" and not report.ineligible
        and {item.question_no for item in report.eligible} == numbers
        and len(report.eligible) == len(numbers)
        and all_evidence == numbers and evidence == numbers
        and stage in {"pending", "processing", "admitted_pending_finalize", "failed"}
    )
    return AdmissionPage(
        job_id, job["paper_name"], job["status"], len(numbers), len(evidence),
        formal_count, len(report.eligible), len(report.ineligible), blocked, stage,
        _safe_page_error(run), can_apply,
    )


def _renew(
    database_path: Path, job_id: int, token: str, *, lease_seconds: float,
) -> None:
    now = _now()
    heartbeat = _iso(now)
    lease = _iso(now + timedelta(seconds=lease_seconds))
    _claimed_update(
        database_path, job_id, token,
        """UPDATE import_web_admission_runs SET heartbeat_at=?,lease_expires_at=?,
           updated_at=?""",
        (heartbeat, lease, heartbeat),
    )


class _LeaseKeeper:
    """Renew a claim while one blocking external stage is running."""

    def __init__(
        self, database_path: Path, job_id: int, token: str, *,
        lease_seconds: float, interval: float,
    ):
        if lease_seconds <= 0 or interval <= 0 or interval >= lease_seconds:
            raise ValueError("invalid lease keeper interval")
        self.database_path = database_path
        self.job_id = job_id
        self.token = token
        self.lease_seconds = lease_seconds
        self.interval = interval
        self._stop = threading.Event()
        self._lost: BaseException | None = None
        self._thread: threading.Thread | None = None

    def checkpoint(self) -> None:
        if self._lost is not None:
            raise WebAdmissionError(SAFE_BUSY, status_code=409) from self._lost
        try:
            _renew(
                self.database_path, self.job_id, self.token,
                lease_seconds=self.lease_seconds,
            )
        except (sqlite3.Error, WebAdmissionError) as exc:
            self._lost = exc
            raise WebAdmissionError(SAFE_BUSY, status_code=409) from exc

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                _renew(
                    self.database_path, self.job_id, self.token,
                    lease_seconds=self.lease_seconds,
                )
            except (sqlite3.Error, WebAdmissionError) as exc:
                self._lost = exc
                self._stop.set()

    def __enter__(self):
        self.checkpoint()
        self._thread = threading.Thread(
            target=self._run, name=f"web-admission-lease-{self.job_id}", daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def _run_external(
    database_path: Path, job_id: int, token: str, function, *,
    lease_seconds: float, keeper_interval: float, checkpoint: bool = True,
):
    with _LeaseKeeper(
        database_path, job_id, token, lease_seconds=lease_seconds,
        interval=keeper_interval,
    ) as keeper:
        result = function()
        if checkpoint:
            keeper.checkpoint()
        return result


def _claim(
    database_path: Path, private_root: Path, job_id: int, *, backup_fn,
    lease_seconds: float, keeper_interval: float,
) -> tuple[str | None, str, str | None]:
    """Create a short coordination claim, then create and anchor the first backup."""
    page = load_admission_page(database_path, private_root, job_id)
    now = _now()
    token = secrets.token_hex(32)
    connection = _connect(database_path)
    needs_backup = False
    source_digest = None
    anchored_backup = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = _job_row(connection, job_id)
        if job is None:
            raise WebAdmissionError("未找到导入任务", status_code=404)
        run = connection.execute(
            "SELECT * FROM import_web_admission_runs WHERE import_job_id=?", (job_id,)
        ).fetchone()
        if run and run["status"] == "completed":
            if job["status"] != "completed" or run["stage"] != "completed":
                raise WebAdmissionError(SAFE_COMPLETED_DRIFT, status_code=409)
            connection.rollback()
            return None, "completed", None
        if job["status"] == "completed":
            if run is not None:
                raise WebAdmissionError(SAFE_COMPLETED_DRIFT, status_code=409)
            connection.rollback()
            return None, "completed", None
        if run and run["status"] == "processing" and run["lease_expires_at"] > _iso(now):
            raise WebAdmissionError(SAFE_BUSY, status_code=409)
        recoverable_ready = bool(
            run and run["stage"] in {
                "preparing_backup", "processing", "admitted_pending_finalize", "failed"
            }
            and page.job_status == "needs_review" and page.ineligible_count == 0
            and page.eligible_count == page.candidate_count
            and page.evidence_count == page.candidate_count
        )
        if not page.can_apply and not recoverable_ready:
            raise WebAdmissionError(SAFE_NOT_READY, status_code=409)
        lease = _iso(now + timedelta(seconds=lease_seconds))
        if run is None:
            source_digest = _business_snapshot_digest(connection)
            connection.execute(
                """INSERT INTO import_web_admission_runs
                   (import_job_id,status,stage,claim_token,expected_count,
                    pre_backup_source_digest,claimed_at,heartbeat_at,
                    lease_expires_at,created_at,updated_at)
                   VALUES(?,'processing','preparing_backup',?,?,?,?,?,?,?,?)""",
                (job_id, token, page.candidate_count, source_digest,
                 _iso(now), _iso(now), lease,
                 _iso(now), _iso(now)),
            )
            stage = "preparing_backup"
            needs_backup = True
        else:
            stage = "processing" if run["stage"] == "failed" else run["stage"]
            if (
                run["backup_relative_path"] is None
                or run["pre_backup_source_digest"] is None
                or run["backup_snapshot_digest"] is None
            ):
                stage = "preparing_backup"
                needs_backup = True
                source_digest = _business_snapshot_digest(connection)
            else:
                anchored_backup = (
                    database_path.parent / run["backup_relative_path"],
                    run["backup_sha256"],
                )
                source_digest = run["pre_backup_source_digest"]
            connection.execute(
                """UPDATE import_web_admission_runs SET status='processing',claim_token=?,
                   stage=?,safe_error=NULL,pre_backup_source_digest=COALESCE(?,pre_backup_source_digest),
                   claimed_at=?,heartbeat_at=?,lease_expires_at=?,updated_at=?
                   WHERE import_job_id=?""",
                (token, stage, source_digest, _iso(now), _iso(now), lease, _iso(now), job_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if not needs_backup:
        if not source_digest or anchored_backup is None:
            _mark_failed(database_path, job_id, token, admitted=stage == "admitted_pending_finalize")
            raise WebAdmissionError(SAFE_APPLY_FAILED, status_code=500)
        if (
            stage != "admitted_pending_finalize"
            and _database_snapshot_digest(database_path) != source_digest
        ):
            _mark_failed(database_path, job_id, token, admitted=False)
            raise WebAdmissionError(SAFE_BACKUP_STALE, status_code=409)
        _, _, _, snapshot_digest = _validate_backup(
            database_path, anchored_backup, error_message=SAFE_APPLY_FAILED,
            expected_snapshot_digest=source_digest,
        )
        if snapshot_digest != run["backup_snapshot_digest"]:
            _mark_failed(database_path, job_id, token, admitted=stage == "admitted_pending_finalize")
            raise WebAdmissionError(SAFE_APPLY_FAILED, status_code=500)
        _renew(database_path, job_id, token, lease_seconds=lease_seconds)
        return token, stage, source_digest

    before = _backup_snapshot(database_path)
    created_backup = None
    try:
        created_backup = _run_external(
            database_path, job_id, token, lambda: backup_fn(database_path),
            lease_seconds=lease_seconds, keeper_interval=keeper_interval,
        )
        _, relative, digest, snapshot_digest = _validate_backup(
            database_path, created_backup, error_message=SAFE_APPLY_FAILED,
            expected_snapshot_digest=source_digest,
        )
        if _database_snapshot_digest(database_path) != source_digest:
            raise WebAdmissionError(SAFE_APPLY_FAILED, status_code=500)
        now_text = _iso(_now())
        _claimed_update(
            database_path, job_id, token,
            """UPDATE import_web_admission_runs SET stage='processing',
               backup_relative_path=?,backup_sha256=?,backup_snapshot_digest=?,heartbeat_at=?,
               lease_expires_at=?,updated_at=?""",
            (relative, digest, snapshot_digest, now_text,
             _iso(_now() + timedelta(seconds=lease_seconds)), now_text),
        )
        return token, "processing", source_digest
    except Exception:
        if created_backup is not None:
            _cleanup_new_backup(database_path, created_backup, before)
        _mark_failed(database_path, job_id, token, admitted=False)
        raise


def _claimed_update(database_path: Path, job_id: int, token: str, sql: str, params=()):
    with closing(_connect(database_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            sql + " WHERE import_job_id=? AND claim_token=? AND status='processing'",
            (*params, job_id, token),
        )
        if cursor.rowcount != 1:
            raise WebAdmissionError(SAFE_BUSY, status_code=409)
        connection.commit()


def _mark_failed(database_path: Path, job_id: int, token: str, *, admitted: bool):
    now = _iso(_now())
    try:
        _claimed_update(
            database_path, job_id, token,
            """UPDATE import_web_admission_runs SET status='failed',
               stage=CASE WHEN ? THEN 'admitted_pending_finalize'
                          WHEN backup_relative_path IS NULL THEN 'failed'
                          ELSE 'processing' END,claim_token=NULL,
               safe_error=?,lease_expires_at=NULL,updated_at=?""",
            (admitted, SAFE_FINALIZE_FAILED if admitted else SAFE_APPLY_FAILED, now),
        )
    except (sqlite3.Error, WebAdmissionError):
        pass


def _verify_completed(connection, job_id: int, expected_count: int, codes) -> None:
    job = connection.execute("SELECT status FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    rows = connection.execute(
        """SELECT q.question_code,s.source_question_no FROM question_sources s
           JOIN questions q ON q.id=s.question_id WHERE s.import_job_id=?""",
        (job_id,),
    ).fetchall()
    if (
        job is None or job["status"] != "completed" or len(rows) != expected_count
        or {row["question_code"] for row in rows} != set(codes)
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise WebAdmissionError(SAFE_FINALIZE_FAILED, status_code=500)


def _anchor_admitted(
    database_path: Path, job_id: int, token: str, result, codes_digest: str,
    *, lease_seconds: float,
) -> None:
    now = _iso(_now())
    with closing(_connect(database_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        digest = _formal_batch_digest(connection, job_id)
        cursor = connection.execute(
            """UPDATE import_web_admission_runs SET stage='admitted_pending_finalize',
               inserted_count=?,already_present_count=?,eligible_count=?,
               question_code_digest=?,formal_batch_digest=?,heartbeat_at=?,
               lease_expires_at=?,updated_at=?
               WHERE import_job_id=? AND claim_token=? AND status='processing'""",
            (result.inserted, result.already_present, result.eligible, codes_digest,
             digest, now, _iso(_now() + timedelta(seconds=lease_seconds)), now,
             job_id, token),
        )
        if cursor.rowcount != 1:
            raise WebAdmissionError(SAFE_BUSY, status_code=409)
        connection.commit()


def _complete_run(
    database_path: Path, job_id: int, expected_count: int, codes,
) -> None:
    with closing(_connect(database_path)) as connection:
        run = connection.execute(
            "SELECT * FROM import_web_admission_runs WHERE import_job_id=?",
            (job_id,),
        ).fetchone()
        if run is None or (run["status"], run["stage"]) != ("completed", "completed"):
            raise WebAdmissionError(SAFE_FINALIZE_FAILED, status_code=500)
        _verify_completed(connection, job_id, expected_count, codes)
        _verify_formal_batch(connection, run, job_id)


def _verify_source_snapshot_in_transaction(
    connection: sqlite3.Connection, expected_digest: str,
) -> None:
    """Reject any business commit after an anchored admission backup."""
    if _business_snapshot_digest(connection) != expected_digest:
        raise WebAdmissionError(SAFE_BACKUP_STALE, status_code=409)


def _anchor_finalized_in_transaction(
    connection: sqlite3.Connection, job_id: int, token: str,
    expected_count: int, codes,
) -> None:
    """Atomically complete the coordinator in the job-finalization transaction."""
    _verify_completed(connection, job_id, expected_count, codes)
    digest = _formal_batch_digest(connection, job_id)
    now = _iso(_now())
    cursor = connection.execute(
        """UPDATE import_web_admission_runs
           SET status='completed',stage='completed',claim_token=NULL,
               safe_error=NULL,lease_expires_at=NULL,formal_batch_digest=?,
               completed_at=?,updated_at=?
           WHERE import_job_id=? AND claim_token=? AND status='processing'""",
        (digest, now, now, job_id, token),
    )
    if cursor.rowcount != 1:
        raise WebAdmissionError(SAFE_BUSY, status_code=409)


def apply_web_admission(
    database_path, private_root, job_id: int, *, backup_fn=backup_database,
    admit_fn=admit_questions, finalize_fn=finalize_review,
    lease_seconds=LEASE_SECONDS, keeper_interval=None,
):
    """Apply one explicitly authorized strict batch, with retryable phase boundaries."""
    database_path = Path(database_path)
    private_root = Path(private_root)
    if keeper_interval is None:
        keeper_interval = min(30.0, lease_seconds / 5)
    try:
        token, stage, source_digest = _claim(
            database_path, private_root, job_id, backup_fn=backup_fn,
            lease_seconds=lease_seconds, keeper_interval=keeper_interval,
        )
    except WebAdmissionError:
        raise
    except Exception as exc:
        raise WebAdmissionError(SAFE_APPLY_FAILED, status_code=500) from exc
    if token is None:
        return "completed"
    if not source_digest:
        _mark_failed(database_path, job_id, token, admitted=False)
        raise WebAdmissionError(SAFE_APPLY_FAILED, status_code=500)
    admitted = stage == "admitted_pending_finalize"
    try:
        result = _run_external(
            database_path, job_id, token,
            lambda: admit_fn(
                database_path, private_root, job_id, require_complete_batch=True,
                pre_apply_callback=(
                    None if admitted else lambda connection: (
                        _verify_source_snapshot_in_transaction(
                            connection, source_digest,
                        )
                    )
                ),
            ),
            lease_seconds=lease_seconds, keeper_interval=keeper_interval,
        )
        codes_digest = _digest(result.question_codes)
        now = _iso(_now())
        if not admitted:
            _anchor_admitted(
                database_path, job_id, token, result, codes_digest,
                lease_seconds=lease_seconds,
            )
            admitted = True
        with closing(_connect(database_path)) as connection:
            run = connection.execute(
                "SELECT * FROM import_web_admission_runs WHERE import_job_id=?", (job_id,)
            ).fetchone()
        finalize_backup_path = run["finalize_backup_relative_path"]
        finalize_backup_sha = run["finalize_backup_sha256"]
        finalize_source_digest = run["finalize_source_digest"]
        finalize_snapshot_digest = run["finalize_backup_snapshot_digest"]
        if finalize_backup_path is None:
            before = _backup_snapshot(database_path)
            created_finalize_backup = None
            try:
                finalize_source_digest = _database_snapshot_digest(database_path)
                _claimed_update(
                    database_path, job_id, token,
                    """UPDATE import_web_admission_runs SET finalize_source_digest=?""",
                    (finalize_source_digest,),
                )
                created_finalize_backup = _run_external(
                    database_path, job_id, token, lambda: backup_fn(database_path),
                    lease_seconds=lease_seconds, keeper_interval=keeper_interval,
                )
                (
                    _, finalize_backup_path, finalize_backup_sha,
                    finalize_snapshot_digest,
                ) = _validate_backup(
                    database_path, created_finalize_backup,
                    error_message=SAFE_FINALIZE_FAILED,
                    expected_snapshot_digest=finalize_source_digest,
                )
                if _database_snapshot_digest(database_path) != finalize_source_digest:
                    raise WebAdmissionError(SAFE_FINALIZE_FAILED, status_code=500)
                now = _iso(_now())
                _claimed_update(
                    database_path, job_id, token,
                    """UPDATE import_web_admission_runs SET finalize_backup_relative_path=?,
                       finalize_backup_sha256=?,finalize_backup_snapshot_digest=?,
                       heartbeat_at=?,lease_expires_at=?,updated_at=?""",
                    (finalize_backup_path, finalize_backup_sha, finalize_snapshot_digest, now,
                     _iso(_now() + timedelta(seconds=lease_seconds)), now),
                )
            except Exception:
                if created_finalize_backup is not None:
                    _cleanup_new_backup(
                        database_path, created_finalize_backup, before
                    )
                raise
        backup_absolute = database_path.parent / finalize_backup_path
        if not finalize_source_digest or not finalize_snapshot_digest:
            raise WebAdmissionError(SAFE_FINALIZE_FAILED, status_code=500)
        if _database_snapshot_digest(database_path) != finalize_source_digest:
            raise WebAdmissionError(SAFE_BACKUP_STALE, status_code=409)
        _, _, _, verified_finalize_snapshot = _validate_backup(
            database_path, (backup_absolute, finalize_backup_sha),
            error_message=SAFE_FINALIZE_FAILED,
            expected_snapshot_digest=finalize_source_digest,
        )
        if verified_finalize_snapshot != finalize_snapshot_digest:
            raise WebAdmissionError(SAFE_FINALIZE_FAILED, status_code=500)
        _run_external(
            database_path, job_id, token,
            lambda: finalize_fn(
                database_path, private_root, job_id, apply=True,
                backup_result=(backup_absolute, finalize_backup_sha),
                pre_apply_callback=lambda connection: (
                    _verify_source_snapshot_in_transaction(
                        connection, finalize_source_digest,
                    )
                ),
                completion_callback=lambda connection: _anchor_finalized_in_transaction(
                    connection, job_id, token, run["expected_count"],
                    result.question_codes,
                ),
            ),
            lease_seconds=lease_seconds, keeper_interval=keeper_interval,
            checkpoint=False,
        )
        _complete_run(
            database_path, job_id, run["expected_count"], result.question_codes,
        )
        return "completed"
    except WebAdmissionError:
        _mark_failed(database_path, job_id, token, admitted=admitted)
        raise
    except Exception as exc:
        _mark_failed(database_path, job_id, token, admitted=admitted)
        message = SAFE_FINALIZE_FAILED if admitted else SAFE_APPLY_FAILED
        raise WebAdmissionError(message, status_code=500) from exc
