"""MVP recordings: upload, finalize and read (D8, docs/mvp-recording-design.md, #72).

POST /api/uploads                 createUpload   (Idempotency-Key)
POST /api/uploads/{id}/finalize   finalizeUpload (Idempotency-Key)
GET  /api/recordings              listRecordings
GET  /api/recordings/{id}         getRecording
GET  /api/jobs/{id}               getJob

Rules (design 6, 7): uploads need an ACTIVE connection and both participants' analysis
consent, checked again at finalize. A recording is visible to the two participants of
its connection only, also after a disconnect (status only). Another user's recording,
a deleted one and a missing one all answer 404.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, Field, field_validator

from umsal_backend import analysis
from umsal_backend.connections import ID_PATTERN
from umsal_backend.db import Db
from umsal_backend.errors import ApiError, new_request_id
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser
from umsal_backend.storage import (
    UPLOAD_URL_SECONDS,
    AudioStore,
    StorageUnavailable,
    recording_key,
    recording_prefix,
    sha256_b64,
)

MAX_BYTES = 52_428_800
MAX_DURATION_SECONDS = 600
OPEN_UPLOADS_PER_USER = 3
CONTENT_TYPES = ("audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a")
RecordingStatus = Literal[
    "UPLOADING",
    "PREPARING_INPUT",
    "QUEUED_AUDIO",
    "PROCESSING_AUDIO",
    "NEEDS_SPEAKER_CONFIRMATION",
    "QUEUED_TEXT",
    "PROCESSING_TEXT",
    "COMPLETED",
    "ABSTAINED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
]
SHA256 = r"^[a-f0-9]{64}$"
RecordingId = Annotated[str, Path(pattern=ID_PATTERN)]


class UploadRequest(BaseModel):
    # Not model-wide strict: occurred_at arrives as a JSON string. Numbers and flags are
    # strict so "yes" or 1 never becomes a confirmation (as in consents).
    model_config = {"extra": "forbid"}
    connection_id: str = Field(pattern=ID_PATTERN)
    occurred_at: datetime
    filename: str = Field(min_length=1, max_length=200)
    # Checked in the handler: the contract answers 415 / 413, not 422, for these.
    declared_content_type: str
    size_bytes: int = Field(ge=1, strict=True)
    sha256: str = Field(pattern=SHA256)
    # Contract: const true. A strict bool, so 1 or "true" is not a confirmation either.
    participants_permission_confirmed: bool = Field(strict=True)
    demo: bool | None = Field(default=None, strict=True)

    @field_validator("participants_permission_confirmed")
    @classmethod
    def must_confirm(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("participants_permission_confirmed must be true")
        return value


class DeclaredLimits(BaseModel):
    max_bytes: int
    max_duration_seconds: int
    status: Literal["PROPOSED_LIMITS"] = "PROPOSED_LIMITS"


class UploadTicket(BaseModel):
    upload_id: str
    recording_id: str
    upload_url: str
    expires_at: datetime
    required_headers: dict[str, str]
    declared_limits: DeclaredLimits


class FinalizeRequest(BaseModel):
    model_config = {"extra": "forbid", "strict": True}  # no dates here
    version_id: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=SHA256)
    size_bytes: int = Field(ge=1)
    demo: bool | None = None


class Accepted(BaseModel):
    request_id: str
    status: Literal["ACCEPTED", "PREPARING_INPUT", "DELETING", "CANCELLED"]
    resource_id: str


class Availability(BaseModel):
    # UNKNOWN: RUNNING would read as GPU capacity, OFF as "nothing happens" (design 4).
    worker_availability: Literal["OFF", "STARTING", "RUNNING", "DRAINING", "UNKNOWN"] = "UNKNOWN"
    next_processing_at: datetime | None = None


class SpeakerInterval(BaseModel):
    speaker_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)


class Recording(BaseModel):
    recording_id: str
    connection_id: str
    occurred_at: datetime
    status: RecordingStatus
    speaker_assignment_version: int = Field(ge=0)
    local_file_sha256: str | None = None
    speaker_intervals: list[SpeakerInterval] | None = None
    job_id: str | None
    expires_at: datetime | None = None
    raw_available: bool = False
    raw_expires_at: datetime | None = None
    availability: Availability = Field(default_factory=Availability)


class RecordingPage(BaseModel):
    items: list[Recording]
    next_cursor: str | None


class Job(BaseModel):
    job_id: str
    recording_id: str
    stage: Literal["AUDIO_PREP", "TEXT_ANALYSIS"]
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "ABSTAINED", "FAILED", "CANCELLED", "EXPIRED"]
    recording_status: RecordingStatus
    attempt: int = Field(ge=0)
    failure_code: str | None
    retryable: bool
    availability: Availability = Field(default_factory=Availability)


router = APIRouter(prefix="/api", tags=["App"])


def _availability() -> Availability:
    return Availability(worker_availability="UNKNOWN", next_processing_at=None)


def _not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "The resource was not found.")


def _audio(request: Request) -> AudioStore:
    store: AudioStore | None = getattr(request.app.state, "audio_store", None)
    if store is None:
        raise ApiError(503, "TEMPORARILY_UNAVAILABLE", "Recording storage is not configured.")
    return store


def _analyzer(request: Request) -> analysis.Analyzer:
    return getattr(request.app.state, "analyzer", None) or analysis.StubAnalyzer()


def _storage_down() -> ApiError:
    return ApiError(
        503, "TEMPORARILY_UNAVAILABLE", "Storage is temporarily unavailable.", retryable=True
    )


def _require_upload_allowed(
    db: psycopg.Connection, connection_id: str, user_id: str
) -> tuple[str, str]:
    """(participant_a, participant_b) if ``user_id`` may upload to this connection now."""
    row = db.execute(
        "SELECT user_a_id, user_b_id, status FROM connections "
        "WHERE id = %(id)s AND (user_a_id = %(me)s OR user_b_id = %(me)s) FOR SHARE",
        {"id": connection_id, "me": user_id},
    ).fetchone()
    if row is None:
        raise _not_found()
    user_a, user_b, status = row
    if status != "ACTIVE":
        raise ApiError(403, "FORBIDDEN", "The connection is not active.")
    allowed = db.execute(
        "SELECT count(*) FILTER (WHERE analysis_allowed) FROM consent_grants "
        "WHERE connection_id = %s",
        (connection_id,),
    ).fetchone()[0]
    if allowed != 2:
        raise ApiError(403, "CONSENT_REQUIRED", "Both participants must allow analysis first.")
    return user_a, user_b


def _ticket_fields(
    audio: AudioStore, recording_id: str, content_type: str, size: int, sha256: str, seconds: int
):
    signed = audio.presign_put(
        recording_key(recording_id),
        content_type=content_type,
        size=size,
        sha256_hex=sha256,
        expires_in=seconds,
    )
    return signed.url, signed.headers


@router.post("/uploads", status_code=201, response_model=UploadTicket, operation_id="createUpload")
def create_upload(
    user: CurrentUser,
    db: Db,
    request: Request,
    idempotency_key: IdempotencyKey,
    body: UploadRequest,
):
    audio = _audio(request)

    def handler() -> tuple[int, Any]:
        if body.declared_content_type not in CONTENT_TYPES:
            raise ApiError(
                415, "UNSUPPORTED_MEDIA_TYPE", "The declared audio format is not supported."
            )
        if body.size_bytes > MAX_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "The declared file is larger than the limit.")
        user_a, user_b = _require_upload_allowed(db, body.connection_id, user.user_id)
        # Serialize this user's uploads so two concurrent requests cannot both pass the cap.
        db.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user.user_id,))
        open_uploads = db.execute(
            "SELECT count(*) FROM upload_sessions "
            "WHERE owner_id = %s AND finalized_at IS NULL AND expires_at > now()",
            (user.user_id,),
        ).fetchone()[0]
        if open_uploads >= OPEN_UPLOADS_PER_USER:
            raise ApiError(
                429,
                "TOO_MANY_REQUESTS",
                "Finish or wait for earlier uploads first.",
                retryable=True,
            )
        recording_id = db.execute(
            """
            INSERT INTO recordings (connection_id, uploader_id, participant_a_id, participant_b_id,
                occurred_at, filename, content_type, size_bytes, sha256, s3_key, demo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '', %s) RETURNING id
            """,
            (
                body.connection_id,
                user.user_id,
                user_a,
                user_b,
                body.occurred_at,
                body.filename,
                body.declared_content_type,
                body.size_bytes,
                body.sha256,
                bool(body.demo),
            ),
        ).fetchone()[0]
        db.execute(
            "UPDATE recordings SET s3_key = %s WHERE id = %s",
            (recording_key(recording_id), recording_id),
        )
        upload_id, expires_at = db.execute(
            "INSERT INTO upload_sessions (recording_id, owner_id, expires_at) "
            "VALUES (%s, %s, now() + %s) RETURNING id, expires_at",
            (recording_id, user.user_id, timedelta(seconds=UPLOAD_URL_SECONDS)),
        ).fetchone()
        url, headers = _ticket_fields(
            audio,
            recording_id,
            body.declared_content_type,
            body.size_bytes,
            body.sha256,
            UPLOAD_URL_SECONDS,
        )
        ticket = UploadTicket(
            upload_id=upload_id,
            recording_id=recording_id,
            upload_url=url,
            expires_at=expires_at,
            required_headers=headers,
            declared_limits=DeclaredLimits(
                max_bytes=MAX_BYTES, max_duration_seconds=MAX_DURATION_SECONDS
            ),
        )
        return 201, ticket

    def store(content: dict) -> dict:
        # A presigned URL is a bearer credential: never keep it in the database.
        return {key: value for key, value in content.items() if key != "upload_url"}

    def replay(status: int, stored: dict) -> tuple[int, Any]:
        row = db.execute(
            "SELECT r.content_type, r.size_bytes, r.sha256, s.expires_at, "
            "GREATEST(0, floor(extract(epoch FROM s.expires_at - now())))::int, "
            "s.finalized_at IS NULL AND r.deleted_at IS NULL "
            "FROM upload_sessions s JOIN recordings r ON r.id = s.recording_id WHERE s.id = %s",
            (stored["upload_id"],),
        ).fetchone()
        content_type, size, sha256, _expires_at, seconds, open_ = row
        if not open_ or seconds < 1:
            raise ApiError(409, "INVALID_STATE", "This upload is no longer open.")
        url, _ = _ticket_fields(audio, stored["recording_id"], content_type, size, sha256, seconds)
        return status, {**stored, "upload_url": url}

    payload = body.model_dump(mode="json", exclude_none=True)
    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="POST /uploads",
        key=idempotency_key,
        payload=payload,
        handler=handler,
        store=store,
        replay=replay,
    )


@router.post(
    "/uploads/{upload_id}/finalize",
    status_code=202,
    response_model=Accepted,
    operation_id="finalizeUpload",
)
def finalize_upload(
    user: CurrentUser,
    db: Db,
    request: Request,
    idempotency_key: IdempotencyKey,
    body: FinalizeRequest,
    upload_id: Annotated[str, Path(pattern=ID_PATTERN)],
):
    audio = _audio(request)
    queued: list[str] = []

    def handler() -> tuple[int, Any]:
        row = db.execute(
            """
            SELECT s.recording_id, s.finalized_at IS NOT NULL, s.expires_at <= now(),
                   r.connection_id, r.size_bytes, r.sha256, r.s3_key, r.deleted_at IS NOT NULL
            FROM upload_sessions s JOIN recordings r ON r.id = s.recording_id
            WHERE s.id = %s AND s.owner_id = %s
            FOR UPDATE OF s, r
            """,
            (upload_id, user.user_id),
        ).fetchone()
        if row is None:
            raise _not_found()
        recording_id, finalized, expired, connection_id, size, sha256, key, deleted = row
        if deleted:
            raise _not_found()
        if finalized:
            raise ApiError(409, "INVALID_STATE", "This upload was already finalized.")
        if expired:
            raise ApiError(409, "INVALID_STATE", "This upload has expired.")
        if body.size_bytes != size or body.sha256 != sha256:
            raise ApiError(
                422, "SEMANTIC_CHECK_FAILED", "The file does not match the declared upload."
            )
        _require_upload_allowed(db, connection_id, user.user_id)
        try:
            version = audio.head_version(key, body.version_id)
            if version is None:
                raise ApiError(422, "SEMANTIC_CHECK_FAILED", "The uploaded version was not found.")
            if version.size != size or version.checksum_sha256 != sha256_b64(sha256):
                audio.delete_version(key, body.version_id)
                raise ApiError(
                    422,
                    "SEMANTIC_CHECK_FAILED",
                    "The uploaded file does not match the declaration.",
                )
            # Pin this version; other versions (re-uploads within the URL window) go now.
            audio.delete_versions(recording_prefix(recording_id), keep=body.version_id)
        except StorageUnavailable as error:
            raise _storage_down() from error
        updated = db.execute(
            "UPDATE recordings SET s3_version_id = %s, status = 'PREPARING_INPUT', "
            "updated_at = now() "
            "WHERE id = %s AND status = 'UPLOADING'",
            (body.version_id, recording_id),
        ).rowcount
        if updated != 1:
            raise ApiError(409, "INVALID_STATE", "The recording is not waiting for an upload.")
        db.execute("UPDATE upload_sessions SET finalized_at = now() WHERE id = %s", (upload_id,))
        job_id = analysis.enqueue(db, recording_id, "AUDIO_PREP")
        queued.append(job_id)
        return 202, Accepted(
            request_id=new_request_id(), status="PREPARING_INPUT", resource_id=job_id
        )

    payload = {"upload_id": upload_id, **body.model_dump(mode="json", exclude_none=True)}
    response = run_idempotent(
        db,
        actor_id=user.user_id,
        route="POST /uploads/{id}/finalize",
        key=idempotency_key,
        payload=payload,
        handler=handler,
    )
    if queued:  # not on a replay: the first request already ran the job
        db.commit()  # transaction 1: job QUEUED and the idempotent response
        analysis.drain_safely(db, queued[0], _analyzer(request))
    return response


_RECORDING_COLUMNS = """
    r.id, r.connection_id, r.occurred_at, r.status, r.speaker_assignment_version,
    r.sha256, r.speaker_intervals, r.current_job_id, r.uploader_id, r.created_at
