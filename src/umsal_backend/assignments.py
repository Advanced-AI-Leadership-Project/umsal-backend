"""Speaker assignment and result reads (MVP stage 4, #75; docs/mvp-recording-design.md 3, 5-7).

PUT /api/recordings/{id}/speaker-assignment   assignSpeaker (Idempotency-Key)
GET /api/recordings/{id}/result               getResult

Rules:
* Only the uploader confirms the speaker (design 7: the confirmation material is the
  uploader's local file). Both participants' analysis consent and an ACTIVE connection
  are checked again, and ``expected_version`` must equal the current version.
* CONFIRMED starts a TEXT_ANALYSIS job for the new version; UNSURE ends ABSTAINED.
* The result's subject is the current assignment's ``target_subject_id``. The subject
  sees every scope; the other participant only the scopes the subject shares with them,
  and nothing once the subject's analysis consent is off or the connection is not
  ACTIVE. Older versions' results are never shown. Without ``evidence`` the summary
  carries no quotes, times or utterance ids (contract).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, Field, model_validator

from umsal_backend import analysis, pilot
from umsal_backend.connections import ID_PATTERN
from umsal_backend.db import Db
from umsal_backend.errors import ApiError, new_request_id
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser
from umsal_backend.recordings import (
    Accepted,
    _analyzer,
    _not_found,
    _require_upload_allowed,
    _results,
)
from umsal_backend.storage import StorageUnavailable

SCOPES = ("summary", "repetition", "evidence")
Scope = Literal["summary", "repetition", "evidence"]
RecordingId = Annotated[str, Path(pattern=ID_PATTERN)]
# Recording states a decision may follow (design 5 transitions).
CONFIRMABLE = ("NEEDS_SPEAKER_CONFIRMATION", "COMPLETED", "ABSTAINED")
ABSTAINABLE = ("NEEDS_SPEAKER_CONFIRMATION", "COMPLETED")
ABSTAINED_LIMITATION = "화자를 확인하지 않아 분석을 보류했어요."
# Quoted spans, utterance ids and clock times leak evidence into a summary (contract).
_CITATION = re.compile(
    r'"[^"]*"|“[^”]*”|‘[^’]*’|\'[^\']*\'|「[^」]*」|『[^』]*』'
    # Not \b: Hangul counts as a word character, so "00:05에" has no boundary after 05.
    r"|(?<![A-Za-z0-9_])utt_[A-Za-z0-9_-]+|(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"
)

router = APIRouter(prefix="/api", tags=["App"])


class SpeakerAssignment(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    expected_version: int = Field(ge=0)
    target_subject_id: str = Field(pattern=ID_PATTERN)
    speaker_id: str | None = Field(pattern=ID_PATTERN)  # required, null for UNSURE
    decision: Literal["CONFIRMED", "UNSURE"]
    demo: bool | None = None

    @model_validator(mode="after")
    def speaker_matches_decision(self) -> SpeakerAssignment:
        if (self.decision == "CONFIRMED") != (self.speaker_id is not None):
            raise ValueError("speaker_id is required for CONFIRMED and null for UNSURE")
        return self


class EvidenceView(BaseModel):
    utterance_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    text: str = Field(max_length=1500)


class Observation(BaseModel):
    category: Literal["WORRY", "SLEEP", "APPETITE"]
    evidence: list[EvidenceView] | None = None


class ResultView(BaseModel):
    recording_id: str
    status: Literal["OBSERVED", "NOT_OBSERVED", "ABSTAINED", "FAILED"]
    target_subject_id: str
    scope: list[Scope]
    summary: str | None = Field(default=None, max_length=2000)
    observations: list[Observation] | None = None
    limitations: list[str]
    demo: bool
    pipeline_version: str | None = None


@router.put(
    "/recordings/{recording_id}/speaker-assignment",
    status_code=202,
    response_model=Accepted,
    operation_id="assignSpeaker",
)
def assign_speaker(
    user: CurrentUser,
    db: Db,
    request: Request,
    idempotency_key: IdempotencyKey,
    body: SpeakerAssignment,
    recording_id: RecordingId,
):
    queued: list[tuple[str, bool]] = []

    def handler() -> tuple[int, Any]:
        row = db.execute(
            """
            SELECT connection_id, uploader_id, participant_a_id, participant_b_id, status,
                   speaker_assignment_version, speaker_intervals
            FROM recordings
            WHERE id = %(id)s AND deleted_at IS NULL
              AND (participant_a_id = %(me)s OR participant_b_id = %(me)s)
            FOR UPDATE
            """,
            {"id": recording_id, "me": user.user_id},
        ).fetchone()
        if row is None:
            raise _not_found()
        connection_id, uploader, user_a, user_b, status, version, intervals = row
        if uploader != user.user_id:
            raise ApiError(403, "FORBIDDEN", "Only the uploader confirms the speaker.")
        confirmed = body.decision == "CONFIRMED"
        if status not in (CONFIRMABLE if confirmed else ABSTAINABLE):
            raise ApiError(
                409, "INVALID_STATE", "The recording is not waiting for a speaker decision."
            )
        if body.expected_version != version:
            raise ApiError(
                409, "VERSION_CONFLICT", "The speaker assignment changed. Reload and retry."
            )
        if body.target_subject_id not in (user_a, user_b):
            raise ApiError(
                422, "SEMANTIC_CHECK_FAILED", "The target is not a participant of the recording."
            )
        speakers = {item["speaker_id"] for item in intervals or []}
        if confirmed and body.speaker_id not in speakers:
            raise ApiError(
                422, "SEMANTIC_CHECK_FAILED", "The speaker is not one of this recording's speakers."
            )
        # Lock order recording -> connection, as in finalize (design 6).
        _require_upload_allowed(db, connection_id, user.user_id)
        snapshot = pilot.audio_snapshot(db, recording_id)
        versions = pilot.grant_versions(db, connection_id)
        if snapshot is not None and snapshot != versions:
            raise ApiError(
                403, "CONSENT_REQUIRED", "Analysis consent changed after audio processing."
            )
        new_version = version + 1
        db.execute(
            "UPDATE recordings SET speaker_assignment_version = %s, updated_at = now() "
            "WHERE id = %s AND speaker_assignment_version = %s",
            (new_version, recording_id, version),
        )
        db.execute(
            "INSERT INTO speaker_assignments "
            "(recording_id, version, target_subject_id, speaker_id, decision, confirmed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                recording_id,
                new_version,
                body.target_subject_id,
                body.speaker_id,
                body.decision,
                user.user_id,
            ),
        )
        if confirmed:
            job_id = (
                pilot.enqueue(
                    db,
                    recording_id,
                    "TEXT_ANALYSIS",
                    snapshot,
                    new_version,
                    pipeline_version=pilot.audio_pipeline(db, recording_id),
                )
                if snapshot
                else analysis.enqueue(
                    db, recording_id, "TEXT_ANALYSIS", new_version, consent_versions=versions
                )
            )
            queued.append((job_id, snapshot is not None))
            resource_id = job_id
        else:
            db.execute(
                "UPDATE recordings SET status = 'ABSTAINED', updated_at = now() WHERE id = %s",
                (recording_id,),
            )
            resource_id = recording_id  # no job: nothing will be analyzed
        return 202, Accepted(
            request_id=new_request_id(), status="ACCEPTED", resource_id=resource_id
        )

    payload = {"recording_id": recording_id, **body.model_dump(mode="json", exclude_none=True)}
    response = run_idempotent(
        db,
        actor_id=user.user_id,
        route="PUT /recordings/{id}/speaker-assignment",
        key=idempotency_key,
        payload=payload,
        handler=handler,
    )
    if queued:  # not on a replay: the first request already ran the job
        db.commit()  # transaction 1: assignment version, TEXT job QUEUED, response
        job_id, use_worker = queued[0]
        if not use_worker:
            analysis.drain_safely(db, job_id, _analyzer(request), _results(request))
    return response


def _visible_scopes(
    db: psycopg.Connection, connection_id: str, subject_id: str, viewer_id: str
) -> list[str]:
    """Scopes ``viewer_id`` may see of ``subject_id``'s result right now (design 7)."""
    if viewer_id == subject_id:
        return list(SCOPES)
    row = db.execute(
        """
        SELECT c.status, g.analysis_allowed, g.sharing_scopes
        FROM connections c
        JOIN consent_grants g ON g.connection_id = c.id
        WHERE c.id = %(connection)s AND g.subject_id = %(subject)s AND g.recipient_id = %(me)s
        """,
        {"connection": connection_id, "subject": subject_id, "me": viewer_id},
    ).fetchone()
    if row is None or row[0] != "ACTIVE":
        raise ApiError(403, "FORBIDDEN", "The connection is not active.")
    if not row[1]:
        raise ApiError(403, "CONSENT_REQUIRED", "The subject does not allow analysis.")
    scopes = [scope for scope in SCOPES if scope in row[2]]
    if not scopes:
        # A participant already knows the recording exists (the list shows it): 403, not 404.
        raise ApiError(403, "FORBIDDEN", "The subject does not share this result with you.")
    return scopes


