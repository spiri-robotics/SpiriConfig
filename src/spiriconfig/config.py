"""Configuration, read from the environment.

SpiriConfig is configured entirely through environment variables. There is no
config file format to learn, and no config file for us to rewrite behind the
user's back -- a service manager, a shell, or a container runtime can all supply
settings the same way.

Core settings are prefixed ``SPIRICONFIG_``. Plugins get their own prefix (see
:class:`~spiriconfig.plugins.Plugin`) so that their settings are namespaced,
e.g. ``SPIRICONFIG_DOCKER_``.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Core settings, shared by the CLI and the web UI."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    """Address the web UI binds to. ``SPIRICONFIG_HOST``.

    Defaults to loopback so a fresh install is not exposed to the network. Set
    ``SPIRICONFIG_HOST=0.0.0.0`` to bind all interfaces once you have decided the
    UI should be reachable from elsewhere.
    """

    port: int = 8080
    """Port the web UI binds to. ``SPIRICONFIG_PORT``."""

    log_level: str = "INFO"
    """Minimum log level. ``SPIRICONFIG_LOG_LEVEL``."""

    log_file: str | None = None
    """Optional file to log to, in addition to stderr. ``SPIRICONFIG_LOG_FILE``."""

    advanced: bool = False
    """Default for advanced mode, for someone who has not chosen. ``SPIRICONFIG_ADVANCED``.

    Only a *default*: each person can toggle it in the UI, and their choice wins
    from then on. Ship a developer image with this on and a customer image with it
    off, from the same code.

    Advanced mode hides features from the web UI. It is not a permission boundary
    -- see :mod:`spiriconfig.advanced`.
    """

    storage_secret: str | None = None
    """Signs the cookie that identifies a browser. ``SPIRICONFIG_STORAGE_SECRET``.

    Set this to something secret and stable, or per-person settings (advanced mode
    today, anything user-scoped later) reset every time the process restarts,
    because the cookie they were keyed on can no longer be verified.
    """


def settings() -> Settings:
    """Load core settings from the environment.

    Not cached: tests and long-lived processes should be able to observe an
    changed environment without reaching into a module global.
    """
    return Settings()
