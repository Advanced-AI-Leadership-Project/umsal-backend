"""FastAPI application factory. Keep handlers free of AWS credentials and real user data."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI

from umsal_backend import __version__, connections, consents, identity, invitations
from umsal_backend.auth import TokenVerifier
from umsal_backend.db import database_url, readiness_probes_from_env
from umsal_backend.errors import install_error_handlers
from umsal_backend.health import DependencyName, Probe, create_health_router

_FROM_ENV = object()


def create_app(
    readiness_probes: Mapping[DependencyName, Probe] | None | object = _FROM_ENV,
    token_verifier: TokenVerifier | None | object = _FROM_ENV,
    database: str | None | object = _FROM_ENV,
) -> FastAPI:
    app = FastAPI(title="UMSAL Backend", version=__version__)
    # User APIs read this through umsal_backend.auth.require_principal. Without
    # Cognito settings it stays None and protected routes answer 503.
    app.state.token_verifier = (
        TokenVerifier.from_env() if token_verifier is _FROM_ENV else token_verifier
    )
    # User APIs open connections through umsal_backend.db.get_db; None answers 503.
    app.state.database_url = database_url() if database is _FROM_ENV else database
    install_error_handlers(app)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        # Deployment check: process liveness only. It does not attest Aurora,
        # DynamoDB or other dependencies; see /api/health/ready for those.
        return {"status": "ok", "version": __version__}

    # DATABASE_URL enables the Aurora/PostgreSQL check; DynamoDB has no probe yet.
    probes = readiness_probes_from_env() if readiness_probes is _FROM_ENV else readiness_probes
    app.include_router(create_health_router(probes))
    app.include_router(identity.router)
    app.include_router(connections.router)
    app.include_router(invitations.router)
    app.include_router(consents.router)
    return app


app = create_app()
