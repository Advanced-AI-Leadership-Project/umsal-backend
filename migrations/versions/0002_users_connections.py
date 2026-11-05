"""Users and connections (design doc "PostgreSQL 최소 엔터티").

- users: one row per Cognito ``sub``. Only a display name is stored; no phone number
  or email is needed by the backend.
- connections: a pair of users stored in canonical order (``user_a_id < user_b_id``,
  compared byte-wise via COLLATE "C" so it matches Python ``sorted``)
  so the same pair cannot appear twice in different orders; at most one
  non-disconnected connection per pair.

IDs are opaque prefixed strings that match the contract pattern
``^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$``. ``gen_random_uuid()`` is built in from
PostgreSQL 13, so Aurora PostgreSQL must be 13 or later (planned: 17).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'usr_' || replace(gen_random_uuid()::text, '-', ''),
            cognito_sub text NOT NULL UNIQUE CHECK (cognito_sub <> ''),
            display_name text NOT NULL DEFAULT ''
                CHECK (char_length(display_name) <= 80),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE connections (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'con_' || replace(gen_random_uuid()::text, '-', ''),
            user_a_id text COLLATE "C" NOT NULL REFERENCES users (id),
            user_b_id text COLLATE "C" NOT NULL REFERENCES users (id),
            status text NOT NULL
                CHECK (status IN ('PENDING', 'ACTIVE', 'DISCONNECTED')),
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT connections_canonical_pair CHECK (user_a_id < user_b_id)
        )
        """
    )
    # At most one live (pending or active) connection per pair.
    op.execute(
        """
        CREATE UNIQUE INDEX connections_live_pair
            ON connections (user_a_id, user_b_id)
            WHERE status <> 'DISCONNECTED'
        """
    )
    # "My connections" lookups from either side, newest first (cursor pagination).
    op.execute(
        "CREATE INDEX connections_user_a ON connections (user_a_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX connections_user_b ON connections (user_b_id, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE connections")
    op.execute("DROP TABLE users")
