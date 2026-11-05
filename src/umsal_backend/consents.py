"""Per-connection consent: GET/PUT /api/connections/{id}/consents (FR-03).

Each participant has one grant row per connection (they are its ``subject``; the other
participant is the ``recipient``). The server computes whether both allowed analysis
and whether both allowed a verification preview; a client flag never grants anything.

Not yet applicable: the contract says turning analysis off cancels new and running
mixed-call processing. Recordings and jobs do not exist yet; the analysis feature must
re-check grants at upload, claim and commit time (AGENTS product invariants).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Path
from pydantic import BaseModel, Field, field_validator

from umsal_backend.connections import ID_PATTERN
from umsal_backend.db import Db
from umsal_backend.errors import ApiError
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser

Scope = Literal["summary", "repetition", "evidence"]
SCOPE_ORDER = ("summary", "repetition", "evidence")


class ConsentUpdate(BaseModel):
    # strict: "yes", 1 or "true" must not silently become a consent of True.
    model_config = {"extra": "forbid", "strict": True}
    expected_version: int = Field(ge=0)
    analysis_allowed: bool
    sharing_scopes: list[Scope]
    verification_preview_allowed_to_other_participant: bool
    demo: bool | None = None

    @field_validator("sharing_scopes")
    @classmethod
    def unique_scopes(cls, scopes: list[str]) -> list[str]:
        if len(set(scopes)) != len(scopes):
            raise ValueError("sharing_scopes must be unique")  # contract: uniqueItems
        return scopes


class ConsentView(BaseModel):
    connection_id: str
    subject_id: str
    recipient_id: str
    version: int = Field(ge=1)
    analysis_allowed: bool
    sharing_scopes: list[Scope]
    verification_preview_allowed_to_other_participant: bool
    updated_at: datetime


class PartnerToMe(BaseModel):
    subject_id: str
    sharing_scopes: list[Scope]


class ConsentState(BaseModel):
    mine: ConsentView
    partner_to_me: PartnerToMe
    both_analysis_allowed: bool
    verification_preview_allowed: bool


def _ordered(scopes) -> list[str]:
    return [scope for scope in SCOPE_ORDER if scope in set(scopes)]


def create_default_grants(db: psycopg.Connection, connection_id: str, a: str, b: str) -> None:
    """Fully denied, version 1 grants for both directions of a new connection.

    Rows are always inserted in the same (sorted) order whoever calls, so two
    participants creating them concurrently cannot deadlock on the primary key.
    """
    low, high = sorted((a, b))
    db.execute(
        "INSERT INTO consent_grants (connection_id, subject_id, recipient_id) "
        "VALUES (%(c)s, %(low)s, %(high)s), (%(c)s, %(high)s, %(low)s) "
        "ON CONFLICT (connection_id, subject_id) DO NOTHING",
        {"c": connection_id, "low": low, "high": high},
    )


def _participation(
    db: psycopg.Connection, connection_id: str, user_id: str, *, lock: bool = False
) -> tuple[str, str]:
    """(partner_id, status) if ``user_id`` participates, else 404."""
    row = db.execute(
        "SELECT CASE WHEN user_a_id = %(me)s THEN user_b_id ELSE user_a_id END, status "
        "FROM connections WHERE id = %(id)s AND (user_a_id = %(me)s OR user_b_id = %(me)s)"
        + (" FOR SHARE" if lock else ""),
        {"id": connection_id, "me": user_id},
    ).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "The resource was not found.")
    partner_id, status = row
    # Connections made before grants existed (or inserted directly) get default rows.
    # This makes GET write once per such connection; it is not a read-only endpoint.
    create_default_grants(db, connection_id, user_id, partner_id)
    return partner_id, status


_GRANT_COLUMNS = (
    "subject_id, recipient_id, version, analysis_allowed, sharing_scopes, "
    "verification_preview_allowed, updated_at"
)


def _view(connection_id: str, row: tuple) -> ConsentView:
    subject_id, recipient_id, version, analysis, scopes, preview, updated_at = row
    return ConsentView(
        connection_id=connection_id,
        subject_id=subject_id,
        recipient_id=recipient_id,
        version=version,
        analysis_allowed=analysis,
        sharing_scopes=_ordered(scopes),
        verification_preview_allowed_to_other_participant=preview,
        updated_at=updated_at,
    )


router = APIRouter(prefix="/api", tags=["App"])
ConnectionId = Annotated[str, Path(pattern=ID_PATTERN)]


@router.get(
    "/connections/{connection_id}/consents",
    response_model=ConsentState,
    operation_id="getConsents",
)
def get_consents(user: CurrentUser, db: Db, connection_id: ConnectionId) -> ConsentState:
    partner_id, status = _participation(db, connection_id, user.user_id)
    rows = {
        row[0]: row
        for row in db.execute(
            f"SELECT {_GRANT_COLUMNS} FROM consent_grants WHERE connection_id = %s",
            (connection_id,),
        ).fetchall()
    }
    mine, theirs = _view(connection_id, rows[user.user_id]), _view(connection_id, rows[partner_id])
    active = status == "ACTIVE"
    return ConsentState(
        mine=mine,
        partner_to_me=PartnerToMe(subject_id=partner_id, sharing_scopes=theirs.sharing_scopes),
        both_analysis_allowed=active and mine.analysis_allowed and theirs.analysis_allowed,
        verification_preview_allowed=active
        and mine.verification_preview_allowed_to_other_participant
        and theirs.verification_preview_allowed_to_other_participant,
    )


@router.put(
    "/connections/{connection_id}/consents",
    response_model=ConsentView,
    operation_id="updateConsents",
)
def update_consents(
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey,
    body: ConsentUpdate,
    connection_id: ConnectionId,
):
    payload = {"connection_id": connection_id, **body.model_dump(exclude_none=True)}

    def handler() -> tuple[int, ConsentView]:
        # Lock order for anything touching grants: connection row first, then grant rows.
        # FOR SHARE here; a disconnect must take FOR UPDATE on the connection, then grants.
        _partner_id, status = _participation(db, connection_id, user.user_id, lock=True)
        if status != "ACTIVE":
            raise ApiError(409, "INVALID_STATE", "The connection is not active.")
        current = db.execute(
            "SELECT version FROM consent_grants "
            "WHERE connection_id = %s AND subject_id = %s FOR UPDATE",
            (connection_id, user.user_id),
        ).fetchone()[0]
        if current != body.expected_version:
            raise ApiError(409, "VERSION_CONFLICT", "The consent was changed. Reload and retry.")
        row = db.execute(
            f"""
            UPDATE consent_grants
            SET version = version + 1, analysis_allowed = %s, sharing_scopes = %s,
                verification_preview_allowed = %s, updated_at = now()
            WHERE connection_id = %s AND subject_id = %s
            RETURNING {_GRANT_COLUMNS}
            """,
            (
                body.analysis_allowed,
                _ordered(body.sharing_scopes),
                body.verification_preview_allowed_to_other_participant,
                connection_id,
                user.user_id,
            ),
        ).fetchone()
        return 200, _view(connection_id, row)

    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="PUT /connections/{id}/consents",
        key=idempotency_key,
        payload=payload,
        handler=handler,
    )
