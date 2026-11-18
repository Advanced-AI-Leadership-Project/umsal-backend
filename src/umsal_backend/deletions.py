"""Durable recording deletion: block access before purging versioned storage."""

from __future__ import annotations

from datetime import datetime
from time import monotonic
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Path, Query
from pydantic import BaseModel

from umsal_backend.db import Db
from umsal_backend.errors import ApiError, new_request_id
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser
from umsal_backend.recordings import ID_PATTERN, _decode_cursor, _encode_cursor
from umsal_backend.storage import StorageUnavailable, recording_key

router = APIRouter(prefix="/api", tags=["App"])
ResourceId = Annotated[str, Path(pattern=ID_PATTERN)]


class DeletionAccepted(BaseModel):
    request_id: str
    deletion_id: str
    status: Literal["DELETING"] = "DELETING"
    live_access_blocked: Literal[True] = True
    status_url: str


class DeletionStatus(BaseModel):
    deletion_id: str
    scope: Literal["RECORDING"]
    live_access_blocked: Literal[True] = True
    live_deletion_status: Literal["PENDING", "IN_PROGRESS", "COMPLETED", "FAILED"]
    live_deleted_at: datetime | None
    backup_status: Literal["RETAINED_PENDING_EXPIRY"] = "RETAINED_PENDING_EXPIRY"
    backup_expires_at: datetime | None = None
    retryable: bool
    last_error_code: (
        Literal["TEMPORARY_STORAGE_ERROR", "DEPENDENT_JOB_RUNNING", "BACKUP_POLICY_PENDING"] | None
    )


class DeletionPage(BaseModel):
    items: list[DeletionStatus]
    next_cursor: str | None


def purge_recording_storage(recording_id: str, s3_key: str) -> None:
    # Deferred so importing the API never creates AWS clients. The storage adapter
    # returns only after all S3 versions/markers and result versions are absent.
    from umsal_backend.deletion_storage import purge_recording_storage as purge

    purge(recording_id, s3_key)


def _status(row: tuple) -> DeletionStatus:
    deletion_id, scope, status, deleted_at, error = row[:5]
    return DeletionStatus(
        deletion_id=deletion_id,
        scope=scope,
        live_deletion_status=status,
        live_deleted_at=deleted_at,
        retryable=status != "COMPLETED",
        last_error_code=error,
    )


def process_deletion(
    db: psycopg.Connection,
    deletion_id: str,
    *,
    force: bool = False,
    verify_completed: bool = False,
) -> bool:
    """Attempt one durable deletion; false means not due or owned by another worker.

    Lock order is recording -> deletion -> analysis job. Every result writer also
    locks the recording, so a cancelled writer cannot recreate a purged result.
    A successful early purge still waits until the last issued PUT has expired,
    plus a five-minute grace period, then purges and verifies again.
    """
    with db.transaction():
        link = db.execute(
            "SELECT recording_id FROM deletion_jobs WHERE id = %s", (deletion_id,)
        ).fetchone()
        if link is None:
            return False
        recording = db.execute(
            "SELECT s3_key, deleted_at FROM recordings WHERE id = %s FOR UPDATE SKIP LOCKED",
            (link[0],),
        ).fetchone()
        if recording is None or recording[1] is None:
            return False
        job = db.execute(
            "SELECT live_deletion_status, attempts, "
            "next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp(), "
            "sweep_after <= clock_timestamp() "
            "FROM deletion_jobs WHERE id = %s FOR UPDATE",
            (deletion_id,),
        ).fetchone()
        if job[0] == "COMPLETED" and not verify_completed:
            return False
        if not force and not job[2]:
            return False
        try:
            purge_recording_storage(link[0], recording[0])
        except StorageUnavailable:
            db.execute(
                "UPDATE deletion_jobs SET live_deletion_status = 'FAILED', "
                "live_deleted_at = NULL, last_error_code = 'TEMPORARY_STORAGE_ERROR', "
                "attempts = attempts + 1, next_attempt_at = clock_timestamp() + "
                "make_interval(secs => %s) WHERE id = %s",
                (min(900, 60 * 2 ** min(job[1], 4)), deletion_id),
            )
        else:
            db.execute(
                "UPDATE deletion_jobs SET live_deletion_status = CASE "
                "WHEN %s THEN 'COMPLETED' ELSE 'IN_PROGRESS' END, "
                "live_deleted_at = CASE WHEN %s "
                "THEN COALESCE(live_deleted_at, clock_timestamp()) ELSE NULL END, "
                "last_error_code = NULL, "
                "attempts = attempts + 1, next_attempt_at = CASE "
                "WHEN %s THEN clock_timestamp() + interval '1 day' ELSE sweep_after END "
                "WHERE id = %s",
                (job[3], job[3], job[3], deletion_id),
            )
    return True


