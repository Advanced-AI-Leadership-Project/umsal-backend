"""FastAPI application factory. Keep handlers free of AWS credentials and real user data."""

from __future__ import annotations

from fastapi import FastAPI

from umsal_backend import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="UMSAL Backend", version=__version__)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        # Process liveness only. It does not attest DynamoDB or other dependencies.
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
