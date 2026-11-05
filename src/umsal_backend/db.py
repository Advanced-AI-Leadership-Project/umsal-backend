"""PostgreSQL connection targets: local URL (Docker) or Aurora with IAM authentication.

* ``DATABASE_URL`` (local, tests): a libpq URL such as ``postgresql://u:p@host:5432/db``.
* ``DB_HOST`` (AWS, stage 2): Aurora PostgreSQL in the private VPC. The password is a
  15-minute IAM auth token signed locally with the Lambda role (no Secrets Manager, no
  network call), and TLS verifies the server against the bundled RDS CA certificates.

Neither the URL nor a token is ever logged or returned.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Protocol

import psycopg
from fastapi import Depends, Request

from umsal_backend.errors import ApiError
from umsal_backend.health import Probe

CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 3000
TOKEN_REUSE_SECONDS = 600  # IAM tokens are valid for 15 minutes
DEFAULT_CA_BUNDLE = Path(__file__).resolve().parent / "certs" / "rds-global-bundle.pem"


class DatabaseProbeError(Exception):
    """PostgreSQL answered, but not with the expected probe result."""


class DatabaseConfigError(RuntimeError):
    """The database settings are incomplete; raised at startup, not per request."""


class DatabaseTarget(Protocol):
    def connect(self, *, autocommit: bool = False) -> psycopg.Connection: ...


def _session_options() -> str:
    return f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"


def _connect_timeout() -> int:
    return int(os.environ.get("DB_CONNECT_TIMEOUT", CONNECT_TIMEOUT_SECONDS))


@dataclass(frozen=True)
class UrlTarget:
    url: str

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        return connect(self.url, autocommit=autocommit)


TokenSigner = Callable[[str, int, str, str], str]


def _boto3_signer(host: str, port: int, user: str, region: str) -> str:
    import boto3  # deferred: only needed on AWS

    client = boto3.client("rds", region_name=region)
    return client.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user, Region=region)


@dataclass
class IamTarget:
    host: str
    user: str
    dbname: str
    region: str
    port: int = 5432
    ca_bundle: Path = DEFAULT_CA_BUNDLE
    sign: TokenSigner = _boto3_signer
    clock: Callable[[], float] = time.monotonic
    _token: str | None = field(default=None, init=False, repr=False)
    _issued_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.ca_bundle.is_file():
            raise DatabaseConfigError("RDS CA bundle is missing; TLS cannot be verified.")

    def token(self) -> str:
        with self._lock:
            now = self.clock()
            if self._token is None or now - self._issued_at >= TOKEN_REUSE_SECONDS:
                self._token = self.sign(self.host, self.port, self.user, self.region)
                self._issued_at = now
            return self._token

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            dbname=self.dbname,
            password=self.token(),
            sslmode="verify-full",
            sslrootcert=str(self.ca_bundle),
            connect_timeout=_connect_timeout(),
            options=_session_options(),
            autocommit=autocommit,
        )


def database_url() -> str | None:
    value = os.environ.get("DATABASE_URL", "").strip()
    return value or None


def database_from_env() -> DatabaseTarget | None:
    """DATABASE_URL wins (local); otherwise DB_HOST selects Aurora with IAM auth."""
    url = database_url()
    if url:
        return UrlTarget(url)
    host = os.environ.get("DB_HOST", "").strip()
    if not host:
        return None
    user = os.environ.get("DB_USER", "").strip()
    region = os.environ.get("AWS_REGION", "").strip()
    if not user or not region:
        raise DatabaseConfigError("DB_HOST is set but DB_USER or AWS_REGION is missing.")
    return IamTarget(
        host=host,
        user=user,
        dbname=os.environ.get("DB_NAME", "umsal").strip() or "umsal",
        region=region,
        port=int(os.environ.get("DB_PORT", "5432")),
        ca_bundle=Path(os.environ.get("DB_CA_BUNDLE", "").strip() or DEFAULT_CA_BUNDLE),
    )


def connect(url: str, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a URL connection with short connect and statement timeouts."""
    return psycopg.connect(
        url,
        connect_timeout=_connect_timeout(),
        options=_session_options(),
        autocommit=autocommit,
    )


def as_target(database: str | DatabaseTarget | None) -> DatabaseTarget | None:
    return UrlTarget(database) if isinstance(database, str) else database


def postgres_probe(target: str | DatabaseTarget) -> Probe:
    resolved = as_target(target)

    def probe() -> None:
        with resolved.connect(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise DatabaseProbeError

    return probe


def readiness_probes_from_env() -> dict[str, Probe]:
    target = database_from_env()
    return {"aurora": postgres_probe(target)} if target else {}


def get_db(request: Request) -> Iterator[psycopg.Connection]:
    """FastAPI dependency: one connection and transaction per request.

    The transaction commits when the handler returns and rolls back when it raises.
    ``scope="function"`` (see ``Db``) runs that commit before the response is sent,
    so a failed commit answers 503 instead of an already-sent 200. The connection is
    closed after every request, so a warm Lambda never keeps Aurora from auto-pausing.
    Connection failures surface as psycopg.OperationalError (503 handler).
    """
    target: DatabaseTarget | None = getattr(request.app.state, "database", None)
    if target is None:
        raise ApiError(503, "TEMPORARILY_UNAVAILABLE", "The database is not configured.")
    with target.connect() as connection:
        yield connection


Db = Annotated[psycopg.Connection, Depends(get_db, scope="function")]
