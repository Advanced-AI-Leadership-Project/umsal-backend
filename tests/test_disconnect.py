"""DELETE /api/connections/{id} against real PostgreSQL."""

from __future__ import annotations

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

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


def _h(sub: str, key: str | None = None) -> dict[str, str]:
    headers = {"X-Test-Sub": sub}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def connected(client):
    ticket = client.post("/api/invitations", headers=_h("alice", "invite-key-1"), json={}).json()
    connection = client.post(
        f"/api/invitations/{ticket['invitation_id']}/accept",
        headers=_h("bob", "accept-key-1"),
        json={"invite_code": ticket["invite_code"]},
    ).json()
    connection_id = connection["connection_id"]
    for sub in ("alice", "bob"):
        client.put(
            f"/api/connections/{connection_id}/consents",
            headers=_h(sub, f"consent-{sub}-1"),
            json={
                "expected_version": 1,
                "analysis_allowed": True,
                "sharing_scopes": ["summary", "evidence"],
                "verification_preview_allowed_to_other_participant": True,
            },
        )
    return connection_id


def _delete(client, connection_id, sub, key):
    return client.delete(f"/api/connections/{connection_id}", headers=_h(sub, key))


def _assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert set(body) == {"code", "message", "request_id", "retryable"}


def test_disconnect_revokes_connection_and_both_grants(client, connected, contract_schema):
    response = _delete(client, connected, "bob", "delete-key-1")
    assert response.status_code == 202
    contract_schema("Accepted").validate(response.json())
    assert response.json()["status"] == "ACCEPTED"
    assert response.json()["resource_id"] == connected

    for sub in ("alice", "bob"):
        connection = client.get(f"/api/connections/{connected}", headers=_h(sub)).json()
        assert connection["status"] == "DISCONNECTED"
        assert connection["version"] == 2
        state = client.get(f"/api/connections/{connected}/consents", headers=_h(sub)).json()
        assert state["mine"]["analysis_allowed"] is False
        assert state["mine"]["sharing_scopes"] == []
        assert state["mine"]["verification_preview_allowed_to_other_participant"] is False
        assert state["mine"]["version"] == 3  # 1 default, 2 allowed, 3 revoked by disconnect
        assert state["partner_to_me"]["sharing_scopes"] == []
        assert state["both_analysis_allowed"] is False
        assert state["verification_preview_allowed"] is False


def test_consent_updates_after_disconnect_are_rejected(client, connected):
    _delete(client, connected, "alice", "delete-key-1")
    response = client.put(
        f"/api/connections/{connected}/consents",
        headers=_h("alice", "consent-after-1"),
        json={
            "expected_version": 3,
            "analysis_allowed": True,
            "sharing_scopes": [],
            "verification_preview_allowed_to_other_participant": False,
        },
    )
    _assert_error(response, 409, "INVALID_STATE")


def test_repeat_disconnect_is_a_harmless_202(client, connected):
    assert _delete(client, connected, "alice", "delete-key-1").status_code == 202
    again = _delete(client, connected, "bob", "delete-key-2")
    assert again.status_code == 202
    connection = client.get(f"/api/connections/{connected}", headers=_h("alice")).json()
    assert connection["version"] == 2  # no second bump


def test_disconnect_replays_same_key(client, connected):
    first = _delete(client, connected, "alice", "delete-key-1")
    again = _delete(client, connected, "alice", "delete-key-1")
    assert again.status_code == 202
    assert again.json() == first.json()


def test_non_participant_cannot_disconnect(client, connected):
    client.get("/api/me", headers=_h("mallory"))
    _assert_error(_delete(client, connected, "mallory", "delete-key-1"), 404, "NOT_FOUND")
    _assert_error(_delete(client, "con_doesnotexist", "alice", "delete-key-2"), 404, "NOT_FOUND")
    connection = client.get(f"/api/connections/{connected}", headers=_h("alice")).json()
    assert connection["status"] == "ACTIVE"


def test_disconnect_requires_idempotency_key(client, connected):
    response = client.delete(f"/api/connections/{connected}", headers=_h("alice"))
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_reconnecting_after_disconnect_starts_denied(client, connected):
    _delete(client, connected, "alice", "delete-key-1")
    ticket = client.post("/api/invitations", headers=_h("alice", "invite-key-2"), json={}).json()
    new = client.post(
        f"/api/invitations/{ticket['invitation_id']}/accept",
        headers=_h("bob", "accept-key-2"),
        json={"invite_code": ticket["invite_code"]},
    ).json()["connection_id"]
    assert new != connected
    state = client.get(f"/api/connections/{new}/consents", headers=_h("alice")).json()
    assert state["mine"]["version"] == 1
    assert state["both_analysis_allowed"] is False


def test_disconnect_and_consent_update_race_never_leaves_an_active_grant(
    client, connected, migrated_db, run_concurrently
):
    """Lock order (connection, then grants) serializes disconnect and a concurrent PUT."""
    results: dict[str, int] = {}

    def disconnect():
        results["delete"] = _delete(client, connected, "alice", "race-delete-1").status_code

    def update():
        results["put"] = client.put(
            f"/api/connections/{connected}/consents",
            headers=_h("bob", "race-put-1"),
            json={
                "expected_version": 2,
                "analysis_allowed": True,
                "sharing_scopes": ["summary"],
                "verification_preview_allowed_to_other_participant": True,
            },
        ).status_code

    run_concurrently(disconnect, update)
    assert results["delete"] == 202
    assert results["put"] in (200, 409)
    with psycopg.connect(migrated_db) as connection:
        allowed = connection.execute(
            "SELECT count(*) FROM consent_grants WHERE connection_id = %s "
            "AND (analysis_allowed OR verification_preview_allowed OR sharing_scopes <> '{}')",
            (connected,),
        ).fetchone()
    assert allowed == (0,)  # whichever ran first, the final state is fully revoked
