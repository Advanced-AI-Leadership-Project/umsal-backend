"""Caller identity: maps the verified Cognito ``sub`` to an internal user (GET /api/me).

The first authenticated request from a new ``sub`` creates the user row, so every
user API can rely on ``CurrentUser`` existing. The ``sub`` is never returned.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from umsal_backend.auth import CurrentPrincipal
from umsal_backend.db import Db


class Actor(BaseModel):
    user_id: str
    # Contract requires a string. Its source is undecided (api-contract D10); until a
    # profile operation exists it is empty.
    display_name: str = Field(max_length=80)


def get_or_create_user(db: Db, principal: CurrentPrincipal) -> Actor:
    """FastAPI dependency: the internal user for the verified caller."""
    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (cognito_sub) VALUES (%s) ON CONFLICT (cognito_sub) DO NOTHING",
            (principal.subject,),
        )
        cursor.execute(
            "SELECT id, display_name FROM users WHERE cognito_sub = %s",
            (principal.subject,),
        )
        user_id, display_name = cursor.fetchone()
    return Actor(user_id=user_id, display_name=display_name)


CurrentUser = Annotated[Actor, Depends(get_or_create_user)]

router = APIRouter(prefix="/api", tags=["App"])


@router.get("/me", response_model=Actor, operation_id="getMe")
def get_me(user: CurrentUser) -> Actor:
    return user
