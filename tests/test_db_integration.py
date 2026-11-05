"""Runs against a real PostgreSQL only when TEST_DATABASE_URL is set (see README).

The database must be disposable: the test applies and reverts migrations.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from umsal_backend import db
from umsal_backend.app import create_app

TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")

ROOT = Path(__file__).resolve().parents[1]


def test_probe_against_real_postgres():
    db.postgres_probe(TEST_URL)()


def test_ready_reports_aurora_ok(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    checks = TestClient(create_app()).get("/api/health/ready").json()["checks"]
    assert checks["aurora"] == "ok"


def test_migrations_upgrade_and_downgrade(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    with db.connect(TEST_URL) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001",)
    command.downgrade(config, "base")
    with db.connect(TEST_URL) as connection:
        assert connection.execute("SELECT count(*) FROM alembic_version").fetchone() == (0,)
