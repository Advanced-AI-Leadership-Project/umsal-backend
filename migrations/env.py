"""Alembic environment. Migrations are hand-written SQL (op.execute / op.create_*).

The URL comes from DATABASE_URL only; it is never printed. Offline (--sql) mode is
supported so a migration can be reviewed as SQL before it is applied to Aurora.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from umsal_backend.db import STATEMENT_TIMEOUT_MS, database_url


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


def run_online() -> None:
    engine = create_engine(
        sqlalchemy_url(),
        poolclass=pool.NullPool,
        connect_args={
            "connect_timeout": 3,
            # Migrations may legitimately run longer than API statements.
            "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS * 20}",
        },
    )
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
