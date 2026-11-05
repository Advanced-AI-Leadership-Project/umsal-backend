"""Runs against a real PostgreSQL via the ``test_database_url`` fixture (see conftest).

The database must be disposable: the test applies and reverts migrations.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from umsal_backend import db
from umsal_backend.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_probe_against_real_postgres(test_database_url):
    db.postgres_probe(test_database_url)()


def test_ready_reports_aurora_ok(monkeypatch, test_database_url):
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    checks = TestClient(create_app()).get("/api/health/ready").json()["checks"]
    assert checks["aurora"] == "ok"


def test_migrations_upgrade_and_downgrade(monkeypatch, test_database_url):
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    config = Config(str(ROOT / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    tables = "SELECT to_regclass('users') IS NOT NULL, to_regclass('connections') IS NOT NULL"
    command.upgrade(config, "head")
    with db.connect(test_database_url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        assert connection.execute(tables).fetchone() == (True, True)
    command.downgrade(config, "base")
    with db.connect(test_database_url) as connection:
        assert connection.execute("SELECT count(*) FROM alembic_version").fetchone() == (0,)
        assert connection.execute(tables).fetchone() == (False, False)
    command.upgrade(config, "head")  # every downgrade path can be re-applied
