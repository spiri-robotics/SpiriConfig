#!/bin/sh
# Run only docker-dependent tests.
#
# Usage:
#   scripts/test-docker.sh    # run docker tests and exit on failure
set -eu

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    pip install -q uv
fi

uv sync --group dev
uv run pytest -m docker -v "$@"
