"""Which shell we run, and where.

One function's worth of logic, given a module of its own because the CLI and the
web page must agree about it. The promise the whole project makes is that the
button and the command line do the same thing; here that promise is literally the
same :class:`~spiriconfig.commands.Command`, built once and used by both.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from loguru import logger

from spiriconfig.commands import Command

from spiriconfig_terminal.config import TerminalSettings

log = logger.bind(plugin="terminal")

#: What we fall back to when nothing on the machine will say. POSIX guarantees it
#: exists, which is the only claim being made for it -- it is the shell you get
#: when we have run out of better ideas, not a shell anybody asked for.
FALLBACK_SHELL = "/bin/sh"


def shell_path(settings: TerminalSettings) -> str:
    """The shell to run, in descending order of who knows best.

    The configured one wins: somebody said so on purpose. Then ``$SHELL``, which
    is the answer when SpiriConfig was started from a human's terminal -- during
    development, mostly. Then the passwd entry, which is the answer on a real
    device, where we are a service and inherited an environment with no ``$SHELL``
    in it at all. Then :data:`FALLBACK_SHELL`, so that a machine with a corrupt
    passwd file still gets a usable terminal in which to go and fix it.
    """
    if settings.shell:
        return settings.shell

    if from_env := os.environ.get("SHELL"):
        return from_env

    try:
        if from_passwd := pwd.getpwuid(os.getuid()).pw_shell:
            return from_passwd
    except KeyError:
        # A uid with no passwd entry. Happens inside containers run with `--user`,
        # and it is not fatal -- it just means nobody can tell us anything.
        log.debug("uid {} has no passwd entry", os.getuid())

    return FALLBACK_SHELL


def home() -> Path:
    """Where the shell starts, which is where a person would expect to land.

    Falls back to ``/`` rather than to the current working directory: our cwd is
    wherever the service manager happened to start us, and dropping the user into
    it would be arbitrary in a way that is hard to notice and easy to act on.
    """
    try:
        directory = Path.home()
    except RuntimeError:  # no home resolvable at all
        return Path("/")
    return directory if directory.is_dir() else Path("/")


def shell_command(settings: TerminalSettings) -> Command:
    """The shell, as a :class:`Command` -- printable, and the same one for both faces.

    Note what is *not* here: no ``su``, no ``sudo``, no user to become. This shell
    runs as whoever SpiriConfig runs as, and that is the whole of the access story
    today. It is not an escalation -- anyone who can reach this page can already
    reach the plugins that run docker as that same user -- and it is not a
    boundary either. Authentication is a separate piece of work, and when it lands
    it goes in front of the *whole application*, not in front of this page.
    """
    argv = [shell_path(settings)]
    if settings.login:
        argv.append("-l")
    return Command(argv=argv, cwd=home())


__all__ = ["FALLBACK_SHELL", "home", "shell_command", "shell_path"]
