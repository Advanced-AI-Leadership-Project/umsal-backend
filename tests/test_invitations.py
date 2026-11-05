"""POST /api/invitations and POST /api/invitations/{id}/accept against real PostgreSQL."""

from __future__ import annotations

import hashlib
import threading

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from umsal_backend.app import create_app
from umsal_backend.auth import Principal, require_principal
from umsal_backend.idempotency import request_hash
from umsal_backend.invitations import CODE_ALPHABET, CODE_LENGTH, code_hash, normalize_code


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


def _invite(client, sub="inviter", key="invite-key-1", body=None):
    return client.post("/api/invitations", headers=_h(sub, key), json=body or {})


def _accept(client, invitation_id, code, sub="invitee", key="accept-key-1"):
    return client.post(
        f"/api/invitations/{invitation_id}/accept",
        headers=_h(sub, key),
        json={"invite_code": code},
    )


def _assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert set(body) == {"code", "message", "request_id", "retryable"}


# --- create ------------------------------------------------------------------------


def test_create_returns_ticket_and_stores_only_the_hash(client, migrated_db, contract_schema):
    response = _invite(client)
    assert response.status_code == 201
    ticket = response.json()
    contract_schema("InvitationTicket").validate(ticket)
    assert ticket["invitation_id"].startswith("inv_")
    assert len(ticket["invite_code"]) == CODE_LENGTH
    assert set(ticket["invite_code"]) <= set(CODE_ALPHABET)
    with psycopg.connect(migrated_db) as connection:
        stored, lifetime = connection.execute(
            "SELECT code_hash, expires_at - created_at FROM invitations WHERE id = %s",
            (ticket["invitation_id"],),
        ).fetchone()
    assert bytes(stored) == hashlib.sha256(ticket["invite_code"].encode()).digest()
    assert lifetime.total_seconds() == 24 * 3600


def test_create_replays_same_key_and_rejects_reuse_for_other_request(client, migrated_db):
    first = _invite(client, key="same-key-123")
    again = _invite(client, key="same-key-123")
    assert again.status_code == 201
    assert again.json() == first.json()
    with psycopg.connect(migrated_db) as connection:
        assert connection.execute("SELECT count(*) FROM invitations").fetchone() == (1,)
    _assert_error(
        _invite(client, key="same-key-123", body={"demo": True}), 409, "IDEMPOTENCY_CONFLICT"
    )
    # Keys are scoped per actor: another user may use the same key independently.
    other = _invite(client, sub="someone-else", key="same-key-123")
    assert other.status_code == 201
    assert other.json()["invitation_id"] != first.json()["invitation_id"]


@pytest.mark.parametrize(
    "key", [None, "short", "x" * 129], ids=["missing", "too-short", "too-long"]
)
def test_create_requires_valid_idempotency_key(client, key):
    _assert_error(_invite(client, key=key), 422, "VALIDATION_FAILED")


def test_create_rejects_unknown_fields(client):
    _assert_error(_invite(client, body={"inviter_id": "usr_x"}), 422, "VALIDATION_FAILED")


def test_expired_idempotency_keys_are_purged(client, migrated_db):
    _invite(client, key="old-key-123")
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute("UPDATE idempotency_keys SET expires_at = now() - interval '1 second'")
    fresh = _invite(client, key="new-key-123")
    assert fresh.status_code == 201
    with psycopg.connect(migrated_db) as connection:
        keys = connection.execute("SELECT key FROM idempotency_keys ORDER BY key").fetchall()
    assert keys == [("new-key-123",)]  # the stored raw code of the old ticket is gone


# --- accept ------------------------------------------------------------------------


def test_accept_creates_active_connection_visible_to_both(client, contract_schema):
    ticket = _invite(client).json()
    response = _accept(client, ticket["invitation_id"], ticket["invite_code"])
    assert response.status_code == 200
    connection = response.json()
    contract_schema("Connection").validate(connection)
    assert connection["status"] == "ACTIVE"
    assert connection["version"] == 1
    for sub in ("inviter", "invitee"):
        page = client.get("/api/connections", headers=_h(sub)).json()
        assert [item["connection_id"] for item in page["items"]] == [connection["connection_id"]]


def test_accept_does_not_create_any_grant(client, migrated_db):
    ticket = _invite(client).json()
    _accept(client, ticket["invitation_id"], ticket["invite_code"])
    with psycopg.connect(migrated_db) as connection:
        grants = connection.execute("SELECT to_regclass('consent_grants')").fetchone()[0]
    assert grants is None  # FR-02: accepting is not consent; no grant table exists yet


