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
    "COGNITO_JWKS",
    "COGNITO_JWKS_FETCH",
    "DB_HOST",
    "DB_USER",
    "DB_NAME",
    "DB_PORT",
    "DB_CONNECT_TIMEOUT",
    "DB_CA_BUNDLE",
    "AWS_REGION",
    "MIGRATIONS_ROOT",
    "UMSAL_ENV",
    "DYNAMODB_TABLE",
    "AUDIO_BUCKET",
    "UMSAL_CPU_REAL_AUDIO_ENABLED",
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

    # This fixture migrates and TRUNCATEs. Refuse anything that is not clearly a
    # local or test database so a mistyped URL cannot wipe real data.
    info = psycopg.conninfo.conninfo_to_dict(test_database_url)
    local = info.get("host", "localhost") in {"localhost", "127.0.0.1", "::1"}
    if not (local or "test" in info.get("dbname", "")):
        pytest.fail("TEST_DATABASE_URL must point at a local or *test* database")

    with monkeypatch.context() as patched:
        patched.setenv("DATABASE_URL", test_database_url)  # read by migrations/env.py
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with _admin_connection(test_database_url) as connection:
        try:
            connection.execute(
                "TRUNCATE users, connections, invitations, idempotency_keys, consent_grants, "
                "connection_labels CASCADE"
            )
        except psycopg.errors.LockNotAvailable:
            # main verification of cdf46da waited here for 13 minutes (#56).
            pytest.fail(
                f"TRUNCATE waited over {LOCK_TIMEOUT_MS} ms for a table lock. "
                f"Previous test: {_LAST_TEST['nodeid']}. "
                f"Other sessions: {_other_sessions(connection)}",
                pytrace=False,
            )
    yield test_database_url
    _fail_on_leaked_transactions(test_database_url)


LOCK_TIMEOUT_MS = 10_000
LEAK_GRACE_SECONDS = 3.0
_LAST_TEST: dict[str, str | None] = {"nodeid": None}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    yield
    _LAST_TEST["nodeid"] = item.nodeid


def _admin_connection(url: str):
    """Fixture-side connection that fails instead of waiting forever on a lock."""
    import psycopg

    return psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=5,
        options=f"-c lock_timeout={LOCK_TIMEOUT_MS} -c statement_timeout=60000",
    )


def _other_sessions(connection) -> list[tuple]:
    return connection.execute(
        "SELECT pid, state, now() - xact_start AS xact_age, left(query, 160) "
        "FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend' ORDER BY xact_start NULLS LAST"
    ).fetchall()


def _fail_on_leaked_transactions(url: str) -> None:
    """A test must not leave a session idle inside an open transaction.

    Such a session keeps its row/table locks, so the next test's TRUNCATE blocks. This
    names the leaking test directly instead of hanging in the following one (#56).
    Connections may still be closing on worker threads, so allow a short grace period.
    """
    import time

    deadline = time.monotonic() + LEAK_GRACE_SECONDS
    # Not ``with``: a test may still have psycopg.Connection.commit monkeypatched here
    # (monkeypatch is undone after this fixture), and ``with`` would call it on exit.
    connection = _admin_connection(url)
    try:
        while True:
            leaked = [s for s in _other_sessions(connection) if s[1] == "idle in transaction"]
            if not leaked or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    finally:
        connection.close()
    if leaked:
        pytest.fail(f"test left sessions idle in a transaction: {leaked}", pytrace=False)


CONCURRENCY_TIMEOUT_SECONDS = 30


def _run_concurrently(*targets, timeout: float = CONCURRENCY_TIMEOUT_SECONDS) -> None:
    """Start ``targets`` together on daemon threads and fail instead of hanging.

    Each target receives no arguments and is released at the same moment by a barrier.
    A barrier that never fills, a thread that never finishes, or an exception inside a
    thread becomes a test failure naming the target, so CI cannot stall until the job
    timeout (main verification of ad32e45 hung for 15 minutes, issue #48).
    """
    import threading
    import time

    # The barrier gives up slightly before the join deadline, so a target that never
    # reaches it is reported as BrokenBarrierError rather than as "did not finish".
    barrier = threading.Barrier(len(targets), timeout=timeout * 0.8)
    errors: list[tuple[str, BaseException]] = []

    def wrap(name: str, target):
        def run() -> None:
            try:
                barrier.wait()
                target()
            except BaseException as error:  # reported to the test below
                errors.append((name, error))

        return run

    # Index the names so passing the same function twice still says which one hung.
    names = [f"{index}:{target.__name__}" for index, target in enumerate(targets)]
    threads = [
        threading.Thread(target=wrap(name, target), name=name, daemon=True)
        for name, target in zip(names, targets, strict=True)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    stuck = [thread.name for thread in threads if thread.is_alive()]
    if stuck:
        pytest.fail(f"concurrent targets did not finish within {timeout}s: {stuck}")
    if errors:
        summary = ", ".join(f"{name!r} raised {error!r}" for name, error in errors)
        raise AssertionError(f"concurrent targets failed: {summary}") from errors[0][1]


@pytest.fixture
def run_concurrently():
    """Run callables at the same time with a hard timeout (see ``_run_concurrently``)."""
    return _run_concurrently


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
