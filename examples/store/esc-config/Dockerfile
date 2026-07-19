# syntax=docker/dockerfile:1

# Two stages: build the virtualenv with uv, then copy just the venv and the app
# into a clean runtime. uv, its cache, and the dev dependencies stay in the build
# stage and never reach the published image.

FROM python:3.13-slim AS build

# uv, copied from its official image. No pip, no `curl | sh`.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies against only the lockfile first, so this layer is cached
# until the dependencies actually change. Editing source does not reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Then add the project itself.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim AS runtime

# A store app should not need root, and the machines it ships onto do not hand it
# out. Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app
USER app
WORKDIR /app

COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "esc_config"]
