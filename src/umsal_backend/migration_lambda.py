"""Migration Lambda (stage 2): applies Alembic migrations to Aurora from inside the VPC.

Packaged separately (``python infra/aws/build_lambda.py --kind migrate``) with Alembic,
SQLAlchemy, ``alembic.ini`` and ``migrations/`` at the package root; the API zip does
not contain Alembic. Only the operator role may invoke it; it is not behind API Gateway.
It runs ``upgrade head`` and returns/logs only revision identifiers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _config():
    from alembic.config import Config

    root = Path(os.environ.get("MIGRATIONS_ROOT", Path(__file__).resolve().parents[1]))
    return Config(str(root / "alembic.ini"))


def _current_revision(config) -> str | None:
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, pool

    from umsal_backend.db import database_from_env

    target = database_from_env()
    if target is None:
        raise SystemExit("DATABASE_URL or DB_HOST is not set.")
    engine = create_engine("postgresql+psycopg://", creator=target.connect, poolclass=pool.NullPool)
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    ScriptDirectory.from_config(config)  # validates the script location is packaged
    return current


def handler(_event: Any, _context: Any) -> dict[str, str | None]:
    from alembic import command

    config = _config()
    before = _current_revision(config)
    command.upgrade(config, "head")
    after = _current_revision(config)
    result = {"before": before, "after": after}
    print(json.dumps({"migrated": result}))
    return result
