from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from umsal_backend import db
from umsal_backend.app import create_app

URL = "postgresql://synthetic:synthetic-secret@db.invalid:5432/umsal"


def _fake_connection(row):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.__enter__.return_value = connection
    return connection, cursor


def test_database_url_from_env(monkeypatch):
    assert db.database_url() is None
    monkeypatch.setenv("DATABASE_URL", "  ")
    assert db.database_url() is None
    monkeypatch.setenv("DATABASE_URL", f" {URL} ")
    assert db.database_url() == URL


def test_connect_uses_short_timeouts():
    with patch("umsal_backend.db.psycopg.connect") as connect:
        db.connect(URL, autocommit=True)
    connect.assert_called_once_with(
        URL, connect_timeout=3, options="-c statement_timeout=3000", autocommit=True
    )


def test_probe_runs_select_one():
    connection, cursor = _fake_connection((1,))
    with patch("umsal_backend.db.psycopg.connect", return_value=connection):
        db.postgres_probe(URL)()
    cursor.execute.assert_called_once_with("SELECT 1")


def test_probe_rejects_unexpected_result():
    connection, _cursor = _fake_connection((0,))
    with (
        patch("umsal_backend.db.psycopg.connect", return_value=connection),
        pytest.raises(db.DatabaseProbeError),
    ):
        db.postgres_probe(URL)()


def test_no_probe_without_database_url():
    assert db.readiness_probes_from_env() == {}


def test_ready_uses_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    connection, _cursor = _fake_connection((1,))
    with patch("umsal_backend.db.psycopg.connect", return_value=connection):
        body = TestClient(create_app()).get("/api/health/ready").json()
    # DynamoDB has no probe yet, so the service as a whole is still not ready.
    assert body["checks"] == {
        "database": "unavailable",
        "aurora": "ok",
        "dynamodb": "not_configured",
    }


def test_unreachable_database_is_unavailable_without_leaking_url(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", URL)
    error = db.psycopg.OperationalError(f"connection to {URL} failed")
    with patch("umsal_backend.db.psycopg.connect", side_effect=error):
        response = TestClient(create_app()).get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["aurora"] == "unavailable"
    assert "synthetic-secret" not in response.text
    assert "synthetic-secret" not in caplog.text
