"""PWA-compatible health endpoints under ``/api/health`` (api-contract decision D2).

``live`` reports process liveness only. ``ready`` reports each storage dependency
separately and is ``ready`` only when every dependency was actually checked and
answered. A dependency without a configured probe is ``not_configured`` and makes
the service ``not_ready``: an unchecked dependency is never reported as healthy.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# A probe returns normally when the dependency answers and raises otherwise.
Probe = Callable[[], None]
DependencyName = Literal["aurora", "dynamodb"]
DEPENDENCIES: tuple[DependencyName, ...] = ("aurora", "dynamodb")

CheckStatus = Literal["ok", "unavailable", "not_configured"]


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["api"] = "api"
    environment: str


class ReadinessChecks(BaseModel):
    # Aggregate kept for the PWA client, which reads ``checks.database``.
    database: Literal["ok", "unavailable"]
    aurora: CheckStatus
    dynamodb: CheckStatus


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: Literal["api"] = "api"
    environment: str
    checks: ReadinessChecks


def _environment() -> str:
    return os.environ.get("UMSAL_ENV", "local")


def _run_probe(probe: Probe | None) -> CheckStatus:
    if probe is None:
        return "not_configured"
    try:
        probe()
    except Exception:
        # Driver errors can carry DSNs, hosts or credentials. Neither the
        # exception nor its text is logged or returned.
        return "unavailable"
    return "ok"


def create_health_router(probes: Mapping[DependencyName, Probe] | None = None) -> APIRouter:
    configured = dict(probes or {})
    unknown = set(configured) - set(DEPENDENCIES)
    if unknown:
        raise ValueError(f"unknown readiness dependencies: {sorted(unknown)}")

    router = APIRouter(prefix="/api/health", tags=["health"])

    @router.get("/live", response_model=LiveResponse, operation_id="getHealthLive")
    def live() -> LiveResponse:
        return LiveResponse(environment=_environment())

    @router.get(
        "/ready",
        response_model=ReadyResponse,
        operation_id="getHealthReady",
        responses={503: {"model": ReadyResponse}},
    )
    def ready() -> ReadyResponse | JSONResponse:
        results = {name: _run_probe(configured.get(name)) for name in DEPENDENCIES}
        all_ok = all(result == "ok" for result in results.values())
        body = ReadyResponse(
            status="ready" if all_ok else "not_ready",
            environment=_environment(),
            checks=ReadinessChecks(database="ok" if all_ok else "unavailable", **results),
        )
        if all_ok:
            return body
        return JSONResponse(status_code=503, content=body.model_dump())

    return router
