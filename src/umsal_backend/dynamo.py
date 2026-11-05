"""DynamoDB result table settings and the readiness probe (stage 3 foundation, #52).

``DYNAMODB_TABLE`` names the one table that holds result bodies (SPEC storage layout).
Item reads and writes arrive with the result APIs after the MVP scope decision (D8);
until then the API role may only call ``DescribeTable`` on this table.

Inside the VPC the call goes through the DynamoDB gateway endpoint. Short timeouts and a
single attempt keep a broken dependency from holding the readiness request open.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from umsal_backend.health import Probe

CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 3


class DynamoConfigError(RuntimeError):
    """The DynamoDB settings are incomplete; raised at startup, not per request."""


class DynamoProbeError(Exception):
    """DynamoDB answered, but the table is not ready for use."""


ClientFactory = Callable[[str], Any]


def _boto3_client(region: str) -> Any:
    import boto3  # deferred: only needed on AWS
    from botocore.config import Config

    config = Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        # total_max_attempts counts the first call; botocore's max_attempts counts retries.
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    return boto3.client("dynamodb", region_name=region, config=config)


def table_name_from_env() -> str | None:
    value = os.environ.get("DYNAMODB_TABLE", "").strip()
    return value or None


def dynamodb_probe(table: str, region: str, client_factory: ClientFactory = _boto3_client) -> Probe:
    """Readiness: the table exists, this role may describe it, and it is ACTIVE."""
    client = None

    def probe() -> None:
        nonlocal client
        if client is None:  # created on first use and reused by a warm Lambda
            client = client_factory(region)
        status = client.describe_table(TableName=table)["Table"]["TableStatus"]
        if status != "ACTIVE":
            raise DynamoProbeError

    return probe


def dynamodb_probe_from_env(client_factory: ClientFactory = _boto3_client) -> Probe | None:
    table = table_name_from_env()
    if table is None:
        return None
    region = os.environ.get("AWS_REGION", "").strip()
    if not region:
        raise DynamoConfigError("DYNAMODB_TABLE is set but AWS_REGION is missing.")
    return dynamodb_probe(table, region, client_factory)
