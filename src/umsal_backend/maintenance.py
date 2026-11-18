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
import os
import sys
from typing import Any

import psycopg

from umsal_backend.db import database_from_env
from umsal_backend.deletions import requeue_object_created, sweep_deletions


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


def scheduled_handler(event: Any, _context: Any) -> dict[str, int]:
    """Lambda entry point for a scheduled rule. Logs only counts, never row contents.

    The recording task retries explicit deletion requests and audits a small daily
    subset of completed tombstones as a fallback for lost object events.
    Automatic retention expiry is separate; daily invitation cleanup has its own task.
    """
    if isinstance(event, dict) and ("source" in event or "detail-type" in event):
        key = _object_created_key(event)
        counts = {"deletions_requeued": 0}
        if key is not None:
            target = database_from_env()
            if target is None:
                raise RuntimeError("Recording maintenance database is not configured.")
            with target.connect(autocommit=True) as connection:
                counts["deletions_requeued"] = int(requeue_object_created(connection, key))
        print(json.dumps({"recordings": counts}))
        return counts
    if isinstance(event, dict) and event.get("task") == "recordings":
        target = database_from_env()
        if target is None:
            raise RuntimeError("Recording maintenance database is not configured.")
        with target.connect(autocommit=True) as connection:
            counts = sweep_deletions(connection)
        print(json.dumps({"recordings": counts}))
        return counts
    counts = run()
    print(json.dumps({"purged": counts}))
    return counts


def _object_created_key(event: dict) -> str | None:
    """Accept only this stack's EventBridge S3 object-created event shape."""
    if event.get("source") != "aws.s3" or event.get("detail-type") != "Object Created":
        return None
    detail = event.get("detail")
    if not isinstance(detail, dict):
        return None
    bucket, object_ = detail.get("bucket"), detail.get("object")
    configured = os.environ.get("AUDIO_BUCKET", "").strip()
    if not configured or not isinstance(bucket, dict) or bucket.get("name") != configured:
        return None
    if not isinstance(object_, dict):
        return None
    key = object_.get("key")
    return key if isinstance(key, str) and 0 < len(key) <= 1024 else None


def main() -> None:
    print(json.dumps({"purged": run()}))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
