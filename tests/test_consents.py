"""GET/PUT /api/connections/{id}/consents against real PostgreSQL (FR-03)."""

from __future__ import annotations

import threading

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
    """Alice invites Bob and Bob accepts: (connection_id, alice_id, bob_id)."""
    ticket = client.post("/api/invitations", headers=_h("alice", "invite-key-1"), json={}).json()
    connection = client.post(
        f"/api/invitations/{ticket['invitation_id']}/accept",
        headers=_h("bob", "accept-key-1"),
        json={"invite_code": ticket["invite_code"]},
    ).json()
    ids = {p["user_id"] for p in connection["participants"]}
    alice = client.get("/api/me", headers=_h("alice")).json()["user_id"]
    (bob,) = ids - {alice}
    return connection["connection_id"], alice, bob


def _get(client, connection_id, sub):
    return client.get(f"/api/connections/{connection_id}/consents", headers=_h(sub))


def _put(client, connection_id, sub, key, **changes):
    body = {
        "expected_version": 1,
        "analysis_allowed": False,
        "sharing_scopes": [],
        "verification_preview_allowed_to_other_participant": False,
    } | changes
    return client.put(f"/api/connections/{connection_id}/consents", headers=_h(sub, key), json=body)


def _assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert set(body) == {"code", "message", "request_id", "retryable"}


def test_new_connection_starts_fully_denied(client, connected, contract_schema):
    connection_id, alice, bob = connected
    state = _get(client, connection_id, "alice").json()
    contract_schema("ConsentState").validate(state)
    assert state["mine"]["subject_id"] == alice
    assert state["mine"]["recipient_id"] == bob
    assert state["mine"]["version"] == 1
    assert state["mine"]["analysis_allowed"] is False
    assert state["mine"]["sharing_scopes"] == []
    assert state["mine"]["verification_preview_allowed_to_other_participant"] is False
    assert state["partner_to_me"] == {"subject_id": bob, "sharing_scopes": []}
    assert state["both_analysis_allowed"] is False
    assert state["verification_preview_allowed"] is False


def test_update_changes_only_my_grant_and_bumps_version(client, connected, contract_schema):
    connection_id, alice, bob = connected
    response = _put(
        client,
        connection_id,
        "alice",
        "put-key-1",
        analysis_allowed=True,
        sharing_scopes=["evidence", "summary"],
        verification_preview_allowed_to_other_participant=True,
    )
    assert response.status_code == 200
    view = response.json()
    contract_schema("ConsentView").validate(view)
    assert view["version"] == 2
    assert view["subject_id"] == alice
    assert view["sharing_scopes"] == ["summary", "evidence"]  # stored in canonical order

    bob_state = _get(client, connection_id, "bob").json()
    assert bob_state["mine"]["version"] == 1  # Bob's own grant is untouched
    assert bob_state["mine"]["analysis_allowed"] is False
    assert bob_state["partner_to_me"] == {
        "subject_id": alice,
        "sharing_scopes": ["summary", "evidence"],
    }
    assert bob_state["both_analysis_allowed"] is False  # only one side allowed


def test_both_flags_require_both_participants(client, connected):
    connection_id, _alice, _bob = connected
    for sub in ("alice", "bob"):
        _put(
            client,
            connection_id,
            sub,
            f"put-{sub}-1",
            analysis_allowed=True,
            verification_preview_allowed_to_other_participant=True,
        )
    for sub in ("alice", "bob"):
        state = _get(client, connection_id, sub).json()
        assert state["both_analysis_allowed"] is True
        assert state["verification_preview_allowed"] is True

    _put(
        client,
        connection_id,
        "bob",
        "put-bob-2",
        expected_version=2,
        analysis_allowed=False,
        verification_preview_allowed_to_other_participant=True,
    )
    state = _get(client, connection_id, "alice").json()
    assert state["both_analysis_allowed"] is False  # revoking one side turns it off
    assert state["verification_preview_allowed"] is True


def test_stale_expected_version_is_409(client, connected):
    connection_id, _alice, _bob = connected
    assert _put(client, connection_id, "alice", "put-key-1").status_code == 200
    stale = _put(client, connection_id, "alice", "put-key-2", expected_version=1)
    _assert_error(stale, 409, "VERSION_CONFLICT")
    never = _put(client, connection_id, "alice", "put-key-3", expected_version=0)
    _assert_error(never, 409, "VERSION_CONFLICT")


