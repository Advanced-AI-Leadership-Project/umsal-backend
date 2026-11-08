from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from umsal_backend.app import create_app
from umsal_backend.health import create_health_router


def _ok() -> None:
    return None


def _fail() -> None:
    raise RuntimeError("postgresql://synthetic-user:synthetic-secret@db.invalid/umsal")


def test_live_matches_pwa_shape(monkeypatch):
    monkeypatch.delenv("UMSAL_ENV", raising=False)
    response = TestClient(create_app()).get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api", "environment": "local"}


def test_environment_comes_from_env_var(monkeypatch):
    monkeypatch.setenv("UMSAL_ENV", "dev")
    response = TestClient(create_app()).get("/api/health/live")
    assert response.json()["environment"] == "dev"


def test_ready_without_probes_is_not_ready():
    # No storage connection exists yet: unchecked dependencies are never healthy.
    response = TestClient(create_app()).get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["service"] == "api"
    assert body["checks"] == {
        "database": "unavailable",
        "aurora": "not_configured",
        "dynamodb": "not_configured",
    }


def test_ready_when_all_dependencies_answer():
    client = TestClient(create_app({"aurora": _ok, "dynamodb": _ok}))
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"] == {"database": "ok", "aurora": "ok", "dynamodb": "ok"}


@pytest.mark.parametrize(
    ("probes", "expected"),
    [
        ({"aurora": _ok}, {"aurora": "ok", "dynamodb": "not_configured"}),
        ({"aurora": _ok, "dynamodb": _fail}, {"aurora": "ok", "dynamodb": "unavailable"}),
        ({"aurora": _fail, "dynamodb": _ok}, {"aurora": "unavailable", "dynamodb": "ok"}),
    ],
)
def test_ready_is_not_ready_unless_every_dependency_is_ok(probes, expected):
    response = TestClient(create_app(probes)).get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == {"database": "unavailable", **expected}


def test_probe_error_text_is_not_returned(caplog):
    response = TestClient(create_app({"aurora": _fail, "dynamodb": _fail})).get("/api/health/ready")
    assert "synthetic-secret" not in response.text
    assert "synthetic-secret" not in caplog.text


def test_unknown_dependency_is_rejected():
    with pytest.raises(ValueError, match="unknown readiness dependencies"):
        create_health_router({"redis": _ok})  # type: ignore[dict-item]


def test_health_routes_are_in_openapi():
    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    assert {"/health", "/api/health/live", "/api/health/ready"} <= set(paths)


def test_interactive_docs_are_local_only(monkeypatch):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert TestClient(create_app()).get(path).status_code == 200
    monkeypatch.setenv("UMSAL_ENV", "dev")
    deployed = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert deployed.get(path).status_code == 404
    assert deployed.get("/api/health/live").status_code == 200
