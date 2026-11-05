"""Scheduled maintenance: remove expired idempotency records and dead invitations.

Why: a stored createInvitation response contains the raw invite code, and requests
only purge the *same* actor's expired keys (api-contract D11). This job removes every
expired key regardless of actor, and unaccepted invitations once they have expired
(their code hash is useless after expiry). Accepted invitations are kept because they
record how a connection was made.

Run locally:      DATABASE_URL=... python -m umsal_backend.maintenance
Run on AWS later: EventBridge Scheduler -> Lambda handler ``scheduled_handler``
                  (stage 2 deployment; not wired yet).
"""

from __future__ import annotations

import json
import sys
from typing import Any

import psycopg

from umsal_backend.db import database_from_env


def purge_expired(connection: psycopg.Connection) -> dict[str, int]:
    """Delete expired rows in one transaction and return how many were removed."""
    with connection.transaction():
        keys = connection.execute("DELETE FROM idempotency_keys WHERE expires_at <= now()").rowcount
        invitations = connection.execute(
            "DELETE FROM invitations WHERE accepted_by IS NULL AND expires_at <= now()"
        ).rowcount
    return {"idempotency_keys": keys, "invitations": invitations}


def run() -> dict[str, int]:
    target = database_from_env()
    if target is None:
        raise SystemExit("DATABASE_URL or DB_HOST is not set.")
    with target.connect(autocommit=True) as connection:
        return purge_expired(connection)


def scheduled_handler(_event: Any, _context: Any) -> dict[str, int]:
    """Lambda entry point for a scheduled rule. Logs only counts, never row contents."""
    counts = run()
    print(json.dumps({"purged": counts}))
    return counts


def main() -> None:
    print(json.dumps({"purged": run()}))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