def test_update_replays_same_key_and_rejects_reuse(client, connected):
    connection_id, _alice, _bob = connected
    first = _put(client, connection_id, "alice", "put-key-1", analysis_allowed=True)
    again = _put(client, connection_id, "alice", "put-key-1", analysis_allowed=True)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert _get(client, connection_id, "alice").json()["mine"]["version"] == 2  # applied once
    other = _put(client, connection_id, "alice", "put-key-1", analysis_allowed=False)
    _assert_error(other, 409, "IDEMPOTENCY_CONFLICT")


def test_concurrent_updates_with_same_version_apply_once(client, connected):
    connection_id, _alice, _bob = connected
    statuses: list[int] = []
    barrier = threading.Barrier(2)

    def update(key: str):
        barrier.wait()
        statuses.append(
            _put(client, connection_id, "alice", key, analysis_allowed=True).status_code
        )

    threads = [threading.Thread(target=update, args=(k,)) for k in ("race-key-1", "race-key-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200, 409]
    assert _get(client, connection_id, "alice").json()["mine"]["version"] == 2


def test_non_participant_gets_404_for_read_and_write(client, connected):
    connection_id, _alice, _bob = connected
    client.get("/api/me", headers=_h("mallory"))
    _assert_error(_get(client, connection_id, "mallory"), 404, "NOT_FOUND")
    _assert_error(_put(client, connection_id, "mallory", "put-key-1"), 404, "NOT_FOUND")
    _assert_error(_get(client, "con_doesnotexist", "alice"), 404, "NOT_FOUND")


@pytest.mark.parametrize(
    "changes",
    [
        {"sharing_scopes": ["summary", "summary"]},
        {"sharing_scopes": ["raw_audio"]},
        {"expected_version": -1},
        {"analysis_allowed": "yes"},
        {"analysis_allowed": 1},
        {"expected_version": "1"},
        {"subject_id": "usr_someone_else"},
    ],
    ids=[
        "duplicate-scope",
        "unknown-scope",
        "negative-version",
        "string-bool",
        "int-bool",
        "string-version",
        "extra-field",
    ],
)
def test_invalid_update_is_422(client, connected, changes):
    connection_id, _alice, _bob = connected
    response = _put(client, connection_id, "alice", "put-key-1", **changes)
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_update_requires_idempotency_key(client, connected):
    connection_id, _alice, _bob = connected
    response = client.put(
        f"/api/connections/{connection_id}/consents",
        headers=_h("alice"),
        json={
            "expected_version": 1,
            "analysis_allowed": True,
            "sharing_scopes": [],
            "verification_preview_allowed_to_other_participant": False,
        },
    )
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_disconnected_connection_rejects_updates_and_computes_false(client, connected, migrated_db):
    connection_id, _alice, _bob = connected
    for sub in ("alice", "bob"):
        _put(
            client,
            connection_id,
            sub,
            f"put-{sub}-1",
            analysis_allowed=True,
            verification_preview_allowed_to_other_participant=True,
        )
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute("UPDATE connections SET status = 'DISCONNECTED'")
    state = _get(client, connection_id, "alice").json()
    assert state["both_analysis_allowed"] is False
    assert state["verification_preview_allowed"] is False
    response = _put(client, connection_id, "alice", "put-key-9", expected_version=2)
    _assert_error(response, 409, "INVALID_STATE")


def test_connection_without_rows_gets_denied_defaults(client, migrated_db):
    # Connections inserted directly (or before grants existed) still read as denied.
    alice = client.get("/api/me", headers=_h("alice")).json()["user_id"]
    bob = client.get("/api/me", headers=_h("bob")).json()["user_id"]
    low, high = sorted((alice, bob))
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection_id = connection.execute(
            "INSERT INTO connections (user_a_id, user_b_id, status) "
            "VALUES (%s, %s, 'ACTIVE') RETURNING id",
            (low, high),
        ).fetchone()[0]
    state = _get(client, connection_id, "bob").json()
    assert state["mine"]["version"] == 1
    assert state["both_analysis_allowed"] is False


def test_schema_rejects_unknown_scope_and_self_grant(client, connected, migrated_db):
    connection_id, alice, _bob = connected
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE consent_grants SET sharing_scopes = ARRAY['raw_audio'] "
                "WHERE subject_id = %s",
                (alice,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE consent_grants SET recipient_id = subject_id WHERE subject_id = %s",
                (alice,),
            )
