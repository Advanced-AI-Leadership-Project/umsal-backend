"""Connection read APIs: GET /api/connections (D7) and GET /api/connections/{id}.

A caller only ever sees connections they participate in. Another user's connection
answers 404 exactly like a missing one, so its existence is not revealed.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from umsal_backend.db import Db
from umsal_backend.errors import ApiError
from umsal_backend.identity import Actor, CurrentUser

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$"


class Connection(BaseModel):
    connection_id: str
    participants: list[Actor] = Field(min_length=2, max_length=2)
    status: Literal["PENDING", "ACTIVE", "DISCONNECTED"]
    version: int = Field(ge=1)


class ConnectionPage(BaseModel):
    items: list[Connection]
    next_cursor: str | None


_SELECT = """
    SELECT c.id, c.status, c.version, c.created_at,
           ua.id, ua.display_name, ub.id, ub.display_name
    FROM connections c
    JOIN users ua ON ua.id = c.user_a_id
    JOIN users ub ON ub.id = c.user_b_id
"""


def _row_to_connection(row: tuple) -> Connection:
    connection_id, status, version, _created_at, a_id, a_name, b_id, b_name = row
    return Connection(
        connection_id=connection_id,
        participants=[
            Actor(user_id=a_id, display_name=a_name),
            Actor(user_id=b_id, display_name=b_name),
        ],
        status=status,
        version=version,
    )


def _invalid_cursor() -> ApiError:
    return ApiError(422, "VALIDATION_FAILED", "The request is invalid.")


def encode_cursor(created_at: datetime, connection_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), connection_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, connection_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        parsed = datetime.fromisoformat(created_at)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise _invalid_cursor() from exc
    # A tampered id (NUL, lone surrogate, other shapes) must be a 422, not a driver error.
    if (
        parsed.tzinfo is None
        or not isinstance(connection_id, str)
        or not re.fullmatch(ID_PATTERN, connection_id)
    ):
        raise _invalid_cursor()
    return parsed, connection_id


router = APIRouter(prefix="/api", tags=["App"])


@router.get("/connections", response_model=ConnectionPage, operation_id="listConnections")
def list_connections(
    user: CurrentUser,
    db: Db,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ConnectionPage:
    after = decode_cursor(cursor) if cursor else None
    query = _SELECT + " WHERE (c.user_a_id = %(me)s OR c.user_b_id = %(me)s)"
    params: dict = {"me": user.user_id, "limit": limit + 1}
    if after:
        query += " AND (c.created_at, c.id) < (%(after_at)s, %(after_id)s)"
        params.update(after_at=after[0], after_id=after[1])
    query += " ORDER BY c.created_at DESC, c.id DESC LIMIT %(limit)s"
    rows = db.execute(query, params).fetchall()
    page, extra = rows[:limit], rows[limit:]
    next_cursor = encode_cursor(page[-1][3], page[-1][0]) if extra else None
    return ConnectionPage(items=[_row_to_connection(r) for r in page], next_cursor=next_cursor)


@router.get("/connections/{connection_id}", response_model=Connection, operation_id="getConnection")
def get_connection(
    user: CurrentUser,
    db: Db,
    connection_id: Annotated[str, Path(pattern=ID_PATTERN)],
) -> Connection:
    row = db.execute(
        _SELECT + " WHERE c.id = %(id)s AND (c.user_a_id = %(me)s OR c.user_b_id = %(me)s)",
        {"id": connection_id, "me": user.user_id},
    ).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "The resource was not found.")
    return _row_to_connection(row)
