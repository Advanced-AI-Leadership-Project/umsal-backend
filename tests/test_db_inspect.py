"""Read-only inspection Lambda against real PostgreSQL (synthetic rows only)."""

from __future__ import annotations

import json

import psycopg
import pytest

from umsal_backend import db, db_inspect


def _seed(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO users (id, cognito_sub, display_name) VALUES "
            "('usr_b', 'sub-b', 'Bob'), ('usr_a', 'sub-a', 'Alice')"
        )


def test_tables_lists_counts(migrated_db, monkeypatch, capsys):
    _seed(migrated_db)
    monkeypatch.setattr(db_inspect, "database_from_env", lambda: db.UrlTarget(migrated_db))

    result = db_inspect.handler({"action": "tables"}, None)

    assert result["tables"]["users"] == 2
    assert result["tables"]["connections"] == 0
    assert "alembic_version" in result["tables"]
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log == {"inspected": {"action": "tables", "tables": len(result["tables"])}}


def test_rows_are_ordered_limited_and_jsonable(migrated_db, monkeypatch, capsys):
    _seed(migrated_db)
    monkeypatch.setattr(db_inspect, "database_from_env", lambda: db.UrlTarget(migrated_db))

    result = db_inspect.handler({"action": "rows", "table": "users", "limit": 1}, None)

    assert result["columns"] == ["id", "cognito_sub", "display_name", "created_at"]
    assert [row["id"] for row in result["rows"]] == ["usr_a"]
    json.dumps(result)  # Lambda must be able to serialize the response
    out = capsys.readouterr().out
    assert "Alice" not in out  # row contents never reach the log
    assert json.loads(out.strip().splitlines()[-1]) == {
        "inspected": {"action": "rows", "table": "users", "rows": 1}
    }


def test_transaction_is_read_only(migrated_db):
    with psycopg.connect(migrated_db) as connection:
        connection.read_only = True
        with connection.transaction(force_rollback=True):
            assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        # inspect() restores the flag even though it forced a rollback
        db_inspect.inspect(connection, {"action": "tables"})
        assert connection.read_only is False


@pytest.mark.parametrize(
    "event",
    [
        [],
        {"action": "drop"},
        {"action": "rows"},
        {"action": "rows", "table": "users", "limit": 0},
        {"action": "rows", "table": "users", "limit": 101},
        {"action": "rows", "table": "users", "limit": True},
        {"action": "rows", "table": "users; DROP TABLE users"},
        {"action": "rows", "table": "pg_roles"},
    ],
)
def test_rejects_unsupported_requests(migrated_db, event):
    with psycopg.connect(migrated_db) as connection, pytest.raises(db_inspect.InspectRequestError):
        db_inspect.inspect(connection, event)


def test_jsonable_converts_binary_and_nested_values():
    import datetime as dt
    import decimal
    import uuid

    value = {"b": b"\x00\x01", "n": [decimal.Decimal("1.5"), uuid.UUID(int=0)]}
    value["d"] = dt.date(2026, 11, 6)
    assert db_inspect._jsonable(value) == {
        "b": {"base64": "AAE="},
        "n": ["1.5", "00000000-0000-0000-0000-000000000000"],
        "d": "2026-11-06",
    }


def test_run_requires_database_settings():
    with pytest.raises(SystemExit):
        db_inspect.run({"action": "tables"})
