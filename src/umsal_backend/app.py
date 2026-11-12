"""FastAPI application factory. Keep handlers free of AWS credentials and real user data."""

from __future__ import annotations

import os
from collections.abc import Mapping

from fastapi import FastAPI

from umsal_backend import (
    __version__,
    assignments,
    calls,
    connections,
    consents,
    disconnect,
    identity,
    invitations,
    labels,
    recordings,
)
from umsal_backend.auth import TokenVerifier
from umsal_backend.call_storage import CallServices, calls_from_env
from umsal_backend.db import DatabaseTarget, as_target, database_from_env, postgres_probe
from umsal_backend.dynamo import dynamodb_probe_from_env
from umsal_backend.errors import install_error_handlers
from umsal_backend.health import DependencyName, Probe, create_health_router
from umsal_backend.results import ResultStore, result_store_from_env
from umsal_backend.storage import AudioStore, audio_store_from_env

_FROM_ENV = object()


def create_app(
    readiness_probes: Mapping[DependencyName, Probe] | None | object = _FROM_ENV,
    token_verifier: TokenVerifier | None | object = _FROM_ENV,
    database: str | DatabaseTarget | None | object = _FROM_ENV,
    audio_store: AudioStore | None | object = _FROM_ENV,
    result_store: ResultStore | None | object = _FROM_ENV,
    call_services: CallServices | None | object = _FROM_ENV,
) -> FastAPI:
    # Interactive docs and the generated schema only on a developer machine (#66);
    # deployed environments are public through CloudFront and the contract lives in
    # contracts/openapi.yaml. app.openapi() still works for the parity tests.
    local = os.environ.get("UMSAL_ENV", "local") == "local"
    app = FastAPI(
        title="UMSAL Backend",
        version=__version__,
        docs_url="/docs" if local else None,
        redoc_url="/redoc" if local else None,
        openapi_url="/openapi.json" if local else None,
    )
    # User APIs read this through umsal_backend.auth.require_principal. Without
    # Cognito settings it stays None and protected routes answer 503.
    app.state.token_verifier = (
        TokenVerifier.from_env() if token_verifier is _FROM_ENV else token_verifier
    )
    # User APIs open connections through umsal_backend.db.get_db; None answers 503.
    app.state.database = database_from_env() if database is _FROM_ENV else as_target(database)
    # Recording uploads (#72): AUDIO_BUCKET enables S3; without it upload routes answer 503.
    app.state.audio_store = audio_store_from_env() if audio_store is _FROM_ENV else audio_store
    # Result bodies (#75): DYNAMODB_TABLE enables them; without it TEXT jobs end
    # FAILED/STAGE_NOT_AVAILABLE and getResult answers 503 for a completed recording.
    app.state.result_store = result_store_from_env() if result_store is _FROM_ENV else result_store
    install_error_handlers(app)
    app.state.call_services = calls_from_env() if call_services is _FROM_ENV else call_services

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
    app.include_router(recordings.router)
    app.include_router(assignments.router)
    app.include_router(calls.router)
    return app


app = create_app()
