"""Fence analysis permission changes independently of sharing settings."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE consent_grants ADD COLUMN analysis_revision integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE analysis_jobs ADD COLUMN consent_versions jsonb")
    op.execute("""
        CREATE FUNCTION bump_analysis_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.analysis_revision := OLD.analysis_revision +
                CASE WHEN NEW.analysis_allowed IS DISTINCT FROM OLD.analysis_allowed
                     THEN 1 ELSE 0 END;
            RETURN NEW;
        END;
        $$
        """)
    op.execute("""
        CREATE TRIGGER consent_analysis_revision BEFORE UPDATE ON consent_grants
        FOR EACH ROW EXECUTE FUNCTION bump_analysis_revision()
        """)


def downgrade() -> None:
    op.execute("DROP TRIGGER consent_analysis_revision ON consent_grants")
    op.execute("DROP FUNCTION bump_analysis_revision()")
    op.execute("ALTER TABLE analysis_jobs DROP COLUMN consent_versions")
    op.execute("ALTER TABLE consent_grants DROP COLUMN analysis_revision")
