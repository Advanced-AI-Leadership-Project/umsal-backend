"""The private Lambda invocation budget and credential-safe dependency failures."""

import logging
from unittest.mock import patch

import pytest
from botocore.exceptions import ReadTimeoutError

from umsal_backend.call_storage import AwsCallServices
from umsal_backend.errors import ApiError


def test_service_specific_response_budgets_and_client_reuse():
    service = AwsCallServices("synthetic-table", "synthetic-function", "ap-southeast-2")
    with patch("boto3.client") as create:
        client = service.client("lambda")
        assert service.client("lambda") is client
        assert create.call_args.kwargs["config"].read_timeout == 8
        assert create.call_count == 1
        service.client("dynamodb")
        assert create.call_args.kwargs["config"].read_timeout == 3


def test_dependency_error_does_not_log_exception_details(caplog):
    service = AwsCallServices("synthetic-table", "synthetic-function", "ap-southeast-2")
    with patch.object(service, "client") as create:
        create.return_value.invoke.side_effect = ReadTimeoutError(
            endpoint_url="https://synthetic-secret.example/token"
        )
        with caplog.at_level(logging.WARNING), pytest.raises(ApiError) as found:
            service.invoke("lambda", "invoke")
    assert found.value.code == "CALLS_UNAVAILABLE"
    assert "lambda/invoke, ReadTimeoutError" in caplog.text
    assert "synthetic-secret" not in caplog.text
