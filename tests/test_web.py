"""Tests for the web shell.

These render pages in-process with NiceGUI's own test harness. Registering a page
is the moment FastAPI inspects its signature, and a page it cannot build takes the
whole process down at startup -- so "does the app boot with a plugin installed"
is worth asserting, not just checking by hand once.
"""

from __future__ import annotations

import socket

import pytest
import typer
from nicegui import ui
from nicegui.testing import User

from spiriconfig import proxy, web
from spiriconfig.plugins import Plugin


class Pageful(Plugin):
    name = "pageful"
    title = "Has A Page"
    description = "A plugin with a web page."

    def page(self) -> None:
        ui.label("hello from the plugin")


class CliOnly(Plugin):
    name = "cli-only"
    title = "No Page Here"
    description = "A plugin with no web page."

    def cli(self) -> typer.Typer:
        return typer.Typer()


class Exploding(Plugin):
    name = "exploding"
    title = "Explodes On Render"
    description = "A plugin that raises when rendered."

    def page(self) -> None:
        raise RuntimeError("the plugin blew up")


class TestRoutes:
    async def test_the_app_boots_with_a_plugin_installed(self, user: User) -> None:
        """A page FastAPI cannot handle fails at *registration*, so merely
        building the app with a plugin installed is the thing worth asserting."""
        web.build([Pageful()])
        await user.open("/")
        await user.should_see("SpiriConfig")

    async def test_a_plugin_page_is_served_at_its_name(self, user: User) -> None:
        web.build([Pageful()])
        await user.open("/pageful")
        await user.should_see("hello from the plugin")

    async def test_the_index_lists_every_plugin(self, user: User) -> None:
        web.build([Pageful(), CliOnly()])
        await user.open("/")
        await user.should_see("Has A Page")
        await user.should_see("No Page Here")

    async def test_a_cli_only_plugin_gets_no_route(self, user: User) -> None:
        web.build([CliOnly()])
        response = await user.http_client.get("/cli-only")
        assert response.status_code == 404

    async def test_an_unknown_path_is_a_404(self, user: User) -> None:
        web.build([Pageful()])
        response = await user.http_client.get("/nope")
        assert response.status_code == 404

    async def test_no_plugins_still_serves_an_index(self, user: User) -> None:
        web.build([])
        await user.open("/")
        await user.should_see("No plugins are installed")


class TestTheSidebar:
    async def test_it_links_to_every_plugin_with_a_page(self, user: User) -> None:
        """The nav is the sidebar now, so it is there on a plugin's own page too."""
        web.build([Pageful(), CliOnly()])
        await user.open("/pageful")
        await user.should_see("Has A Page")

    async def test_the_advanced_toggle_lives_in_it(self, user: User) -> None:
        web.build([Pageful()])
        await user.open("/pageful")
        await user.should_see("Developer mode")

    async def test_it_can_be_collapsed_and_brought_back(self, user: User) -> None:
        """Hiding the sidebar must not be a one-way door: the button that hides it
        lives in the header, which stays put."""
        web.build([Pageful()])
        await user.open("/pageful")

        drawer = user.find(marker="sidebar").elements.pop()
        assert drawer.value is True

        user.find(marker="sidebar-toggle").click()
        assert drawer.value is False

        user.find(marker="sidebar-toggle").click()
        assert drawer.value is True


class TestTheHeader:
    async def test_it_names_the_machine(self, user: User) -> None:
        """Shells nest, so the header has to say whose shell this one is."""
        web.build([])
        await user.open("/")
        await user.should_see(socket.gethostname(), marker="hostname")


class TestPluginFailureIsContained:
    async def test_a_page_that_raises_shows_an_error_instead_of_dying(
        self, user: User
    ) -> None:
        """The user gets an error on the page, not a dead server."""
        web.build([Exploding()])
        await user.open("/exploding")
        await user.should_see("failed to render")
        await user.should_see("the plugin blew up")

    async def test_a_broken_plugin_does_not_break_the_others(self, user: User) -> None:
        web.build([Exploding(), Pageful()])
        await user.open("/pageful")
        await user.should_see("hello from the plugin")


class TestAProxiedPluginsFrame:
    """The shell page at ``/app/<name>`` frames ``/plugin/<name>``. When the shell is
    itself proxied (a robot's SpiriConfig inside the SDK's), that path only exists
    under the prefix the outer proxy announces."""

    @pytest.fixture(autouse=True)
    def _one_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(proxy, "_DISCOVERED", {})
        monkeypatch.setattr(proxy, "_MANUAL", {})
        proxy.register("fw", "http://fw:8080", title="Firmware")

    @staticmethod
    def _iframe_src(user: User) -> str:
        (frame,) = [e for e in user.client.elements.values() if e.tag == "iframe"]
        return frame.props["src"]

    async def test_unproxied_it_frames_the_plugin_at_our_root(self, user: User) -> None:
        web.build([])
        await user.open("/app/fw/deep/path")
        assert self._iframe_src(user) == "/plugin/fw/deep/path"

    async def test_nested_it_frames_the_plugin_under_the_forwarded_prefix(
        self, user: User
    ) -> None:
        web.build([])
        user.http_client.headers["X-Forwarded-Prefix"] = "/plugin/mu-1"
        await user.open("/app/fw/deep/path")
        assert self._iframe_src(user) == "/plugin/mu-1/plugin/fw/deep/path"

    async def test_a_hostile_prefix_cannot_break_out_of_the_attribute(
        self, user: User
    ) -> None:
        """The header is whatever a client sends when we are exposed directly."""
        web.build([])
        user.http_client.headers["X-Forwarded-Prefix"] = '/x" onload="alert(1)'
        await user.open("/app/fw")
        assert '"' not in self._iframe_src(user)


class TestTheRealPluginSet:
    async def test_the_app_boots_with_whatever_is_actually_installed(
        self, user: User
    ) -> None:
        """No arguments: discover the real entry points, and serve them."""
        web.build()
        await user.open("/docker")
        await user.should_see("Apps")
