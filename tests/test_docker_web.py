"""Tests for the docker plugin's page.

There were none, and that is how the disappearing-logs bug got in: every other
test asserts on *which command we built*, which is the right thing to test for a
tool that shells out -- and is completely blind to a dialog that renders the
output correctly and then deletes itself.

These drive the page the way a person does: click the button, look at what is on
the screen a moment later.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest
import typer
from nicegui import ui
from nicegui.element import Element
from nicegui.testing import User

from spiriconfig import theme, web
from spiriconfig.commands import Command, Result
from spiriconfig.plugins import Plugin
from spiriconfig_docker import env
from spiriconfig_docker import web as docker_web
from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.stacks import Stack, Usage

from tests.conftest import docker_required


class _DockerPage(Plugin):
    """The docker plugin, pinned to a temporary compose directory."""

    name = "docker"
    title = "Apps"

    def __init__(self, settings: DockerSettings) -> None:
        self._settings = settings

    def cli(self) -> typer.Typer:
        return typer.Typer()

    def page(self) -> None:
        from spiriconfig_docker import web as docker_web

        docker_web.page(self._settings)


class TestTheOutputDialog:
    async def test_it_stays_open_after_the_command_finishes(
        self, user: User, settings: DockerSettings
    ) -> None:
        """The regression test for the bug that started this.

        Every action dialog streams a command and then hands control back to a
        caller that calls refresh(), and refresh clears the container the dialog
        was created inside -- deleting it. `up` and `pull` stream for long enough
        that nobody noticed. `logs` returns instantly, so the modal flashed up
        and vanished before it could be read.

        So: run something, wait for it to be well and truly finished, and check
        the output is still on screen.
        """
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Logs").click()
        await user.should_see("hello — logs")

        # Long enough for the command to exit and for a stray refresh() to have
        # torn the dialog down, if one were coming.
        await asyncio.sleep(1.0)

        await user.should_see("hello — logs")

    async def test_close_dismisses_it(
        self, user: User, settings: DockerSettings
    ) -> None:
        """And the user can still get rid of it, which is the other half."""
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Logs").click()
        await user.should_see("hello — logs")
        await asyncio.sleep(1.0)

        user.find("Close").click()
        await user.should_not_see("hello — logs")

    async def test_the_page_survives_closing_the_dialog(
        self, user: User, settings: DockerSettings
    ) -> None:
        """Closing a dialog must not blank the page behind it.

        Every action refreshes the page afterwards, and refresh schedules
        `render()` on a `ui.timer`. A NiceGUI handler runs with the *clicked
        element's* slot active -- which is inside the container -- so the timer
        became a child of the container, and `render()`'s first act is to clear
        the container. It deleted the running timer and cancelled itself, leaving
        an empty page.
        """
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Logs").click()
        await user.should_see("hello — logs")
        await asyncio.sleep(1.0)

        user.find("Close").click()
        await asyncio.sleep(1.0)

        # The stack and its buttons are still there, and still usable.
        await user.should_see("hello")
        await user.should_see("Up")
        await user.should_see("Logs")


class TestUsageOnTheCard:
    """CPU and memory on each card, shown to everyone -- that is the whole feature.

    Driven through the real page, because the risk here is not what number we
    compute (that is tested in test_stacks.py) but whether it reaches the screen.
    """

    async def test_running_stack_shows_cpu_and_memory(
        self, user: User, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Stack, "status", lambda self: "running")
        monkeypatch.setattr(
            Stack, "usage", lambda self: Usage(cpu_percent=12.5, mem_bytes=45068554)
        )
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("12.5%")
        await user.should_see("43.0 MiB")

    async def test_not_behind_advanced_mode(
        self, user: User, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A regular user is exactly who "how much is this using" is for."""
        monkeypatch.setattr(Stack, "status", lambda self: "running")
        monkeypatch.setattr(
            Stack, "usage", lambda self: Usage(cpu_percent=3.0, mem_bytes=1024**2)
        )
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        # Without ever turning advanced mode on, the numbers are there.
        await user.should_see("3.0%")

    async def test_stopped_stack_shows_no_numbers(
        self, user: User, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None means "nothing to measure"; the badge already says it is stopped."""
        monkeypatch.setattr(Stack, "status", lambda self: "stopped")
        monkeypatch.setattr(Stack, "usage", lambda self: None)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")
        await user.should_not_see("%")


class TestExecAndAttachAreDeveloperTools:
    """A prompt inside a container is not what an ordinary user came here for.

    So both buttons are advanced-only -- which, as ever, is decluttering and not a
    permission: the CLI hands out the same shell to the same user regardless, and
    the switch that reveals them is one click away in the sidebar.
    """

    def _one_running_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pretend `hello` is up, without needing a daemon to make it so.

        What is being tested here is the page's wiring, not docker's. The real
        thing -- that an exec'd shell dies with the browser tab -- is in
        `tests/test_stacks.py`, against a real container, because nothing less
        would have caught it.
        """
        monkeypatch.setattr(Stack, "running_services", lambda self: ["hello"])

    async def test_they_are_hidden_until_advanced_is_on(
        self, user: User, settings: DockerSettings
    ) -> None:
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        await user.should_not_see("Exec")
        await user.should_not_see("Attach")

        user.find("Advanced").click()

        await user.should_see("Exec")
        await user.should_see("Attach")

    async def test_an_app_that_is_not_running_says_so(
        self, user: User, settings: DockerSettings
    ) -> None:
        """Both of these need a live process, so a stopped app has nothing to offer
        either of them. Saying so beats a menu with nothing in it, and beats handing
        docker a service name it will only reject."""
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Exec").click()

        await user.should_see("no running containers", retries=20)

    async def test_the_exec_dialog_shows_the_command_you_could_have_typed(
        self, user: User, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The honesty guarantee, and the reason `shown` exists at all.

        What we *run* is wrapped in a pidfile babysitter, because docker orphans an
        exec whose client goes away (moby#9098). What we *show* is the plain command
        -- the one the CLI runs, the one worth copying, the one about the user's
        container rather than about our implementation.
        """
        self._one_running_service(monkeypatch)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Exec").click()
        await user.should_see("hello — exec", retries=20)

        await user.should_see("exec hello /bin/sh")
        await user.should_not_see("spiriconfig-exec")

    async def test_the_exec_dialog_will_run_more_than_a_shell(
        self, user: User, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is `docker compose exec`, which runs anything the image has. A button
        labelled "Shell" would be a smaller tool wearing a costume, so the command is
        a box -- and the line rebuilds itself as you type in it."""
        self._one_running_service(monkeypatch)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Exec").click()
        await user.should_see("hello — exec", retries=20)

        user.find(ui.input).elements.pop().set_value("ls -la /etc")

        await user.should_see("exec hello ls -la /etc")

    async def test_attach_always_asks_which_container(
        self, user: User, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with one service to choose from. A dropdown that appears or does not
        depending on how many containers happen to be up is a UI you have to learn,
        and the first thing anybody does is go looking for the menu that is not there.
        """
        self._one_running_service(monkeypatch)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Attach").click()

        await user.should_see("hello — attach", retries=20)
        assert user.find(ui.select).elements


class FakeSession:
    """A :class:`PtySession` that records what was done to it instead of forking.

    The same stand-in the terminal page's tests use, and for the same reason: what
    is being tested here is the *dialog*, and it should not need a container.
    """

    def __init__(self, command: Command, **kwargs: object) -> None:
        self.command = command
        self.started = False
        self.closed = False
        self._ended = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def output(self):
        await self._ended.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    def write(self, data: bytes | str) -> None:
        pass

    def resize(self, rows: int, columns: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self._ended.set()

    async def wait(self) -> int:
        return 0


async def _eventually(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never became true within {timeout}s")


class TestTheExecTerminalItself:
    """Pressing Run. Nothing above this reached the terminal dialog at all.

    Which is a gap worth naming: every test so far stopped at the picker, so the
    part that actually spawns a process -- and the part that cleans up after docker
    -- was covered by nothing. That is precisely the shape of the bug that once let
    a whole feature ship broken while the suite was green.
    """

    @pytest.fixture
    def spy(self, monkeypatch: pytest.MonkeyPatch) -> list[FakeSession]:
        made: list[FakeSession] = []

        def make(command: Command, **kwargs: object) -> FakeSession:
            made.append(FakeSession(command, **kwargs))
            return made[-1]

        monkeypatch.setattr(docker_web, "PtySession", make)
        monkeypatch.setattr(Stack, "running_services", lambda self: ["hello"])
        return made

    @pytest.fixture
    def reaped(self, monkeypatch: pytest.MonkeyPatch) -> list[Command]:
        """Every command the dialog runs *besides* the session -- i.e. the reaper."""
        calls: list[Command] = []

        def fake_run(command: Command, **kwargs: object) -> Result:
            calls.append(command)
            return Result(command, 0, "", "")

        monkeypatch.setattr(docker_web, "run", fake_run)
        return calls

    async def _open_a_shell(self, user: User, settings: DockerSettings) -> None:
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Exec").click()
        await user.should_see("hello — exec", retries=20)
        user.find(marker="exec-run").click()

    async def test_it_runs_the_supervised_exec_but_shows_the_plain_one(
        self,
        user: User,
        settings: DockerSettings,
        spy: list[FakeSession],
        reaped: list[Command],
    ) -> None:
        """The whole trick, asserted from both ends at once."""
        await self._open_a_shell(user, settings)
        await _eventually(lambda: bool(spy) and spy[0].started)

        # What we RUN carries the babysitter, because docker will not clean up.
        assert "spiriconfig-exec" in str(spy[0].command)
        assert 'exec "$@"' in str(spy[0].command)

        # What we SHOW is the line a person could have typed.
        await user.should_see("exec hello /bin/sh")
        await user.should_not_see("spiriconfig-exec")

    async def test_closing_the_dialog_hangs_up_and_reaps_the_container(
        self,
        user: User,
        settings: DockerSettings,
        spy: list[FakeSession],
        reaped: list[Command],
    ) -> None:
        """Two kills, not one. Hanging up our end is what a terminal does; killing
        the process docker left inside the container is what docker will not do."""
        await self._open_a_shell(user, settings)
        await _eventually(lambda: bool(spy) and spy[0].started)

        user.find("Close").click()

        await _eventually(lambda: spy[0].closed)
        await _eventually(lambda: any("kill -HUP" in str(c) for c in reaped))

    async def test_a_closed_browser_tab_reaps_too(
        self,
        user: User,
        settings: DockerSettings,
        spy: list[FakeSession],
        reaped: list[Command],
    ) -> None:
        """The ending nobody announces. A dialog that only cleaned up on its Close
        button would leak a shell into the container for every dropped wifi and
        every closed laptop lid -- which is the common way a web terminal ends."""
        web.build([_DockerPage(settings)])
        client = await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Exec").click()
        await user.should_see("hello — exec", retries=20)
        user.find(marker="exec-run").click()
        await _eventually(lambda: bool(spy) and spy[0].started)

        # Awaiting what comes back, because one of these handlers is a coroutine and
        # calling it is not the same as running it. NiceGUI's own dispatcher hands an
        # awaitable result to a background task; a test that only *called* them would
        # be asserting that the reaper was constructed, not that it ran.
        for handler in client.disconnect_handlers:
            if inspect.isawaitable(result := handler()):
                await result

        await _eventually(lambda: spy[0].closed)
        await _eventually(lambda: any("kill -HUP" in str(c) for c in reaped))

    async def test_attach_needs_no_babysitter(
        self,
        user: User,
        settings: DockerSettings,
        spy: list[FakeSession],
        reaped: list[Command],
    ) -> None:
        """It starts no new process, so it orphans none. Wrapping it would be
        cargo-culting the workaround onto a command that does not need it."""
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        user.find("Advanced").click()
        user.find("Attach").click()
        await user.should_see("hello — attach", retries=20)
        user.find(marker="pick-open").click()

        await _eventually(lambda: bool(spy) and spy[0].started)

        assert "spiriconfig-exec" not in str(spy[0].command)
        assert str(spy[0].command).endswith("attach hello")

        user.find("Close").click()
        await _eventually(lambda: spy[0].closed)
        assert not [c for c in reaped if "kill -HUP" in str(c)]


class TestTheEditorFollowsTheOperatingSystem:
    """The app runs with ``dark=None``, so the *browser* decides whether the page
    is dark and the server never finds out unless it asks. CodeMirror has no theme
    that follows along, so an editor that is not told ends up as a white panel in
    a dark page -- or, worse, unreadable the other way about.
    """

    async def _open_the_editor(self, user: User, settings: DockerSettings) -> Element:
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")

        # Editing is a developer feature, so it is behind the advanced switch.
        user.find("Advanced").click()
        user.find("Edit").click()

        # Wait on "Save", which exists only in this dialog. Waiting on the file
        # name instead looks right and is a false positive: the stack's card is
        # already showing "compose.yaml" behind the dialog, so the assertion
        # passes before the editor has been built.
        # Patiently, because a browser that never answers makes the dialog wait out
        # the round trip's own timeout before it gives up and renders.
        await user.should_see("Save", retries=20)
        return user.find(ui.codemirror).elements.pop()

    async def test_a_dark_browser_gets_the_dark_editor(
        self, user: User, settings: DockerSettings
    ) -> None:
        user.javascript_rules[re.compile(r".*prefers-color-scheme.*")] = lambda _: True

        editor = await self._open_the_editor(user, settings)
        assert editor.props["theme"] == theme.CODEMIRROR_DARK

    async def test_a_light_browser_gets_the_light_editor(
        self, user: User, settings: DockerSettings
    ) -> None:
        user.javascript_rules[re.compile(r".*prefers-color-scheme.*")] = lambda _: False

        editor = await self._open_the_editor(user, settings)
        assert editor.props["theme"] == theme.CODEMIRROR_LIGHT

    async def test_a_browser_that_never_answers_still_gets_an_editor(
        self, user: User, settings: DockerSettings
    ) -> None:
        """No JavaScript rule is registered here, so the question goes unanswered
        and the call times out -- which is the point. The editor is how a broken
        compose file gets fixed, so it has to open even when the page is unwell
        enough to drop a round trip. It opens light, which is legible either way."""
        editor = await self._open_the_editor(user, settings)
        assert editor.props["theme"] == theme.CODEMIRROR_LIGHT


def _a_browser_that_answers(user: User) -> None:
    """Answer the question the settings dialog asks before it renders.

    It carries an editor now, and an editor has to be told whether the page is dark
    (see `theme.codemirror_theme`). A real browser replies in a millisecond; a test
    user replies never, and the dialog waits out the round trip's own timeout before
    giving up -- which is a second of nothing, in every test that opens the form.
    """
    user.javascript_rules[re.compile(r".*prefers-color-scheme.*")] = lambda _: False


class TestTheSettingsForm:
    """The form generated from an app's `x-spiri-settings`.

    `configurable` is a stack that declares one; `hello` is the ordinary kind that
    declares nothing. Both are on the page, which is the interesting part -- an app
    with no settings must not grow a button that opens an empty form.
    """

    async def _open(self, user: User, settings: DockerSettings) -> None:
        _a_browser_that_answers(user)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("configurable")
        user.find("Settings").click()
        await user.should_see("configurable — settings")

    async def test_only_an_app_that_declares_settings_gets_the_button(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("hello")
        await user.should_see("configurable")

        # One Settings button on the page, and it is not `hello`'s.
        assert len(user.find("Settings").elements) == 1

    async def test_the_form_shows_a_widget_for_every_declared_field(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        await self._open(user, settings)

        await user.should_see("Greeting")
        await user.should_see("Port")
        await user.should_see("Log level")
        await user.should_see("Allow anonymous access")
        await user.should_see("Admin password")

        # And the help text the app wrote, which is the entire reason `help:` exists.
        await user.should_see("What the app says when it starts.")

    async def test_the_form_is_filled_in_from_the_env_file(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        (configurable.path / ".env").write_text("GREETING=from-the-file\n")

        await self._open(user, settings)

        greeting = user.find(marker="setting-GREETING").elements.pop()
        assert greeting.value == "from-the-file"

    async def test_a_field_the_env_file_does_not_set_shows_its_default(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """The state of an app nobody has configured yet, which is every app the
        moment it is installed."""
        await self._open(user, settings)

        assert user.find(marker="setting-GREETING").elements.pop().value == "hello"
        # A number widget, so this one has been through float() and back.
        assert user.find(marker="setting-PORT").elements.pop().value == 8080

    async def test_the_raw_env_file_is_an_advanced_thing_to_want(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Turning knobs an app declared is an ordinary act. Reading the file they
        land in is a developer's question, so it is behind the switch -- but it is
        *there*, because a developer should be able to see through the form to the
        file at any point."""
        await self._open(user, settings)
        await user.should_not_see(str(configurable.path / ".env"))

        user.find("Advanced").click()
        await user.should_see(str(configurable.path / ".env"))


class TestResettingAFieldToItsDefault:
    """The per-field reset button.

    It restores the widget to the `default:` the app declared -- the value the form
    offered before anyone touched it -- and it is there only when there is something
    to undo, so a form nobody has changed does not wear a reset on every field.
    """

    async def _open(self, user: User, settings: DockerSettings) -> None:
        _a_browser_that_answers(user)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("configurable")
        user.find("Settings").click()
        await user.should_see("configurable — settings")

    async def test_a_field_at_its_default_has_no_reset_button(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """A fresh form shows every field at its default, so there is nothing to
        reset and the button hides itself -- which `should_not_see` is exactly the
        check for, since a hidden element is one the page does not show."""
        await self._open(user, settings)
        await user.should_not_see(marker="reset-GREETING")

    async def test_the_button_appears_once_the_value_drifts(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """A live value change reveals the reset. `set_value` rather than `type`,
        because it is what fires the change event the harness leaves out of a
        simulated keystroke -- a real q-input emits it as you type, which is why this
        form's own validation lights up under the box."""
        await self._open(user, settings)
        await user.should_not_see(marker="reset-GREETING")

        user.find(marker="setting-GREETING").elements.pop().set_value("something else")
        await user.should_see(marker="reset-GREETING")

    async def test_reset_puts_a_changed_text_field_back(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Set in the file, shown in the box, and put back by the button -- without
        touching the file, which a Cancel would leave exactly as it was."""
        (configurable.path / ".env").write_text("GREETING=from-the-file\n")
        await self._open(user, settings)

        greeting = user.find(marker="setting-GREETING").elements.pop()
        assert greeting.value == "from-the-file"

        user.find(marker="reset-GREETING").click()
        assert greeting.value == "hello"

        # Having undone the change, the button has nothing left to do and retires.
        await user.should_not_see(marker="reset-GREETING")

    async def test_reset_works_for_a_number_widget(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """The default takes the same trip through the widget the .env's text does --
        `"8080"` the string becomes `8080` the number -- so the reset restores, and
        compares to decide its own visibility, like with like."""
        (configurable.path / ".env").write_text("PORT=9000\n")
        await self._open(user, settings)

        port = user.find(marker="setting-PORT").elements.pop()
        assert port.value == 9000

        user.find(marker="reset-PORT").click()
        assert port.value == 8080

    async def test_reset_is_not_written_to_the_file_on_its_own(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Reset is a change to the widget, not to disk. Dismissing the form after a
        reset must leave the file it never saved exactly as it was found -- the same
        bargain typing into the box and cancelling makes."""
        (configurable.path / ".env").write_text("GREETING=from-the-file\n")
        await self._open(user, settings)

        user.find(marker="reset-GREETING").click()
        user.find("Cancel").click()

        assert (configurable.path / ".env").read_text() == "GREETING=from-the-file\n"


class TestAnAppCanMarkItsOwnSettingsAdvanced:
    """`advanced: true` on a field in `x-spiri-settings`.

    `configurable` declares five ordinary fields and one advanced one, `PROFILING`.
    The distinction is the app author's: which of *their* knobs is a developer's
    business. It is about clutter, so it hides a widget and takes nothing away.
    """

    async def _open(self, user: User, settings: DockerSettings) -> None:
        _a_browser_that_answers(user)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("configurable")
        user.find("Settings").click()
        await user.should_see("configurable — settings")

    async def test_an_advanced_field_waits_for_the_switch(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        await self._open(user, settings)

        # The ordinary fields are there for everybody, which is the point of hiding
        # this one: a form of three questions instead of six.
        await user.should_see("Greeting")
        await user.should_not_see("Profiling endpoint")

        user.find("Advanced").click()
        await user.should_see("Profiling endpoint")

    async def test_the_whole_field_is_marked_and_not_just_its_widget(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """A field is a widget *and* the prose under it, so the column carries the
        mark. Marking the widget alone would leave the help text behind: an
        explanation, on the page, of a box that is not there.

        The purple ring goes on with the binding, as it does for every other
        advanced-only element -- one act, in `advanced.mark`, so a thing cannot be
        advanced-only and fail to say so.

        Asserted with the switch *on*, because `user.find` gathers only what is
        visible: with it off there is nothing to find, which is the other half of the
        claim and is what the test above checks."""
        await self._open(user, settings)
        user.find("Advanced").click()
        await user.should_see("Profiling endpoint")

        field = user.find(marker="setting-PROFILING").elements.pop().parent_slot.parent
        assert theme.ADVANCED_CLASS in field.classes

        # And an ordinary field is not wearing a developer's mark.
        plain = user.find(marker="setting-GREETING").elements.pop().parent_slot.parent
        assert theme.ADVANCED_CLASS not in plain.classes

    @docker_required
    async def test_a_hidden_field_is_still_saved(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """The one that would hurt. Someone set `PROFILING` -- in the file, on the
        CLI, in advanced mode last week -- and now a normal user opens the form with
        the switch off and saves an unrelated change. The field they cannot see must
        come back out of the form exactly as it went in.

        Which is why an advanced field is built and then hidden, rather than not
        built: a widget that never existed reads back as nothing, and `.env` is
        patched from what the form says.
        """
        (configurable.path / ".env").write_text("PROFILING=true\n")

        await self._open(user, settings)
        await user.should_not_see("Profiling endpoint")

        user.find(marker="setting-GREETING").clear().type("good morning")
        user.find("Save").click()
        await user.should_see("Saved")

        written = env.read(configurable.path / ".env")
        assert written["GREETING"] == "good morning"
        assert written["PROFILING"] == "true"


class TestEditingTheEnvFileFromTheSettingsWindow:
    """Advanced mode turns the .env preview into an editor.

    The form is the app author's idea of which knobs exist, which is a good default
    and a poor cage: a developer who wants a variable the author never declared, or
    a comment, or a key the form does not know about, should not have to leave the
    page to get one. So the panel that showed them the file now lets them type in
    it, and what they type is what is written.
    """

    async def _open(self, user: User, settings: DockerSettings) -> Element:
        """Open the settings form, turn on advanced mode, and expand the file."""
        _a_browser_that_answers(user)
        web.build([_DockerPage(settings)])
        await user.open("/docker")
        await user.should_see("configurable")

        user.find("Advanced").click()
        user.find("Settings").click()
        await user.should_see("configurable — settings", retries=20)

        # Expanded by setting the value rather than by clicking: an expansion panel
        # opens itself in the browser, and the click that does it never reaches the
        # server. Assigning the value is the same event from the page's point of
        # view, and it is what fills the editor -- the file is rendered when the
        # panel opens, not on every keystroke of the form above it.
        user.find(ui.expansion).elements.pop().value = True
        return user.find(ui.codemirror).elements.pop()

    async def test_the_editor_is_filled_with_the_bytes_that_would_be_written(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Not the file on disk -- the file *as it would be saved*, form answers
        included. A preview is only worth anything before the write."""
        editor = await self._open(user, settings)

        assert "GREETING=hello" in editor.value
        assert "PORT=8080" in editor.value

    async def test_it_shows_what_the_user_already_had_in_the_file(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        (configurable.path / ".env").write_text("# mine\nMINE=keep\nGREETING=old\n")

        editor = await self._open(user, settings)

        assert "# mine" in editor.value
        assert "MINE=keep" in editor.value

    async def test_reopening_the_panel_does_not_tidy_away_a_hand_edit(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Someone who typed in here and then collapsed the panel by accident would
        not thank us for rendering their work away when they opened it again."""
        editor = await self._open(user, settings)
        editor.set_value("GREETING=mine\n")

        expansion = user.find(ui.expansion).elements.pop()
        expansion.value = False
        expansion.value = True

        assert editor.value == "GREETING=mine\n"

    @docker_required
    async def test_what_the_user_types_is_what_is_written(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """The whole feature: bytes in the editor, bytes on disk. Including a
        variable the app never declared, which is the reason to have an editor."""
        editor = await self._open(user, settings)
        editor.set_value("# typed by hand\nGREETING=typed\nUNDECLARED=mine\n")

        user.find("Save").click()
        await user.should_see("Saved", retries=10)

        written = (configurable.path / ".env").read_text()
        assert written == "# typed by hand\nGREETING=typed\nUNDECLARED=mine\n"

    @docker_required
    async def test_a_hand_edit_wins_over_the_form_above_it(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Two doors into one file, and they can disagree. The bytes the user typed
        are the ones they last looked at, so they are the ones that get written --
        and the alternative, patching the form's answers over them, would silently
        undo an edit they had made on purpose."""
        editor = await self._open(user, settings)
        editor.set_value("GREETING=from-the-editor\n")

        user.find("Save").click()
        await user.should_see("Saved", retries=10)

        assert env.read(configurable.path / ".env") == {"GREETING": "from-the-editor"}

    @docker_required
    async def test_an_untouched_editor_leaves_the_form_in_charge(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """Opening the panel to look at the file is not editing it. Someone who
        peers at the preview and then changes a box in the form must get what the
        form says, not a stale copy of the text they were shown."""
        editor = await self._open(user, settings)
        assert "GREETING=hello" in editor.value

        user.find(marker="setting-GREETING").clear().type("good morning")
        user.find("Save").click()
        await user.should_see("Saved", retries=10)

        assert env.read(configurable.path / ".env")["GREETING"] == "good morning"

    @docker_required
    async def test_a_rejected_edit_says_so_and_changes_nothing(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """An editor that could leave an unstartable app behind would be a worse
        tool than the vim it stands in for. `PORT` lands in a `ports:` mapping, so
        `abc` is a file compose will refuse."""
        (configurable.path / ".env").write_text("PORT=9000\n")

        editor = await self._open(user, settings)
        editor.set_value("PORT=abc\n")

        user.find("Save").click()
        await user.should_see("rejected", retries=10)

        assert (configurable.path / ".env").read_text() == "PORT=9000\n"

    @docker_required
    async def test_saving_writes_the_env_file(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """The whole feature, end to end: open the form, change a value, save, and
        find it in a file that `docker compose` will read."""
        await self._open(user, settings)

        user.find(marker="setting-GREETING").clear().type("good morning")
        user.find("Save").click()
        await user.should_see("Saved")

        written = env.read(configurable.path / ".env")
        assert written["GREETING"] == "good morning"

    @docker_required
    async def test_saving_a_password_with_a_dollar_in_it_does_not_mangle_it(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """The bug the encoder exists to prevent, driven from the actual form: a
        password written bare has its `$secret` expanded away by compose, and the
        user is left with a password of `p` and no idea why they cannot log in."""
        await self._open(user, settings)

        user.find(marker="setting-SECRET").clear().type("p$ecret w0rd#!")
        user.find("Save").click()
        await user.should_see("Saved")

        assert env.read(configurable.path / ".env")["SECRET"] == "p$ecret w0rd#!"

    @docker_required
    async def test_a_rejected_save_says_so_and_changes_nothing(
        self, user: User, settings: DockerSettings, configurable: Stack
    ) -> None:
        """`PORT` lands in a `ports:` mapping, so a value that is not a port makes
        compose refuse the project. The user must be told, and their previous,
        working .env must still be there."""
        (configurable.path / ".env").write_text("PORT=9000\n")

        await self._open(user, settings)

        # Through the number widget's own text box, which is how a person would do
        # it -- ui.number does not stop you typing letters, it just yields None.
        user.find(marker="setting-PORT").clear().type("-1")
        user.find("Save").click()
        # Generously: the refusal comes back from a real `docker compose config`.
        await user.should_see("rejected", retries=10)

        assert (configurable.path / ".env").read_text() == "PORT=9000\n"
