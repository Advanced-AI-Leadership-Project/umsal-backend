"""Foreground 1:1 audio calls between active family connections.

Polling carries call state and expiring SDP only. WebRTC carries encrypted audio.
Per-tab leases prevent two devices from taking over one participant's microphone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator

from umsal_backend.call_storage import CallServices, unavailable
from umsal_backend.connections import ID_PATTERN, load_connection
from umsal_backend.db import Db
from umsal_backend.errors import ApiError
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser

ACTIVE = ("RINGING", "ACCEPTED", "ACTIVE")
CallId = Annotated[str, Path(pattern=r"^call_[a-f0-9]{32}$")]
ClientId = Annotated[str, Field(pattern=r"^[a-f0-9-]{36}$")]


class CallClient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: ClientId


class CreateCall(CallClient):
    connection_id: str = Field(pattern=ID_PATTERN)


class CallHeartbeat(CallClient):
    connected: bool = False


class CallSignal(CallClient):
    type: Literal["offer", "answer"]
    sdp: str = Field(min_length=10, max_length=65536)

    @field_validator("sdp")
    @classmethod
    def audio_only(cls, value: str) -> str:
        if not value.startswith("v=0") or "m=audio " not in value:
            raise ValueError("Audio SDP is required")
        if "m=video " in value or "m=application " in value:
            raise ValueError("Only audio is allowed")
        return value


class VoiceCall(BaseModel):
    call_id: str
    connection_id: str
    caller_id: str
    callee_id: str
    state: Literal["RINGING", "ACCEPTED", "ACTIVE", "ENDED", "REJECTED", "CANCELLED", "EXPIRED"]
    created_at: datetime
    accepted_at: datetime | None
    ended_at: datetime | None


class CurrentCall(BaseModel):
    call: VoiceCall | None


class RemoteSignal(BaseModel):
    type: Literal["offer", "answer"]
    sdp: str | None


class IceServer(BaseModel):
    urls: list[str]
    username: str | None = None
    credential: str | None = None


class IceServers(BaseModel):
    ice_servers: list[IceServer]
    expires_at: int


def services(request: Request) -> CallServices:
    if request.app.state.call_services is None:
        raise unavailable()
    return request.app.state.call_services


Services = Annotated[CallServices, Depends(services)]
router = APIRouter(prefix="/api/calls", tags=["Calls"])


def view(row: dict) -> VoiceCall:
    return VoiceCall(
        call_id=row["id"], **{k: row[k] for k in VoiceCall.model_fields if k != "call_id"}
    )


def end(db, row: dict, state: str) -> dict:
    db.execute("UPDATE voice_calls SET state=%s, ended_at=now() WHERE id=%s", (state, row["id"]))
    return {**row, "state": state, "ended_at": datetime.now(UTC)}


def load(db, call_id: str, me: str) -> dict:
    # Lock the connection before the call, matching disconnect's connection-first order.
    relation = db.execute(
        "SELECT connection_id FROM voice_calls WHERE id=%s AND (caller_id=%s OR callee_id=%s)",
        (call_id, me, me),
    ).fetchone()
    if not relation:
        raise ApiError(404, "NOT_FOUND", "The resource was not found.")
    connection = db.execute(
        "SELECT status FROM connections WHERE id=%s FOR NO KEY UPDATE", (relation[0],)
    ).fetchone()
    with db.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            "SELECT * FROM voice_calls WHERE id=%s FOR UPDATE", (call_id,)
        ).fetchone()
    if row["state"] in ACTIVE:
        now = datetime.now(UTC)
        ringing_expired = row["state"] == "RINGING" and now - row["created_at"] > timedelta(
            seconds=60
        )
        stale = now - row["caller_seen_at"] > timedelta(seconds=90)
        if row["accepted_at"]:
            stale |= now - row["callee_seen_at"] > timedelta(seconds=90)
        duration_expired = now - row["created_at"] > timedelta(minutes=30)
        if connection[0] != "ACTIVE" or ringing_expired or stale or duration_expired:
            row = end(db, row, "EXPIRED")
    return row


def side_for(row: dict, me: str, client_id: str, *, active: bool = True) -> str:
    side = "caller" if row["caller_id"] == me else "callee"
    if row[f"{side}_client_id"] != client_id:
        raise ApiError(409, "CALL_OTHER_DEVICE", "This call belongs to another app session.")
    if active and row["state"] not in ACTIVE:
        raise ApiError(409, "CALL_ENDED", "The call has already ended.")
    return side


@router.post("", response_model=VoiceCall, status_code=201, operation_id="createCall")
def create_call(
    user: CurrentUser, db: Db, svc: Services, body: CreateCall, idempotency_key: IdempotencyKey
):
    def create():
        connection = load_connection(db, body.connection_id, user.user_id)
        peer = next(p.user_id for p in connection.participants if p.user_id != user.user_id)
        # Stable user lock order serializes simultaneous calls across different families.
        db.execute(
            "SELECT id FROM users WHERE id IN (%s,%s) ORDER BY id FOR NO KEY UPDATE",
            (user.user_id, peer),
        ).fetchall()
        status = db.execute(
            "SELECT status FROM connections WHERE id=%s FOR NO KEY UPDATE", (body.connection_id,)
        ).fetchone()[0]
        if status != "ACTIVE":
            raise ApiError(409, "CONNECTION_INACTIVE", "The family connection is inactive.")
        ids = db.execute(
            "SELECT id,caller_id FROM voice_calls WHERE state=ANY(%s) AND "
            "(caller_id IN (%s,%s) OR callee_id IN (%s,%s)) ORDER BY id",
            (list(ACTIVE), user.user_id, peer, user.user_id, peer),
        ).fetchall()
        for call_id, caller in ids:
            if load(db, call_id, caller)["state"] in ACTIVE:
                raise ApiError(409, "CALL_BUSY", "One participant is already in a call.")
        call_id = "call_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO voice_calls(id,connection_id,caller_id,callee_id,caller_client_id,state) "
            "VALUES(%s,%s,%s,%s,%s,'RINGING')",
            (call_id, body.connection_id, user.user_id, peer, body.client_id),
        )
        return 201, view(load(db, call_id, user.user_id))

    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="createCall",
        key=idempotency_key,
        payload=body,
        handler=create,
        replay=lambda status, saved: (status, view(load(db, saved["call_id"], user.user_id))),
    )


@router.get("", response_model=CurrentCall, operation_id="getCurrentCall")
def current_call(user: CurrentUser, db: Db, svc: Services):
    ids = db.execute(
        "SELECT id FROM voice_calls WHERE (caller_id=%s OR callee_id=%s) AND state=ANY(%s) "
        "ORDER BY created_at DESC",
        (user.user_id, user.user_id, list(ACTIVE)),
    ).fetchall()
    for (call_id,) in ids:
        row = load(db, call_id, user.user_id)
        if row["state"] in ACTIVE:
            return CurrentCall(call=view(row))
    return CurrentCall(call=None)


@router.get("/{call_id}", response_model=VoiceCall, operation_id="getCall")
def get_call(user: CurrentUser, db: Db, svc: Services, call_id: CallId):
    return view(load(db, call_id, user.user_id))


@router.post("/{call_id}/accept", response_model=VoiceCall, operation_id="acceptCall")
def accept_call(user: CurrentUser, db: Db, svc: Services, call_id: CallId, body: CallClient):
    row = load(db, call_id, user.user_id)
    if row["callee_id"] != user.user_id:
        raise ApiError(403, "CALL_FORBIDDEN", "Only the invited participant may accept.")
    if row["state"] == "RINGING":
        db.execute(
            "UPDATE voice_calls SET state='ACCEPTED',callee_client_id=%s,accepted_at=now(),"
            "callee_seen_at=now() WHERE id=%s",
            (body.client_id, call_id),
        )
    else:
        side_for(row, user.user_id, body.client_id)
    return view(load(db, call_id, user.user_id))


@router.delete("/{call_id}", response_model=VoiceCall, operation_id="endCall")
def end_call(user: CurrentUser, db: Db, svc: Services, call_id: CallId, body: CallClient):
    row = load(db, call_id, user.user_id)
    if row["state"] not in ACTIVE:
        return view(row)
    if row["state"] == "RINGING" and row["callee_id"] == user.user_id:
        state = "REJECTED"
    else:
        side_for(row, user.user_id, body.client_id)
        state = "CANCELLED" if row["state"] == "RINGING" else "ENDED"
    return view(end(db, row, state))


@router.post("/{call_id}/heartbeat", response_model=VoiceCall, operation_id="heartbeatCall")
def heartbeat(user: CurrentUser, db: Db, svc: Services, call_id: CallId, body: CallHeartbeat):
    row = load(db, call_id, user.user_id)
    side = side_for(row, user.user_id, body.client_id)
    # Column names are from the two fixed server-side roles, never request data.
    db.execute(
        f"UPDATE voice_calls SET {side}_seen_at=now(),{side}_connected=%s WHERE id=%s",
        (body.connected, call_id),
    )
    db.execute(
        "UPDATE voice_calls SET state='ACTIVE' WHERE id=%s AND state='ACCEPTED' "
        "AND caller_connected AND callee_connected",
        (call_id,),
    )
    return view(load(db, call_id, user.user_id))


@router.put("/{call_id}/signal", response_model=VoiceCall, operation_id="putCallSignal")
def put_signal(user: CurrentUser, db: Db, svc: Services, call_id: CallId, body: CallSignal):
    row = load(db, call_id, user.user_id)
    side = side_for(row, user.user_id, body.client_id)
    if body.type != ("offer" if side == "caller" else "answer"):
        raise ApiError(403, "CALL_FORBIDDEN", "The signal role is invalid.")
    if side == "callee" and row["state"] == "RINGING":
        raise ApiError(409, "CALL_NOT_ACCEPTED", "Accept the call first.")
    saved = svc.get_signal(call_id, side)
    if saved is not None and saved != body.sdp:
        raise ApiError(409, "CALL_SIGNAL_EXISTS", "The signal was already sent.")
    svc.put_signal(call_id, side, body.sdp)
    return view(row)


@router.get("/{call_id}/signal", response_model=RemoteSignal, operation_id="getCallSignal")
def get_signal(
    user: CurrentUser,
    db: Db,
    svc: Services,
    call_id: CallId,
    client_id: Annotated[str, Query(pattern=r"^[a-f0-9-]{36}$")],
):
    row = load(db, call_id, user.user_id)
    side = side_for(row, user.user_id, client_id)
    remote = "callee" if side == "caller" else "caller"
    return RemoteSignal(
        type="answer" if side == "caller" else "offer", sdp=svc.get_signal(call_id, remote)
    )


@router.get("/{call_id}/ice-servers", response_model=IceServers, operation_id="getCallIceServers")
def ice_servers(
    user: CurrentUser,
    db: Db,
    svc: Services,
    call_id: CallId,
    client_id: Annotated[str, Query(pattern=r"^[a-f0-9-]{36}$")],
):
    row = load(db, call_id, user.user_id)
    side = side_for(row, user.user_id, client_id)
    return svc.ice_servers(call_id, side)
