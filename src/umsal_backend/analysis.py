"""Analysis jobs: two-step state machine with a stub analyzer (docs/mvp-recording-design.md 3).

A write request commits a job as QUEUED (transaction 1, with its idempotent response),
then, before answering, ``drain`` claims it with a short lease, runs the ``Analyzer``
and calls ``complete``/``fail`` (transaction 2). The future GPU worker API will call
the same ``complete``/``fail``; only the in-request ``drain`` call goes away.

A Lambda that stops after commit leaves the job QUEUED, one that stops after claiming
leaves it RUNNING with an expired lease. Reads (``redrain_if_stale``) and the
15-minute sweep claim such jobs again; the claim succeeds only once. After
``MAX_ATTEMPTS`` claims a job fails for good.

The stub never reads the audio. Its output is fixed and marked ``demo``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

PIPELINE_VERSION = "stub-1"
LEASE = timedelta(minutes=2)
STALE_QUEUED = timedelta(seconds=30)
MAX_ATTEMPTS = 3

# stage -> (recording status while queued, while processing, after success)
STAGE_STATUS = {
    "AUDIO_PREP": ("QUEUED_AUDIO", "PROCESSING_AUDIO", "NEEDS_SPEAKER_CONFIRMATION"),
    "TEXT_ANALYSIS": ("QUEUED_TEXT", "PROCESSING_TEXT", "COMPLETED"),
}

STUB_INTERVALS = [
    {"speaker_id": "spk_1", "start_ms": 0, "end_ms": 5000},
    {"speaker_id": "spk_2", "start_ms": 5000, "end_ms": 10000},
]


class AnalysisError(Exception):
    """A stage could not produce a result. ``retryable`` decides re-queue vs FAILED."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AudioPrepResult:
    speaker_intervals: list[dict]


class Analyzer(Protocol):
    def prepare_audio(self, recording_id: str) -> AudioPrepResult: ...


class StubAnalyzer:
    """Fixed speaker intervals; never touches the audio (design 3)."""

    def prepare_audio(self, recording_id: str) -> AudioPrepResult:
        return AudioPrepResult(speaker_intervals=[dict(item) for item in STUB_INTERVALS])


@dataclass(frozen=True)
class Claim:
    job_id: str
    recording_id: str
    stage: str
    assignment_version: int
    attempt: int


def _log(event: str, **fields: object) -> None:
    # Identifiers only: never filenames, hashes, URLs or result text (design 11).
    print(json.dumps({"analysis": event, **fields}))


def enqueue(
    db: psycopg.Connection, recording_id: str, stage: str, assignment_version: int = 0
) -> str:
    """Add a QUEUED job and point the recording at it (caller holds the row lock)."""
    queued = STAGE_STATUS[stage][0]
    job_id = db.execute(
        "INSERT INTO analysis_jobs (recording_id, stage, pipeline_version, assignment_version) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (recording_id, stage, PIPELINE_VERSION, assignment_version),
    ).fetchone()[0]
    db.execute(
        "UPDATE recordings SET current_job_id = %s, status = %s, updated_at = now() WHERE id = %s",
        (job_id, queued, recording_id),
    )
    return job_id


def claim(db: psycopg.Connection, job_id: str) -> Claim | None:
    """Take a QUEUED job, or a RUNNING one whose lease expired. Succeeds only once."""
    row = db.execute(
        """
        UPDATE analysis_jobs
        SET status = 'RUNNING', attempt = attempt + 1, lease_expires_at = now() + %s,
            updated_at = now()
        WHERE id = %s
          AND (status = 'QUEUED' OR (status = 'RUNNING' AND lease_expires_at < now()))
        RETURNING recording_id, stage, assignment_version, attempt
        """,
        (LEASE, job_id),
    ).fetchone()
    if row is None:
        return None
    recording_id, stage, assignment_version, attempt = row
    queued, processing, _ = STAGE_STATUS[stage]
    db.execute(
        "UPDATE recordings SET status = %s, updated_at = now() "
        "WHERE id = %s AND deleted_at IS NULL AND status IN (%s, %s)",
        (processing, recording_id, queued, processing),
    )
    return Claim(job_id, recording_id, stage, assignment_version, attempt)


