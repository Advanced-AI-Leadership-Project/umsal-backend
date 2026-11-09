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

TEXT_ANALYSIS (stage 4, #75) writes the result body to DynamoDB under the recording's
row lock, then commits COMPLETED (design 6): a deletion that takes the lock first makes
the job CANCELLED before anything is written, and one that comes later deletes the item.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from umsal_backend.results import ResultStore

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
DEMO_LIMITATION = (
    "대역 분석 결과예요. 실제 음성을 분석하지 않았고, 파일이 음성인지도 확인하지 않았어요."
)


class AnalysisError(Exception):
    """A stage could not produce a result. ``retryable`` decides re-queue vs FAILED."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AudioPrepResult:
    speaker_intervals: list[dict]


@dataclass(frozen=True)
class TextResult:
    """One result artifact. ``getResult`` filters it by scope; it is never shown whole
    to anyone but the target subject."""

    status: str  # OBSERVED | NOT_OBSERVED
    summary: str
    observations: list[dict]  # [{"category": ..., "evidence": [EvidenceView, ...]}]
    limitations: list[str]
    demo: bool

    def body(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "observations": self.observations,
            "limitations": self.limitations,
            "demo": self.demo,
            "pipeline_version": PIPELINE_VERSION,
        }


class Analyzer(Protocol):
    def prepare_audio(self, recording_id: str) -> AudioPrepResult: ...

    def analyze_text(self, recording_id: str, speaker_id: str) -> TextResult: ...


class StubAnalyzer:
    """Fixed speaker intervals and a fixed example result; never touches the audio
    (design 3). Every result is ``demo`` and says so in ``limitations``."""

    def prepare_audio(self, recording_id: str) -> AudioPrepResult:
        return AudioPrepResult(speaker_intervals=[dict(item) for item in STUB_INTERVALS])

    def analyze_text(self, recording_id: str, speaker_id: str) -> TextResult:
        interval = next(
            (item for item in STUB_INTERVALS if item["speaker_id"] == speaker_id),
            STUB_INTERVALS[0],
        )
        evidence = {
            "utterance_id": "utt_1",
            "start_ms": interval["start_ms"],
            "end_ms": interval["end_ms"],
            "text": "(대역 예시) 요즘 일이 많아서 조금 걱정돼.",
        }
        return TextResult(
            status="OBSERVED",
            summary="대화에서 걱정을 표현한 부분이 한 번 관찰됐어요.",
            observations=[{"category": "WORRY", "evidence": [evidence]}],
            limitations=[DEMO_LIMITATION],
            demo=True,
        )


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
    """Take a QUEUED job, or a RUNNING one whose lease expired. Succeeds only once.

    Lock order everywhere is recording, then job (``complete``/``fail`` lock the
    recording first too). A RUNNING job whose lease expired after its last allowed
    attempt (the Lambda died every time) ends FAILED instead of being claimed again.
    """
    found = db.execute("SELECT recording_id FROM analysis_jobs WHERE id = %s", (job_id,)).fetchone()
    if found is None:
        return None
    db.execute("SELECT 1 FROM recordings WHERE id = %s FOR UPDATE", (found[0],))
    exhausted = db.execute(
        "UPDATE analysis_jobs SET status = 'FAILED', failure_code = 'ATTEMPTS_EXHAUSTED', "
        "lease_expires_at = NULL, updated_at = now() "
        "WHERE id = %s AND status = 'RUNNING' AND lease_expires_at < now() AND attempt >= %s "
        "RETURNING stage",
        (job_id, MAX_ATTEMPTS),
    ).fetchone()
    if exhausted is not None:
        queued, processing, _ = STAGE_STATUS[exhausted[0]]
        db.execute(
            "UPDATE recordings SET status = 'FAILED', failure_code = 'ATTEMPTS_EXHAUSTED', "
            "updated_at = now() WHERE id = %s AND status IN (%s, %s)",
            (found[0], queued, processing),
        )
        return None
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


def complete_text(
    db: psycopg.Connection, claimed: Claim, result: TextResult, results: ResultStore
) -> None:
    """Write the body under the row lock, then mark COMPLETED (committed by the caller).

    A deleted recording or a newer assignment (which cancels this job) ends the job
    CANCELLED without writing. If this claim lost its lease after the write, the item
    has the same key the newer claim writes, so nothing is left that could be shown.
    """
    row = db.execute(
        "SELECT deleted_at IS NULL, speaker_assignment_version FROM recordings "
        "WHERE id = %s FOR UPDATE",
        (claimed.recording_id,),
    ).fetchone()
    if row is None or not row[0] or row[1] != claimed.assignment_version:
        _finish_job(db, claimed, "CANCELLED", None)
        return
    results.put(claimed.recording_id, claimed.assignment_version, result.body())
    if not _finish_job(db, claimed, "SUCCEEDED", None):
        return
    db.execute(
        "UPDATE recordings SET status = 'COMPLETED', updated_at = now() "
        "WHERE id = %s AND status = 'PROCESSING_TEXT'",
        (claimed.recording_id,),
    )


def _speaker_for(db: psycopg.Connection, claimed: Claim) -> str:
    row = db.execute(
        "SELECT speaker_id FROM speaker_assignments WHERE recording_id = %s AND version = %s",
        (claimed.recording_id, claimed.assignment_version),
    ).fetchone()
    if row is None or row[0] is None:  # TEXT jobs exist only for CONFIRMED assignments
        raise AnalysisError("ASSIGNMENT_MISSING", retryable=False)
    return row[0]


def fail(db: psycopg.Connection, claimed: Claim, error: AnalysisError) -> None:
    queued, processing, _ = STAGE_STATUS[claimed.stage]
    if not _locked_live(db, claimed.recording_id):
        _finish_job(db, claimed, "CANCELLED", None)
        return
    if error.retryable and claimed.attempt < MAX_ATTEMPTS:
        requeued = db.execute(
            "UPDATE analysis_jobs SET status = 'QUEUED', lease_expires_at = NULL, "
            "updated_at = now() "
            "WHERE id = %s AND status = 'RUNNING' AND attempt = %s",
            (claimed.job_id, claimed.attempt),
        ).rowcount
        if requeued != 1:
            return  # a later claim owns the job; this stale attempt must not touch it
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


def drain(
    db: psycopg.Connection,
    job_id: str,
    analyzer: Analyzer,
    results: ResultStore | None = None,
) -> None:
    """Claim and run one job in its own transactions (caller committed its work)."""
    claimed = claim(db, job_id)
    db.commit()
    if claimed is None:
        return
    try:
        if claimed.stage == "AUDIO_PREP":
            complete_audio(db, claimed, analyzer.prepare_audio(claimed.recording_id))
        elif results is None:  # no DYNAMODB_TABLE: nowhere to keep a result
            raise AnalysisError("STAGE_NOT_AVAILABLE", retryable=False)
        else:
            speaker_id = _speaker_for(db, claimed)
            db.commit()  # no lock held while the analyzer runs
            text = analyzer.analyze_text(claimed.recording_id, speaker_id)
            complete_text(db, claimed, text, results)
    except AnalysisError as error:
        db.rollback()
        fail(db, claimed, error)
        _log("failed", job_id=job_id, code=error.code, retryable=error.retryable)
    except Exception as error:  # transient: S3, DynamoDB, an unexpected bug
        db.rollback()
        fail(db, claimed, AnalysisError("TEMPORARY_ERROR", retryable=True))
        _log("error", job_id=job_id, error=type(error).__name__)
    db.commit()


def drain_safely(
    db: psycopg.Connection,
    job_id: str,
    analyzer: Analyzer,
    results: ResultStore | None = None,
) -> None:
    """Run ``drain`` without ever failing the request: the committed response stands."""
    try:
        drain(db, job_id, analyzer, results)
    except Exception as error:
        try:
            db.rollback()
        except Exception:  # a broken connection: the request's own close handles it
            pass
        _log("drain_failed", job_id=job_id, error=type(error).__name__)


def redrain_if_stale(
    db: psycopg.Connection,
    job_id: str | None,
    analyzer: Analyzer,
    results: ResultStore | None = None,
) -> bool:
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
    drain_safely(db, job_id, analyzer, results)
    return True
