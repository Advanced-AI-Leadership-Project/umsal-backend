"""Migration Lambda handler against real PostgreSQL, through the IAM-style target path.

``database_from_env`` is patched to return a URL target while DATABASE_URL is unset, so
migrations/env.py takes the same ``creator`` path that the IAM target uses on AWS.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from umsal_backend import db, migration_lambda

ROOT = Path(__file__).resolve().parents[1]


def test_handler_upgrades_to_head_via_target_creator(migrated_db, monkeypatch, capsys):
    config = Config(str(ROOT / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with monkeypatch.context() as patched:
        patched.setenv("DATABASE_URL", migrated_db)
        command.downgrade(config, "0003")

    monkeypatch.setenv("MIGRATIONS_ROOT", str(ROOT))
    monkeypatch.setattr(db, "database_from_env", lambda: db.UrlTarget(migrated_db))
    result = migration_lambda.handler({}, None)

    assert result == {"before": "0003", "after": head}
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"migrated": result}


def test_handler_requires_database_settings(monkeypatch):
    import pytest

    monkeypatch.setenv("MIGRATIONS_ROOT", str(ROOT))
    with pytest.raises(SystemExit):
        migration_lambda.handler({}, None)