def test_code_is_case_and_separator_insensitive(client):
    ticket = _invite(client).json()
    code = ticket["invite_code"]
    typed = f" {code[:5].lower()}-{code[5:]} "
    assert normalize_code(typed) == code
    assert _accept(client, ticket["invitation_id"], typed).status_code == 200


def test_wrong_code_is_indistinguishable_from_unknown_invitation(client):
    ticket = _invite(client).json()
    wrong = _accept(client, ticket["invitation_id"], "WRONGCODE1")
    unknown = _accept(client, "inv_doesnotexist", ticket["invite_code"], key="accept-key-2")
    _assert_error(wrong, 404, "NOT_FOUND")
    _assert_error(unknown, 404, "NOT_FOUND")
    assert wrong.json()["message"] == unknown.json()["message"]


def test_inviter_cannot_accept_own_invitation(client):
    ticket = _invite(client).json()
    response = _accept(client, ticket["invitation_id"], ticket["invite_code"], sub="inviter")
    _assert_error(response, 422, "SEMANTIC_CHECK_FAILED")


def test_expired_invitation_is_409(client, migrated_db):
    ticket = _invite(client).json()
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(
            "UPDATE invitations SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day'"
        )
    response = _accept(client, ticket["invitation_id"], ticket["invite_code"])
    _assert_error(response, 409, "INVALID_STATE")


def test_invitation_is_single_use(client):
    ticket = _invite(client).json()
    assert _accept(client, ticket["invitation_id"], ticket["invite_code"]).status_code == 200
    second = _accept(client, ticket["invitation_id"], ticket["invite_code"], sub="third")
    _assert_error(second, 409, "INVALID_STATE")


def test_accept_replays_same_key(client):
    ticket = _invite(client).json()
    first = _accept(client, ticket["invitation_id"], ticket["invite_code"])
    again = _accept(client, ticket["invitation_id"], ticket["invite_code"])
    assert again.status_code == 200
    assert again.json() == first.json()


def test_failed_accept_is_not_stored_and_can_be_retried_with_same_key(client):
    ticket = _invite(client).json()
    _assert_error(_accept(client, ticket["invitation_id"], "WRONGCODE1"), 404, "NOT_FOUND")
    retried = _accept(client, ticket["invitation_id"], ticket["invite_code"])
    # Same key, different body: the failed attempt left no record, so no 409.
    assert retried.status_code == 200


def test_second_invitation_between_connected_users_is_409(client):
    first = _invite(client, key="invite-key-1").json()
    _accept(client, first["invitation_id"], first["invite_code"], key="accept-key-1")
    second = _invite(client, key="invite-key-2").json()
    response = _accept(client, second["invitation_id"], second["invite_code"], key="accept-key-2")
    _assert_error(response, 409, "INVALID_STATE")


def test_malformed_accept_requests_are_422(client):
    ticket = _invite(client).json()
    path = f"/api/invitations/{ticket['invitation_id']}/accept"
    _assert_error(
        client.post(path, headers=_h("invitee", "accept-key-1"), json={}), 422, "VALIDATION_FAILED"
    )
    _assert_error(
        client.post(path, headers=_h("invitee", "accept-key-1"), json={"invite_code": "abc"}),
        422,
        "VALIDATION_FAILED",
    )
    _assert_error(
        client.post(
            "/api/invitations/a/accept",
            headers=_h("invitee", "accept-key-1"),
            json={"invite_code": ticket["invite_code"]},
        ),
        422,
        "VALIDATION_FAILED",
    )


def test_concurrent_accepts_create_one_connection(client, migrated_db):
    ticket = _invite(client).json()
    results: list[int] = []
    barrier = threading.Barrier(2)

    def accept(sub: str):
        barrier.wait()
        results.append(
            _accept(client, ticket["invitation_id"], ticket["invite_code"], sub=sub).status_code
        )

    threads = [threading.Thread(target=accept, args=(s,)) for s in ("racer-1", "racer-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [200, 409]
    with psycopg.connect(migrated_db) as connection:
        assert connection.execute("SELECT count(*) FROM connections").fetchone() == (1,)


# --- unit ----------------------------------------------------------------------------


def test_request_hash_is_canonical():
    assert request_hash("r", {"a": 1, "b": 2}) == request_hash("r", {"b": 2, "a": 1})
    assert request_hash("r", {"a": 1}) != request_hash("other", {"a": 1})


def test_code_hash_uses_normalized_code():
    assert code_hash("abcde-fghjk") == code_hash("ABCDEFGHJK")
