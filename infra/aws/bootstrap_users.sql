-- One-time database user bootstrap for Aurora (docs/aws-stage2-design.md, step 4).
-- Run each statement with RDS Data API as the managed master user while EnableDataApi
-- is temporarily true, then set EnableDataApi back to false. Synthetic data only.
--
-- umsal_migrator owns the schema objects (runs Alembic); umsal_app only reads and
-- writes rows (no DDL). Both authenticate with IAM tokens (rds_iam), never passwords.

CREATE USER umsal_migrator;
GRANT rds_iam TO umsal_migrator;
CREATE USER umsal_app;
GRANT rds_iam TO umsal_app;

GRANT CONNECT ON DATABASE umsal TO umsal_migrator, umsal_app;
-- PostgreSQL 15+ no longer lets every user create objects in "public".
GRANT USAGE, CREATE ON SCHEMA public TO umsal_migrator;
GRANT USAGE ON SCHEMA public TO umsal_app;

-- Tables and sequences the migrator creates later become readable/writable by the app.
ALTER DEFAULT PRIVILEGES FOR ROLE umsal_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO umsal_app;
ALTER DEFAULT PRIVILEGES FOR ROLE umsal_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO umsal_app;
