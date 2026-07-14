"""Tests for the terminal plugin, and the pty session underneath it.

Three rules are being defended here.

A terminal must be a *terminal*: the shell has a controlling tty, so Ctrl-C kills
the thing that is running and job control works. Half of that is invisible until
someone is stuck in a runaway `ping` with a key that does nothing.

A closed browser tab must not leak a shell. Nobody sends us a goodbye when a
laptop lid comes down, so the session has to die with the socket, and take what it
was running with it.

And advanced mode still has to be only a display filter -- the terminal is hidden
from the sidebar, and hidden is all it is. If a test here ever has to assert that
advanced mode *prevents* something, we have started using it as a permission
system and it will not hold. See :mod:`spiriconfig.advanced`.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator, Callable

import pytest
from nicegui.testing import User

from spiriconfig import terminal, web
from spiriconfig.commands import Command, CommandError, PtySession
from spiriconfig.plugins import discover

from spiriconfig_terminal import TerminalPlugin, web as terminal_web
from spiriconfig_terminal.config import TerminalSettings
from spiriconfig_terminal.shell import FALLBACK_SHELL, shell_command, shell_path


async def read_until(
    session: PtySession, pattern: bytes, timeout: float = 5.0
) -> re.Match[bytes]:
    """Collect output until ``pattern`` matches it, or fail the test.

    A *pattern*, and not a plain marker, because of a trap that made three of
    these tests pass while proving nothing. **A terminal echoes what you type.**
    So the bytes coming back contain the command as well as its output, and
    ``write("echo yes-a-tty\\n")`` followed by ``assert b"yes-a-tty" in output``
    passes on a broken pty, on a closed one, and on one with no shell behind it at
    all -- it is asserting that the line discipline can echo, which it can.

    The way out is to expect something the typed line *cannot* contain: a value
    the shell has to have actually computed. ``echo alive=$$`` echoes as literal
    ``alive=$$`` and only prints ``alive=1234`` if a real shell really ran it, so
    ``alive=\\d+`` is a claim about the shell rather than about the terminal.

    Waiting on the output rather than sleeping a fixed time keeps the suite honest
    the other way too: a shell takes as long as it takes to start, and on a loaded
    machine that is not a number we could have guessed.
    """
    seen = bytearray()
    found: re.Match[bytes] | None = None

    async def collect() -> None:
        nonlocal found
        async for chunk in session.output():
            seen.extend(chunk)
            if match := re.search(pattern, bytes(seen)):
                found = match
                return

    try:
        await asyncio.wait_for(collect(), timeout)
    except TimeoutError:
        pytest.fail(f"never matched {pattern!r} in:\n{seen.decode(errors='replace')}")

    assert found is not None
    return found


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for something to become true, or fail the test.

    The alternative -- sleeping a plausible-looking 0.3 seconds and then asserting
    -- passes on an idle laptop and fails in a full test run, which is the worst
    of both: it is not measuring the thing it claims to, and it fails at random.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"never became true within {timeout}s")


def alive(pid: int) -> bool:
    """Whether a process we do not own is still around."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FakeSession:
    """A :class:`PtySession` that records what was done to it instead of forking.

    For the page tests, which are about the *wiring* -- does opening the page start
    a shell, does closing the tab end one -- and would otherwise fork a real shell
    per test to find out.
    """

    def __init__(self, command: Command, **kwargs: object) -> None:
        self.command = command
        self.rows = kwargs.get("rows")
        self.columns = kwargs.get("columns")
        self.started = False
        self.closed = False
        self.written: list[bytes | str] = []
        self._ended = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def output(self) -> AsyncIterator[bytes]:
        await self._ended.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    def write(self, data: bytes | str) -> None:
        self.written.append(data)

    def resize(self, rows: int, columns: int) -> None:
        self.rows, self.columns = rows, columns

    def close(self) -> None:
        self.closed = True
        self._ended.set()

    async def wait(self) -> int:
        return 0


@pytest.fixture
async def session() -> AsyncIterator[PtySession]:
    """A live shell, hung up on afterwards however the test ends."""
    started = PtySession(Command(argv=["/bin/sh"]), rows=24, columns=80)
    await started.start()
    try:
        yield started
    finally:
        started.close()


