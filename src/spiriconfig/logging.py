"""Loguru setup.

Called once at startup by the CLI. Library code and plugins should just
``from loguru import logger`` and log; they must not configure sinks.
"""

from __future__ import annotations

import sys

from loguru import logger

from spiriconfig.config import Settings

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> <level>{level: <8}</level> "
    "<cyan>{extra[plugin]}</cyan> <level>{message}</level>"
)


def configure(config: Settings) -> None:
    """Point loguru at stderr, and optionally a file.

    Removes loguru's default handler so that we do not double-log, and binds a
    ``plugin`` field so the format string can always reference it. Plugins
    override it with ``logger.bind(plugin="docker")``.
    """
    logger.remove()
    logger.configure(extra={"plugin": "core"})

    logger.add(
        sys.stderr,
        level=config.log_level.upper(),
        format=_CONSOLE_FORMAT,
        colorize=True,
    )

    if config.log_file:
        logger.add(
            config.log_file,
            level=config.log_level.upper(),
            rotation="10 MB",
            retention=5,
            enqueue=True,
        )
