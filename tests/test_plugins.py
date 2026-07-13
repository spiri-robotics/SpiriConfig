"""Tests for plugin discovery.

The rule these tests defend: one broken plugin must never take down the
application. A user with a half-written plugin should still get a working UI,
and a loud reason why theirs is missing from it.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest
import typer

from spiriconfig import plugins
from spiriconfig.plugins import ENTRY_POINT_GROUP, Plugin, discover


class Good(Plugin):
    name = "good"
    title = "Good Plugin"
    description = "Works fine."

    def cli(self) -> typer.Typer:
        return typer.Typer()

    def page(self) -> None:
        pass


class CliOnly(Plugin):
    name = "cli-only"
    title = "CLI Only"

    def cli(self) -> typer.Typer:
        return typer.Typer()


class Exploding(Plugin):
    name = "exploding"
    title = "Exploding"

    def __init__(self) -> None:
        raise RuntimeError("boom")


class NotAPlugin:
    """Registered under the entry point group, but not actually a Plugin."""

    name = "impostor"


@pytest.fixture
def fake_entry_points(monkeypatch: pytest.MonkeyPatch):
    """Let a test declare the entry points that discovery will see."""

    def install(**targets: object) -> None:
        loaded = dict(targets)

        class FakeEntryPoint(EntryPoint):
            def load(self):  # type: ignore[override]
                target = loaded[self.name]
                if isinstance(target, Exception):
                    raise target
                return target

        eps = [
            FakeEntryPoint(name=name, value=f"fake:{name}", group=ENTRY_POINT_GROUP)
            for name in loaded
        ]
        monkeypatch.setattr(
            plugins,
            "entry_points",
            lambda group: eps if group == ENTRY_POINT_GROUP else [],
        )

    return install


class TestDiscovery:
    def test_loads_a_plugin(self, fake_entry_points) -> None:
        fake_entry_points(good=Good)
        found = discover()
        assert [p.name for p in found] == ["good"]
        assert isinstance(found[0], Good)

    def test_sorts_by_name(self, fake_entry_points) -> None:
        fake_entry_points(good=Good, cli_only=CliOnly)
        assert [p.name for p in discover()] == ["cli-only", "good"]

    def test_no_plugins_is_not_an_error(self, fake_entry_points) -> None:
        fake_entry_points()
        assert discover() == []


class TestBrokenPluginsAreSkipped:
    """None of these may raise: a bad plugin is skipped, not fatal."""

    def test_a_plugin_that_fails_to_import(self, fake_entry_points) -> None:
        fake_entry_points(good=Good, broken=ImportError("no such module"))
        assert [p.name for p in discover()] == ["good"]

    def test_a_plugin_that_raises_when_constructed(self, fake_entry_points) -> None:
        fake_entry_points(good=Good, exploding=Exploding)
        assert [p.name for p in discover()] == ["good"]

    def test_something_that_is_not_a_plugin_at_all(self, fake_entry_points) -> None:
        fake_entry_points(good=Good, impostor=NotAPlugin)
        assert [p.name for p in discover()] == ["good"]


class TestHasPage:
    def test_true_when_the_plugin_overrides_page(self) -> None:
        assert Good().has_page is True

    def test_false_when_it_does_not(self) -> None:
        """A CLI-only plugin gets no nav entry and no route."""
        assert CliOnly().has_page is False

    def test_the_base_page_raises_rather_than_silently_doing_nothing(self) -> None:
        with pytest.raises(NotImplementedError):
            CliOnly().page()


class TestTheRealDockerPlugin:
    """The bundled plugin must load through the same public machinery."""

    def test_is_discovered_from_its_entry_point(self) -> None:
        found = {p.name for p in discover()}
        assert "docker" in found, "the docker plugin is not installed"

    def test_offers_both_a_cli_and_a_page(self) -> None:
        docker = next(p for p in discover() if p.name == "docker")
        assert isinstance(docker.cli(), typer.Typer)
        assert docker.has_page is True
