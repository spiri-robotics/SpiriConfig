"""Docker plugin settings, read from the environment."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class DockerSettings(BaseSettings):
    """Settings for the docker plugin, prefixed ``SPIRICONFIG_DOCKER_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_DOCKER_",
        env_file=".env",
        extra="ignore",
    )

    compose_dir: Path = Path("/srv/compose")
    """Directory holding one subdirectory per compose project.

    ``SPIRICONFIG_DOCKER_COMPOSE_DIR``. We only ever look one level deep, and we
    never create, move, or delete project directories -- the user owns this tree.
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
