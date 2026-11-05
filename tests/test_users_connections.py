"""GET /api/me, GET /api/connections, GET /api/connections/{id} against real PostgreSQL.

Authentication is replaced by a test stand-in that trusts an ``X-Test-Sub`` header;
token verification itself is covered by test_auth.py.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from umsal_backend.app import create_app
from umsal_backend.auth import (
    CognitoSettings,
    KeyNotFound,
    Principal,
    TokenVerifier,
    require_principal,
)
from umsal_backend.connections import decode_cursor, encode_cursor
from umsal_backend.errors import ApiError


def _principal(request: Request) -> Principal:
    return Principal(
        subject=request.headers["x-test-sub"],
        username=None,
        client_id="synthetic-client",
        scopes=frozenset(),
        groups=frozenset(),
    )


def _app(database):
    app = create_app(token_verifier=None, database=database)
    app.dependency_overrides[require_principal] = _principal
    return app


@pytest.fixture
def client(migrated_db):
    return TestClient(_app(migrated_db))


def _as(sub: str) -> dict[str, str]:
    return {"X-Test-Sub": sub}


def _user_id(client, sub: str) -> str:
    return client.get("/api/me", headers=_as(sub)).json()["user_id"]


def _connect(url: str, a: str, b: str, status: str = "ACTIVE") -> str:
    low, high = sorted((a, b))
    with psycopg.connect(url, autocommit=True) as connection:
        return connection.execute(
            "INSERT INTO connections (user_a_id, user_b_id, status) "
            "VALUES (%s, %s, %s) RETURNING id",
            (low, high, status),
        ).fetchone()[0]


def _assert_error(response, status: int, code: str):
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert set(body) == {"code", "message", "request_id", "retryable"}


# --- /api/me ---------------------------------------------------------------------


def test_me_creates_user_once_and_matches_contract(client, contract_schema):
    first = client.get("/api/me", headers=_as("sub-alice"))
    second = client.get("/api/me", headers=_as("sub-alice"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["user_id"].startswith("usr_")
    assert first.json()["display_name"] == ""
    assert "sub-alice" not in first.text
    contract_schema("Actor").validate(first.json())


def test_different_subjects_are_different_users(client):
    assert _user_id(client, "sub-alice") != _user_id(client, "sub-bob")


def test_user_apis_answer_503_without_database():
    response = TestClient(_app(None)).get("/api/me", headers=_as("sub"))
    _assert_error(response, 503, "TEMPORARILY_UNAVAILABLE")


def test_unreachable_database_is_retryable_503_without_leaking_url():
    url = "postgresql://synthetic:synthetic-secret@127.0.0.1:1/umsal"
    response = TestClient(_app(url)).get("/api/me", headers=_as("sub"))
    _assert_error(response, 503, "TEMPORARILY_UNAVAILABLE")
    assert response.json()["retryable"] is True
    assert "synthetic-secret" not in response.text


def _no_keys(_kid):
    raise KeyNotFound


@pytest.mark.parametrize("header", [None, "Bearer not-a-jwt"], ids=["no-token", "bad-token"])
def test_authentication_fails_before_opening_a_connection(header):
    # The database is unreachable: a 401 (not 503) proves auth ran before get_db.
    settings = CognitoSettings("ap-southeast-2", "ap-southeast-2_SYNTH", frozenset({"c"}))
    app = create_app(
        token_verifier=TokenVerifier(settings, _no_keys),
        database="postgresql://synthetic:synthetic@127.0.0.1:1/umsal",
    )
    headers = {"Authorization": header} if header else {}
    client = TestClient(app)
    for path in ("/api/me", "/api/connections", "/api/connections/con_x1"):
        _assert_error(client.get(path, headers=headers), 401, "UNAUTHENTICATED")


def test_failed_commit_is_503_not_a_sent_200(client, monkeypatch):
    def fail_commit(_self):
        raise psycopg.OperationalError("synthetic commit failure")

    monkeypatch.setattr(psycopg.Connection, "commit", fail_commit)
    response = client.get("/api/me", headers=_as("sub-commit"))
    _assert_error(response, 503, "TEMPORARILY_UNAVAILABLE")


def test_user_apis_require_authentication(migrated_db):
    # Real auth dependency without Cognito settings fails closed (503).
    client = TestClient(create_app(token_verifier=None, database=migrated_db))
    _assert_error(client.get("/api/me"), 503, "TEMPORARILY_UNAVAILABLE")
    _assert_error(client.get("/api/connections"), 503, "TEMPORARILY_UNAVAILABLE")


# --- /api/connections -------------------------------------------------------------


def test_list_shows_only_my_connections(client, migrated_db, contract_schema):
    alice, bob, carol = (_user_id(client, s) for s in ("a", "b", "c"))
    _user_id(client, "d")
    ab = _connect(migrated_db, alice, bob)
    ac = _connect(migrated_db, alice, carol, "PENDING")

    page = client.get("/api/connections", headers=_as("a")).json()
    contract_schema("ConnectionPage").validate(page)
    assert {item["connection_id"] for item in page["items"]} == {ab, ac}
    assert page["next_cursor"] is None

    bob_page = client.get("/api/connections", headers=_as("b")).json()
    assert [item["connection_id"] for item in bob_page["items"]] == [ab]
    assert {p["user_id"] for p in bob_page["items"][0]["participants"]} == {alice, bob}

    empty = client.get("/api/connections", headers=_as("d")).json()
    assert empty == {"items": [], "next_cursor": None}


def test_list_pages_newest_first_without_gaps_or_duplicates(client, migrated_db):
    me = _user_id(client, "me")
    created = [_connect(migrated_db, me, _user_id(client, f"other-{i}")) for i in range(5)]

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get("/api/connections", headers=_as("me"), params=params).json()
        seen += [item["connection_id"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == list(reversed(created))


def test_default_limit_is_twenty(client, migrated_db):
    me = _user_id(client, "me")
    for i in range(21):
        _connect(migrated_db, me, _user_id(client, f"o{i}"))
    page = client.get("/api/connections", headers=_as("me")).json()
    assert len(page["items"]) == 20
    assert page["next_cursor"] is not None


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": "x"},
        {"cursor": "not-a-cursor"},
        {"cursor": "W10"},  # base64 of "[]"
    ],
    ids=["limit-0", "limit-51", "limit-text", "garbage-cursor", "empty-array-cursor"],
)
def test_invalid_query_is_422_in_contract_shape(client, params):
    response = client.get("/api/connections", headers=_as("me"), params=params)
    _assert_error(response, 422, "VALIDATION_FAILED")
    assert "not-a-cursor" not in response.text


@pytest.mark.parametrize(
    "connection_id",
    ["bad" + chr(0) + "id", chr(0xD800) + "surrogate", "x", "has space", 7],
    ids=["nul", "lone-surrogate", "too-short", "space", "not-a-string"],
)
def test_tampered_cursor_id_is_422(client, connection_id):
    raw = json.dumps(["2026-11-05T00:00:00+00:00", connection_id])  # ASCII-escaped
    cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    response = client.get("/api/connections", headers=_as("me"), params={"cursor": cursor})
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_cursor_naming_another_users_connection_leaks_nothing(client, migrated_db):
    alice, bob = _user_id(client, "a"), _user_id(client, "b")
    _user_id(client, "intruder")
    theirs = _connect(migrated_db, alice, bob)
    future = encode_cursor(datetime(2999, 1, 1, tzinfo=UTC), theirs)
    page = client.get("/api/connections", headers=_as("intruder"), params={"cursor": future})
    assert page.json() == {"items": [], "next_cursor": None}


def test_cursor_round_trip_and_rejects_naive_time():
    moment = datetime(2026, 11, 5, 12, 0, 0, 123456, tzinfo=UTC)
    assert decode_cursor(encode_cursor(moment, "con_x")) == (moment, "con_x")
    with pytest.raises(ApiError) as excinfo:
        decode_cursor(encode_cursor(datetime(2026, 11, 5), "con_x"))
    assert excinfo.value.status_code == 422


# --- /api/connections/{id} --------------------------------------------------------


def test_get_connection_for_participant(client, migrated_db, contract_schema):
    alice, bob = _user_id(client, "a"), _user_id(client, "b")
    connection_id = _connect(migrated_db, alice, bob)
    for sub in ("a", "b"):
        response = client.get(f"/api/connections/{connection_id}", headers=_as(sub))
        assert response.status_code == 200
        contract_schema("Connection").validate(response.json())
        assert response.json()["status"] == "ACTIVE"
        assert response.json()["version"] == 1


def test_other_users_connection_is_indistinguishable_from_missing(client, migrated_db):
    alice, bob = _user_id(client, "a"), _user_id(client, "b")
    _user_id(client, "intruder")
    connection_id = _connect(migrated_db, alice, bob)
    hidden = client.get(f"/api/connections/{connection_id}", headers=_as("intruder"))
    missing = client.get("/api/connections/con_doesnotexist", headers=_as("intruder"))
    _assert_error(hidden, 404, "NOT_FOUND")
    _assert_error(missing, 404, "NOT_FOUND")
    assert hidden.json()["message"] == missing.json()["message"]


def test_malformed_connection_id_is_422(client):
    response = client.get("/api/connections/a", headers=_as("me"))
    _assert_error(response, 422, "VALIDATION_FAILED")


# --- schema constraints -------------------------------------------------------------


def test_schema_rejects_reversed_self_duplicate_and_unknown_status(client, migrated_db):
    alice, bob = sorted((_user_id(client, "a"), _user_id(client, "b")))
    insert = "INSERT INTO connections (user_a_id, user_b_id, status) VALUES (%s, %s, %s)"
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(insert, (alice, bob, "ACTIVE"))
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert, (bob, alice, "ACTIVE"))  # not canonical order
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert, (alice, alice, "ACTIVE"))  # same user twice
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(insert, (alice, bob, "PENDING"))  # second live pair
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert, (alice, bob, "UNKNOWN"))
        connection.execute("UPDATE connections SET status = 'DISCONNECTED'")
        connection.execute(insert, (alice, bob, "PENDING"))  # allowed after disconnect
