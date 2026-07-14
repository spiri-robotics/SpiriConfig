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
import os
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


def _pty_env(command: Command, rows: int, columns: int) -> dict[str, str]:
    """The environment a program attached to one of our ptys runs in."""
    return {
        **os.environ,
        # Without this, docker sees TERM unset and falls back to no-colour output
        # even though it has a tty.
        "TERM": "xterm-256color",
        "COLUMNS": str(columns),
        "LINES": str(rows),
        **command.env,
    }


def _take_controlling_terminal() -> None:
    """Adopt the pty as our controlling terminal. Runs in the child, before exec.

    ``start_new_session`` has already put us in a session of our own by the time
    this runs, and the pty's slave end is fd 0 -- but a session leader only picks
    up a controlling terminal by *opening* a tty, and we inherited ours rather
    than opening it. So nothing has connected the two, and we have to say it.

    Without a controlling terminal there is no foreground process group for the
    pty to signal, so Ctrl-C lands nowhere: the line discipline dutifully turns it
    into a SIGINT and has no one to deliver it to. The runaway `ping` keeps running
    and the user keeps pressing a key that does nothing.

    Bash, as it happens, would rescue itself here -- it wants job control, notices
    it is a session leader with no terminal, and does this ioctl for itself. Which
    is exactly why we do not leave it to the program on the other end: dash does
    not, busybox's shell does not, and neither does anything that is not a shell at
    all. Every real terminal emulator does this, and it is not the shell's job.
    """
    import fcntl
    import termios

    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class PtySession:
    """A long-lived process on a pty, with the *input* end still connected.

    :func:`stream_pty` runs a command and lets you watch it. This lets you talk
    back to it, which is a different thing and needs a different shape: there is
    no natural end to wait for, the size can change while it runs, and something
    has to notice when the person at the other end walks away.

    So it is a session rather than a generator, and it is the caller's job to end
    it. In the web UI the caller is a page, and the page ends the session when the
    browser disconnects -- see :mod:`spiriconfig_terminal.web`.
    """

    def __init__(
        self,
        command: Command,
        *,
        rows: int = 30,
        columns: int = 120,
        log: Logger = logger,
    ) -> None:
        self.command = command
        self.rows = rows
        self.columns = columns
        self._log = log
        self._master: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

    @property
    def running(self) -> bool:
        """Whether the process is alive and its pty is still open."""
        return self._proc is not None and self._proc.poll() is None

    async def start(self) -> None:
        """Spawn the process on a new pty and begin reading its output.

        Spawned with :class:`subprocess.Popen` rather than asyncio's
        ``create_subprocess_exec``, which is the odd one out in this module and is
        not a matter of taste.

        The child needs :func:`_take_controlling_terminal` to run between the fork
        and the exec, and the only way to ask for that is ``preexec_fn`` -- which
        **uvloop refuses to implement**. It raises ``SubprocessError: Exception
        occurred in preexec_fn`` rather than run it. That matters here and nowhere
        else in this file, because NiceGUI serves on uvloop: this code would work
        in every test and then fail on the only loop that is ever going to run it.
        ``Popen`` forks the child itself, so no event loop gets a say, and it works
        the same under both.
        """
        import pty

        self._log.info("$ {}", self.command)

        master, slave = pty.openpty()
        _set_winsize(slave, self.rows, self.columns)

        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv is built by us
                list(self.command.argv),
                cwd=self.command.cwd,
                env=_pty_env(self.command, self.rows, self.columns),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                # Together these hand the child a terminal it can actually drive:
                # a session of its own, and then that session's controlling tty.
                start_new_session=True,
                preexec_fn=_take_controlling_terminal,  # noqa: PLW1509 - see above
            )
        except FileNotFoundError as exc:
            os.close(master)
            os.close(slave)
            raise CommandError(
                Result(
                    self.command, 127, "", f"executable not found: {self.command.argv[0]}"
                )
            ) from exc

        # Same reason as stream_pty: while we hold the slave, the pty can never
        # report EOF, because we are one of the things keeping it open.
        os.close(slave)

        self._master = master
        asyncio.get_running_loop().add_reader(master, self._readable)

    def _readable(self) -> None:
        """Called by the event loop whenever the pty has something to say."""
        assert self._master is not None
        try:
            data = os.read(self._master, 65536)
        except OSError:
            # EIO on Linux is how a pty reports "the other end has gone away".
            data = b""
        if data:
            self._chunks.put_nowait(data)
        else:
            self._detach()
            self._chunks.put_nowait(None)

    async def output(self) -> AsyncIterator[bytes]:
        """Yield raw output bytes until the process exits or the session is closed.

        Raw, exactly as :func:`stream_pty` yields them, and for the same reason:
        the escape sequences *are* the output, and xterm.js is the thing that
        knows what they mean.
        """
        while (chunk := await self._chunks.get()) is not None:
            yield chunk

    def write(self, data: bytes | str) -> None:
        """Send keystrokes to the process, as though they had been typed.

        Silently does nothing once the session is over. A key pressed in a dead
        terminal is not an error worth raising into the page -- the person can
        already see that the shell has exited, because it said so.
        """
        if self._master is None:
            return
        payload = data.encode() if isinstance(data, str) else data
        try:
            os.write(self._master, payload)
        except OSError as exc:
            self._log.debug("could not write to the pty: {}", exc)

    def resize(self, rows: int, columns: int) -> None:
        """Tell the process its terminal is now this big.

        Recorded even when there is no pty yet, so that a size arriving from the
        browser *before* the process starts is not lost -- it becomes the size the
        process is born with, which is the whole point of asking.
        """
        self.rows = rows
        self.columns = columns
        if self._master is None:
            return
        try:
            # The kernel raises SIGWINCH in the foreground process group, which is
            # how the shell and whatever it is running find out to redraw.
            _set_winsize(self._master, rows, columns)
        except OSError as exc:
            self._log.debug("could not resize the pty: {}", exc)

    def _detach(self) -> None:
        """Stop reading, and let go of the master end. Safe to call twice."""
        master, self._master = self._master, None
        if master is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(master)
        except RuntimeError:  # no loop left, i.e. we are shutting down
            pass
        os.close(master)

    def close(self) -> None:
        """End the session: hang up the terminal, and let the process notice.

        Closing the master end is a hangup, and the kernel does the rest -- SIGHUP
        to the session leader and to the pty's foreground process group. That is
        precisely what happens when a person closes a terminal window, so a shell
        needs no special handling to cope with it, and neither does whatever the
        shell was running: `ping` dies here for the same reason it dies there.
        """
        self._detach()
        # Wakes up anyone still iterating output(), which has no other way to know.
        self._chunks.put_nowait(None)

    async def wait(self) -> int:
        """Wait for the process to exit, and reap it. Returns its exit code.

        Negative when a signal ended it, which is the ordinary case here: hanging
        up the terminal kills the shell with SIGHUP, so a closed browser tab comes
        back as ``-1`` rather than as a failure.

        ``Popen.wait`` blocks, so it goes to a thread. Blocking the event loop here
        would freeze the UI of every other person connected -- and the thing we are
        waiting for is a shell, which might not exit for hours.
        """
        if self._proc is None:
            return 0
        returncode = await asyncio.to_thread(self._proc.wait)
        self._log.debug("-> exit {}", returncode)
        return returncode


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
    import pty

    log.info("$ {}", command)

    master, slave = pty.openpty()
    _set_winsize(slave, rows, columns)

    try:
        proc = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            env=_pty_env(command, rows, columns),
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
