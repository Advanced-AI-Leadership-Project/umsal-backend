"""Per-connection labels (D10, #62) against real PostgreSQL."""

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


def _connect(client, inviter_label=None, invitee_label=None):
    """Alice invites Bob, Bob accepts. Returns (connection_id, accept response JSON)."""
    invite_body = {} if inviter_label is None else {"label": inviter_label}
    ticket = client.post("/api/invitations", headers=_h("alice", "invite-key-1"), json=invite_body)
    assert ticket.status_code == 201, ticket.text
    accept_body = {"invite_code": ticket.json()["invite_code"]}
    if invitee_label is not None:
        accept_body["label"] = invitee_label
    accepted = client.post(
        f"/api/invitations/{ticket.json()['invitation_id']}/accept",
        headers=_h("bob", "accept-key-1"),
        json=accept_body,
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["connection_id"], accepted.json()


def _label(client, connection_id, sub):
    response = client.get(f"/api/connections/{connection_id}", headers=_h(sub))
    assert response.status_code == 200, response.text
    return response.json()["label"]


def _put(client, connection_id, sub, key, body):
    return client.put(f"/api/connections/{connection_id}/label", headers=_h(sub, key), json=body)


def _assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_each_side_sees_only_its_own_label(client, contract_schema):
    connection_id, accepted = _connect(client, inviter_label="아들", invitee_label=" 엄마 ")
    contract_schema("Connection").validate(accepted)
    # The accept response is Bob's view: Bob's label, trimmed.
    assert accepted["label"] == "엄마"
    assert _label(client, connection_id, "alice") == "아들"
    assert _label(client, connection_id, "bob") == "엄마"
    listed = client.get("/api/connections", headers=_h("alice")).json()["items"]
    assert [item["label"] for item in listed] == ["아들"]
    contract_schema("ConnectionPage").validate(
        client.get("/api/connections", headers=_h("alice")).json()
    )
    # Labels never change the participants' display names.
    assert {p["display_name"] for p in accepted["participants"]} == {""}


def test_labels_are_optional_and_null_until_set(client, contract_schema):
    connection_id, accepted = _connect(client)
    assert accepted["label"] is None
    assert _label(client, connection_id, "alice") is None
    contract_schema("Connection").validate(accepted)


def test_set_label_updates_only_the_caller(client, contract_schema):
    connection_id, _ = _connect(client, inviter_label="아들")
    response = _put(client, connection_id, "bob", "label-key-1", {"label": "어머니"})
    assert response.status_code == 200, response.text
    contract_schema("Connection").validate(response.json())
    assert response.json()["label"] == "어머니"
    again = _put(client, connection_id, "bob", "label-key-2", {"label": "엄마"})
    assert again.json()["label"] == "엄마"
    assert _label(client, connection_id, "bob") == "엄마"
    assert _label(client, connection_id, "alice") == "아들"


def test_set_label_replays_and_rejects_key_reuse(client, migrated_db):
    connection_id, _ = _connect(client)
    first = _put(client, connection_id, "bob", "same-key-1", {"label": "엄마"})
    replay = _put(client, connection_id, "bob", "same-key-1", {"label": "엄마"})
    assert replay.status_code == 200 and replay.json() == first.json()
    _assert_error(
        _put(client, connection_id, "bob", "same-key-1", {"label": "아빠"}),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    with psycopg.connect(migrated_db) as connection:
        assert connection.execute("SELECT label FROM connection_labels").fetchall() == [("엄마",)]


def test_set_label_requires_an_idempotency_key(client):
    connection_id, _ = _connect(client)
    response = client.put(
        f"/api/connections/{connection_id}/label", headers=_h("bob"), json={"label": "엄마"}
    )
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_non_participant_gets_not_found(client):
    connection_id, _ = _connect(client)
    _assert_error(
        _put(client, connection_id, "mallory", "label-key-1", {"label": "x"}), 404, "NOT_FOUND"
    )
    _assert_error(
        _put(client, "con_missing", "bob", "label-key-2", {"label": "x"}), 404, "NOT_FOUND"
    )


def test_disconnected_connection_cannot_be_relabelled(client):
    connection_id, _ = _connect(client, invitee_label="엄마")
    gone = client.delete(f"/api/connections/{connection_id}", headers=_h("bob", "disconnect-key-1"))
    assert gone.status_code == 202, gone.text
    _assert_error(
        _put(client, connection_id, "bob", "label-key-1", {"label": "x"}), 409, "INVALID_STATE"
    )
    # The existing label stays readable to its owner.
    assert _label(client, connection_id, "bob") == "엄마"


@pytest.mark.parametrize(
    "body",
    [
        {"label": ""},
        {"label": "   "},
        {"label": "가" * 17},
        {"label": "엄\n마"},
        {"label": "엄\u0085마"},
        {"label": 1},
        {"label": "엄마", "extra": True},
        {},
    ],
)
def test_invalid_label_is_rejected(client, body):
    connection_id, _ = _connect(client)
    _assert_error(_put(client, connection_id, "bob", "label-key-1", body), 422, "VALIDATION_FAILED")


def test_sixteen_characters_are_accepted(client):
    connection_id, _ = _connect(client)
    response = _put(client, connection_id, "bob", "label-key-1", {"label": "가" * 16})
    assert response.status_code == 200 and response.json()["label"] == "가" * 16


@pytest.mark.parametrize("route", ["invite", "accept"])
def test_invitation_labels_are_validated(client, route):
    if route == "invite":
        response = client.post(
            "/api/invitations", headers=_h("alice", "invite-key-1"), json={"label": "가" * 17}
        )
    else:
        ticket = client.post(
            "/api/invitations", headers=_h("alice", "invite-key-1"), json={}
        ).json()
        response = client.post(
            f"/api/invitations/{ticket['invitation_id']}/accept",
            headers=_h("bob", "accept-key-1"),
            json={"invite_code": ticket["invite_code"], "label": "\t"},
        )
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_database_rejects_labels_the_api_would_reject(client, migrated_db):
    connection_id, _ = _connect(client)
    with psycopg.connect(migrated_db) as connection:
        owner = connection.execute("SELECT user_a_id FROM connections").fetchone()[0]
        for bad in ["", " 엄마", "가" * 17, "엄\x01마"]:
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                connection.execute(
                    "INSERT INTO connection_labels (connection_id, owner_id, label) "
                    "VALUES (%s, %s, %s)",
                    (connection_id, owner, bad),
                )
