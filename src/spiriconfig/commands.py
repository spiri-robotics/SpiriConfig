"""Running external commands.

Everything SpiriConfig does to the system is done by shelling out to the tool a
human would have used. This module is the only place that spawns processes, and
:class:`Command` is deliberately printable: the string it renders is the exact
line a user can paste into a shell to do the same thing by hand.

That is the whole point of the design. A plugin should never reach for a Python
API when a command line exists, because then the UI can do something the user
cannot reproduce or audit.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # loguru only exports the Logger type to type checkers
    from loguru import Logger


@dataclass(frozen=True, slots=True)
class Command:
    """An external command, plus the context needed to reproduce it by hand."""

    argv: Sequence[str]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        """Render as a shell line the user can copy, paste, and run."""
        parts = [f"{k}={shlex.quote(v)}" for k, v in sorted(self.env.items())]
        parts += [shlex.quote(str(a)) for a in self.argv]
        line = " ".join(parts)
        if self.cwd is not None:
            return f"cd {shlex.quote(str(self.cwd))} && {line}"
        return line


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of a :class:`Command` that ran to completion."""

    command: Command
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> Result:
        """Return self, or raise :class:`CommandError` if the command failed."""
        if not self.ok:
            raise CommandError(self)
        return self


class CommandError(RuntimeError):
    """A command exited non-zero."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(
            f"command failed with exit code {result.returncode}: {result.command}\n"
            f"{result.stderr.strip()}"
        )


def _popen_env(command: Command) -> dict[str, str] | None:
    """Overlay the command's env on the current one, or None to inherit as-is."""
    if not command.env:
        return None
    import os

    return {**os.environ, **command.env}


def run(
    command: Command,
    *,
    timeout: float | None = 60.0,
    log: Logger = logger,
) -> Result:
    """Run ``command`` to completion and capture its output.

    Use this for short, quick commands. Anything that streams output or may run
    for a long time (``up``, ``pull``, ``logs -f``) should use :func:`stream`,
    so the user sees progress instead of a spinner.

    The command line is logged at DEBUG rather than INFO. Captured commands are
    overwhelmingly read-only queries -- statuses, listings -- and logging each
    one at INFO buries the commands that actually changed something.

    Pass ``log`` to attribute the command to a plugin, e.g.
    ``log=logger.bind(plugin="docker")``.
    """
    log.debug("$ {}", command)
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built by us, never a shell string
            list(command.argv),
            cwd=command.cwd,
            env=_popen_env(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            Result(command, 127, "", f"executable not found: {command.argv[0]}")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            Result(command, 124, "", f"timed out after {timeout}s")
        ) from exc

    result = Result(command, proc.returncode, proc.stdout, proc.stderr)
    if result.ok:
        log.debug("-> exit 0")
    else:
        log.warning("-> exit {}: {}", result.returncode, result.stderr.strip())
    return result


def _set_winsize(fd: int, rows: int, columns: int) -> None:
    """Tell the pty how big it is, so programs wrap and draw to the right width."""
    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


async def stream_pty(
    command: Command,
    *,
    log: Logger = logger,
    rows: int = 30,
    columns: int = 120,
) -> AsyncIterator[bytes]:
    """Run ``command`` attached to a pseudo-terminal, yielding raw output bytes.

    The difference between this and :func:`stream` is not cosmetic, and it is not
    really about colour either.

    Docker asks whether its output is going to a terminal, and changes what it
    *says* based on the answer. Through a pipe it gives up and emits a flat
    transcript; on a tty it draws progress bars, redraws layer download status in
    place, and uses colour to separate services. So piping the output does not
    give us a plain version of the same information -- it gives us a program that
    decided we were not worth talking to properly. `docker compose pull` is the
    obvious victim: on a pipe it is a wall of "Pulling", on a tty it is a live
    picture of what is happening.

    So we give it a terminal. The bytes that come back are raw, carriage returns
    and escape sequences and all, and they are only meaningful to something that
    can interpret them -- which is why they go to xterm.js in the browser rather
    than to :class:`ui.log`. Interpreting them ourselves would mean writing a
    terminal emulator, which is a thing that already exists.

    Yields ``bytes``, deliberately: decoding here would mean splitting on
    character boundaries we do not control, and a multi-byte character straddling
    a read boundary would be mangled. The browser reassembles the stream.
    """
    import os
    import pty

    log.info("$ {}", command)

    master, slave = pty.openpty()
    _set_winsize(slave, rows, columns)

    env = {
        **os.environ,
        # Without this, docker sees TERM unset and falls back to no-colour output
        # even though it has a tty.
        "TERM": "xterm-256color",
        "COLUMNS": str(columns),
        "LINES": str(rows),
        **command.env,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
    except FileNotFoundError as exc:
        os.close(master)
        os.close(slave)
        raise CommandError(
            Result(command, 127, "", f"executable not found: {command.argv[0]}")
        ) from exc

    # The child holds the only copy that matters now. Ours has to go, or we will
    # never see EOF on the master: the pty stays open as long as any process has
    # the slave, and that would include us, forever.
    os.close(slave)

    loop = asyncio.get_running_loop()
    chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _readable() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:
            # EIO on Linux is how a pty reports "the other end has gone away".
            # It is the normal end of the stream, not a failure.
            data = b""
        if data:
            chunks.put_nowait(data)
        else:
            loop.remove_reader(master)
            chunks.put_nowait(None)

    loop.add_reader(master, _readable)
    try:
        while (chunk := await chunks.get()) is not None:
            yield chunk
    finally:
        loop.remove_reader(master)
        os.close(master)

    returncode = await proc.wait()
    if returncode != 0:
        log.warning("-> exit {}", returncode)
        # \r\n, not \n: this goes to a terminal, where a bare newline moves down
        # a line without returning to the left margin.
        yield f"\r\n[command exited with code {returncode}]\r\n".encode()
    else:
        log.debug("-> exit 0")


async def stream(command: Command, *, log: Logger = logger) -> AsyncIterator[str]:
    """Run ``command``, yielding output lines as they arrive.

    stderr is folded into stdout because that is what the user would see in a
    terminal, and docker compose writes most of its progress to stderr. The
    final line yielded is a synthetic marker when the command fails, so callers
    that only render lines still surface the failure.

    Unlike :func:`run`, the command line is logged at INFO: these are the long,
    state-changing commands, and the log is the record of what we did to the
    user's machine.
    """
    log.info("$ {}", command)
    proc = await asyncio.create_subprocess_exec(
        *command.argv,
        cwd=command.cwd,
        env=_popen_env(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None

    async for raw in proc.stdout:
        yield raw.decode(errors="replace").rstrip("\n")

    returncode = await proc.wait()
    if returncode != 0:
        log.warning("-> exit {}", returncode)
        yield f"[command exited with code {returncode}]"
    else:
        log.debug("-> exit 0")
