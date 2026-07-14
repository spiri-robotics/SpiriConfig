"""Terminal plugin settings, read from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class TerminalSettings(BaseSettings):
    """Settings for the terminal plugin, prefixed ``SPIRICONFIG_TERMINAL_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_TERMINAL_",
        env_file=".env",
        extra="ignore",
    )

    shell: str = ""
    """The shell to run. ``SPIRICONFIG_TERMINAL_SHELL``.

    Empty means "work it out", which is the answer that is right more often than
    any path we could write here -- see :func:`spiriconfig_terminal.shell.shell_path`.
    Set it when the machine's idea of your shell is not the one you want in this
    window, e.g. ``/bin/bash`` on a box whose passwd entry still says ``/bin/sh``.
    """

    login: bool = True
    """Run the shell as a login shell, with ``-l``. ``SPIRICONFIG_TERMINAL_LOGIN``.

    On by default, because the alternative is a shell that surprises people: no
    ``/etc/profile``, so a ``PATH`` that is missing whatever the machine's own
    profile adds to it, and commands that work over SSH failing here with "not
    found". The cost is startup files running, which is what a terminal is for.

    Turn it off for a shell that does not understand ``-l``.
    """


def terminal_settings() -> TerminalSettings:
    """Load terminal plugin settings from the environment."""
    return TerminalSettings()
