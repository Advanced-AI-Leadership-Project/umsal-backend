"""Foreground family voice calls. SDP and audio never enter PostgreSQL."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE voice_calls (
            id text PRIMARY KEY,
            connection_id text NOT NULL REFERENCES connections(id),
            caller_id text NOT NULL REFERENCES users(id),
            callee_id text NOT NULL REFERENCES users(id),
            caller_client_id text NOT NULL,
            callee_client_id text,
            state text NOT NULL CHECK (state IN
                ('RINGING','ACCEPTED','ACTIVE','ENDED','REJECTED','CANCELLED','EXPIRED')),
            created_at timestamptz NOT NULL DEFAULT now(),
            accepted_at timestamptz,
            ended_at timestamptz,
            caller_seen_at timestamptz NOT NULL DEFAULT now(),
            callee_seen_at timestamptz,
            caller_connected boolean NOT NULL DEFAULT false,
            callee_connected boolean NOT NULL DEFAULT false,
            CHECK (caller_id <> callee_id)
        )
        """)
    op.execute("CREATE INDEX voice_calls_caller ON voice_calls(caller_id, created_at DESC)")
    op.execute("CREATE INDEX voice_calls_callee ON voice_calls(callee_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE voice_calls")
