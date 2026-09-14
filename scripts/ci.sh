#!/bin/sh
# Run the full test suite (docker tests are skipped when unavailability).
#
# Usage:
#   scripts/ci.sh            # regular run (skip non-critical failures)
#   scripts/ci.sh --fail     # exit on first failure (for strict CI)
set -eu

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    pip install -q uv
fi

uv sync --group dev
uv run pytest "$@"
