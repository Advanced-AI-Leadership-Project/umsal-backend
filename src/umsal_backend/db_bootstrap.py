"""One-time database user bootstrap Lambda (temporary stack infra/aws/db-bootstrap.yaml).

RDS for PostgreSQL has no Data API, and the app and migration users cannot use IAM auth
until they exist. This function runs inside the VPC, reads the RDS-managed master secret
through a temporary Secrets Manager interface endpoint, and executes the idempotent
``sql/bootstrap_users.sql`` over verified TLS. It returns and logs only role names. The
stack (endpoint included) is deleted right after a successful run.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from importlib import resources
from typing import Any

import psycopg

from umsal_backend.db import DEFAULT_CA_BUNDLE

BOOTSTRAP_ROLES = ("umsal_app", "umsal_migrator")


def bootstrap_sql() -> str:
    return resources.files("umsal_backend").joinpath("sql/bootstrap_users.sql").read_text()


def run(connect: Callable[[], psycopg.Connection]) -> dict[str, list[str]]:
    """Execute the bootstrap SQL and report which bootstrap roles now exist."""
    with connect() as connection:
        connection.execute(bootstrap_sql())
        rows = connection.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(BOOTSTRAP_ROLES),),
        ).fetchall()
    return {"roles": [row[0] for row in rows]}


def _master_credentials(secret_arn: str, region: str) -> tuple[str, str]:
    import boto3  # deferred: only needed on AWS

    secret = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_arn
    )
    values = json.loads(secret["SecretString"])
    return values["username"], values["password"]


def handler(_event: Any, _context: Any) -> dict[str, list[str]]:
    host = os.environ["DB_HOST"]
    region = os.environ["AWS_REGION"]
    user, password = _master_credentials(os.environ["DB_MASTER_SECRET_ARN"], region)

    def connect() -> psycopg.Connection:
        return psycopg.connect(
            host=host,
            port=int(os.environ.get("DB_PORT", "5432")),
            user=user,
            password=password,
            dbname=os.environ.get("DB_NAME", "umsal"),
            sslmode="verify-full",
            sslrootcert=str(DEFAULT_CA_BUNDLE),
            connect_timeout=20,
            autocommit=True,
        )

    result = run(connect)
    print(json.dumps({"bootstrapped": result}))
    return result
