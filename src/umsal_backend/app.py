"""FastAPI application factory. Keep handlers free of AWS credentials and real user data."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI

from umsal_backend import (
    __version__,
    connections,
    consents,
    disconnect,
    identity,
    invitations,
    labels,
)
from umsal_backend.auth import TokenVerifier
from umsal_backend.db import DatabaseTarget, as_target, database_from_env, postgres_probe
from umsal_backend.dynamo import dynamodb_probe_from_env
from umsal_backend.errors import install_error_handlers
from umsal_backend.health import DependencyName, Probe, create_health_router

_FROM_ENV = object()


def create_app(
    readiness_probes: Mapping[DependencyName, Probe] | None | object = _FROM_ENV,
    token_verifier: TokenVerifier | None | object = _FROM_ENV,
    database: str | DatabaseTarget | None | object = _FROM_ENV,
) -> FastAPI:
    app = FastAPI(title="UMSAL Backend", version=__version__)
    # User APIs read this through umsal_backend.auth.require_principal. Without
    # Cognito settings it stays None and protected routes answer 503.
    app.state.token_verifier = (
        TokenVerifier.from_env() if token_verifier is _FROM_ENV else token_verifier
    )
    # User APIs open connections through umsal_backend.db.get_db; None answers 503.
    app.state.database = database_from_env() if database is _FROM_ENV else as_target(database)
    install_error_handlers(app)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        # Deployment check: process liveness only. It does not attest Aurora,
        # DynamoDB or other dependencies; see /api/health/ready for those.
        return {"status": "ok", "version": __version__}

    # DATABASE_URL/DB_HOST enable the PostgreSQL check, DYNAMODB_TABLE the DynamoDB one.
    # The readiness check reuses the app's own target (one IAM token cache, not two).
    if readiness_probes is _FROM_ENV:
        probes: dict[DependencyName, Probe] = {}
        database = app.state.database
        if database is not None:
            probes["aurora"] = postgres_probe(database)
        dynamo = dynamodb_probe_from_env()
        if dynamo is not None:
            probes["dynamodb"] = dynamo
    else:
        probes = readiness_probes
    app.include_router(create_health_router(probes))
    app.include_router(identity.router)
    app.include_router(connections.router)
    app.include_router(invitations.router)
    app.include_router(consents.router)
    app.include_router(disconnect.router)
    app.include_router(labels.router)
    return app


app = create_app()
