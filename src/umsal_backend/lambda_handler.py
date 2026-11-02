"""AWS Lambda entry point behind API Gateway (ap-southeast-2)."""

from __future__ import annotations

from mangum import Mangum

from umsal_backend.app import app

handler = Mangum(app, lifespan="off")