class TestItIsARealTerminal:
    """The shell must not be able to tell it is talking to us rather than to xterm."""

    async def test_the_shell_gets_a_tty(self, session: PtySession) -> None:
        """`tty` prints the device, which the word "tty" on its own would not."""
        session.write("tty\n")
        await read_until(session, rb"/dev/pts/\d+")

    async def test_the_child_gets_a_controlling_terminal(self) -> None:
        """And gets it from *us*, rather than from whatever we happened to run.

        Deliberately not a shell, which would hide the bug this is here to catch.
        Bash acquires a controlling terminal for itself when it finds it has none,
        so with bash on the far end every symptom of our not providing one goes
        away -- and then the first person to point ``SPIRICONFIG_TERMINAL_SHELL``
        at dash gets a Ctrl-C key that silently does nothing.

        ``/dev/tty`` *is* the controlling terminal, by definition, so opening it
        succeeds exactly when you have one -- and the assertion is on the child's
        **exit status**, not on anything it printed. Text was tried and it lied:
        a Python traceback quotes the source line it died on, so a child that
        failed to open the terminal still printed the ``print('has-ctty')`` it
        never reached, and the test passed while proving the opposite. An exit
        code has no such second way of being true.
        """
        prove = "import os; os.open('/dev/tty', os.O_RDONLY)"
        session = PtySession(Command(argv=[sys.executable, "-c", prove]))
        await session.start()
        try:
            async for _ in session.output():
                pass
            assert await asyncio.wait_for(session.wait(), 5) == 0
        finally:
            session.close()

    async def test_ctrl_c_kills_what_is_running(self, session: PtySession) -> None:
        """What the controlling terminal is *for*, from the user's end of it."""
        session.write("sleep 30\n")
        await asyncio.sleep(0.3)  # let the shell actually get into the sleep

        session.write("\x03")  # ^C, as a keystroke, exactly as the browser sends it
        session.write("echo alive=$$\n")

        # Only a shell that got its prompt back can expand $$ -- while `sleep` still
        # owns the terminal, this line is echoed and nothing more.
        await read_until(session, rb"alive=\d+")

    async def test_the_shell_believes_the_size_it_was_given(
        self, session: PtySession
    ) -> None:
        session.write("stty size\n")
        await read_until(session, rb"\b24 80\b")

    async def test_resizing_is_visible_to_the_shell(self, session: PtySession) -> None:
        """A pty that lies about its width is a vim that draws off the edge."""
        session.resize(40, 100)
        session.write("stty size\n")
        await read_until(session, rb"\b40 100\b")


class TestTheSessionEnds:
    """A closed tab must leave nothing running."""

    async def test_close_hangs_up_the_shell(self, session: PtySession) -> None:
        """That it *ends*, not how -- the two ways it can end are a coin toss.

        Closing the master both hangs the terminal up and gives the slave an EOF,
        and the shell races itself: SIGHUP kills it (``-1``), or it reads the EOF
        first and exits of its own accord (``0``). Asserting on either one is a
        test that fails on a busy machine for reasons that mean nothing.
        """
        session.write("echo alive=$$\n")
        await read_until(session, rb"alive=\d+")

        session.close()
        await asyncio.wait_for(session.wait(), 5)
        assert not session.running

    async def test_close_takes_the_foreground_command_with_it(
        self, session: PtySession
    ) -> None:
        """The `ping` you left running dies too, exactly as when a window closes.

        Foreground, and said explicitly, because the kernel's guarantee is about
        the terminal's *foreground process group* -- that is who gets the SIGHUP
        when the master end closes, and nobody has to cooperate for it to land.

        A background job is a different question with a different answer: it has a
        process group of its own, so the kernel does not signal it, and whether it
        survives is between the user and their shell (bash forwards the hangup to
        its jobs; dash does not; a `disown`ed process is meant to outlive you).
        Asserting on that would be asserting on bash, and would fail the day
        somebody's /bin/sh was dash -- so it is not asserted here.

        The inner `sh` prints its pid and then *execs* the sleep, so the pid it
        printed is the pid the sleep runs under, and the sleep is in the foreground
        where the rule applies.
        """
        session.write("sh -c 'echo pid=$$; exec sleep 300'\n")
        child = int((await read_until(session, rb"pid=(\d+)")).group(1))
        assert alive(child)

        session.close()
        await asyncio.wait_for(session.wait(), 5)

        await eventually(lambda: not alive(child))

    async def test_close_ends_the_output_stream(self, session: PtySession) -> None:
        """Otherwise the page's pump task hangs forever on a shell that is gone."""

        async def drain() -> None:
            async for _ in session.output():
                pass

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.2)
        session.close()
        await asyncio.wait_for(task, 5)

    async def test_close_is_safe_to_call_twice(self, session: PtySession) -> None:
        """The disconnect handler and the fixture both call it, and both may win."""
        session.close()
        session.close()

    async def test_writing_to_a_closed_session_is_not_an_error(
        self, session: PtySession
    ) -> None:
        """A key pressed in a dead pane is not worth an exception on the page."""
        session.close()
        session.write("echo hello\n")

    async def test_a_missing_shell_is_a_command_error(self) -> None:
        """And not an obscure traceback out of the guts of asyncio."""
        broken = PtySession(Command(argv=["definitely-not-a-real-shell-4f2a"]))
        with pytest.raises(CommandError, match="executable not found"):
            await broken.start()


