"""Idempotency-Key handling for state-changing user APIs (contract: actor+route scope).

* same key + same request  -> the stored response is returned again
* same key + other request -> 409 IDEMPOTENCY_CONFLICT
* errors are not stored: the transaction rolls back, so the client may retry

Concurrency: the first request inserts a placeholder row inside its transaction. A
concurrent request with the same key blocks on that row's unique index until the first
commits (then it replays) or rolls back (then it proceeds itself).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

import psycopg
from fastapi import Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from umsal_backend.errors import ApiError

RETENTION = timedelta(hours=24)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def request_hash(route: str, payload: Any) -> bytes:
    canonical = json.dumps(
        {"route": route, "payload": jsonable_encoder(payload)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).digest()


def run_idempotent(
    db: psycopg.Connection,
    *,
    actor_id: str,
    route: str,
    key: str,
    payload: Any,
    handler: Callable[[], tuple[int, Any]],
) -> JSONResponse:
    """Run ``handler`` at most once per (actor, route, key) and return its response."""
    digest = request_hash(route, payload)
    # Lazy cleanup of this actor's expired keys (and any raw invite codes in them).
    db.execute(
        "DELETE FROM idempotency_keys WHERE actor_id = %s AND expires_at <= now()",
        (actor_id,),
    )
    inserted = db.execute(
        """
        INSERT INTO idempotency_keys (actor_id, route, key, request_hash, expires_at)
        VALUES (%s, %s, %s, %s, now() + %s)
        ON CONFLICT (actor_id, route, key) DO NOTHING
        RETURNING 1
        """,
        (actor_id, route, key, digest, RETENTION),
    ).fetchone()
    if inserted is None:
        stored_hash, status, body = db.execute(
            "SELECT request_hash, response_status, response_body FROM idempotency_keys "
            "WHERE actor_id = %s AND route = %s AND key = %s",
            (actor_id, route, key),
        ).fetchone()
        if bytes(stored_hash) != digest or status is None:
            raise ApiError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "The Idempotency-Key was already used for a different request.",
            )
        return JSONResponse(status_code=status, content=body)

    status, body = handler()
    content = jsonable_encoder(body)
    db.execute(
        "UPDATE idempotency_keys SET response_status = %s, response_body = %s "
        "WHERE actor_id = %s AND route = %s AND key = %s",
        (status, Jsonb(content), actor_id, route, key),
    )
    return JSONResponse(status_code=status, content=content)
