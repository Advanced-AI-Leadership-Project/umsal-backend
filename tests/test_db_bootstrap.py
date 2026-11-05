"""Database user bootstrap: idempotent SQL on real PostgreSQL, handler wiring with mocks.

The local server stands in for RDS: a ``rds_iam`` role is created if missing, and the
test connects as a non-superuser that has CREATEROLE, like the RDS master user.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from umsal_backend import db_bootstrap

MASTER = "umsal_fake_rds_master"


def _cleanup(admin: psycopg.Connection) -> None:
    roles = [
        role
        for (role,) in admin.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (["umsal_app", "umsal_migrator", MASTER],),
        ).fetchall()
    ]
    if {"umsal_migrator", "umsal_app"} <= set(roles):
        # Revoke the migrator's default privileges for the app explicitly first; dropping
        # them implicitly through DROP OWNED can fail on default ACL entries.
        for kind in ("TABLES", "SEQUENCES"):
            admin.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE umsal_migrator IN SCHEMA public "
                f"REVOKE ALL ON {kind} FROM umsal_app"
            )
    for role in ("umsal_migrator", "umsal_app", MASTER):
        if role in roles:
            admin.execute(f'DROP OWNED BY "{role}"')
    for role in roles:
        admin.execute(f'DROP ROLE "{role}"')


@pytest.fixture
def rds_like_master(migrated_db):
    """A CREATEROLE (not superuser) login owning nothing, plus an rds_iam role."""
    admin = psycopg.connect(migrated_db, autocommit=True)
    _cleanup(admin)
    if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'").fetchone():
        admin.execute("CREATE ROLE rds_iam")
    admin.execute(f"CREATE ROLE {MASTER} LOGIN CREATEROLE PASSWORD 'synthetic-master'")
    admin.execute(f"GRANT rds_iam TO {MASTER} WITH ADMIN OPTION")
    dbname = admin.info.dbname
    admin.execute(f'GRANT CREATE, CONNECT ON DATABASE "{dbname}" TO {MASTER} WITH GRANT OPTION')
    admin.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {MASTER} WITH GRANT OPTION")
    info = psycopg.conninfo.conninfo_to_dict(migrated_db)
    info.update(user=MASTER, password="synthetic-master")
    yield lambda: psycopg.connect(**info, autocommit=True)
    _cleanup(admin)
    admin.close()


def test_bootstrap_is_idempotent_and_grants_least_privilege(rds_like_master, migrated_db):
    assert db_bootstrap.run(rds_like_master) == {"roles": ["umsal_app", "umsal_migrator"]}
    assert db_bootstrap.run(rds_like_master) == {"roles": ["umsal_app", "umsal_migrator"]}

    with psycopg.connect(migrated_db, autocommit=True) as admin:
        admin.execute("SET ROLE umsal_migrator")
        admin.execute("CREATE TABLE bootstrap_probe (id serial)")
        admin.execute("RESET ROLE")
        admin.execute("SET ROLE umsal_app")
        # serial needs USAGE on the sequence: checks the SEQUENCES default privilege too.
        admin.execute("INSERT INTO bootstrap_probe DEFAULT VALUES")
        assert admin.execute("SELECT count(*) FROM bootstrap_probe").fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("CREATE TABLE app_must_not_create (id int)")
        admin.execute("RESET ROLE")
        member = admin.execute(
            "SELECT pg_has_role('umsal_app', 'rds_iam', 'member'), "
            "pg_has_role('umsal_migrator', 'rds_iam', 'member')"
        ).fetchone()
        admin.execute("DROP TABLE bootstrap_probe")
    assert member == (True, True)


def test_handler_reads_master_secret_and_verifies_tls(monkeypatch, capsys):
    monkeypatch.setenv("DB_HOST", "umsal.synthetic.ap-southeast-2.rds.amazonaws.com")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("DB_MASTER_SECRET_ARN", "arn:aws:secretsmanager:ap-southeast-2:1:secret:x")
    secret = {"SecretString": json.dumps({"username": "umsal_admin", "password": "synthetic"})}
    connection = MagicMock()
    with (
        patch("boto3.client") as client,
        patch("umsal_backend.db_bootstrap.psycopg.connect", return_value=connection) as connect,
        patch.object(db_bootstrap, "run", side_effect=lambda c: (c(), {"roles": ["umsal_app"]})[1]),
    ):
        client.return_value.get_secret_value.return_value = secret
        assert db_bootstrap.handler({}, None) == {"roles": ["umsal_app"]}
    client.assert_called_once_with("secretsmanager", region_name="ap-southeast-2")
    kwargs = connect.call_args.kwargs
    assert kwargs["user"] == "umsal_admin"
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["dbname"] == "umsal"
    assert kwargs["sslrootcert"] == str(db_bootstrap.DEFAULT_CA_BUNDLE)
    assert kwargs["port"] == 5432
    assert kwargs["autocommit"] is True
    assert kwargs["connect_timeout"] == 20
    output = capsys.readouterr().out
    assert "synthetic" not in output.replace("synthetic.ap-southeast-2", "")
    assert json.loads(output.strip()) == {"bootstrapped": {"roles": ["umsal_app"]}}


def test_bootstrap_sql_is_packaged():
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE umsal_migrator" in db_bootstrap.bootstrap_sql()
