"""IAM-invoked CPU worker operations; never exposed through the public HTTP API."""

from __future__ import annotations

import json
import os
import re
from typing import Annotated, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from umsal_backend import analysis, pilot
from umsal_backend.db import database_from_env
from umsal_backend.results import ResultStore, result_store_from_env
from umsal_backend.storage import S3AudioStore, audio_store_from_env

ID = re.compile(r"^job_[a-f0-9]{32}$")
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "ABSTAINED", "EXPIRED"}
SPEAKER = Literal["spk_1", "spk_2"]
Category = Literal["WORRY", "SLEEP", "APPETITE"]
SUMMARY = {
    "WORRY": "확인한 화자의 말에서 걱정과 관련된 표현이 관찰됐어요.",
    "SLEEP": "확인한 화자의 말에서 수면과 관련된 표현이 관찰됐어요.",
    "APPETITE": "확인한 화자의 말에서 식사나 식욕과 관련된 표현이 관찰됐어요.",
}
LIMITATION = "짧은 합성 음성에 대한 CPU 모델의 시연 결과예요. 대화의 맥락을 놓칠 수 있어요."


class Interval(BaseModel):
    model_config = {"strict": True, "extra": "forbid"}
    speaker_id: SPEAKER
    start_ms: int = Field(ge=0, lt=60000)
    end_ms: int = Field(gt=0, le=60000)

    @model_validator(mode="after")
    def ordered(self) -> Interval:
        if self.start_ms >= self.end_ms:
            raise ValueError("invalid interval")
        return self


class TextOutput(BaseModel):
    model_config = {"strict": True, "extra": "forbid"}
    category: Category | None
    text: str | None = Field(max_length=1500)
    start_ms: int | None
    end_ms: int | None


def dispatch(db: psycopg.Connection, sqs, queue_url: str) -> int:
    """Publish committed IDs. A send followed by a failed commit can redeliver."""
    rows = db.execute(
        "SELECT o.job_id FROM analysis_outbox o JOIN analysis_jobs j ON j.id = o.job_id "
        "WHERE o.published_at IS NULL AND j.status = 'QUEUED' "
        "ORDER BY j.created_at LIMIT 5 FOR UPDATE OF o SKIP LOCKED"
    ).fetchall()
    for (job_id,) in rows:
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"job_id": job_id}))
        db.execute("UPDATE analysis_outbox SET published_at = now() WHERE job_id = %s", (job_id,))
    return len(rows)


def _close(db, job_id, recording_id, status, code):
    db.execute(
        "UPDATE analysis_jobs SET status=%s, failure_code=%s, lease_expires_at=NULL, "
        "updated_at=now() WHERE id=%s",
        (status, code, job_id),
    )
    db.execute(
        "UPDATE recordings SET status=%s, failure_code=%s, updated_at=now() "
        "WHERE id=%s AND current_job_id=%s AND deleted_at IS NULL",
        (status, code, recording_id, job_id),
    )