def without_citations(summary: str) -> str:
    return re.sub(r"\s{2,}", " ", _CITATION.sub("", summary)).strip()


def _filtered(body: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    if "summary" in scopes:
        summary = body["summary"]
        view["summary"] = summary if "evidence" in scopes else without_citations(summary)
    if "repetition" in scopes or "evidence" in scopes:
        view["observations"] = [
            {"category": item["category"]}
            | ({"evidence": item.get("evidence", [])} if "evidence" in scopes else {})
            for item in body["observations"]
        ]
    return view


def _not_ready(status: str) -> ApiError:
    final = status in ("FAILED", "CANCELLED", "EXPIRED")
    return ApiError(409, "RESULT_NOT_READY", "The result is not ready.", retryable=not final)


def _load(db: psycopg.Connection, recording_id: str, user_id: str) -> tuple:
    row = db.execute(
        "SELECT connection_id, status, speaker_assignment_version, current_job_id "
        "FROM recordings WHERE id = %(id)s AND deleted_at IS NULL "
        "AND (participant_a_id = %(me)s OR participant_b_id = %(me)s) FOR SHARE",
        {"id": recording_id, "me": user_id},
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


@router.get(
    "/recordings/{recording_id}/result",
    response_model=ResultView,
    response_model_exclude_unset=True,
    operation_id="getResult",
)
def get_result(
    user: CurrentUser, db: Db, request: Request, recording_id: RecordingId
) -> ResultView:
    results = _results(request)
    row = _load(db, recording_id, user.user_id)
    if analysis.redrain_if_stale(db, row[3], _analyzer(request), results):
        row = _load(db, recording_id, user.user_id)
    connection_id, status, version, _ = row
    if status not in ("COMPLETED", "ABSTAINED"):
        raise _not_ready(status)
    found = db.execute(
        "SELECT target_subject_id FROM speaker_assignments "
        "WHERE recording_id = %s AND version = %s",
        (recording_id, version),
    ).fetchone()
    if found is None:
        raise _not_ready(status)
    subject = found[0]
    snapshot = pilot.audio_snapshot(db, recording_id)
    if snapshot is not None and snapshot != pilot.grant_versions(db, connection_id):
        raise ApiError(403, "CONSENT_REQUIRED", "Analysis consent changed after audio processing.")
    scopes = _visible_scopes(db, connection_id, subject, user.user_id)
    if status == "ABSTAINED":
        return ResultView(
            recording_id=recording_id,
            status="ABSTAINED",
            target_subject_id=subject,
            scope=[],
            limitations=[ABSTAINED_LIMITATION],
            demo=db.execute("SELECT demo FROM recordings WHERE id=%s", (recording_id,)).fetchone()[
                0
            ],
            **(
                {"pipeline_version": pilot.audio_pipeline(db, recording_id)}
                if snapshot is not None
                else {}
            ),
        )
    if results is None:
        raise ApiError(503, "TEMPORARILY_UNAVAILABLE", "Result storage is not configured.")
    try:
        body = results.get(recording_id, version)
    except StorageUnavailable as error:
        raise ApiError(
            503, "TEMPORARILY_UNAVAILABLE", "Storage is temporarily unavailable.", retryable=True
        ) from error
    if body is None:  # COMPLETED is committed only after the write; treat as not ready
        raise _not_ready("PROCESSING_TEXT")
    try:
        return ResultView(
            recording_id=recording_id,
            status=body["status"],
            target_subject_id=subject,
            scope=scopes,
            limitations=list(body["limitations"]),
            demo=bool(body.get("demo", True)),
            **(
                {"pipeline_version": body["pipeline_version"]}
                if body.get("pipeline_version") in analysis.CPU_PIPELINES
                else {}
            ),
            **_filtered(body, scopes),
        )
    except (KeyError, TypeError, ValueError) as error:  # ValidationError is a ValueError
        # A malformed item (only this API writes them): no partial or unfiltered body.
        raise ApiError(
            503, "TEMPORARILY_UNAVAILABLE", "The stored result could not be read."
        ) from error
