"""Synthetic family identities and signaling, real transactional PostgreSQL."""

import uuid

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from umsal_backend.app import create_app
from umsal_backend.auth import Principal, require_principal

A, B = str(uuid.uuid4()), str(uuid.uuid4())
SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


class Calls:
    def __init__(self):
        self.signals = {}

    def put_signal(self, call_id, side, sdp):
        self.signals[call_id, side] = sdp

    def get_signal(self, call_id, side):
        return self.signals.get((call_id, side))

    def ice_servers(self, call_id, side):
        return {
            "ice_servers": [
                {
                    "urls": ["turn:192.0.2.1:3478"],
                    "username": "synthetic",
                    "credential": "synthetic",
                }
            ],
            "expires_at": 2000000000,
        }


def principal(request: Request):
    return Principal(
        subject=request.headers["x-test-sub"],
        username=None,
        client_id="test",
        scopes=frozenset(),
        groups=frozenset(),
    )


@pytest.fixture
def family(migrated_db):
    app = create_app(database=migrated_db, token_verifier=None, call_services=Calls())
    app.dependency_overrides[require_principal] = principal
    with TestClient(app) as client:
        ids = [
            client.get("/api/me", headers=h(sub)).json()["user_id"]
            for sub in ["alice", "bob", "stranger"]
        ]
        with psycopg.connect(migrated_db) as db:
            con = db.execute(
                "INSERT INTO connections(user_a_id,user_b_id,status) "
                "VALUES(%s,%s,'ACTIVE') RETURNING id",
                tuple(sorted(ids[:2])),
            ).fetchone()[0]
        yield client, con, migrated_db


def h(sub="alice", key="synthetic-call-key"):
    return {"X-Test-Sub": sub, "Idempotency-Key": key}


def create(client, con):
    response = client.post("/api/calls", headers=h(), json={"connection_id": con, "client_id": A})
    assert response.status_code == 201, response.text
    return response.json()


def test_complete_call_and_no_analysis_consent_required(family, contract_schema):
    client, con, _ = family
    call = create(client, con)
    contract_schema("VoiceCall").validate(call)
    call_id = call["call_id"]
    assert client.get("/api/calls", headers=h("bob")).json()["call"]["call_id"] == call_id
    assert (
        client.post(f"/api/calls/{call_id}/accept", headers=h("bob"), json={"client_id": B}).json()[
            "state"
        ]
        == "ACCEPTED"
    )
    for sub, owner, kind in [("alice", A, "offer"), ("bob", B, "answer")]:
        assert (
            client.put(
                f"/api/calls/{call_id}/signal",
                headers=h(sub),
                json={"client_id": owner, "type": kind, "sdp": SDP},
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/calls/{call_id}/ice-servers?client_id={owner}", headers=h(sub)
            ).status_code
            == 200
        )
        beat = client.post(
            f"/api/calls/{call_id}/heartbeat",
            headers=h(sub),
            json={"client_id": owner, "connected": True},
        )
        assert beat.status_code == 200, beat.text
    assert beat.json()["state"] == "ACTIVE"
    assert (
        client.get(f"/api/calls/{call_id}/signal?client_id={B}", headers=h("bob")).json()["sdp"]
        == SDP
    )
    ended = client.request(
        "DELETE", f"/api/calls/{call_id}", headers=h("bob"), json={"client_id": B}
    )
    assert ended.json()["state"] == "ENDED"
    assert (
        client.get(f"/api/calls/{call_id}/signal?client_id={B}", headers=h("bob")).status_code
        == 409
    )
    assert client.get("/api/calls", headers=h()).json() == {"call": None}


def test_visibility_busy_idempotence_and_tab_ownership(family):
    client, con, _ = family
    call = create(client, con)
    call_id = call["call_id"]
    assert create(client, con) == call
    assert (
        client.post(
            "/api/calls",
            headers=h(key="another-call-key"),
            json={"connection_id": con, "client_id": A},
        ).json()["code"]
        == "CALL_BUSY"
    )
    assert client.get(f"/api/calls/{call_id}", headers=h("stranger")).status_code == 404
    assert client.get("/api/calls", headers=h("stranger")).json() == {"call": None}
    assert (
        client.get(f"/api/calls/{call_id}/ice-servers?client_id={B}", headers=h()).json()["code"]
        == "CALL_OTHER_DEVICE"
    )
    client.post(f"/api/calls/{call_id}/accept", headers=h("bob"), json={"client_id": B})
    assert (
        client.post(f"/api/calls/{call_id}/accept", headers=h("bob"), json={"client_id": A}).json()[
            "code"
        ]
        == "CALL_OTHER_DEVICE"
    )
    assert (
        client.post(f"/api/calls/{call_id}/accept", headers=h(), json={"client_id": A}).status_code
        == 403
    )


@pytest.mark.parametrize("action", ["expiry", "disconnect"])
def test_liveness_and_disconnection_end_call(family, action):
    client, con, database = family
    call_id = create(client, con)["call_id"]
    with psycopg.connect(database) as db:
        if action == "expiry":
            db.execute(
                "UPDATE voice_calls SET created_at=now()-interval '61 seconds' WHERE id=%s",
                (call_id,),
            )
        else:
            db.execute("UPDATE connections SET status='DISCONNECTED' WHERE id=%s", (con,))
    assert client.get(f"/api/calls/{call_id}", headers=h()).json()["state"] == "EXPIRED"
    assert (
        client.get(f"/api/calls/{call_id}/ice-servers?client_id={A}", headers=h()).status_code
        == 409
    )


def test_reject_cancel_and_audio_only_validation(family):
    client, con, _ = family
    call_id = create(client, con)["call_id"]
    response = client.put(
        f"/api/calls/{call_id}/signal",
        headers=h(),
        json={"client_id": A, "type": "offer", "sdp": SDP + "m=video 9 RTP/AVP 96\r\n"},
    )
    assert response.status_code == 422 and "m=video" not in response.text
    assert (
        client.request(
            "DELETE", f"/api/calls/{call_id}", headers=h("bob"), json={"client_id": B}
        ).json()["state"]
        == "REJECTED"
    )
    assert create(client, con)["state"] == "REJECTED"  # replay reports current state


def test_disabled_without_relay_config(migrated_db):
    app = create_app(database=migrated_db, token_verifier=None, call_services=None)
    app.dependency_overrides[require_principal] = principal
    with TestClient(app) as client:
        assert client.get("/api/calls", headers=h()).status_code == 503
