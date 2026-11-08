"""Invitations: POST /api/invitations and POST /api/invitations/{id}/accept (FR-02).

The inviter creates an expiring code and shares it together with the invitation id.
The invitee accepts with the code, which creates an ACTIVE connection. Accepting does
not allow any analysis or sharing (FR-02): both grant rows start fully denied.

Only SHA-256(code) is stored on the invitation. A wrong code answers 404 exactly like an
unknown invitation, so the id alone does not confirm that an invitation exists.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from umsal_backend.connections import ID_PATTERN, Connection, load_connection
from umsal_backend.consents import create_default_grants
from umsal_backend.db import Db
from umsal_backend.errors import ApiError
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser
from umsal_backend.labels import Label, store_label

INVITATION_LIFETIME = timedelta(hours=24)
# Crockford base32 without I, L, O, U: easy to read aloud or type on a phone.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 10  # 50 bits; acceptance also needs the random 128-bit invitation id
CODE_ATTEMPTS = 3


class InvitationRequest(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    # How the inviter will call the invitee once connected (D10).
    label: Label | None = None
    demo: bool | None = None


class InvitationTicket(BaseModel):
    invitation_id: str
    invite_code: str
    expires_at: datetime


class AcceptInvitation(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    invite_code: str = Field(min_length=6, max_length=64)
    # How the invitee will call the inviter (D10).
    label: Label | None = None
    demo: bool | None = None


def new_invite_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


def code_hash(code: str) -> bytes:
    return hashlib.sha256(normalize_code(code).encode()).digest()


def _not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "The resource was not found.")


router = APIRouter(prefix="/api", tags=["App"])


@router.post(
    "/invitations",
    status_code=201,
    response_model=InvitationTicket,
    operation_id="createInvitation",
)
def create_invitation(
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey,
    body: InvitationRequest | None = None,
):
    request = body or InvitationRequest()
    payload = request.model_dump(exclude_none=True)
    body_label = request.label

    def handler() -> tuple[int, InvitationTicket]:
        # code_hash is unique; a 50-bit collision is improbable but must not be a 500.
        for _attempt in range(CODE_ATTEMPTS):
            code = new_invite_code()
            try:
                with db.transaction():  # savepoint per attempt
                    invitation_id, expires_at = db.execute(
                        "INSERT INTO invitations"
                        " (code_hash, inviter_id, expires_at, inviter_label)"
                        " VALUES (%s, %s, now() + %s, %s) RETURNING id, expires_at",
                        (code_hash(code), user.user_id, INVITATION_LIFETIME, body_label),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                continue
            return 201, InvitationTicket(
                invitation_id=invitation_id, invite_code=code, expires_at=expires_at
            )
        raise ApiError(
            503, "TEMPORARILY_UNAVAILABLE", "Could not create an invitation.", retryable=True
        )

    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="POST /invitations",
        key=idempotency_key,
        payload=payload,
        handler=handler,
    )


@router.post(
    "/invitations/{invitation_id}/accept",
    response_model=Connection,
    operation_id="acceptInvitation",
)
def accept_invitation(
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey,
    body: AcceptInvitation,
    invitation_id: Annotated[str, Path(pattern=ID_PATTERN)],
):
    payload = {"invitation_id": invitation_id, **body.model_dump(exclude_none=True)}

    def handler() -> tuple[int, Connection]:
        # Check the code without a lock first, so wrong-code attempts cannot hold the
        # row and delay a legitimate accept.
        stored = db.execute(
            "SELECT code_hash FROM invitations WHERE id = %s", (invitation_id,)
        ).fetchone()
        if stored is None or not hmac.compare_digest(bytes(stored[0]), code_hash(body.invite_code)):
            raise _not_found()
        # Then lock the invitation so two concurrent accepts cannot both succeed.
        inviter_id, expired, accepted_by, inviter_label = db.execute(
            "SELECT inviter_id, expires_at <= now(), accepted_by, inviter_label "
            "FROM invitations WHERE id = %s FOR UPDATE",
            (invitation_id,),
        ).fetchone()
        if inviter_id == user.user_id:
            raise ApiError(
                422, "SEMANTIC_CHECK_FAILED", "An invitation cannot be accepted by its sender."
            )
        if accepted_by is not None:
            raise ApiError(409, "INVALID_STATE", "The invitation was already accepted.")
        if expired:
            raise ApiError(409, "INVALID_STATE", "The invitation has expired.")
        low, high = sorted((inviter_id, user.user_id))
        try:
            with db.transaction():  # savepoint: a duplicate pair must not poison the tx
                connection_id = db.execute(
                    "INSERT INTO connections (user_a_id, user_b_id, status) "
                    "VALUES (%s, %s, 'ACTIVE') RETURNING id",
                    (low, high),
                ).fetchone()[0]
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError(409, "INVALID_STATE", "These users are already connected.") from exc
        db.execute(
            "UPDATE invitations SET accepted_by = %s, accepted_at = now(), connection_id = %s "
            "WHERE id = %s",
            (user.user_id, connection_id, invitation_id),
        )
        # Accepting is not consent (FR-02): both directions start fully denied.
        create_default_grants(db, connection_id, inviter_id, user.user_id)
        # Each side's label is private to that side (D10).
        if inviter_label is not None:
            store_label(db, connection_id, inviter_id, inviter_label)
        if body.label is not None:
            store_label(db, connection_id, user.user_id, body.label)
        return 200, load_connection(db, connection_id, user.user_id)

    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="POST /invitations/{id}/accept",
        key=idempotency_key,
        payload=payload,
        handler=handler,
    )
