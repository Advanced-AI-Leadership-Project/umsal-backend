"""Contract ``Error`` responses: ``code``, ``message``, ``request_id``, ``retryable``.

Messages are fixed strings. Never put tokens, headers, keys, signed URLs, audio or
transcripts into them (AGENTS "민감 자료").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = dict(headers or {})


def new_request_id() -> str:
    return uuid.uuid4().hex


def error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "code": error.code,
            "message": error.message,
            "request_id": new_request_id(),
            "retryable": error.retryable,
        },
        headers=error.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, _exc: RequestValidationError):
        # FastAPI's default body echoes the offending input; the contract Error does not.
        return error_response(ApiError(422, "VALIDATION_FAILED", "The request is invalid."))

    @app.exception_handler(psycopg.OperationalError)
    async def handle_database_unavailable(_request: Request, _exc: psycopg.OperationalError):
        # Connection loss, timeouts, Aurora resuming. The driver message can carry the
        # host or user, so it is never returned.
        return error_response(
            ApiError(
                503,
                "TEMPORARILY_UNAVAILABLE",
                "The service is temporarily unavailable.",
                retryable=True,
            )
        )
