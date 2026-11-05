"""DynamoDB readiness probe (#52). botocore Stubber answers instead of AWS; no network."""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from umsal_backend import app as app_module
from umsal_backend import dynamo
from umsal_backend.app import create_app

TABLE = "synthetic-results"
REGION = "ap-southeast-2"


@pytest.fixture
def stubbed():
    client = boto3.client(
        "dynamodb",
        region_name=REGION,
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",  # noqa: S106 - fake value for the stub
    )
    with Stubber(client) as stubber:
        yield client, stubber


def _describe(stubber: Stubber, status: str) -> None:
    stubber.add_response(
        "describe_table",
        {"Table": {"TableName": TABLE, "TableStatus": status}},
        {"TableName": TABLE},
    )


def test_active_table_is_ready(stubbed):
    client, stubber = stubbed
    _describe(stubber, "ACTIVE")
    dynamo.dynamodb_probe(TABLE, REGION, lambda _region: client)()
    stubber.assert_no_pending_responses()


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "DELETING", "ARCHIVED"])
def test_table_that_is_not_active_fails(stubbed, status):
    client, stubber = stubbed
    _describe(stubber, status)
    with pytest.raises(dynamo.DynamoProbeError):
        dynamo.dynamodb_probe(TABLE, REGION, lambda _region: client)()


def test_service_error_propagates_to_the_health_layer(stubbed):
    client, stubber = stubbed
    stubber.add_client_error("describe_table", "AccessDeniedException", http_status_code=400)
    with pytest.raises(Exception, match="AccessDenied"):
        dynamo.dynamodb_probe(TABLE, REGION, lambda _region: client)()


def test_client_is_created_once_and_reused(stubbed):
    client, stubber = stubbed
    _describe(stubber, "ACTIVE")
    _describe(stubber, "ACTIVE")
    created: list[str] = []

    def factory(region: str):
        created.append(region)
        return client

    probe = dynamo.dynamodb_probe(TABLE, REGION, factory)
    probe()
    probe()
    assert created == [REGION]


def test_not_configured_without_table(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    assert dynamo.dynamodb_probe_from_env() is None


def test_table_without_region_fails_at_startup(monkeypatch):
    monkeypatch.setenv("DYNAMODB_TABLE", TABLE)
    with pytest.raises(dynamo.DynamoConfigError):
        dynamo.dynamodb_probe_from_env()


def test_probe_from_env_uses_the_named_table(monkeypatch, stubbed):
    client, stubber = stubbed
    _describe(stubber, "ACTIVE")
    monkeypatch.setenv("DYNAMODB_TABLE", f"  {TABLE} ")
    monkeypatch.setenv("AWS_REGION", REGION)
    probe = dynamo.dynamodb_probe_from_env(lambda _region: client)
    assert probe is not None
    probe()
    stubber.assert_no_pending_responses()


def test_default_client_has_short_timeouts_and_one_attempt(monkeypatch, tmp_path):
    # Never read the developer's AWS profile or login session; creating a client makes
    # no network call.
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic")
    client = dynamo._boto3_client(REGION)
    config = client.meta.config
    assert config.connect_timeout == dynamo.CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == dynamo.READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == 1
    assert client.meta.region_name == REGION


def test_ready_reports_dynamodb_from_env(monkeypatch, stubbed):
    client, stubber = stubbed
    _describe(stubber, "ACTIVE")
    monkeypatch.setattr(
        app_module,
        "dynamodb_probe_from_env",
        lambda: dynamo.dynamodb_probe(TABLE, REGION, lambda _region: client),
    )
    body = TestClient(create_app()).get("/api/health/ready").json()
    # No PostgreSQL is configured in this test, so the service as a whole is not ready.
    assert body["checks"] == {
        "database": "unavailable",
        "aurora": "not_configured",
        "dynamodb": "ok",
    }


def test_ready_hides_dynamodb_error_details(monkeypatch, stubbed, caplog):
    client, stubber = stubbed
    stubber.add_client_error(
        "describe_table", "AccessDeniedException", "synthetic-secret arn", http_status_code=400
    )
    monkeypatch.setattr(
        app_module,
        "dynamodb_probe_from_env",
        lambda: dynamo.dynamodb_probe(TABLE, REGION, lambda _region: client),
    )
    response = TestClient(create_app()).get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["dynamodb"] == "unavailable"
    assert "synthetic-secret" not in response.text
    assert "synthetic-secret" not in caplog.text
