"""Docker plugin settings, read from the environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from spiriconfig import paths


class DockerSettings(BaseSettings):
    """Settings for the docker plugin, prefixed ``SPIRICONFIG_DOCKER_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_DOCKER_",
        env_file=".env",
        extra="ignore",
    )

    compose_dir: Path = Field(default_factory=paths.compose_dir)
    """Directory holding one subdirectory per compose project.

    ``SPIRICONFIG_DOCKER_COMPOSE_DIR``. We only ever look one level deep, and we
    never move or delete project directories -- the user owns this tree. The one
    exception is :func:`~spiriconfig_docker.stacks.create`, which makes a new
    project directory on explicit request and deletes it again if the compose file
    it just wrote does not validate; it touches only what it made, and nothing that
    was already here.

    Defaults to ``/srv/compose`` as root and ``~/spiri-apps`` otherwise -- the
    same answer ``spiriconfig install`` writes, so a bare ``spiriconfig serve`` and
    the installed service agree (see :mod:`spiriconfig.paths`). A checkout points
    this at ``test_data/compose`` through its ``.env``, which
    ``./scripts/test-data.sh`` writes, so a developer's real apps are left alone.
    """

    docker_bin: str = "docker"
    """The docker executable. ``SPIRICONFIG_DOCKER_DOCKER_BIN``."""

    command_timeout: float = 300.0
    """Seconds before a compose command is considered hung.

    ``SPIRICONFIG_DOCKER_COMMAND_TIMEOUT``. Only applies to captured commands;
    streamed ones (``up``, ``logs``) are not subject to it.
    """


def docker_settings() -> DockerSettings:
    """Load docker plugin settings from the environment."""
    return DockerSettings()
