"""Database targets: local URL vs Aurora IAM authentication (no AWS access in tests)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from umsal_backend import db

URL = "postgresql://synthetic:synthetic-secret@db.invalid:5432/umsal"


@pytest.fixture
def ca_bundle(tmp_path) -> Path:
    path = tmp_path / "rds-global-bundle.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n")
    return path


def _iam_env(monkeypatch, ca_bundle):
    monkeypatch.setenv("DB_HOST", "umsal.cluster-synthetic.ap-southeast-2.rds.amazonaws.com")
    monkeypatch.setenv("DB_USER", "umsal_app")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("DB_CA_BUNDLE", str(ca_bundle))


def test_no_settings_means_no_database():
    assert db.database_from_env() is None


def test_database_url_wins_over_iam_settings(monkeypatch, ca_bundle):
    _iam_env(monkeypatch, ca_bundle)
    monkeypatch.setenv("DATABASE_URL", URL)
    assert db.database_from_env() == db.UrlTarget(URL)


def test_iam_settings_build_an_iam_target(monkeypatch, ca_bundle):
    _iam_env(monkeypatch, ca_bundle)
    monkeypatch.setenv("DB_NAME", "umsal_test")
    monkeypatch.setenv("DB_PORT", "6543")
    target = db.database_from_env()
    assert isinstance(target, db.IamTarget)
    assert (target.user, target.dbname, target.port, target.region) == (
        "umsal_app",
        "umsal_test",
        6543,
        "ap-southeast-2",
    )
    assert target.ca_bundle == ca_bundle


@pytest.mark.parametrize("missing", ["DB_USER", "AWS_REGION"])
def test_incomplete_iam_settings_fail_at_startup(monkeypatch, ca_bundle, missing):
    _iam_env(monkeypatch, ca_bundle)
    monkeypatch.delenv(missing)
    with pytest.raises(db.DatabaseConfigError):
        db.database_from_env()


def test_missing_ca_bundle_fails_at_startup(monkeypatch, tmp_path):
    with pytest.raises(db.DatabaseConfigError):
        db.IamTarget(host="h", user="u", dbname="d", region="r", ca_bundle=tmp_path / "missing.pem")


def test_iam_token_is_reused_for_ten_minutes(ca_bundle):
    now = [1000.0]
    signer = MagicMock(side_effect=["token-1", "token-2"])
    target = db.IamTarget(
        host="h",
        user="u",
        dbname="d",
        region="r",
        ca_bundle=ca_bundle,
        sign=signer,
        clock=lambda: now[0],
    )
    assert target.token() == "token-1"
    now[0] += db.TOKEN_REUSE_SECONDS - 1
    assert target.token() == "token-1"
    now[0] += 1
    assert target.token() == "token-2"
    signer.assert_called_with("h", 5432, "u", "r")
    assert signer.call_count == 2


def test_iam_connect_verifies_tls_and_uses_token(ca_bundle, monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "8")
    target = db.IamTarget(
        host="h",
        user="u",
        dbname="d",
        region="r",
        ca_bundle=ca_bundle,
        sign=lambda *_: "synthetic-token",
    )
    with patch("umsal_backend.db.psycopg.connect") as connect:
        target.connect(autocommit=True)
    connect.assert_called_once_with(
        host="h",
        port=5432,
        user="u",
        dbname="d",
        password="synthetic-token",
        sslmode="verify-full",
        sslrootcert=str(ca_bundle),
        connect_timeout=8,
        options="-c statement_timeout=3000",
        autocommit=True,
    )


def test_boto3_signer_requests_an_rds_auth_token():
    with patch("boto3.client") as client:
        client.return_value.generate_db_auth_token.return_value = "signed"
        assert db._boto3_signer("h", 5432, "u", "ap-southeast-2") == "signed"
    client.assert_called_once_with("rds", region_name="ap-southeast-2")
    client.return_value.generate_db_auth_token.assert_called_once_with(
        DBHostname="h", Port=5432, DBUsername="u", Region="ap-southeast-2"
    )


def test_url_target_and_probe_use_the_target():
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    target = MagicMock()
    target.connect.return_value = connection
    db.postgres_probe(target)()
    target.connect.assert_called_once_with(autocommit=True)
    with patch("umsal_backend.db.connect") as connect:
        db.UrlTarget(URL).connect()
    connect.assert_called_once_with(URL, autocommit=False)


def test_as_target():
    assert db.as_target(None) is None
    assert db.as_target(URL) == db.UrlTarget(URL)
    target = db.UrlTarget(URL)
    assert db.as_target(target) is target


def test_readiness_uses_iam_target(monkeypatch, ca_bundle):
    _iam_env(monkeypatch, ca_bundle)
    assert set(db.readiness_probes_from_env()) == {"aurora"}


# --- get_db: the request connection is always closed (#56) ----------------------------


class _FakeConnection:
    """Mimics psycopg: ``with`` commits on success, rolls back on error, then closes,
    except that a failing commit raises before close (the case that leaked sessions)."""

    def __init__(self, fail_commit: bool = False):
        self.fail_commit = fail_commit
        self.events: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.events.append("rollback")
        elif self.fail_commit:
            self.events.append("commit-failed")
            raise db.psycopg.OperationalError("synthetic commit failure")
        else:
            self.events.append("commit")
        self.close()
        return False

    def close(self):
        self.events.append("close")


def _request_with(connection: _FakeConnection) -> MagicMock:
    target = MagicMock()
    target.connect.return_value = connection
    request = MagicMock()
    request.app.state.database = target
    return request


def test_get_db_commits_and_closes():
    connection = _FakeConnection()
    dependency = db.get_db(_request_with(connection))
    assert next(dependency) is connection
    with pytest.raises(StopIteration):
        next(dependency)
    assert connection.events == ["commit", "close", "close"]


def test_get_db_closes_even_when_commit_fails():
    connection = _FakeConnection(fail_commit=True)
    dependency = db.get_db(_request_with(connection))
    next(dependency)
    with pytest.raises(db.psycopg.OperationalError, match="synthetic commit failure"):
        next(dependency)
    assert connection.events == ["commit-failed", "close"]


def test_get_db_rolls_back_and_closes_when_the_handler_raises():
    connection = _FakeConnection()
    dependency = db.get_db(_request_with(connection))
    next(dependency)
    with pytest.raises(ValueError, match="handler"):
        dependency.throw(ValueError("handler failed"))
    assert connection.events == ["rollback", "close", "close"]