def sweep_deletions(
    db: psycopg.Connection,
    *,
    limit: int = 25,
    completed_limit: int = 2,
    completed_budget_seconds: float = 10,
) -> dict[str, int]:
    """Prioritize unfinished jobs, then audit a bounded number of old tombstones.

    Completed jobs are checked daily even if Object Created event delivery failed.
    The time budget limits new audit attempts; an already running SDK call keeps
    its adapter timeout. Automatic retention expiry is a separate feature.
    """
    with db.transaction():
        pending = db.execute(
            "SELECT id FROM deletion_jobs WHERE live_deletion_status <> 'COMPLETED' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp()) "
            "ORDER BY created_at, id LIMIT %s",
            (limit,),
        ).fetchall()
    attempted = sum(process_deletion(db, row[0]) for row in pending)
    deadline = monotonic() + completed_budget_seconds
    with db.transaction():
        completed = db.execute(
            "SELECT id FROM deletion_jobs WHERE live_deletion_status = 'COMPLETED' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp()) "
            "ORDER BY next_attempt_at NULLS FIRST, created_at, id LIMIT %s",
            (completed_limit,),
        ).fetchall()
    for row in completed:
        if monotonic() >= deadline:
            break
        attempted += process_deletion(db, row[0], verify_completed=True)
    return {"deletions_attempted": attempted}


def requeue_object_created(db: psycopg.Connection, object_key: str) -> bool:
    """Reopen a tombstoned recording when S3 reports a later completed write.

    A PUT admitted before URL expiry can finish after the grace period. Completed
    deletion jobs therefore remain tombstones and Object Created notifications
    schedule another verified purge. The event itself performs no storage calls.
    """
    parts = object_key.split("/", 4)
    if len(parts) < 3 or parts[0] != "recordings":
        return False
    if parts[1] == "users":
        if len(parts) != 5 or not parts[4]:
            return False
        owner, recording_id = parts[2:4]
    else:
        owner, recording_id = None, parts[1]
        if not "/".join(parts[2:]):
            return False
    try:
        expected_source = recording_key(recording_id, owner)
    except ValueError:
        return False
    if not object_key.startswith(expected_source.removesuffix("source")):
        return False
    with db.transaction():
        # Wait for a concurrent purge rather than skipping its lock: the event
        # must durably reopen the job after that purge commits, never get lost.
        row = db.execute(
            "SELECT s3_key, deleted_at FROM recordings WHERE id = %s FOR UPDATE",
            (recording_id,),
        ).fetchone()
        if row is None or row[1] is None or row[0] != expected_source:
            return False
        reopened = db.execute(
            "UPDATE deletion_jobs SET live_deletion_status = 'IN_PROGRESS', "
            "live_deleted_at = NULL, last_error_code = NULL, "
            "next_attempt_at = clock_timestamp() WHERE recording_id = %s RETURNING id",
            (recording_id,),
        ).fetchone()
    return reopened is not None


