"""Tests for the default on-disk locations.

The point of :mod:`spiriconfig.paths` is that a bare ``spiriconfig serve`` and an
installed service agree on where apps live, so the settings defaults are checked
here alongside the paths themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig import paths
from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_docker.config import DockerSettings


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths.os, "geteuid", lambda: 0)


@pytest.fixture
def as_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A non-root user with a throwaway HOME, so the paths are not the runner's."""
    monkeypatch.setattr(paths.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return tmp_path


@pytest.fixture
def no_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars and no ``.env`` -- a checkout's own ``.env`` points at test_data."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "SPIRICONFIG_DOCKER_COMPOSE_DIR",
        "SPIRICONFIG_APPSTORE_STORE_DIR",
        "SPIRICONFIG_APPSTORE_STORES",
    ):
        monkeypatch.delenv(name, raising=False)


class TestRoot:
    def test_paths(self, as_root) -> None:
        assert paths.system()
        assert paths.compose_dir() == Path("/srv/compose")
        assert paths.store_dir() == Path("/var/lib/spiriconfig/stores")


class TestUser:
    def test_paths(self, as_user: Path) -> None:
        assert not paths.system()
        assert paths.compose_dir() == as_user / "spiri-apps"
        assert paths.store_dir() == as_user / ".local/share/spiriconfig/stores"

    def test_store_dir_honours_xdg(
        self, as_user: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(as_user / "data"))
        assert paths.store_dir() == as_user / "data/spiriconfig/stores"


class TestSettingsDefaults:
    """What ``uvx spiriconfig serve`` uses with nothing configured."""

    def test_they_are_the_install_paths(self, as_user: Path, no_overrides) -> None:
        assert DockerSettings().compose_dir == as_user / "spiri-apps"
        assert AppStoreSettings().store_dir == as_user / ".local/share/spiriconfig/stores"

    def test_no_store_is_seeded(self, as_user: Path, no_overrides) -> None:
        """No path into a checkout that a real machine does not have."""
        assert AppStoreSettings().stores == []

    def test_the_environment_still_wins(
        self, as_user: Path, no_overrides, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_DOCKER_COMPOSE_DIR", "/elsewhere")
        assert DockerSettings().compose_dir == Path("/elsewhere")
