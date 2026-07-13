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

import typer
from nicegui.testing import User

from spiriconfig import web
from spiriconfig.plugins import Plugin
from spiriconfig_docker.config import DockerSettings


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
