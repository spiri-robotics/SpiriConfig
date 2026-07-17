"""Tests for installing and updating SpiriConfig itself.

All command-building and file rendering, no ``systemctl`` and no ``uv`` -- the
same reason the docker suite never starts a container. The system-scope paths are
exercised by constructing ``Scope(system=True)`` directly, because the machine
running the suite is (rightly) not root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig import service
from spiriconfig.service import Scope, ServiceConfig, ServiceError


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME, so user-scope paths are deterministic and not the runner's."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    return tmp_path


@pytest.fixture
def config() -> ServiceConfig:
    return ServiceConfig(compose_dir=Path("/srv/compose"), storage_secret="s3cr3t")


class TestScope:
    def test_detect_is_system_only_as_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service.os, "geteuid", lambda: 0)
        assert Scope.detect().system is True
        monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
        assert Scope.detect().system is False

    def test_system_paths(self) -> None:
        scope = Scope(system=True)
        assert scope.unit_path == Path("/etc/systemd/system/spiriconfig.service")
        assert scope.env_path == Path("/etc/spiriconfig/config.env")
        assert scope.systemctl == ["systemctl"]
        assert scope.wanted_by == "multi-user.target"

    def test_user_paths(self, home: Path) -> None:
        scope = Scope(system=False)
        assert scope.unit_path == home / ".config/systemd/user/spiriconfig.service"
        assert scope.env_path == home / ".config/spiriconfig/config.env"
        assert scope.systemctl == ["systemctl", "--user"]
        assert scope.wanted_by == "default.target"

    def test_user_paths_honour_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        scope = Scope(system=False)
        assert scope.env_path == tmp_path / "cfg/spiriconfig/config.env"


class TestExecutablePath:
    def test_defaults_to_local_bin(self, home: Path) -> None:
        assert service.executable_path() == home / ".local/bin/spiriconfig"

    def test_honours_xdg_bin_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "bin"))
        assert service.executable_path() == tmp_path / "bin/spiriconfig"


class TestServiceConfigEnv:
    def test_pam_includes_the_auth_variables(self, config: ServiceConfig) -> None:
        env = config.env()
        assert env["SPIRICONFIG_AUTH"] == "pam"
        assert env["SPIRICONFIG_AUTH_SERVICE"] == "login"
        assert env["SPIRICONFIG_AUTH_GROUP"] == "sudo"

    def test_none_drops_the_auth_variables(self) -> None:
        env = ServiceConfig(
            compose_dir=Path("/srv/compose"), storage_secret="x", auth="none"
        ).env()
        assert env["SPIRICONFIG_AUTH"] == "none"
        assert "SPIRICONFIG_AUTH_SERVICE" not in env
        assert "SPIRICONFIG_AUTH_GROUP" not in env

    def test_carries_the_compose_dir_and_bind(self, config: ServiceConfig) -> None:
        env = config.env()
        assert env["SPIRICONFIG_DOCKER_COMPOSE_DIR"] == "/srv/compose"
        assert env["SPIRICONFIG_HOST"] == "127.0.0.1"
        assert env["SPIRICONFIG_PORT"] == "8337"

    def test_render_is_sorted_and_secret_free_of_quoting(
        self, config: ServiceConfig
    ) -> None:
        text = service.render_env_file(config)
        body = [ln for ln in text.splitlines() if not ln.startswith("#")]
        assert body == sorted(body)
        # A value is written literally, not shell-quoted -- systemd reads the file.
        assert "SPIRICONFIG_STORAGE_SECRET=s3cr3t" in text


class TestExposureGuard:
    def test_refuses_none_off_box(self) -> None:
        config = ServiceConfig(
            compose_dir=Path("/srv/compose"),
            storage_secret="x",
            auth="none",
            host="0.0.0.0",
        )
        with pytest.raises(ServiceError):
            service.check_exposure(config)

    def test_allows_none_on_loopback(self) -> None:
        config = ServiceConfig(
            compose_dir=Path("/srv/compose"), storage_secret="x", auth="none"
        )
        service.check_exposure(config)  # does not raise

    def test_allows_auth_off_box(self) -> None:
        off_box = ServiceConfig(
            compose_dir=Path("/srv/compose"),
            storage_secret="x",
            auth="pam",
            host="0.0.0.0",
        )
        service.check_exposure(off_box)  # pam is a login, so this is fine


class TestUnitFile:
    def test_system_unit(self) -> None:
        unit = service.render_unit_file(Scope(system=True), Path("/root/.local/bin/spiriconfig"))
        assert "After=network-online.target" in unit
        assert "WantedBy=multi-user.target" in unit
        assert "EnvironmentFile=/etc/spiriconfig/config.env" in unit
        assert "ExecStart=/root/.local/bin/spiriconfig serve" in unit

    def test_user_unit_has_no_network_ordering(self, home: Path) -> None:
        unit = service.render_unit_file(
            Scope(system=False), home / ".local/bin/spiriconfig"
        )
        assert "network-online" not in unit
        assert "WantedBy=default.target" in unit


class TestCommands:
    def test_install_defaults_to_pypi(self) -> None:
        assert str(service.install_tool_command()) == "uv tool install spiriconfig"

    def test_install_editable_and_git(self) -> None:
        assert str(service.install_tool_command(".", editable=True)) == (
            "uv tool install --editable ."
        )
        assert str(
            service.install_tool_command("git+https://example.com/x@main")
        ) == "uv tool install git+https://example.com/x@main"

    def test_upgrade_reinstall(self) -> None:
        assert str(service.upgrade_tool_command()) == "uv tool upgrade spiriconfig"
        assert str(service.upgrade_tool_command(reinstall=True)) == (
            "uv tool upgrade --reinstall spiriconfig"
        )

    def test_enable_and_reload_carry_user_flag(self) -> None:
        user = Scope(system=False)
        assert str(service.enable_command(user)) == (
            "systemctl --user enable --now spiriconfig"
        )
        assert str(service.daemon_reload_command(Scope(system=True))) == (
            "systemctl daemon-reload"
        )

    def test_restart_is_detached_by_default(self) -> None:
        assert str(service.restart_command(Scope(system=True))) == (
            "systemd-run --on-active=2 systemctl restart spiriconfig"
        )
        assert str(service.restart_command(Scope(system=False))) == (
            "systemd-run --user --on-active=2 systemctl --user restart spiriconfig"
        )
        assert str(service.restart_command(Scope(system=True), detached=False)) == (
            "systemctl restart spiriconfig"
        )

    def test_linger(self) -> None:
        assert str(service.linger_command("alice")) == "loginctl enable-linger alice"


class TestParseToolVersion:
    def test_finds_the_version(self) -> None:
        text = "beanhub-cli v3.0.0b2\nspiriconfig v0.1.0\npyan3 v1.2.1\n"
        assert service.parse_tool_version(text) == "0.1.0"

    def test_absent_is_none(self) -> None:
        assert service.parse_tool_version("litellm v1.89.2\n") is None
