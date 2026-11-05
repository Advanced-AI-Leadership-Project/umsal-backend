from __future__ import annotations

import os

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


@pytest.fixture
def test_database_url() -> str:
    """URL of a disposable PostgreSQL for tests that need real SQL.

    Skips locally when TEST_DATABASE_URL is unset. CI sets REQUIRE_DATABASE_TESTS=1,
    where a missing database fails the test instead of silently skipping it.
    """
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if url:
        return url
    if os.environ.get("REQUIRE_DATABASE_TESTS") == "1":
        pytest.fail("REQUIRE_DATABASE_TESTS=1 but TEST_DATABASE_URL is not set")
    pytest.skip("TEST_DATABASE_URL is not set")