def _finish_job(db: psycopg.Connection, claimed: Claim, status: str, failure: str | None) -> bool:
    """Close the job if this claim still owns it (same attempt, still RUNNING)."""
    return (
        db.execute(
            "UPDATE analysis_jobs SET status = %s, failure_code = %s, lease_expires_at = NULL, "
            "updated_at = now() WHERE id = %s AND status = 'RUNNING' AND attempt = %s",
            (status, failure, claimed.job_id, claimed.attempt),
        ).rowcount
        == 1
    )


def _locked_live(db: psycopg.Connection, recording_id: str) -> bool:
    row = db.execute(
        "SELECT deleted_at IS NULL FROM recordings WHERE id = %s FOR UPDATE", (recording_id,)
    ).fetchone()
    return bool(row and row[0])


def complete_audio(db: psycopg.Connection, claimed: Claim, result: AudioPrepResult) -> None:
    if not _locked_live(db, claimed.recording_id):
        _finish_job(db, claimed, "CANCELLED", None)
        return
    if not _finish_job(db, claimed, "SUCCEEDED", None):
        return  # lease lost to a later claim; that claim owns the result now
    db.execute(
        "UPDATE recordings SET status = 'NEEDS_SPEAKER_CONFIRMATION', speaker_intervals = %s, "
        "updated_at = now() WHERE id = %s AND status = 'PROCESSING_AUDIO'",
        (Jsonb(result.speaker_intervals), claimed.recording_id),
    )


def fail(db: psycopg.Connection, claimed: Claim, error: AnalysisError) -> None:
    queued, processing, _ = STAGE_STATUS[claimed.stage]
    if not _locked_live(db, claimed.recording_id):
        _finish_job(db, claimed, "CANCELLED", None)
        return
    if error.retryable and claimed.attempt < MAX_ATTEMPTS:
        db.execute(
            "UPDATE analysis_jobs SET status = 'QUEUED', lease_expires_at = NULL, "
            "updated_at = now() "
            "WHERE id = %s AND status = 'RUNNING' AND attempt = %s",
            (claimed.job_id, claimed.attempt),
        )
        db.execute(
            "UPDATE recordings SET status = %s, updated_at = now() WHERE id = %s AND status = %s",
            (queued, claimed.recording_id, processing),
        )
        return
    if _finish_job(db, claimed, "FAILED", error.code):
        db.execute(
            "UPDATE recordings SET status = 'FAILED', failure_code = %s, updated_at = now() "
            "WHERE id = %s AND status IN (%s, %s)",
            (error.code, claimed.recording_id, queued, processing),
        )


def drain(db: psycopg.Connection, job_id: str, analyzer: Analyzer) -> None:
    """Claim and run one job in its own transactions (caller committed its work)."""
    claimed = claim(db, job_id)
    db.commit()
    if claimed is None:
        return
    try:
        if claimed.stage != "AUDIO_PREP":
            raise AnalysisError("STAGE_NOT_AVAILABLE", retryable=False)
        complete_audio(db, claimed, analyzer.prepare_audio(claimed.recording_id))
    except AnalysisError as error:
        db.rollback()
        fail(db, claimed, error)
        _log("failed", job_id=job_id, code=error.code, retryable=error.retryable)
    except Exception as error:  # transient: S3, DynamoDB, an unexpected bug
        db.rollback()
        fail(db, claimed, AnalysisError("TEMPORARY_ERROR", retryable=True))
        _log("error", job_id=job_id, error=type(error).__name__)
    db.commit()


def drain_safely(db: psycopg.Connection, job_id: str, analyzer: Analyzer) -> None:
    """Run ``drain`` without ever failing the request: the committed response stands."""
    try:
        drain(db, job_id, analyzer)
    except Exception as error:
        db.rollback()
        _log("drain_failed", job_id=job_id, error=type(error).__name__)


def redrain_if_stale(db: psycopg.Connection, job_id: str | None, analyzer: Analyzer) -> bool:
    """On a read: retry a job left QUEUED for 30 s or RUNNING past its lease."""
    if job_id is None:
        return False
    row = db.execute(
        "SELECT (status = 'QUEUED' AND updated_at < now() - %s) "
        "OR (status = 'RUNNING' AND lease_expires_at < now()) FROM analysis_jobs WHERE id = %s",
        (STALE_QUEUED, job_id),
    ).fetchone()
    if not row or not row[0]:
        return False
    db.commit()
    drain_safely(db, job_id, analyzer)
    return True
