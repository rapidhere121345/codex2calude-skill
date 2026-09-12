#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "Python 3.12 is required")'

if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="$PROJECT_DIR/.uv-cache" uv venv --python "$PYTHON_BIN" .venv
    UV_CACHE_DIR="$PROJECT_DIR/.uv-cache" uv sync --frozen --no-dev
else
    "$PYTHON_BIN" -m venv .venv
    .venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir .
fi

printf '%s\n' 'Install complete.'
printf '%s\n' 'Runtime: .venv/bin/claude-research-mcp'
