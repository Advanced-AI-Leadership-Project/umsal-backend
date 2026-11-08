"""Operator-only, read-only database inspection Lambda (stack infra/aws/db-inspect.yaml).

RDS PostgreSQL is reachable only inside the VPC, so the operator machine cannot open a
SQL session. This function runs in the VPC with the app user's IAM auth and answers two
fixed requests; it never runs SQL taken from the event.

  {"action": "tables"}                                -> every public table and its row count
  {"action": "rows", "table": "users", "limit": 20}   -> up to ``MAX_LIMIT`` rows of one table

Every query runs in a READ ONLY transaction. Table names are matched against the real
tables in ``public`` before being quoted as identifiers. Rows go only to the invoker's
response; logs contain only the action, the table name and the row count.

Secrets and private values never leave the database (#61 review): ``*_hash`` columns and
binary values are always replaced with ``REDACTED`` (an invite code hash could be brute
forced offline), private labels and names are replaced unless the event says
``"reveal_private": true``, and long text is cut to ``MAX_TEXT`` characters.

Run locally:  DATABASE_URL=... python -m umsal_backend.db_inspect tables
              DATABASE_URL=... python -m umsal_backend.db_inspect rows users 5
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import sys
import uuid
from typing import Any

import psycopg
from psycopg import sql

from umsal_backend.db import database_from_env

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_TEXT = 200
SCHEMA = "public"
REDACTED = "<redacted>"
# Values only their owner should see (D10 labels, profile names).
PRIVATE_COLUMNS = frozenset({"label", "inviter_label", "display_name"})


class InspectRequestError(ValueError):
    """The event asked for something this function does not do."""


def _tables(connection: psycopg.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
        (SCHEMA,),
    ).fetchall()
    return [row[0] for row in rows]


def _primary_key(connection: psycopg.Connection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = to_regclass(quote_ident(%s) || '.' || quote_ident(%s)) "
        "AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)",
        (SCHEMA, table),
    ).fetchall()
    return [row[0] for row in rows]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID | decimal.Decimal | dt.timedelta):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _shown(column: str, value: Any, reveal_private: bool) -> Any:
    if column.endswith("_hash") or isinstance(value, bytes | memoryview):
        return None if value is None else REDACTED
    if column in PRIVATE_COLUMNS and not reveal_private:
        return None if value is None else REDACTED
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return _jsonable(value)


def list_tables(connection: psycopg.Connection) -> dict[str, Any]:
    counts = {}
    for table in _tables(connection):
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(SCHEMA, table))
        counts[table] = connection.execute(query).fetchone()[0]
    return {"action": "tables", "tables": counts}


def table_rows(
    connection: psycopg.Connection, table: str, limit: int, reveal_private: bool = False
) -> dict[str, Any]:
    if table not in _tables(connection):
        raise InspectRequestError(f"Unknown table in schema {SCHEMA}.")
    key = _primary_key(connection, table)
    order = (
        sql.SQL(" ORDER BY {}").format(sql.SQL(", ").join(map(sql.Identifier, key)))
        if key
        else sql.SQL("")
    )
    query = sql.SQL("SELECT * FROM {}{} LIMIT %s").format(sql.Identifier(SCHEMA, table), order)
    cursor = connection.execute(query, (limit,))
    columns = [column.name for column in cursor.description]
    rows = [
        {
            column: _shown(column, value, reveal_private)
            for column, value in zip(columns, row, strict=True)
        }
        for row in cursor.fetchall()
    ]
    return {"action": "rows", "table": table, "columns": columns, "limit": limit, "rows": rows}


def _limit(event: dict[str, Any]) -> int:
    value = event.get("limit", DEFAULT_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise InspectRequestError(f"limit must be an integer from 1 to {MAX_LIMIT}.")
    return value


def inspect(connection: psycopg.Connection, event: Any) -> dict[str, Any]:
    """Answer one request inside a READ ONLY transaction (rolled back, nothing to keep)."""
    if not isinstance(event, dict):
        raise InspectRequestError("The event must be a JSON object.")
    action = event.get("action", "tables")
    if action == "tables":
        work = list_tables
    elif action == "rows":
        table, limit = event.get("table"), _limit(event)
        if not isinstance(table, str) or not table:
            raise InspectRequestError("rows needs a table name.")
        reveal = event.get("reveal_private", False)
        if not isinstance(reveal, bool):
            raise InspectRequestError("reveal_private must be true or false.")

        def work(conn: psycopg.Connection) -> dict[str, Any]:
            return table_rows(conn, table, limit, reveal)
    else:
        raise InspectRequestError('action must be "tables" or "rows".')

    connection.read_only = True
    try:
        with connection.transaction(force_rollback=True):
            return work(connection)
    finally:
        connection.read_only = False


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    if result["action"] == "tables":
        return {"action": "tables", "tables": len(result["tables"])}
    return {"action": "rows", "table": result["table"], "rows": len(result["rows"])}


def run(event: Any) -> dict[str, Any]:
    target = database_from_env()
    if target is None:
        raise SystemExit("DATABASE_URL or DB_HOST is not set.")
    with target.connect() as connection:
        return inspect(connection, event)


def handler(event: Any, _context: Any) -> dict[str, Any]:
    """Lambda entry point. Logs only a summary, never row contents."""
    result = run(event)
    print(json.dumps({"inspected": _summary(result)}))
    return result


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    event: dict[str, Any] = {"action": args[0] if args else "tables"}
    if len(args) > 1:
        event["table"] = args[1]
    if len(args) > 2:
        event["limit"] = int(args[2])
    print(json.dumps(run(event), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
