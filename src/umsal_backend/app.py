"""FastAPI application factory. Keep handlers free of AWS credentials and real user data."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI

from umsal_backend import __version__
from umsal_backend.health import DependencyName, Probe, create_health_router


def create_app(readiness_probes: Mapping[DependencyName, Probe] | None = None) -> FastAPI:
    app = FastAPI(title="UMSAL Backend", version=__version__)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        # Deployment check: process liveness only. It does not attest Aurora,
        # DynamoDB or other dependencies; see /api/health/ready for those.
        return {"status": "ok", "version": __version__}

    app.include_router(create_health_router(readiness_probes))
    return app


app = create_app()
