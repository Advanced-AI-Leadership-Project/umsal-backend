from __future__ import annotations

import pytest

# Settings read from the environment by create_app(). Tests start without them so a
# developer's local DATABASE_URL or Cognito settings never change test results.
_APP_ENV_VARS = (
    "DATABASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_APP_CLIENT_IDS",
    "COGNITO_REGION",
    "UMSAL_ENV",
)


@pytest.fixture(autouse=True)
def _isolated_app_env(monkeypatch):
    for name in _APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
