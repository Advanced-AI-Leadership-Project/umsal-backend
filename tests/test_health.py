from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from umsal_backend import __main__ as cli
from umsal_backend import __version__
from umsal_backend.app import create_app
from umsal_backend.lambda_handler import handler


def test_health_reports_process_liveness_only():
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_unknown_route_is_not_reported_as_healthy():
    response = TestClient(create_app()).get("/does-not-exist")
    assert response.status_code == 404


def test_lambda_handler_serves_api_gateway_http_event():
    event = {
        "version": "2.0",
        "routeKey": "GET /health",
        "rawPath": "/health",
        "rawQueryString": "",
        "headers": {"host": "synthetic.example.invalid"},
        "requestContext": {
            "http": {
                "method": "GET",
                "path": "/health",
                "protocol": "HTTP/1.1",
                "sourceIp": "192.0.2.1",
                "userAgent": "synthetic-test",
            },
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }
    result = handler(event, None)
    assert result["statusCode"] == 200
    assert '"status":"ok"' in result["body"]


def test_local_server_uses_loopback_by_default():
    with patch("uvicorn.run") as run:
        cli.main([])
    run.assert_called_once_with("umsal_backend.app:app", host="127.0.0.1", port=8000)
