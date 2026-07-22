"""Run and recover user-authorized render/split stages in their required order."""

import logging
import sqlite3
import time
from enum import Enum
from pathlib import Path

from src.processing.pdf_page_renderer import (
    PageRenderError,
    claim_render_job,
    run_claimed_render,
)
from src.processing.question_splitter import (
    claim_split_job,
    record_split_claim_failure,
    run_claimed_split,
)


LOGGER = logging.getLogger(__name__)


class AutomaticImportOutcome(Enum):
    """Internal recovery result; Web responses do not expose these values."""

    COMPLETED = "completed"
    NOOP = "noop"
    BUSY = "busy"
    FAILED = "failed"


def interrupted_automatic_import_job_ids(database_path):
    """Return stable IDs for interrupted Web-confirmed work, never failed work."""
    with sqlite3.connect(Path(database_path)) as connection:
        rows = connection.execute(
            """SELECT j.id
               FROM import_jobs AS j
               JOIN import_upload_receipts AS receipt
                 ON receipt.import_job_id=j.id
               LEFT JOIN import_page_render_runs AS render
                 ON render.import_job_id=j.id
               LEFT JOIN import_question_split_runs AS split
                 ON split.import_job_id=j.id
               WHERE j.status='pending'
                 AND (
                   split.import_job_id IS NULL
                   OR split.status IN ('pending','processing')
                 )
                 AND (
                   render.import_job_id IS NULL
                   OR render.status='processing'
                   OR (
                     render.status='completed'
                     AND (
                       split.import_job_id IS NULL
                       OR split.status IN ('pending','processing')
                     )
                   )
                 )
               ORDER BY j.id"""
        ).fetchall()
    return [row[0] for row in rows]


def _job_still_needs_recovery(database_path, job_id):
    return job_id in interrupted_automatic_import_job_ids(database_path)


def _no_claim_outcome(database_path, job_id):
    try:
        if _job_still_needs_recovery(database_path, job_id):
            return AutomaticImportOutcome.BUSY
    except (OSError, sqlite3.Error):
        return AutomaticImportOutcome.FAILED
    return AutomaticImportOutcome.NOOP


def run_automatic_import(
    database_path,
    private_root,
    job_id,
    *,
    split_runner=None,
    render_worker=run_claimed_render,
    split_worker=run_claimed_split,
):
    """Render one import, then split it; claims retain all locking semantics."""
    try:
        render_claim = claim_render_job(database_path, private_root, job_id)
    except PageRenderError:
        return AutomaticImportOutcome.FAILED
    if render_claim is None:
        return _no_claim_outcome(database_path, job_id)
    if render_worker(render_claim) is None:
        return AutomaticImportOutcome.FAILED
    try:
        split_claim = claim_split_job(
            database_path,
            private_root,
            job_id,
            runner=split_runner,
        )
    except Exception as error:
        recorded = record_split_claim_failure(
            database_path, private_root, job_id, error
        )
        if not recorded:
            return _no_claim_outcome(database_path, job_id)
        return AutomaticImportOutcome.FAILED
    if split_claim is None:
        return _no_claim_outcome(database_path, job_id)
    if split_worker(split_claim) is None:
        return AutomaticImportOutcome.FAILED
    return AutomaticImportOutcome.COMPLETED


def resume_interrupted_automatic_imports(
    database_path,
    private_root,
    *,
    automatic_import_runner=run_automatic_import,
    split_runner=None,
    render_worker=run_claimed_render,
    split_worker=run_claimed_split,
    sleep=time.sleep,
    backoff_seconds=5.0,
    max_rounds=None,
):
    """Serially rescan recoverable work, backing off while workers own locks."""
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        try:
            job_ids = interrupted_automatic_import_job_ids(database_path)
        except (OSError, sqlite3.Error):
            LOGGER.warning("automatic import recovery scan skipped")
            return None
        if not job_ids:
            return None
        rounds += 1
        retry_candidates = set()
        for job_id in job_ids:
            try:
                outcome = automatic_import_runner(
                    database_path,
                    private_root,
                    job_id,
                    split_runner=split_runner,
                    render_worker=render_worker,
                    split_worker=split_worker,
                )
                if outcome in {
                    AutomaticImportOutcome.BUSY,
                    AutomaticImportOutcome.FAILED,
                }:
                    retry_candidates.add(job_id)
            except Exception:
                retry_candidates.add(job_id)
                LOGGER.warning(
                    "automatic import recovery skipped job %s after a safe failure",
                    job_id,
                )
        if not retry_candidates or (
            max_rounds is not None and rounds >= max_rounds
        ):
            return None
        try:
            remaining = set(interrupted_automatic_import_job_ids(database_path))
        except (OSError, sqlite3.Error):
            LOGGER.warning("automatic import recovery rescan skipped")
            return None
        if not retry_candidates.intersection(remaining):
            return None
        sleep(backoff_seconds)
    return None
