"""Alembic environment. Migrations are hand-written SQL (op.execute / op.create_*).

Connection settings come from the environment only and are never printed:
* DATABASE_URL (local Docker, tests, offline --sql review), or
* DB_HOST/DB_USER/AWS_REGION (Aurora in the VPC with an IAM auth token and verified
  TLS, used by the migration Lambda). See umsal_backend.db.

Offline (--sql) mode is supported so a migration can be reviewed as SQL before it is
applied to Aurora.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from umsal_backend.db import STATEMENT_TIMEOUT_MS, database_from_env, database_url

# Migrations may legitimately run longer than API statements.
MIGRATION_STATEMENT_TIMEOUT_MS = STATEMENT_TIMEOUT_MS * 20


def sqlalchemy_url() -> str:
    url = database_url()
    if not url:
        raise SystemExit("DATABASE_URL is not set.")
    # Use the psycopg 3 driver that the application already depends on.
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def run_offline() -> None:
    context.configure(url=sqlalchemy_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def _engine():
    if database_url():
        return create_engine(
            sqlalchemy_url(),
            poolclass=pool.NullPool,
            connect_args={
                "connect_timeout": 3,
                "options": f"-c statement_timeout={MIGRATION_STATEMENT_TIMEOUT_MS}",
            },
        )
    target = database_from_env()
    if target is None:
        raise SystemExit("DATABASE_URL or DB_HOST is not set.")

    def creator():
        connection = target.connect()
        connection.execute(f"SET statement_timeout = {MIGRATION_STATEMENT_TIMEOUT_MS}")
        connection.commit()
        return connection

    return create_engine("postgresql+psycopg://", creator=creator, poolclass=pool.NullPool)


def run_online() -> None:
    with _engine().connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
