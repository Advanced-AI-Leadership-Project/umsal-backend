from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

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


@pytest.fixture
def migrated_db(test_database_url, monkeypatch):
    """Disposable database at the latest migration with empty application tables."""
    import psycopg
    from alembic import command
    from alembic.config import Config

    with monkeypatch.context() as patched:
        patched.setenv("DATABASE_URL", test_database_url)  # read by migrations/env.py
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with psycopg.connect(test_database_url, autocommit=True) as connection:
        connection.execute("TRUNCATE users, connections CASCADE")
    return test_database_url


@pytest.fixture(scope="session")
def contract_schema():
    """Validator factory for components in contracts/openapi.yaml (no network)."""
    import jsonschema
    import yaml
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    path = ROOT / "contracts" / "openapi.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    resource = Resource.from_contents(document, default_specification=DRAFT202012)
    registry = Registry().with_resource(path.as_uri(), resource)

    def validator(name: str) -> jsonschema.Draft202012Validator:
        return jsonschema.Draft202012Validator(
            {"$ref": f"{path.as_uri()}#/components/schemas/{name}"},
            registry=registry,
            format_checker=jsonschema.FormatChecker(),
        )

    return validator
