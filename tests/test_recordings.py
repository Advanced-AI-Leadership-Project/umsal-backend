"""MVP recording upload, finalize and reads (#72) against real PostgreSQL, fake S3."""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from umsal_backend import analysis
from umsal_backend.app import create_app
from umsal_backend.auth import Principal, require_principal
from umsal_backend.storage import ObjectVersion, PresignedPut, StorageUnavailable, sha256_b64

SHA = hashlib.sha256(b"synthetic audio").hexdigest()
SIZE = 1234


class FakeAudio:
    """In-memory versioned bucket: {key: {version_id: (size, checksum_b64)}}."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, tuple[int, str]]] = {}
        self.signed: list[dict] = []
        self.down = False

    def presign_put(self, key, *, content_type, size, sha256_hex, expires_in):
        self.signed.append(
            {"key": key, "type": content_type, "size": size, "expires_in": expires_in}
        )
        checksum = sha256_b64(sha256_hex)
        return PresignedPut(
            url=f"https://audio.test/{key}?X-Amz-Signature=sig{len(self.signed)}",
            headers={
                "Content-Type": content_type,
                "x-amz-checksum-sha256": checksum,
                "x-amz-sdk-checksum-algorithm": "SHA256",
            },
        )

    def put(self, key: str, version_id: str, size: int = SIZE, sha: str = SHA) -> None:
        self.objects.setdefault(key, {})[version_id] = (size, sha256_b64(sha))

    def head_version(self, key, version_id):
        if self.down:
            raise StorageUnavailable
        found = self.objects.get(key, {}).get(version_id)
        return None if found is None else ObjectVersion(size=found[0], checksum_sha256=found[1])

    def delete_version(self, key, version_id):
        self.objects.get(key, {}).pop(version_id, None)

    def delete_versions(self, prefix, *, keep=None):
        removed = 0
        for key, versions in self.objects.items():
            if key.startswith(prefix):
                for version_id in [v for v in versions if v != keep]:
                    del versions[version_id]
                    removed += 1
        return removed

    def remaining_versions(self, prefix):
        return sum(len(v) for k, v in self.objects.items() if k.startswith(prefix))


def _principal(request: Request) -> Principal:
    return Principal(
        subject=request.headers["x-test-sub"],
        username=None,
        client_id="synthetic-client",
        scopes=frozenset(),
        groups=frozenset(),
    )


@pytest.fixture
def audio():
    return FakeAudio()


@pytest.fixture
def client(migrated_db, audio):
    app = create_app(token_verifier=None, database=migrated_db, audio_store=audio)
    app.dependency_overrides[require_principal] = _principal
    return TestClient(app)


def _h(sub: str, key: str | None = None) -> dict[str, str]:
    headers = {"X-Test-Sub": sub}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _consent(client, connection_id: str, sub: str, allowed: bool = True) -> None:
    version = client.get(f"/api/connections/{connection_id}/consents", headers=_h(sub)).json()[
        "mine"
    ]["version"]
    response = client.put(
        f"/api/connections/{connection_id}/consents",
        headers=_h(sub, f"consent-{sub}-{version}"),
        json={
            "expected_version": version,
            "analysis_allowed": allowed,
            "sharing_scopes": ["summary"],
            "verification_preview_allowed_to_other_participant": False,
        },
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def connected(client):
    ticket = client.post("/api/invitations", headers=_h("alice", "invite-key-1"), json={}).json()
    connection_id = client.post(
        f"/api/invitations/{ticket['invitation_id']}/accept",
        headers=_h("bob", "accept-key-1"),
        json={"invite_code": ticket["invite_code"]},
    ).json()["connection_id"]
    client.get("/api/me", headers=_h("carol"))  # a third user, not in the connection
    return connection_id


@pytest.fixture
def consented(client, connected):
    _consent(client, connected, "alice")
    _consent(client, connected, "bob")
    return connected


def _body(connection_id: str, **extra) -> dict:
    body = {
        "connection_id": connection_id,
        "occurred_at": "2026-11-08T09:00:00+09:00",
        "filename": "synthetic.m4a",
        "declared_content_type": "audio/mp4",
        "size_bytes": SIZE,
        "sha256": SHA,
        "participants_permission_confirmed": True,
    }
    body.update(extra)
    return body


def _upload(client, connection_id, key="upload-key-1", sub="alice", **extra):
    return client.post("/api/uploads", headers=_h(sub, key), json=_body(connection_id, **extra))


def _finalize(client, upload_id, version="v1", key="final-key-1", sub="alice", **extra):
    body = {"version_id": version, "sha256": SHA, "size_bytes": SIZE, **extra}
    return client.post(f"/api/uploads/{upload_id}/finalize", headers=_h(sub, key), json=body)


def _assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert set(body) == {"code", "message", "request_id", "retryable"}


def test_upload_finalize_and_read_through_the_stub(
    client, consented, audio, migrated_db, contract_schema
):
    ticket = _upload(client, consented)
    assert ticket.status_code == 201, ticket.text
    contract_schema("UploadTicket").validate(ticket.json())
    t = ticket.json()
    assert t["required_headers"] == {
        "Content-Type": "audio/mp4",
        "x-amz-checksum-sha256": sha256_b64(SHA),
        "x-amz-sdk-checksum-algorithm": "SHA256",
    }
    assert t["declared_limits"] == {
        "max_bytes": 52428800,
        "max_duration_seconds": 600,
        "status": "PROPOSED_LIMITS",
    }
    assert audio.signed[0] == {
        "key": f"recordings/{t['recording_id']}/source",
        "type": "audio/mp4",
        "size": SIZE,
        "expires_in": 900,
    }
    with psycopg.connect(migrated_db) as connection:
        stored = connection.execute(
            "SELECT response_body::text FROM idempotency_keys WHERE route = 'POST /uploads'"
        ).fetchone()[0]
    assert (
        "upload_url" not in stored and "X-Amz-Signature" not in stored
    )  # credentials never stored

    before = client.get(f"/api/recordings/{t['recording_id']}", headers=_h("alice")).json()
    assert before["status"] == "UPLOADING" and before["job_id"] is None

    audio.put(f"recordings/{t['recording_id']}/source", "v0")  # an earlier re-upload
    audio.put(f"recordings/{t['recording_id']}/source", "v1")
    accepted = _finalize(client, t["upload_id"])
    assert accepted.status_code == 202, accepted.text
    contract_schema("Accepted").validate(accepted.json())
    assert accepted.json()["status"] == "PREPARING_INPUT"
    job_id = accepted.json()["resource_id"]
    assert list(audio.objects[f"recordings/{t['recording_id']}/source"]) == [
        "v1"
    ]  # only the pinned version

    recording = client.get(f"/api/recordings/{t['recording_id']}", headers=_h("alice")).json()
    contract_schema("Recording").validate(recording)
    assert recording["status"] == "NEEDS_SPEAKER_CONFIRMATION"
    assert recording["job_id"] == job_id
    assert recording["speaker_intervals"] == analysis.STUB_INTERVALS
    assert recording["local_file_sha256"] == SHA
    assert recording["raw_available"] is False and recording["expires_at"] is None
    assert recording["availability"] == {
        "worker_availability": "UNKNOWN",
        "next_processing_at": None,
    }

    job = client.get(f"/api/jobs/{job_id}", headers=_h("bob")).json()
    contract_schema("Job").validate(job)
    assert job | {"availability": None} == {
        "job_id": job_id,
        "recording_id": t["recording_id"],
        "stage": "AUDIO_PREP",
        "status": "SUCCEEDED",
        "recording_status": "NEEDS_SPEAKER_CONFIRMATION",
        "attempt": 1,
        "failure_code": None,
        "retryable": False,
        "availability": None,
    }
    partner = client.get(f"/api/recordings/{t['recording_id']}", headers=_h("bob")).json()
    assert "local_file_sha256" not in partner  # the uploader's fingerprint only

    for sub in ("alice", "bob"):
        page = client.get("/api/recordings", headers=_h(sub)).json()
        contract_schema("RecordingPage").validate(page)
        assert [item["recording_id"] for item in page["items"]] == [t["recording_id"]]
    assert client.get("/api/recordings", headers=_h("carol")).json() == {
        "items": [],
        "next_cursor": None,
    }
    _assert_error(
        client.get(f"/api/recordings/{t['recording_id']}", headers=_h("carol")), 404, "NOT_FOUND"
    )
    _assert_error(client.get(f"/api/jobs/{job_id}", headers=_h("carol")), 404, "NOT_FOUND")


def test_upload_needs_an_active_connection_and_both_consents(client, connected):
    _assert_error(_upload(client, connected), 403, "CONSENT_REQUIRED")
    _consent(client, connected, "alice")
    _assert_error(_upload(client, connected, key="upload-key-2"), 403, "CONSENT_REQUIRED")
    _consent(client, connected, "bob")
    _assert_error(_upload(client, connected, key="upload-key-3", sub="carol"), 404, "NOT_FOUND")
    client.delete(f"/api/connections/{connected}", headers=_h("bob", "delete-key-1"))
    _assert_error(_upload(client, connected, key="upload-key-4"), 403, "FORBIDDEN")


@pytest.mark.parametrize(
    ("extra", "status", "code"),
    [
        ({"declared_content_type": "video/mp4"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
        ({"size_bytes": 52428801}, 413, "PAYLOAD_TOO_LARGE"),
        ({"participants_permission_confirmed": False}, 422, "VALIDATION_FAILED"),
        ({"size_bytes": "1234"}, 422, "VALIDATION_FAILED"),
        ({"sha256": "A" * 64}, 422, "VALIDATION_FAILED"),
        ({"unexpected": 1}, 422, "VALIDATION_FAILED"),
    ],
)
def test_upload_request_checks(client, consented, extra, status, code):
    _assert_error(_upload(client, consented, **extra), status, code)


def test_open_uploads_are_capped_per_user(client, consented):
    for number in range(3):
        assert _upload(client, consented, key=f"upload-key-{number}").status_code == 201
    _assert_error(_upload(client, consented, key="upload-key-9"), 429, "TOO_MANY_REQUESTS")


def test_upload_replay_signs_a_fresh_url_until_finalized(client, consented, audio):
    first = _upload(client, consented).json()
    again = _upload(client, consented)
    assert again.status_code == 201
    assert again.json()["upload_id"] == first["upload_id"]
    assert again.json()["upload_url"] != first["upload_url"]  # re-signed, not stored
    assert 0 < audio.signed[-1]["expires_in"] <= 900
    _assert_error(_upload(client, consented, filename="other.m4a"), 409, "IDEMPOTENCY_CONFLICT")
    audio.put(f"recordings/{first['recording_id']}/source", "v1")
    assert _finalize(client, first["upload_id"]).status_code == 202
    _assert_error(_upload(client, consented), 409, "INVALID_STATE")


def test_finalize_checks(client, consented, audio, migrated_db):
    ticket = _upload(client, consented).json()
    key = f"recordings/{ticket['recording_id']}/source"
    _assert_error(_finalize(client, ticket["upload_id"], sub="bob"), 404, "NOT_FOUND")
    _assert_error(
        _finalize(client, ticket["upload_id"], key="final-key-2", size_bytes=99),
        422,
        "SEMANTIC_CHECK_FAILED",
    )
    _assert_error(
        _finalize(client, ticket["upload_id"], key="final-key-3"), 422, "SEMANTIC_CHECK_FAILED"
    )  # no version
    audio.put(key, "bad", size=SIZE, sha=hashlib.sha256(b"other").hexdigest())
    _assert_error(
        _finalize(client, ticket["upload_id"], version="bad", key="final-key-4"),
        422,
        "SEMANTIC_CHECK_FAILED",
    )
    assert "bad" not in audio.objects[key]  # a mismatching version is removed
    audio.down = True
    _assert_error(
        _finalize(client, ticket["upload_id"], key="final-key-5"), 503, "TEMPORARILY_UNAVAILABLE"
    )
    audio.down = False
    audio.put(key, "v1")
    first = _finalize(client, ticket["upload_id"], key="final-key-6")
    assert first.status_code == 202
    replay = _finalize(client, ticket["upload_id"], key="final-key-6")
    assert replay.json() == first.json()
    _assert_error(_finalize(client, ticket["upload_id"], key="final-key-7"), 409, "INVALID_STATE")
    with psycopg.connect(migrated_db) as connection:
        assert connection.execute("SELECT count(*) FROM analysis_jobs").fetchone() == (1,)


def test_finalize_rechecks_consent_and_expiry(client, consented, audio, migrated_db):
    ticket = _upload(client, consented).json()
    audio.put(f"recordings/{ticket['recording_id']}/source", "v1")
    _consent(client, consented, "bob", allowed=False)
    _assert_error(_finalize(client, ticket["upload_id"]), 403, "CONSENT_REQUIRED")
    _consent(client, consented, "bob", allowed=True)
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute("UPDATE upload_sessions SET expires_at = now() - interval '1 second'")
    _assert_error(_finalize(client, ticket["upload_id"], key="final-key-2"), 409, "INVALID_STATE")


def _finalized(client, consented, audio, analyzer=None, n: int = 1) -> tuple[str, str]:
    if analyzer is not None:
        client.app.state.analyzer = analyzer
    ticket = _upload(client, consented, key=f"upload-key-{n}").json()
    audio.put(f"recordings/{ticket['recording_id']}/source", "v1")
    accepted = _finalize(client, ticket["upload_id"], key=f"final-key-{n}")
    return ticket["recording_id"], accepted.json()["resource_id"]


class FlakyAnalyzer:
    def __init__(self, failures: int, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    def prepare_audio(self, recording_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise analysis.AnalysisError("BROKEN_INPUT", retryable=self.retryable)
        return analysis.StubAnalyzer().prepare_audio(recording_id)


def _age_job(migrated_db, job_id: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(
            "UPDATE analysis_jobs SET updated_at = now() - interval '1 minute' WHERE id = %s",
            (job_id,),
        )


def test_retryable_failure_requeues_and_a_stale_read_finishes_it(
    client, consented, audio, migrated_db
):
    recording_id, job_id = _finalized(client, consented, audio, FlakyAnalyzer(failures=1))
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["recording_status"], job["attempt"]) == ("QUEUED", "QUEUED_AUDIO", 1)
    _age_job(migrated_db, job_id)
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["recording_status"], job["attempt"]) == (
        "SUCCEEDED",
        "NEEDS_SPEAKER_CONFIRMATION",
        2,
    )


def test_failures_end_after_the_attempt_limit_or_when_not_retryable(
    client, consented, audio, migrated_db
):
    _, job_id = _finalized(client, consented, audio, FlakyAnalyzer(failures=1, retryable=False))
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["recording_status"], job["failure_code"]) == (
        "FAILED",
        "FAILED",
        "BROKEN_INPUT",
    )

    recording_id, job_id = _finalized(client, consented, audio, FlakyAnalyzer(failures=99), n=2)
    for _ in range(analysis.MAX_ATTEMPTS):
        _age_job(migrated_db, job_id)
        client.get(f"/api/recordings/{recording_id}", headers=_h("alice"))
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["attempt"]) == ("FAILED", analysis.MAX_ATTEMPTS)


def test_an_unexpected_error_in_the_stub_never_breaks_the_response(client, consented, audio):
    class Broken:
        def prepare_audio(self, recording_id):
            raise RuntimeError("bug")

    recording_id, job_id = _finalized(client, consented, audio, Broken())
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["recording_status"]) == ("QUEUED", "QUEUED_AUDIO")


def test_an_expired_lease_is_claimed_again_exactly_once(
    client, consented, audio, migrated_db, run_concurrently
):
    class Stop:
        def prepare_audio(self, recording_id):
            raise analysis.AnalysisError("STOP", retryable=True)

    recording_id, job_id = _finalized(client, consented, audio, Stop())
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(
            "UPDATE analysis_jobs SET status = 'RUNNING', "
            "lease_expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (job_id,),
        )
    claims: list = []

    def take():
        with psycopg.connect(migrated_db) as connection:
            claims.append(analysis.claim(connection, job_id))
            connection.commit()

    run_concurrently(take, take)
    assert sorted(claim is not None for claim in claims) == [False, True]


def test_a_deleted_recording_is_hidden_and_its_job_cancelled(client, consented, audio, migrated_db):
    recording_id, job_id = _finalized(client, consented, audio, FlakyAnalyzer(failures=1))
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(
            "UPDATE recordings SET deleted_at = now() WHERE id = %s", (recording_id,)
        )
    _assert_error(
        client.get(f"/api/recordings/{recording_id}", headers=_h("alice")), 404, "NOT_FOUND"
    )
    _assert_error(client.get(f"/api/jobs/{job_id}", headers=_h("alice")), 404, "NOT_FOUND")
    with psycopg.connect(migrated_db) as connection:
        analysis.drain(connection, job_id, analysis.StubAnalyzer())
        assert connection.execute(
            "SELECT status FROM analysis_jobs WHERE id = %s", (job_id,)
        ).fetchone() == ("CANCELLED",)


def test_recordings_page_with_a_cursor(client, consented, audio):
    ids = []
    for number in range(3):
        ticket = _upload(client, consented, key=f"upload-key-{number}").json()
        ids.append(ticket["recording_id"])
    first = client.get("/api/recordings?limit=2", headers=_h("bob")).json()
    assert [item["recording_id"] for item in first["items"]] == ids[::-1][:2]
    second = client.get(
        f"/api/recordings?limit=2&cursor={first['next_cursor']}", headers=_h("bob")
    ).json()
    assert [item["recording_id"] for item in second["items"]] == ids[:1] and second[
        "next_cursor"
    ] is None
    _assert_error(
        client.get("/api/recordings?cursor=not-a-cursor", headers=_h("bob")),
        422,
        "VALIDATION_FAILED",
    )


def test_uploads_answer_503_without_storage(migrated_db):
    app = create_app(token_verifier=None, database=migrated_db, audio_store=None)
    app.dependency_overrides[require_principal] = _principal
    response = TestClient(app).post(
        "/api/uploads", headers=_h("alice", "upload-key-1"), json=_body("con_x")
    )
    _assert_error(response, 503, "TEMPORARILY_UNAVAILABLE")