def operate(db: psycopg.Connection, event: dict, audio: S3AudioStore, results: ResultStore) -> dict:
    operation = event.get("operation")
    job_id = event.get("job_id")
    if operation not in {"claim", "heartbeat", "complete", "fail"} or not (
        isinstance(job_id, str) and ID.fullmatch(job_id)
    ):
        raise ValueError("invalid worker request")
    link = db.execute("SELECT recording_id FROM analysis_jobs WHERE id=%s", (job_id,)).fetchone()
    if link is None:
        return {"state": "unknown"}
    recording_id = link[0]
    recording = db.execute(
        "SELECT connection_id, current_job_id, speaker_assignment_version, deleted_at, "
        "s3_key, s3_version_id, size_bytes, sha256, speaker_intervals "
        "FROM recordings WHERE id=%s FOR UPDATE",
        (recording_id,),
    ).fetchone()
    if recording is None:
        return {"state": "unknown"}
    connection, current, version, deleted, key, object_version, size, digest, intervals = recording
    consent = pilot.grant_versions(db, connection)
    job = db.execute(
        "SELECT j.stage, j.status, j.assignment_version, j.attempt, "
        "COALESCE(j.lease_expires_at > clock_timestamp(), false), o.consent_versions "
        "FROM analysis_jobs j JOIN analysis_outbox o ON o.job_id=j.id "
        "WHERE j.id=%s AND j.pipeline_version=%s FOR UPDATE OF j",
        (job_id, analysis.CPU_PIPELINE_VERSION),
    ).fetchone()
    if job is None:
        return {"state": "unknown"}
    if job[1] in TERMINAL:
        return {"state": "terminal"}
    stage, status, assignment, attempt, live, snapshot = job
    if deleted or current != job_id or assignment != version or snapshot != consent:
        _close(db, job_id, recording_id, "CANCELLED", "RECORDING_OR_CONSENT_CHANGED")
        return {"state": "terminal"}
    if operation == "claim":
        if status == "RUNNING" and live:
            return {"state": "busy"}
        if attempt >= analysis.MAX_ATTEMPTS:
            _close(db, job_id, recording_id, "FAILED", "ATTEMPTS_EXHAUSTED")
            return {"state": "terminal"}
        if not object_version:
            _close(db, job_id, recording_id, "FAILED", "MISSING_AUDIO")
            return {"state": "terminal"}
        speaker = None
        if stage == "TEXT_ANALYSIS":
            assignment_row = db.execute(
                "SELECT speaker_id FROM speaker_assignments "
                "WHERE recording_id=%s AND version=%s AND decision='CONFIRMED'",
                (recording_id, version),
            ).fetchone()
            if assignment_row is None or assignment_row[0] not in {"spk_1", "spk_2"}:
                _close(db, job_id, recording_id, "FAILED", "ASSIGNMENT_MISSING")
                return {"state": "terminal"}
            speaker = assignment_row[0]
        db.execute(
            "UPDATE analysis_jobs SET status='RUNNING', attempt=attempt+1, "
            "lease_expires_at=now()+%s, updated_at=now() WHERE id=%s",
            (analysis.LEASE, job_id),
        )
        db.execute(
            "UPDATE recordings SET status=%s, updated_at=now() WHERE id=%s",
            (analysis.STAGE_STATUS[stage][1], recording_id),
        )
        url = audio.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": audio.bucket, "Key": key, "VersionId": object_version},
            ExpiresIn=90,
        )
        return {
            "state": "claimed",
            "stage": stage,
            "attempt": attempt + 1,
            "url": url,
            "size_bytes": size,
            "sha256": digest,
            "speaker_id": speaker,
        }
    if type(event.get("attempt")) is not int or event["attempt"] != attempt or not live:
        return {"state": "stale"}
    if status != "RUNNING":
        return {"state": "stale"}
    if operation == "heartbeat":
        db.execute(
            "UPDATE analysis_jobs SET lease_expires_at=now()+%s, updated_at=now() WHERE id=%s",
            (analysis.LEASE, job_id),
        )
        return {"state": "running"}
    if operation == "fail":
        if event.get("retryable") is True and attempt < analysis.MAX_ATTEMPTS:
            db.execute(
                "UPDATE analysis_jobs SET status='QUEUED', lease_expires_at=NULL, "
                "updated_at=now() WHERE id=%s",
                (job_id,),
            )
            db.execute(
                "UPDATE recordings SET status=%s, updated_at=now() WHERE id=%s",
                (analysis.STAGE_STATUS[stage][0], recording_id),
            )
            return {"state": "retry"}
        _close(db, job_id, recording_id, "FAILED", "CPU_ANALYSIS_FAILED")
        return {"state": "terminal"}

    if stage == "AUDIO_PREP":
        from pydantic import TypeAdapter

        output = TypeAdapter(Annotated[list[Interval], Field(min_length=2, max_length=80)])
        candidates = output.validate_python(event.get("output"))
        if {candidate.speaker_id for candidate in candidates} != {"spk_1", "spk_2"}:
            raise ValueError("two channels must contain speech")
        db.execute(
            "UPDATE recordings SET speaker_intervals=%s WHERE id=%s",
            (Jsonb([candidate.model_dump() for candidate in candidates]), recording_id),
        )
    else:
        output = TextOutput.model_validate(event.get("output"))
        observations = []
        summary = "확인한 화자의 말에서 시연 범위의 단서를 찾지 못했어요."
        if output.category:
            speaker = db.execute(
                "SELECT speaker_id FROM speaker_assignments WHERE recording_id=%s AND version=%s",
                (recording_id, version),
            ).fetchone()[0]
            matches_interval = any(
                i["speaker_id"] == speaker
                and i["start_ms"] == output.start_ms
                and i["end_ms"] == output.end_ms
                for i in intervals or []
            )
            if not matches_interval or not output.text:
                raise ValueError("result evidence does not match the confirmed speaker")
            observations = [
                {
                    "category": output.category,
                    "evidence": [
                        {
                            "utterance_id": "utt_" + str(output.start_ms),
                            "start_ms": output.start_ms,
                            "end_ms": output.end_ms,
                            "text": output.text,
                        }
                    ],
                }
            ]
            summary = SUMMARY[output.category]
        elif any(v is not None for v in (output.text, output.start_ms, output.end_ms)):
            raise ValueError("unexpected evidence")
        # All live and version checks run before this write with recording, grant and job locks.
        result = analysis.TextResult(
            "OBSERVED" if observations else "NOT_OBSERVED",
            summary,
            observations,
            [LIMITATION],
            True,
        )
        results.put(recording_id, version, result.body(analysis.CPU_PIPELINE_VERSION))
    db.execute(
        "UPDATE analysis_jobs SET status='SUCCEEDED', lease_expires_at=NULL, updated_at=now() "
        "WHERE id=%s",
        (job_id,),
    )
    db.execute(
        "UPDATE recordings SET status=%s, updated_at=now() WHERE id=%s",
        (analysis.STAGE_STATUS[stage][2], recording_id),
    )
    return {"state": "terminal"}


def handler(event, _context):
    database = database_from_env()
    audio = audio_store_from_env()
    results = result_store_from_env()
    if database is None or audio is None or results is None:
        raise RuntimeError("worker dependencies unavailable")
    try:
        with database.connect() as db:
            return operate(db, event, audio, results)
    except Exception:
        # Lambda exceptions must never serialize request contents or signed URLs.
        raise RuntimeError("worker operation failed") from None


def publisher_handler(_event, _context):
    import boto3
    from botocore.config import Config

    database = database_from_env()
    if database is None:
        raise RuntimeError("publisher database unavailable")
    client = boto3.client(
        "sqs", config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
    )
    with database.connect() as db:
        return {"published": dispatch(db, client, os.environ["UMSAL_QUEUE_URL"])}
