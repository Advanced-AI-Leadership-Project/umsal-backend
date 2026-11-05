"""Invitations and idempotency keys (FR-02 / WF-01, design doc "PostgreSQL 최소 엔터티").

- invitations: only the SHA-256 of the invite code is stored. An invitation is accepted
  at most once and expires.
- idempotency_keys: one row per (actor, route, key). A replay with the same request
  hash returns the stored response; a different hash is a 409. Rows expire and are
  removed lazily, so the raw invite code inside a stored createInvitation response is
  not kept beyond its expiry.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE invitations (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'inv_' || replace(gen_random_uuid()::text, '-', ''),
            code_hash bytea NOT NULL UNIQUE CHECK (octet_length(code_hash) = 32),
            inviter_id text COLLATE "C" NOT NULL REFERENCES users (id),
            expires_at timestamptz NOT NULL,
            accepted_by text COLLATE "C" REFERENCES users (id),
            accepted_at timestamptz,
            connection_id text COLLATE "C" REFERENCES connections (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT invitations_accept_fields CHECK (
                (accepted_by IS NULL AND accepted_at IS NULL AND connection_id IS NULL)
                OR (accepted_by IS NOT NULL AND accepted_at IS NOT NULL
                    AND connection_id IS NOT NULL)
            ),
            CONSTRAINT invitations_not_self CHECK (accepted_by IS DISTINCT FROM inviter_id),
            CONSTRAINT invitations_expiry_after_creation CHECK (expires_at > created_at)
        )
        """
    )
    op.execute("CREATE INDEX invitations_inviter ON invitations (inviter_id)")
    op.execute(
        """
        CREATE TABLE idempotency_keys (
            actor_id text COLLATE "C" NOT NULL REFERENCES users (id),
            route text NOT NULL,
            key text NOT NULL CHECK (char_length(key) BETWEEN 8 AND 128),
            request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
            -- NULL while the first request is still running inside its transaction.
            response_status smallint,
            response_body jsonb,
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (actor_id, route, key)
        )
        """
    )
    op.execute("CREATE INDEX idempotency_keys_expiry ON idempotency_keys (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE idempotency_keys")
    op.execute("DROP TABLE invitations")