@router.delete(
    "/recordings/{recording_id}",
    status_code=202,
    response_model=DeletionAccepted,
    operation_id="deleteRecording",
)
def delete_recording(
    user: CurrentUser, db: Db, recording_id: ResourceId, idempotency_key: IdempotencyKey
):
    def handler() -> tuple[int, DeletionAccepted]:
        row = db.execute(
            "SELECT id FROM recordings WHERE id = %s "
            "AND (participant_a_id = %s OR participant_b_id = %s) FOR UPDATE",
            (recording_id, user.user_id, user.user_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "The resource was not found.")
        existing = db.execute(
            "SELECT id FROM deletion_jobs WHERE recording_id = %s", (recording_id,)
        ).fetchone()
        if existing is None:
            db.execute(
                "UPDATE recordings SET deleted_at = clock_timestamp(), filename = NULL, "
                "speaker_intervals = NULL, status = 'CANCELLED', updated_at = clock_timestamp() "
                "WHERE id = %s",
                (recording_id,),
            )
            db.execute(
                "UPDATE analysis_jobs SET status = 'CANCELLED', lease_expires_at = NULL, "
                "failure_code = 'RECORDING_DELETED', updated_at = clock_timestamp() "
                "WHERE recording_id = %s AND status IN ('QUEUED', 'RUNNING')",
                (recording_id,),
            )
            deletion_id = db.execute(
                "INSERT INTO deletion_jobs (recording_id, requested_by, sweep_after) "
                "VALUES (%s, %s, COALESCE((SELECT expires_at + interval '5 minutes' "
                "FROM upload_sessions WHERE recording_id = %s), clock_timestamp())) RETURNING id",
                (recording_id, user.user_id, recording_id),
            ).fetchone()[0]
        else:
            deletion_id = existing[0]
        return 202, DeletionAccepted(
            request_id=new_request_id(),
            deletion_id=deletion_id,
            status_url=f"/deletions/{deletion_id}",
        )

    # The request transaction durably blocks access and enqueues deletion. Storage
    # work is reserved for the maintenance role, avoiding new public-API privileges.
    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="DELETE /recordings/{id}",
        key=idempotency_key,
        payload={"recording_id": recording_id},
        handler=handler,
    )


_STATUS_COLUMNS = "d.id, d.scope, d.live_deletion_status, d.live_deleted_at, d.last_error_code"


@router.get("/deletions", response_model=DeletionPage, operation_id="listDeletions")
def list_deletions(
    user: CurrentUser,
    db: Db,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> DeletionPage:
    after = _decode_cursor(cursor) if cursor else None
    query = (
        f"SELECT {_STATUS_COLUMNS}, d.created_at FROM deletion_jobs d "
        "JOIN recordings r ON r.id = d.recording_id "
        "WHERE (d.requested_by = %(me)s OR r.participant_a_id = %(me)s "
        "OR r.participant_b_id = %(me)s)"
    )
    params: dict = {"me": user.user_id, "limit": limit + 1}
    if after:
        query += " AND (d.created_at, d.id) < (%(after_at)s, %(after_id)s)"
        params.update(after_at=after[0], after_id=after[1])
    query += " ORDER BY d.created_at DESC, d.id DESC LIMIT %(limit)s"
    rows = db.execute(query, params).fetchall()
    page = rows[:limit]
    return DeletionPage(
        items=[_status(row) for row in page],
        next_cursor=_encode_cursor(page[-1][5], page[-1][0]) if len(rows) > limit else None,
    )


@router.get(
    "/deletions/{deletion_id}", response_model=DeletionStatus, operation_id="getDeletionStatus"
)
def get_deletion_status(user: CurrentUser, db: Db, deletion_id: ResourceId) -> DeletionStatus:
    row = db.execute(
        f"SELECT {_STATUS_COLUMNS} FROM deletion_jobs d "
        "JOIN recordings r ON r.id = d.recording_id WHERE d.id = %s "
        "AND (d.requested_by = %s OR r.participant_a_id = %s OR r.participant_b_id = %s)",
        (deletion_id, user.user_id, user.user_id, user.user_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "The resource was not found.")
    return _status(row)