class TestTheLoopTheServerActuallyRunsOn:
    """The suite runs on asyncio. NiceGUI does not. That gap hid a total failure.

    Every test above passed while the feature could not start a shell *at all* in
    the real application. Spawning the child needs ``preexec_fn`` to claim the
    controlling terminal, and **uvloop refuses to implement it** -- it raises
    ``SubprocessError: Exception occurred in preexec_fn`` instead. uvicorn installs
    uvloop, so the only loop this code ever runs on in production was the one loop
    the tests never used.

    Hence this: the same session, on the loop the server has. It is a slow, ugly
    test that stands up a whole second event loop, and it is worth every line --
    without it, "the tests pass" and "it works" are two different claims.
    """

    def test_a_shell_starts_and_answers_under_uvloop(self) -> None:
        uvloop = pytest.importorskip("uvloop", reason="uvicorn ships it; NiceGUI uses it")

        async def main() -> None:
            session = PtySession(Command(argv=["/bin/sh"]), rows=24, columns=80)
            await session.start()
            try:
                session.write("echo alive=$$\n")
                await read_until(session, rb"alive=\d+")
            finally:
                session.close()
                await session.wait()

        uvloop.run(main())


class TestWhichShell:
    def test_the_setting_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert shell_path(TerminalSettings(shell="/bin/bash")) == "/bin/bash"

    def test_then_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert shell_path(TerminalSettings()) == "/bin/zsh"

    def test_a_service_with_no_shell_in_its_environment_still_gets_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The normal case on a device: systemd starts us with no $SHELL at all."""
        monkeypatch.delenv("SHELL", raising=False)
        assert shell_path(TerminalSettings())  # from passwd, or the fallback

    def test_it_is_a_login_shell_by_default(self) -> None:
        """So the PATH matches the one you would have had over SSH."""
        command = shell_command(TerminalSettings(shell="/bin/bash"))
        assert list(command.argv) == ["/bin/bash", "-l"]

    def test_login_can_be_turned_off(self) -> None:
        command = shell_command(TerminalSettings(shell="/bin/sh", login=False))
        assert list(command.argv) == ["/bin/sh"]

    def test_it_starts_somewhere_a_person_would_expect(self) -> None:
        command = shell_command(TerminalSettings(shell=FALLBACK_SHELL))
        assert command.cwd is not None
        assert command.cwd.is_dir()

    def test_the_command_is_the_copyable_line_the_page_shows(self) -> None:
        """The page prints this string, and a person pastes it into a real shell."""
        command = shell_command(TerminalSettings(shell="/bin/bash"))
        assert str(command) == f"cd {command.cwd} && /bin/bash -l"


class TestThePlugin:
    def test_is_discovered_from_its_entry_point(self) -> None:
        assert "terminal" in {p.name for p in discover()}

    def test_offers_both_a_cli_and_a_page(self) -> None:
        plugin = TerminalPlugin()
        assert plugin.cli() is not None
        assert plugin.has_page is True

    def test_it_declares_itself_advanced(self) -> None:
        assert TerminalPlugin().advanced is True


class TestThePageOpensAndClosesTheShell:
    """The wiring between the browser and the pty, with a fake pty on the end.

    The real one is exercised above. What is left to get wrong is the *page*: a
    shell that never starts because nothing fires the timer, and -- the one that
    actually costs something -- a shell that never stops, because a page that
    forgets this leaves one running for every tab anyone ever opened.
    """

    @pytest.fixture
    def spy(self, monkeypatch: pytest.MonkeyPatch) -> list[FakeSession]:
        """Stand in for the pty, and record what the page does to it."""
        made: list[FakeSession] = []

        def make(command: Command, **kwargs: object) -> FakeSession:
            made.append(FakeSession(command, **kwargs))
            return made[-1]

        monkeypatch.setattr(terminal_web, "PtySession", make)
        return made

    async def test_opening_the_page_starts_a_shell(
        self, user: User, spy: list[FakeSession]
    ) -> None:
        web.build([TerminalPlugin()])
        await user.open("/terminal")

        # Not immediately: the page only describes itself when it is built, and the
        # shell is started once the browser is actually on the other end.
        await eventually(lambda: bool(spy) and spy[0].started)
        assert len(spy) == 1

    async def test_it_runs_the_shell_the_cli_would_have_run(
        self, user: User, spy: list[FakeSession]
    ) -> None:
        """The page and the command line must not drift into different shells."""
        web.build([TerminalPlugin()])
        await user.open("/terminal")
        await eventually(lambda: bool(spy))

        assert spy[0].command == shell_command(TerminalSettings())

    async def test_a_browser_that_will_not_be_measured_still_gets_a_shell(
        self, user: User, spy: list[FakeSession]
    ) -> None:
        """The harness never answers ``fit()``, which is the point of this test.

        It stands in for a browser that is slow, wedged, or gone. A page that
        insisted on knowing its size would hang here and hand the user an empty
        black box; this one shrugs, takes the default, and starts the shell.
        """
        web.build([TerminalPlugin()])
        await user.open("/terminal")

        await eventually(lambda: bool(spy) and spy[0].started)
        assert (spy[0].rows, spy[0].columns) == (
            terminal.TERMINAL_ROWS,
            terminal.TERMINAL_COLUMNS,
        )

    async def test_the_shell_dies_with_the_browser(
        self, user: User, spy: list[FakeSession]
    ) -> None:
        """A closed laptop lid must not leave a shell running on the device."""
        web.build([TerminalPlugin()])
        client = await user.open("/terminal")
        await eventually(lambda: bool(spy) and spy[0].started)

        for handler in client.disconnect_handlers:
            handler()

        assert spy[0].closed


class TestItIsHiddenNotForbidden:
    """Advanced mode hides the terminal. That is the entire claim being made."""

    async def test_it_is_not_in_the_sidebar_by_default(self, user: User) -> None:
        web.build([TerminalPlugin()])
        await user.open("/")
        await user.should_not_see("Terminal")

    async def test_turning_advanced_on_reveals_it(self, user: User) -> None:
        web.build([TerminalPlugin()])
        await user.open("/")
        await user.should_not_see("Terminal")

        user.find("Advanced").click()
        await user.should_see("Terminal")

    async def test_the_page_is_still_reachable_with_advanced_off(
        self, user: User
    ) -> None:
        """Not a bug, and not a hole -- it is what advanced mode *is*.

        The sidebar entry is hidden because a terminal is not what a normal user
        came here for, not because we are keeping anybody out of it. The CLI hands
        out the same shell to the same user regardless, and nothing here is
        pretending otherwise. Keeping people out is authentication's job.
        """
        web.build([TerminalPlugin()])
        response = await user.http_client.get("/terminal")
        assert response.status_code == 200
