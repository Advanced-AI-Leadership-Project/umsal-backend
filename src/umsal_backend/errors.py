"""Contract ``Error`` responses: ``code``, ``message``, ``request_id``, ``retryable``.

Messages are fixed strings. Never put tokens, headers, keys, signed URLs, audio or
transcripts into them (AGENTS "민감 자료").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from fastapi import FastAPI, Request
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


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "request_id": new_request_id(),
                "retryable": exc.retryable,
            },
            headers=exc.headers,
        )
