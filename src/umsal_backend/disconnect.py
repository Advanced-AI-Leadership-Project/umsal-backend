"""DELETE /api/connections/{id} (``disconnect``, FR-02·12 / WF-07).

Either participant may disconnect. The connection becomes DISCONNECTED with a new
version, and both directions' grants are reset to fully denied with new versions, so
anything fenced on the old grant versions (future analysis jobs) can no longer commit.
Stored recordings or results are not deleted here; the contract requires a separate,
explicit deletion request.

Lock order (shared with consents.update_consents): the connection row first
(FOR UPDATE here), then the grant rows.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path
from pydantic import BaseModel

from umsal_backend.connections import ID_PATTERN
from umsal_backend.consents import create_default_grants
from umsal_backend.db import Db
from umsal_backend.errors import ApiError, new_request_id
from umsal_backend.idempotency import IdempotencyKey, run_idempotent
from umsal_backend.identity import CurrentUser


class Accepted(BaseModel):
    request_id: str
    status: Literal["ACCEPTED", "PREPARING_INPUT", "DELETING", "CANCELLED"]
    resource_id: str


router = APIRouter(prefix="/api", tags=["App"])


@router.delete(
    "/connections/{connection_id}",
    status_code=202,
    response_model=Accepted,
    operation_id="disconnect",
)
def disconnect(
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey,
    connection_id: Annotated[str, Path(pattern=ID_PATTERN)],
):
    def handler() -> tuple[int, Accepted]:
        row = db.execute(
            "SELECT user_a_id, user_b_id, status FROM connections "
            "WHERE id = %(id)s AND (user_a_id = %(me)s OR user_b_id = %(me)s) FOR UPDATE",
            {"id": connection_id, "me": user.user_id},
        ).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "The resource was not found.")
        user_a, user_b, status = row
        if status != "DISCONNECTED":
            db.execute(
                "UPDATE connections SET status = 'DISCONNECTED', version = version + 1, "
                "updated_at = now() WHERE id = %s",
                (connection_id,),
            )
            create_default_grants(db, connection_id, user_a, user_b)
            db.execute(
                "UPDATE consent_grants SET version = version + 1, analysis_allowed = false, "
                "sharing_scopes = '{}', verification_preview_allowed = false, "
                "updated_at = now() WHERE connection_id = %s",
                (connection_id,),
            )
        # Already disconnected: same outcome, nothing changes (safe to repeat).
        return 202, Accepted(
            request_id=new_request_id(), status="ACCEPTED", resource_id=connection_id
        )

    return run_idempotent(
        db,
        actor_id=user.user_id,
        route="DELETE /connections/{id}",
        key=idempotency_key,
        payload={"connection_id": connection_id},
        handler=handler,
    )
