"""PostgreSQL (Aurora PostgreSQL in AWS, Docker locally) connection settings and probe.

``DATABASE_URL`` is a libpq URL such as ``postgresql://user:pass@host:5432/db``. It is
read from the environment only; it is never logged or returned because it can carry
credentials. On AWS it will be injected from Secrets Manager (stage 2).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, Request

from umsal_backend.errors import ApiError
from umsal_backend.health import Probe

CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 3000


class DatabaseProbeError(Exception):
    """PostgreSQL answered, but not with the expected probe result."""


def database_url() -> str | None:
    value = os.environ.get("DATABASE_URL", "").strip()
    return value or None


def connect(url: str, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection with short connect and statement timeouts."""
    return psycopg.connect(
        url,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        autocommit=autocommit,
    )


def postgres_probe(url: str) -> Probe:
    def probe() -> None:
        with connect(url, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise DatabaseProbeError

    return probe


def readiness_probes_from_env() -> dict[str, Probe]:
    url = database_url()
    return {"aurora": postgres_probe(url)} if url else {}


def get_db(request: Request) -> Iterator[psycopg.Connection]:
    """FastAPI dependency: one connection and transaction per request.

    The transaction commits when the handler returns and rolls back when it raises.
    Connection failures surface as psycopg.OperationalError (503 handler).
    """
    url: str | None = getattr(request.app.state, "database_url", None)
    if not url:
        raise ApiError(503, "TEMPORARILY_UNAVAILABLE", "The database is not configured.")
    with connect(url) as connection:
        yield connection


Db = Annotated[psycopg.Connection, Depends(get_db)]
