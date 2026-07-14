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
import re

import typer
from nicegui import ui
from nicegui.element import Element
from nicegui.testing import User

from spiriconfig import theme, web
from spiriconfig.plugins import Plugin
from spiriconfig_docker import env
from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.stacks import Stack

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


class TestTheSettingsForm:
    """The form generated from an app's `x-spiri-settings`.

    `configurable` is a stack that declares one; `hello` is the ordinary kind that
    declares nothing. Both are on the page, which is the interesting part -- an app
    with no settings must not grow a button that opens an empty form.
    """

    async def _open(self, user: User, settings: DockerSettings) -> None:
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