"""


def _to_recording(row: tuple, viewer: str) -> Recording:
    rec_id, connection_id, occurred_at, status, version, sha256, intervals, job_id, uploader, _ = (
        row
    )
    # Routes answer with exclude_unset: required-but-nullable fields are always set here,
    # optional ones only when they have a value.
    optional: dict[str, Any] = {}
    if uploader == viewer:
        optional["local_file_sha256"] = sha256  # the uploader matches their local file
    if intervals is not None:
        optional["speaker_intervals"] = intervals
    return Recording(
        recording_id=rec_id,
        connection_id=connection_id,
        occurred_at=occurred_at,
        status=status,
        speaker_assignment_version=version,
        job_id=job_id,
        expires_at=None,
        raw_available=False,
        raw_expires_at=None,
        availability=_availability(),
        **optional,
    )


def _load(db: psycopg.Connection, recording_id: str, user_id: str) -> tuple:
    row = db.execute(
        f"SELECT {_RECORDING_COLUMNS} FROM recordings r "
        "WHERE r.id = %(id)s AND r.deleted_at IS NULL "
        "AND (r.participant_a_id = %(me)s OR r.participant_b_id = %(me)s)",
        {"id": recording_id, "me": user_id},
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


@router.get(
    "/recordings/{recording_id}",
    response_model=Recording,
    response_model_exclude_unset=True,
    operation_id="getRecording",
)
def get_recording(
    user: CurrentUser, db: Db, request: Request, recording_id: RecordingId
) -> Recording:
    row = _load(db, recording_id, user.user_id)
    if analysis.redrain_if_stale(db, row[7], _analyzer(request)):
        row = _load(db, recording_id, user.user_id)
    return _to_recording(row, user.user_id)


def _invalid_cursor() -> ApiError:
    return ApiError(422, "VALIDATION_FAILED", "The cursor is not valid.")


def _encode_cursor(created_at: datetime, recording_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), recording_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        when, recording_id = json.loads(raw)
        parsed = datetime.fromisoformat(when)
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise _invalid_cursor() from error
    if (
        parsed.tzinfo is None
        or not isinstance(recording_id, str)
        or not re.fullmatch(ID_PATTERN, recording_id)
    ):
        raise _invalid_cursor()
    return parsed.astimezone(UTC), recording_id


@router.get(
    "/recordings",
    response_model=RecordingPage,
    response_model_exclude_unset=True,
    operation_id="listRecordings",
)
def list_recordings(
    user: CurrentUser,
    db: Db,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> RecordingPage:
    after = _decode_cursor(cursor) if cursor else None
    query = (
        f"SELECT {_RECORDING_COLUMNS} FROM recordings r WHERE r.deleted_at IS NULL "
        "AND (r.participant_a_id = %(me)s OR r.participant_b_id = %(me)s)"
    )
    params: dict = {"me": user.user_id, "limit": limit + 1}
    if after:
        query += " AND (r.created_at, r.id) < (%(after_at)s, %(after_id)s)"
        params.update(after_at=after[0], after_id=after[1])
    query += " ORDER BY r.created_at DESC, r.id DESC LIMIT %(limit)s"
    rows = db.execute(query, params).fetchall()
    page, extra = rows[:limit], rows[limit:]
    next_cursor = _encode_cursor(page[-1][9], page[-1][0]) if extra else None
    return RecordingPage(
        items=[_to_recording(row, user.user_id) for row in page], next_cursor=next_cursor
    )


def _load_job(db: psycopg.Connection, job_id: str, user_id: str) -> tuple:
    row = db.execute(
        """
        SELECT j.id, j.recording_id, j.stage, j.status, r.status, j.attempt, j.failure_code
        FROM analysis_jobs j JOIN recordings r ON r.id = j.recording_id
        WHERE j.id = %(id)s AND r.deleted_at IS NULL
          AND (r.participant_a_id = %(me)s OR r.participant_b_id = %(me)s)
        """,
        {"id": job_id, "me": user_id},
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


@router.get("/jobs/{job_id}", response_model=Job, operation_id="getJob")
def get_job(
    user: CurrentUser, db: Db, request: Request, job_id: Annotated[str, Path(pattern=ID_PATTERN)]
) -> Job:
    row = _load_job(db, job_id, user.user_id)
    if analysis.redrain_if_stale(db, row[0], _analyzer(request)):
        row = _load_job(db, job_id, user.user_id)
    job_id, recording_id, stage, status, recording_status, attempt, failure = row
    return Job(
        job_id=job_id,
        recording_id=recording_id,
        stage=stage,
        status=status,
        recording_status=recording_status,
        attempt=attempt,
        failure_code=failure,
        # A FAILED stub job is final in the MVP (retryJob is phase 2).
        retryable=False,
        availability=_availability(),
    )
