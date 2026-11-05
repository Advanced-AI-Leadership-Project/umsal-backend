export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.tools/python

.PHONY: setup api check python-check format ai-setup ai-doctor workflow-check
.PHONY: verify verify-fast verify-security db-up db-down db-migrate db-test

# Local Docker PostgreSQL from compose.yaml (synthetic data only).
LOCAL_DATABASE_URL ?= postgresql://umsal:umsal_local@127.0.0.1:5434/umsal_backend

setup: ai-setup
	uv sync --locked

api:
	uv run --locked umsal-backend --host 127.0.0.1 --port 8000

# The API uses the local database only when DATABASE_URL is set, e.g.
#   DATABASE_URL=$(LOCAL_DATABASE_URL) make api
db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

db-migrate:
	DATABASE_URL=$(LOCAL_DATABASE_URL) uv run --locked alembic upgrade head

# Integration tests against the local database (they apply and revert migrations).
db-test:
	TEST_DATABASE_URL=$(LOCAL_DATABASE_URL) uv run --locked pytest -q tests/test_db_integration.py

check: python-check workflow-check

# API tests need a real PostgreSQL (make db-up). Like CI, a missing database fails
# instead of skipping, so coverage is never measured on a silently reduced suite.
TEST_DATABASE_URL ?= $(LOCAL_DATABASE_URL)

python-check:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) REQUIRE_DATABASE_TESTS=1 uv run --locked pytest -q

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

# One-time per clone: shared files do not automatically activate Git hooks.
ai-setup:
	python3 tools/ai_workflow.py hooks

ai-doctor:
	python3 tools/ai_workflow.py doctor

workflow-check:
	python3 -m unittest discover -s tools/tests -p 'test_*.py' -v

# Same suites as CI.
verify-fast:
	python3 tools/ci_harness.py quality
	python3 tools/ci_harness.py python

verify-security:
	python3 tools/ci_harness.py security

verify: verify-fast verify-security
