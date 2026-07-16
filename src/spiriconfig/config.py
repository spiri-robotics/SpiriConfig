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

from typing import Literal

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

    auth: Literal["none", "pam"] = "none"
    """Whether the web UI requires a login, and how. ``SPIRICONFIG_AUTH``.

    ``none`` (the default) serves the UI to anyone who can reach the port, which is
    right for a checkout on loopback and wrong the moment the UI is exposed. ``pam``
    puts a login in front of every page, authenticating against the host's PAM stack
    -- see :mod:`spiriconfig.auth`. A deployment that reaches the network turns this
    on; nothing about a developer's loopback session changes until they do.

    Off by default on purpose, the same reason the compose dir defaults somewhere
    harmless: running out of a checkout should not suddenly demand a password.
    """

    auth_service: str = "login"
    """PAM service to authenticate against. ``SPIRICONFIG_AUTH_SERVICE``.

    The name of a file under ``/etc/pam.d/``. ``login`` exists on essentially every
    system and reads the normal password stack, so it is the default. A deployment
    that wants its own policy ships ``/etc/pam.d/spiriconfig`` and sets this to
    ``spiriconfig``. Only consulted when :attr:`auth` is ``pam``.
    """

    auth_group: str = "wheel"
    """Group whose members may log in, *when SpiriConfig runs as root*. ``SPIRICONFIG_AUTH_GROUP``.

    Running as root, PAM can verify any account's password, so we would otherwise
    let every system user (``nobody``, service accounts, ...) into the admin UI.
    Membership of this group is the gate. ``wheel`` on Arch/RHEL, ``sudo`` on Debian
    -- set it to match the box.

    Ignored when SpiriConfig does not run as root: PAM can then only verify the one
    account the process runs as, so there is nothing to gate. See
    :func:`spiriconfig.auth.authenticate`.
    """


def settings() -> Settings:
    """Load core settings from the environment.

    Not cached: tests and long-lived processes should be able to observe an
    changed environment without reaching into a module global.
    """
    return Settings()
