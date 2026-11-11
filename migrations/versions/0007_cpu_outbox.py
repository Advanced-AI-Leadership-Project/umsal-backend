"""Durable dispatch and consent snapshot for CPU jobs."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE analysis_outbox (
            job_id text PRIMARY KEY REFERENCES analysis_jobs (id) ON DELETE CASCADE,
            consent_versions jsonb NOT NULL,
            published_at timestamptz
        )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE analysis_outbox")
