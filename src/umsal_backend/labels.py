"""Per-connection labels (D10): PUT /api/connections/{id}/label.

Each participant names the other for themselves ("엄마", "첫째"). A label is stored per
(connection, owner) and returned only to its owner as ``Connection.label``. The
participants' ``display_name`` stays undecided and is not changed here.
"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Path
from pydantic import BaseModel, StringConstraints

from umsal_backend.connections import ID_PATTERN, Connection, load_connection
from umsal_backend.db import Db
from umsal_backend.errors import ApiError
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser

# Matches the database CHECK: 1-16 characters after trimming, no control characters
# (C0, DEL and C1, which PostgreSQL's [[:cntrl:]] also covers).
Label = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=16, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$"
    ),
]


class LabelUpdate(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    label: Label
    demo: bool | None = None


def store_label(db: psycopg.Connection, connection_id: str, owner_id: str, label: str) -> None:
    db.execute(
        "INSERT INTO connection_labels (connection_id, owner_id, label) VALUES (%s, %s, %s) "
        "ON CONFLICT (connection_id, owner_id) "
        "DO UPDATE SET label = EXCLUDED.label, updated_at = now()",
        (connection_id, owner_id, label),
    )


router = APIRouter(prefix="/api", tags=["App"])


@router.put(
    "/connections/{connection_id}/label",
    response_model=Connection,
    operation_id="setConnectionLabel",
)
def set_connection_label(
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey,
    body: LabelUpdate,
    connection_id: Annotated[str, Path(pattern=ID_PATTERN)],
):
    payload = {"connection_id": connection_id, **body.model_dump(exclude_none=True)}

    def handler() -> tuple[int, Connection]:
        # Lock the connection so a concurrent disconnect cannot interleave.
        row = db.execute(
            "SELECT status FROM connections WHERE id = %s "
            "AND (user_a_id = %s OR user_b_id = %s) FOR UPDATE",
            (connection_id, user.user_id, user.user_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "The resource was not found.")
        if row[0] != "ACTIVE":
            raise ApiError(409, "INVALID_STATE", "The connection is not active.")
        store_label(db, connection_id, user.user_id, body.label)
        return 200, load_connection(db, connection_id, user.user_id)

    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="PUT /connections/{id}/label",
        key=idempotency_key,
        payload=payload,
        handler=handler,
    )
