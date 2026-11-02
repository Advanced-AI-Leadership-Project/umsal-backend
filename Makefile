export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.tools/python

.PHONY: setup api check python-check format ai-setup ai-doctor workflow-check
.PHONY: verify verify-fast verify-security

setup: ai-setup
	uv sync --locked

api:
	uv run --locked umsal-backend --host 127.0.0.1 --port 8000

check: python-check workflow-check

python-check:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked pytest -q

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
