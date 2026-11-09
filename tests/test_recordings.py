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
        self.cleanup_down = False

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
        if self.cleanup_down:
            raise StorageUnavailable
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


class FakeResults:
    """In-memory result table: {(recording_id, version): body}."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, int], dict] = {}
        self.down = False
        self.read_down = False

    def put(self, recording_id, version, body):
        if self.down:
            raise StorageUnavailable
        self.items[(recording_id, version)] = body

    def get(self, recording_id, version):
        if self.read_down:
            raise StorageUnavailable
        return self.items.get((recording_id, version))

    def delete(self, recording_id, version):
        self.items.pop((recording_id, version), None)


@pytest.fixture
def audio():
    return FakeAudio()


@pytest.fixture
def results():
    return FakeResults()


@pytest.fixture
def client(migrated_db, audio, results):
    app = create_app(
        token_verifier=None, database=migrated_db, audio_store=audio, result_store=results
    )
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


def test_concurrent_uploads_from_both_participants(client, consented, run_concurrently):
    statuses: list[int] = []

    def upload(sub: str):
        def run():
            statuses.append(_upload(client, consented, key=f"race-{sub}-1", sub=sub).status_code)

        return run

    run_concurrently(upload("alice"), upload("bob"))
    assert statuses == [201, 201]


def test_a_stale_attempt_cannot_reset_a_newer_claim(client, consented, audio, migrated_db):
    class Stop:
        def prepare_audio(self, recording_id):
            raise analysis.AnalysisError("STOP", retryable=True)

    recording_id, job_id = _finalized(client, consented, audio, Stop())
    with psycopg.connect(migrated_db) as connection:
        first = analysis.claim(connection, job_id)  # attempt 2 (attempt 1 ran in finalize)
        connection.execute(
            "UPDATE analysis_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
        connection.commit()
        second = analysis.claim(connection, job_id)  # attempt 3 takes over
        connection.commit()
        analysis.fail(connection, first, analysis.AnalysisError("LATE", retryable=True))
        connection.commit()
        analysis.complete_audio(
            connection, second, analysis.StubAnalyzer().prepare_audio(recording_id)
        )
        connection.commit()
    recording = client.get(f"/api/recordings/{recording_id}", headers=_h("alice")).json()
    assert recording["status"] == "NEEDS_SPEAKER_CONFIRMATION"


def test_a_job_whose_lambda_keeps_dying_fails_after_the_last_attempt(
    client, consented, audio, migrated_db
):
    recording_id, job_id = _finalized(client, consented, audio, FlakyAnalyzer(failures=1))
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(
            "UPDATE analysis_jobs SET status = 'RUNNING', attempt = %s, "
            "lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (analysis.MAX_ATTEMPTS, job_id),
        )
        connection.execute(
            "UPDATE recordings SET status = 'PROCESSING_AUDIO' WHERE id = %s", (recording_id,)
        )
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["recording_status"], job["failure_code"]) == (
        "FAILED",
        "FAILED",
        "ATTEMPTS_EXHAUSTED",
    )


def test_an_out_of_range_cursor_is_a_validation_error(client, consented):
    import base64
    import json

    cursor = base64.urlsafe_b64encode(json.dumps(["0001-01-01T00:00:00+14:00", "rec_x"]).encode())
    response = client.get(
        f"/api/recordings?cursor={cursor.decode().rstrip('=')}", headers=_h("bob")
    )
    _assert_error(response, 422, "VALIDATION_FAILED")


def test_failed_version_cleanup_does_not_fail_the_finalize(client, consented, audio):
    ticket = _upload(client, consented).json()
    key = f"recordings/{ticket['recording_id']}/source"
    audio.put(key, "v0")
    audio.put(key, "v1")
    audio.cleanup_down = True
    accepted = _finalize(client, ticket["upload_id"])
    assert accepted.status_code == 202, accepted.text
    assert set(audio.objects[key]) == {"v0", "v1"}  # left for the deletion and sweep jobs
    recording = client.get(f"/api/recordings/{ticket['recording_id']}", headers=_h("alice")).json()
    assert recording["status"] == "NEEDS_SPEAKER_CONFIRMATION"


# --- MVP stage 4: speaker assignment and results (#75) ---------------------------------


def _share(client, connection_id: str, sub: str, scopes, allowed: bool = True) -> None:
    version = client.get(f"/api/connections/{connection_id}/consents", headers=_h(sub)).json()[
        "mine"
    ]["version"]
    response = client.put(
        f"/api/connections/{connection_id}/consents",
        headers=_h(sub, f"share-{sub}-{version}"),
        json={
            "expected_version": version,
            "analysis_allowed": allowed,
            "sharing_scopes": list(scopes),
            "verification_preview_allowed_to_other_participant": False,
        },
    )
    assert response.status_code == 200, response.text


def _user_id(client, sub: str) -> str:
    return client.get("/api/me", headers=_h(sub)).json()["user_id"]


def _assign(
    client,
    recording_id,
    target_sub="bob",
    speaker="spk_2",
    decision="CONFIRMED",
    version=0,
    key="assign-key-1",
    sub="alice",
    **extra,
):
    body = {
        "expected_version": version,
        "target_subject_id": _user_id(client, target_sub),
        "speaker_id": speaker,
        "decision": decision,
        **extra,
    }
    return client.put(
        f"/api/recordings/{recording_id}/speaker-assignment", headers=_h(sub, key), json=body
    )


def _result(client, recording_id, sub):
    return client.get(f"/api/recordings/{recording_id}/result", headers=_h(sub))


def test_confirm_runs_the_stub_text_stage_and_the_subject_sees_everything(
    client, consented, audio, results, contract_schema
):
    recording_id, _ = _finalized(client, consented, audio)
    accepted = _assign(client, recording_id)
    assert accepted.status_code == 202, accepted.text
    contract_schema("Accepted").validate(accepted.json())
    assert accepted.json()["status"] == "ACCEPTED"
    job_id = accepted.json()["resource_id"]

    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["stage"], job["status"], job["recording_status"]) == (
        "TEXT_ANALYSIS",
        "SUCCEEDED",
        "COMPLETED",
    )
    recording = client.get(f"/api/recordings/{recording_id}", headers=_h("alice")).json()
    assert (recording["speaker_assignment_version"], recording["job_id"]) == (1, job_id)
    stored = results.items[(recording_id, 1)]
    assert stored["demo"] is True and stored["pipeline_version"] == "stub-1"

    mine = _result(client, recording_id, "bob")
    assert mine.status_code == 200, mine.text
    view = mine.json()
    contract_schema("ResultView").validate(view)
    assert view["target_subject_id"] == _user_id(client, "bob")
    assert view["scope"] == ["summary", "repetition", "evidence"]
    assert view["status"] == "OBSERVED" and view["demo"] is True
    assert view["limitations"] == [analysis.DEMO_LIMITATION]
    evidence = view["observations"][0]["evidence"][0]
    assert (evidence["start_ms"], evidence["end_ms"]) == (5000, 10000)  # spk_2's interval

    # alice's view follows bob's grant to her: ["summary"] from _consent.
    shared = _result(client, recording_id, "alice").json()
    contract_schema("ResultView").validate(shared)
    assert shared["scope"] == ["summary"]
    assert "observations" not in shared and shared["summary"] == view["summary"]
    _assert_error(_result(client, recording_id, "carol"), 404, "NOT_FOUND")


def test_scopes_filter_fields_and_strip_citations_without_evidence(
    client, consented, audio, results
):
    recording_id, _ = _finalized(client, consented, audio)
    assert _assign(client, recording_id).status_code == 202
    quoted = {
        **results.items[(recording_id, 1)],
        "summary": '어제 "잠을 못 잤어"라고 utt_1 00:05에 말했어요. ‘힘들다’고도 했어요.',
    }
    results.items[(recording_id, 1)] = quoted

    _share(client, consented, "bob", ["summary", "repetition"])
    view = _result(client, recording_id, "alice").json()
    assert view["scope"] == ["summary", "repetition"]
    assert view["observations"] == [{"category": "WORRY"}]  # categories, no evidence
    for leaked in ("잠을 못 잤어", "utt_1", "00:05", "힘들다", '"', "‘"):
        assert leaked not in view["summary"]

    _share(client, consented, "bob", ["evidence"])
    view = _result(client, recording_id, "alice").json()
    assert view["scope"] == ["evidence"]
    assert "summary" not in view
    assert view["observations"][0]["evidence"][0]["utterance_id"] == "utt_1"

    _share(client, consented, "bob", ["summary", "evidence"])
    assert _result(client, recording_id, "alice").json()["summary"] == quoted["summary"]


def test_result_access_follows_current_consent_and_connection(client, consented, audio):
    recording_id, _ = _finalized(client, consented, audio)
    assert _assign(client, recording_id).status_code == 202
    _share(client, consented, "bob", [])
    _assert_error(_result(client, recording_id, "alice"), 403, "FORBIDDEN")
    _share(client, consented, "bob", ["summary"], allowed=False)
    _assert_error(_result(client, recording_id, "alice"), 403, "CONSENT_REQUIRED")
    assert _result(client, recording_id, "bob").status_code == 200  # the subject always
    _share(client, consented, "bob", ["summary"])
    assert _result(client, recording_id, "alice").status_code == 200
    client.delete(f"/api/connections/{consented}", headers=_h("bob", "delete-key-1"))
    _assert_error(_result(client, recording_id, "alice"), 403, "FORBIDDEN")
    assert _result(client, recording_id, "bob").status_code == 200


def test_assignment_checks(client, consented, audio, migrated_db):
    recording_id, _ = _finalized(client, consented, audio)
    _assert_error(_assign(client, recording_id, sub="bob"), 403, "FORBIDDEN")
    _assert_error(_assign(client, recording_id, sub="carol"), 404, "NOT_FOUND")
    _assert_error(_assign(client, recording_id, version=3), 409, "VERSION_CONFLICT")
    _assert_error(_assign(client, recording_id, target_sub="carol"), 422, "SEMANTIC_CHECK_FAILED")
    _assert_error(_assign(client, recording_id, speaker="spk_9"), 422, "SEMANTIC_CHECK_FAILED")
    for bad in (
        {"decision": "UNSURE"},  # speaker_id must be null
        {"speaker": None},  # CONFIRMED needs a speaker
        {"version": "0"},
        {"unexpected": 1},
    ):
        _assert_error(_assign(client, recording_id, **bad), 422, "VALIDATION_FAILED")
    _consent(client, consented, "bob", allowed=False)
    _assert_error(_assign(client, recording_id), 403, "CONSENT_REQUIRED")
    _consent(client, consented, "bob", allowed=True)

    first = _assign(client, recording_id, key="assign-key-ok")
    assert first.status_code == 202
    assert _assign(client, recording_id, key="assign-key-ok").json() == first.json()  # replay
    _assert_error(
        _assign(client, recording_id, key="assign-key-new"), 409, "VERSION_CONFLICT"
    )  # version 0 is gone
    with psycopg.connect(migrated_db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM analysis_jobs WHERE stage = 'TEXT_ANALYSIS'"
        ).fetchone() == (1,)


def test_assignment_and_result_wait_for_the_audio_stage(client, consented, audio):
    recording_id, _ = _finalized(client, consented, audio, FlakyAnalyzer(failures=1))
    _assert_error(_assign(client, recording_id), 409, "INVALID_STATE")
    response = _result(client, recording_id, "alice")
    _assert_error(response, 409, "RESULT_NOT_READY")
    assert response.json()["retryable"] is True


def test_unsure_abstains_and_a_later_confirmation_replaces_it(
    client, consented, audio, results, contract_schema
):
    recording_id, _ = _finalized(client, consented, audio)
    unsure = _assign(client, recording_id, speaker=None, decision="UNSURE")
    assert unsure.status_code == 202 and unsure.json()["resource_id"] == recording_id
    recording = client.get(f"/api/recordings/{recording_id}", headers=_h("alice")).json()
    assert (recording["status"], recording["speaker_assignment_version"]) == ("ABSTAINED", 1)
    view = _result(client, recording_id, "bob").json()
    contract_schema("ResultView").validate(view)
    assert (view["status"], view["scope"], view["demo"]) == ("ABSTAINED", [], True)
    _assert_error(
        _assign(client, recording_id, speaker=None, decision="UNSURE", version=1, key="assign-k-2"),
        409,
        "INVALID_STATE",
    )

    # Confirm alice instead; then re-confirm bob: only the current version is shown.
    assert (
        _assign(client, recording_id, "alice", "spk_1", version=1, key="assign-k-3").status_code
        == 202
    )
    assert _result(client, recording_id, "alice").json()["target_subject_id"] == _user_id(
        client, "alice"
    )
    assert (
        _assign(client, recording_id, "bob", "spk_2", version=2, key="assign-k-4").status_code
        == 202
    )
    assert set(results.items) == {(recording_id, 2), (recording_id, 3)}
    view = _result(client, recording_id, "bob").json()
    assert view["target_subject_id"] == _user_id(client, "bob")
    assert view["observations"][0]["evidence"][0]["start_ms"] == 5000


def test_text_stage_without_a_result_store_fails_for_good(migrated_db, audio):
    app = create_app(
        token_verifier=None, database=migrated_db, audio_store=audio, result_store=None
    )
    app.dependency_overrides[require_principal] = _principal
    client = TestClient(app)
    ticket = client.post("/api/invitations", headers=_h("alice", "invite-key-1"), json={}).json()
    connection_id = client.post(
        f"/api/invitations/{ticket['invitation_id']}/accept",
        headers=_h("bob", "accept-key-1"),
        json={"invite_code": ticket["invite_code"]},
    ).json()["connection_id"]
    _consent(client, connection_id, "alice")
    _consent(client, connection_id, "bob")
    recording_id, _ = _finalized(client, connection_id, audio)
    job_id = _assign(client, recording_id).json()["resource_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["failure_code"]) == ("FAILED", "STAGE_NOT_AVAILABLE")
    response = _result(client, recording_id, "bob")
    _assert_error(response, 409, "RESULT_NOT_READY")
    assert response.json()["retryable"] is False


def test_a_result_store_outage_requeues_and_a_stale_read_finishes(
    client, consented, audio, results, migrated_db
):
    recording_id, _ = _finalized(client, consented, audio)
    results.down = True
    job_id = _assign(client, recording_id).json()["resource_id"]
    job = client.get(f"/api/jobs/{job_id}", headers=_h("alice")).json()
    assert (job["status"], job["recording_status"]) == ("QUEUED", "QUEUED_TEXT")
    results.down = False
    _age_job(migrated_db, job_id)
    assert _result(client, recording_id, "bob").status_code == 200  # the read re-drains

    results.read_down = True
    _assert_error(_result(client, recording_id, "bob"), 503, "TEMPORARILY_UNAVAILABLE")


def test_a_deleted_recording_gets_no_result(client, consented, audio, results, migrated_db):
    recording_id, _ = _finalized(client, consented, audio)
    results.down = True
    job_id = _assign(client, recording_id).json()["resource_id"]
    results.down = False
    with psycopg.connect(migrated_db, autocommit=True) as connection:
        connection.execute(
            "UPDATE recordings SET deleted_at = now() WHERE id = %s", (recording_id,)
        )
    with psycopg.connect(migrated_db) as connection:
        analysis.drain(connection, job_id, analysis.StubAnalyzer(), results)
        assert connection.execute(
            "SELECT status FROM analysis_jobs WHERE id = %s", (job_id,)
        ).fetchone() == ("CANCELLED",)
    assert results.items == {}
    _assert_error(_result(client, recording_id, "bob"), 404, "NOT_FOUND")


def test_a_text_job_for_an_old_version_is_cancelled_without_writing(
    client, consented, audio, results, migrated_db
):
    recording_id, _ = _finalized(client, consented, audio)
    results.down = True
    old_job = _assign(client, recording_id).json()["resource_id"]  # QUEUED, version 1
    results.down = False
    stub = analysis.StubAnalyzer().analyze_text(recording_id, "spk_2")
    with psycopg.connect(migrated_db) as connection:
        claimed = analysis.claim(connection, old_job)
        connection.execute(
            "UPDATE recordings SET speaker_assignment_version = 2 WHERE id = %s", (recording_id,)
        )
        connection.commit()
        analysis.complete_text(connection, claimed, stub, results)
        connection.commit()
        assert connection.execute(
            "SELECT status FROM analysis_jobs WHERE id = %s", (old_job,)
        ).fetchone() == ("CANCELLED",)
    assert results.items == {}


def test_dynamo_result_store_calls():
    from botocore.session import Session
    from botocore.stub import Stubber

    from umsal_backend.results import DynamoResultStore

    client = Session().create_client(
        "dynamodb", region_name="ap-southeast-2", aws_access_key_id="x", aws_secret_access_key="y"
    )
    store = DynamoResultStore("results-table", "ap-southeast-2", client=client)
    key = {"pk": {"S": "REC#rec_1"}, "sk": {"S": "RESULT#2"}}
    with Stubber(client) as stub:
        stub.add_response(
            "put_item",
            {},
            {"TableName": "results-table", "Item": {**key, "body": {"S": '{"a": 1}'}}},
        )
        stub.add_response(
            "get_item",
            {"Item": {**key, "body": {"S": '{"a": 1}'}}},
            {"TableName": "results-table", "Key": key, "ConsistentRead": True},
        )
        stub.add_response(
            "get_item", {}, {"TableName": "results-table", "Key": key, "ConsistentRead": True}
        )
        stub.add_response("delete_item", {}, {"TableName": "results-table", "Key": key})
        stub.add_client_error("get_item", "ProvisionedThroughputExceededException")
        store.put("rec_1", 2, {"a": 1})
        assert store.get("rec_1", 2) == {"a": 1}
        assert store.get("rec_1", 2) is None
        store.delete("rec_1", 2)
        with pytest.raises(StorageUnavailable):
            store.get("rec_1", 2)


def test_result_store_from_env(monkeypatch):
    from umsal_backend.results import result_store_from_env

    assert result_store_from_env() is None
    monkeypatch.setenv("DYNAMODB_TABLE", "results-table")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    store = result_store_from_env()
    assert (store.table, store.region) == ("results-table", "ap-southeast-2")


def test_without_citations():
    from umsal_backend.assignments import without_citations

    text = "그는 「괜찮아」 『정말』 “응” 하고 12:30:05에 utt_9-a 답했다."
    assert without_citations(text) == "그는 하고 에 답했다."
