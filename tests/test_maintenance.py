"""Scheduled purge of expired idempotency records and dead invitations (real PostgreSQL)."""

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from umsal_backend import maintenance
from umsal_backend.app import create_app
from umsal_backend.auth import Principal, require_principal


def _principal(request: Request) -> Principal:
    return Principal(
        subject=request.headers["x-test-sub"],
        username=None,
        client_id="synthetic-client",
        scopes=frozenset(),
        groups=frozenset(),
    )


@pytest.fixture
def client(migrated_db):
    app = create_app(token_verifier=None, database=migrated_db)
    app.dependency_overrides[require_principal] = _principal
    return TestClient(app)


def _h(sub: str, key: str) -> dict[str, str]:
    return {"X-Test-Sub": sub, "Idempotency-Key": key}


def _expire(url: str, sql: str, *params):
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql, params)


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


def test_purge_removes_only_expired_rows(client, migrated_db):
    keep = client.post("/api/invitations", headers=_h("alice", "keep-key-1"), json={}).json()
    gone = client.post("/api/invitations", headers=_h("bob", "gone-key-1"), json={}).json()
    used = client.post("/api/invitations", headers=_h("carol", "used-key-1"), json={}).json()
    client.post(
        f"/api/invitations/{used['invitation_id']}/accept",
        headers=_h("dave", "accept-key-1"),
        json={"invite_code": used["invite_code"]},
    )
    # Expire Bob's and Carol's invitations and Bob's stored ticket (with its raw code).
    for invitation in (gone, used):
        _expire(
            migrated_db,
            "UPDATE invitations SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = %s",
            invitation["invitation_id"],
        )
    _expire(
        migrated_db,
        "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE key = %s",
        "gone-key-1",
    )

    with psycopg.connect(migrated_db, autocommit=True) as connection:
        counts = maintenance.purge_expired(connection)
    assert counts == {"idempotency_keys": 1, "invitations": 1}

    with psycopg.connect(migrated_db) as connection:
        invitations = {r[0] for r in connection.execute("SELECT id FROM invitations").fetchall()}
        keys = {r[0] for r in connection.execute("SELECT key FROM idempotency_keys").fetchall()}
    assert invitations == {keep["invitation_id"], used["invitation_id"]}  # accepted is kept
    assert "gone-key-1" not in keys
    assert {"keep-key-1", "used-key-1", "accept-key-1"} <= keys
    raw = json.dumps(gone["invite_code"])
    with psycopg.connect(migrated_db) as connection:
        leftovers = connection.execute(
            "SELECT count(*) FROM idempotency_keys WHERE response_body::text LIKE %s",
            (f"%{raw.strip(chr(34))}%",),
        ).fetchone()
    assert leftovers == (0,)  # the expired raw code is gone


def test_purge_is_a_no_op_when_nothing_expired(client, migrated_db):
    client.post("/api/invitations", headers=_h("alice", "fresh-key-1"), json={})
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        assert maintenance.purge_expired(connection) == {"idempotency_keys": 0, "invitations": 0}
    assert _count(migrated_db, "invitations") == 1


def test_run_and_entry_points_use_database_url(client, migrated_db, monkeypatch, capsys):
    client.post("/api/invitations", headers=_h("alice", "old-key-1"), json={})
    _expire(migrated_db, "UPDATE idempotency_keys SET expires_at = now() - interval '1 second'")
    monkeypatch.setenv("DATABASE_URL", migrated_db)

    assert maintenance.scheduled_handler({}, None) == {"idempotency_keys": 1, "invitations": 0}
    maintenance.main()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"purged": {"idempotency_keys": 1, "invitations": 0}},
        {"purged": {"idempotency_keys": 0, "invitations": 0}},
    ]


def test_run_requires_database_url():
    with pytest.raises(SystemExit):
        maintenance.run()


def test_recording_sweep_task_does_not_run_the_purge(monkeypatch, capsys):
    monkeypatch.setattr(maintenance, "run", lambda: pytest.fail("purge must not run"))
    assert maintenance.scheduled_handler({"task": "recordings"}, None) == {}
    assert json.loads(capsys.readouterr().out) == {"recordings": "not_implemented"}
