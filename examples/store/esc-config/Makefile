# Common tasks. Everything here is a thin wrapper over uv and docker compose, so
# you can always run the underlying command by hand instead.

.PHONY: dev test lint docs release

# Build the image from this checkout and run it with live reload.
dev:
	docker compose -f compose.yaml -f compose.dev.yaml up --build

test:
	uv run pytest

lint:
	uv run ruff check .

docs:
	uv run sphinx-build -b html docs docs/_build/html

# make release BUMP=patch   (or minor, major, or an explicit X.Y.Z)
release:
	uv run scripts/release.py $(BUMP)
