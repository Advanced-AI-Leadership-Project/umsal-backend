-- One-time database user bootstrap (docs/aws-stage2-design.md). Safe to run again.
-- Executed by umsal_backend.db_bootstrap as the RDS master user, from the temporary
-- db-bootstrap stack. Synthetic data only.
--
-- umsal_migrator owns schema objects (runs Alembic); umsal_app only reads and writes rows
-- (no DDL). Both authenticate with IAM tokens (rds_iam), never passwords.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'umsal_migrator') THEN
        CREATE USER umsal_migrator;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'umsal_app') THEN
        CREATE USER umsal_app;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO umsal_migrator, umsal_app',
                   current_database());
END
$$;

GRANT rds_iam TO umsal_migrator, umsal_app;

-- PostgreSQL 15+ no longer lets every user create objects in "public".
GRANT USAGE, CREATE ON SCHEMA public TO umsal_migrator;
GRANT USAGE ON SCHEMA public TO umsal_app;

-- The RDS master is not a superuser: it must be a member of umsal_migrator to set
-- default privileges on its behalf.
GRANT umsal_migrator TO CURRENT_USER;

-- Tables and sequences the migrator creates later become readable/writable by the app.
ALTER DEFAULT PRIVILEGES FOR ROLE umsal_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO umsal_app;
ALTER DEFAULT PRIVILEGES FOR ROLE umsal_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO umsal_app;
