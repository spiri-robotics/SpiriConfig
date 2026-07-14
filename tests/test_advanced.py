"""Tests for advanced mode and the preference seam.

Two things are being defended here.

The first is that advanced mode is *only* a display filter. If a test ever has to
assert that advanced mode prevents an action, someone has started using it as a
permission system and it will not hold.

The second is that the preference store can be swapped -- because user support is
coming, and the whole point of the seam is that arriving users should not require
touching a single plugin.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import ui
from nicegui.testing import User

from spiriconfig import advanced, preferences, theme, web
from spiriconfig.plugins import Plugin


class Gated(Plugin):
    """A plugin with one plain feature and one advanced one."""

    name = "gated"
    title = "Gated"
    description = "Has an advanced feature."

    def page(self) -> None:
        ui.label("everyone sees this")
        ui.button("Ordinary")
        with advanced.only():
            ui.label("developers only")
            ui.button("Developers Only")


@pytest.fixture(autouse=True)
def restore_the_default_store():
    """A test that swaps the store must not leak it into the next one."""
    yield
    preferences.reset()


class FakeStore:
    """A preference store that is not the browser one.

    Stands in for the per-user store that does not exist yet. If advanced mode
    works through this, it will work through a real one.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})
        self.writes: list[tuple[str, Any]] = []

    def get(self, key: str, default: Any) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.writes.append((key, value))


class TestDefault:
    async def test_off_by_default(self, user: User) -> None:
        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("everyone sees this")
        await user.should_not_see("developers only")

    async def test_the_env_var_sets_the_default(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a developer image can ship advanced-on from the same code."""
        monkeypatch.setenv("SPIRICONFIG_ADVANCED", "true")
        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("developers only")

    async def test_a_stored_preference_beats_the_env_default(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The person's own choice wins over the deployment's default."""
        monkeypatch.setenv("SPIRICONFIG_ADVANCED", "true")
        preferences.use(lambda: FakeStore({"advanced": False}))

        web.build([Gated()])
        await user.open("/gated")
        await user.should_not_see("developers only")


class TestToggle:
    async def test_turning_it_on_reveals_advanced_features(self, user: User) -> None:
        web.build([Gated()])
        await user.open("/gated")
        await user.should_not_see("developers only")

        user.find("Advanced").click()
        await user.should_see("developers only")

    async def test_turning_it_off_hides_them_again(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_ADVANCED", "true")
        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("developers only")

        user.find("Advanced").click()
        await user.should_not_see("developers only")

    async def test_the_toggle_itself_is_never_hidden(self, user: User) -> None:
        """A switch you can only see once it is on would be a trap with no way back."""
        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("Advanced")

        user.find("Advanced").click()
        await user.should_see("Advanced")

    async def test_the_choice_is_written_to_the_preference_store(
        self, user: User
    ) -> None:
        store = FakeStore()
        preferences.use(lambda: store)

        web.build([Gated()])
        await user.open("/gated")
        user.find("Advanced").click()
        await user.should_see("developers only")

        assert store.writes == [("advanced", True)]


class TestTheStoreCanBeSwapped:
    """The seam that makes user support a one-line change."""

    async def test_advanced_mode_reads_from_whatever_store_is_registered(
        self, user: User
    ) -> None:
        """Register a store keyed on something other than a browser, and advanced
        mode follows it -- with no change to Gated, or to any other plugin."""
        preferences.use(lambda: FakeStore({"advanced": True}))

        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("developers only")

    async def test_a_per_person_store_gives_different_people_different_answers(
        self, user: User
    ) -> None:
        """The thing user support actually needs: the store resolves per request,
        so who is asking can change the answer."""
        people = {"dev": FakeStore({"advanced": True}), "user": FakeStore()}
        current = "dev"
        preferences.use(lambda: people[current])

        assert advanced.PREFERENCE_KEY == "advanced"

        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("developers only")

        # The same code, the same page, a different person.
        current = "user"
        await user.open("/gated")
        await user.should_not_see("developers only")

    async def test_a_broken_store_does_not_break_the_page(self, user: User) -> None:
        """A preference we cannot read is not a reason to fail to render."""

        class Broken:
            def get(self, key: str, default: Any) -> Any:
                raise RuntimeError("the database is on fire")

            def set(self, key: str, value: Any) -> None:
                raise RuntimeError("the database is still on fire")

        preferences.use(Broken)

        web.build([Gated()])
        await user.open("/gated")
        await user.should_see("everyone sees this")

        # And toggling still works, even though the choice cannot be saved.
        user.find("Advanced").click()
        await user.should_see("developers only")


class TestItLooksLikeAdvanced:
    """Purple means advanced. The mark and the switch must agree, or the switch
    stops being a legend for what it revealed."""

    async def test_advanced_elements_carry_the_ring(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_ADVANCED", "true")
        web.build([Gated()])
        await user.open("/gated")

        marked = user.find("developers only").elements.pop()
        assert theme.ADVANCED_CLASS in marked.classes

        # And a feature everyone sees is not wearing a developer's mark.
        plain = user.find("everyone sees this").elements.pop()
        assert theme.ADVANCED_CLASS not in plain.classes

    async def test_an_advanced_button_is_purple_rather_than_ringed(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quasar sets ``outline: 0`` on every ``.q-btn``, so the ring never paints
        on a button, however carefully we write the rule. A button says it is
        advanced with Quasar's own colour mechanism instead. This test is the only
        thing standing between that and someone "simplifying" it back into a ring
        nobody can see."""
        monkeypatch.setenv("SPIRICONFIG_ADVANCED", "true")
        web.build([Gated()])
        await user.open("/gated")

        advanced_button = user.find("Developers Only").elements.pop()
        assert advanced_button.props["color"] == theme.ADVANCED

        ordinary_button = user.find("Ordinary").elements.pop()
        assert ordinary_button.props["color"] != theme.ADVANCED

    async def test_the_toggle_wears_the_same_colour_it_marks_things_with(
        self, user: User
    ) -> None:
        """Quasar paints the switch in this colour only while it is *on*, which is
        the effect being asked for: purple once you have turned advanced mode on."""
        web.build([Gated()])
        await user.open("/gated")

        switch = user.find(ui.switch).elements.pop()
        assert switch.props["color"] == theme.ADVANCED


class TestItIsNotAPermissionSystem:
    async def test_the_cli_is_complete_regardless_of_advanced_mode(self) -> None:
        """The CLI never consults advanced mode. If this ever fails, someone has
        started treating a display filter as an authorisation boundary."""
        import spiriconfig_docker.cli as docker_cli

        source = __import__("inspect").getsource(docker_cli)
        assert "advanced" not in source
