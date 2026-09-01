"""Tests for the CLI's profile management, Review & Sign, and
git-remote-management commands: ``create``, ``list``, ``status``,
``commit``, ``origin``, ``pull``, ``export``.

Patch and baseline are two independent profile namespaces -- every command
below takes REMOTE before PROFILE, the same order as `create`. See
`spiriconfig_provision.repo`'s module docstring.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spiriconfig_provision.cli import app
from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.repo import create_profile

runner = CliRunner()
_GIT_ENV = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_GIT_ENV, *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture
def settings(tmp_path: Path) -> ProvisionSettings:
    return ProvisionSettings(profiles_dir=tmp_path / "profiles")


class TestCreateCommand:
    def test_creates_a_patch_profile(self) -> None:
        result = runner.invoke(app, ["create", "patch", "shop-floor-fleet"])
        assert result.exit_code == 0
        assert "patch" in result.output
        assert "shop-floor-fleet" in result.output

    def test_creates_a_baseline_profile(self) -> None:
        result = runner.invoke(app, ["create", "baseline", "compliance-only"])
        assert result.exit_code == 0
        assert "baseline" in result.output

    def test_an_unknown_remote_is_rejected(self) -> None:
        result = runner.invoke(app, ["create", "nonsense", "shop-floor-fleet"])
        assert result.exit_code != 0

    def test_a_duplicate_name_in_the_same_namespace_fails(self) -> None:
        runner.invoke(app, ["create", "patch", "shop-floor-fleet"])
        result = runner.invoke(app, ["create", "patch", "shop-floor-fleet"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_the_same_name_in_both_namespaces_is_fine(self) -> None:
        first = runner.invoke(app, ["create", "patch", "shop-floor-fleet"])
        second = runner.invoke(app, ["create", "baseline", "shop-floor-fleet"])
        assert first.exit_code == 0
        assert second.exit_code == 0


class TestListCommand:
    def test_no_profiles_says_so(self) -> None:
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "(none)" in result.output

    def test_lists_created_profiles_under_their_own_namespace(
        self, settings: ProvisionSettings
    ) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        create_profile("dev-kits", "patch", settings)
        create_profile("compliance-only", "baseline", settings)

        result = runner.invoke(app, ["list"])

        assert "shop-floor-fleet" in result.output
        assert "dev-kits" in result.output
        assert "compliance-only" in result.output


class TestStatus:
    def test_an_unknown_profile_is_rejected(self, settings: ProvisionSettings) -> None:
        result = runner.invoke(app, ["status", "patch", "nonexistent"])
        assert result.exit_code == 1
        assert "No such patch profile" in result.output

    def test_reports_pending_changes(self, settings: ProvisionSettings) -> None:
        profile = create_profile("shop-floor-fleet", "patch", settings)
        entry = profile.repo.path / "users" / "add" / "alice"
        entry.mkdir(parents=True)
        entry.joinpath("password.hash").write_text("hash\n")

        result = runner.invoke(app, ["status", "patch", "shop-floor-fleet"])

        assert result.exit_code == 0
        assert "shop-floor-fleet" in result.output


class TestCommit:
    def test_nothing_staged_says_so(self, settings: ProvisionSettings) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        result = runner.invoke(app, ["commit", "patch", "shop-floor-fleet"])
        assert result.exit_code == 0
        assert "Nothing staged" in result.output

    def test_commits_pending_changes_with_a_suggested_message(
        self, settings: ProvisionSettings
    ) -> None:
        profile = create_profile("shop-floor-fleet", "patch", settings)
        _git("config", "user.email", "test@example.com", cwd=profile.repo.path)
        _git("config", "user.name", "Test", cwd=profile.repo.path)
        entry = profile.repo.path / "users" / "add" / "alice"
        entry.mkdir(parents=True)
        entry.joinpath("password.hash").write_text("hash\n")

        result = runner.invoke(app, ["commit", "patch", "shop-floor-fleet"])

        assert result.exit_code == 0
        assert "Committed" in result.output
        assert "Not signed" in result.output

    def test_an_unknown_remote_is_rejected(self, settings: ProvisionSettings) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        result = runner.invoke(app, ["commit", "nonsense", "shop-floor-fleet"])
        assert result.exit_code != 0

    def test_an_unknown_profile_is_rejected(self) -> None:
        result = runner.invoke(app, ["commit", "patch", "nonexistent"])
        assert result.exit_code == 1
        assert "No such patch profile" in result.output

    def test_a_profile_from_the_other_namespace_is_rejected(
        self, settings: ProvisionSettings
    ) -> None:
        create_profile("dev-kits", "patch", settings)
        result = runner.invoke(app, ["commit", "baseline", "dev-kits"])
        assert result.exit_code == 1
        assert "No such baseline profile" in result.output


class TestOrigin:
    def test_get_with_none_configured(self, settings: ProvisionSettings) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        result = runner.invoke(app, ["origin", "patch", "shop-floor-fleet"])
        assert result.exit_code == 0
        assert "No origin configured" in result.output

    def test_set_then_get(self, settings: ProvisionSettings, tmp_path: Path) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        url = str(tmp_path / "somewhere")
        set_result = runner.invoke(app, ["origin", "patch", "shop-floor-fleet", url])
        assert set_result.exit_code == 0

        get_result = runner.invoke(app, ["origin", "patch", "shop-floor-fleet"])
        assert get_result.output.strip() == url


class TestPull:
    def test_refuses_with_no_origin(self, settings: ProvisionSettings) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        result = runner.invoke(app, ["pull", "patch", "shop-floor-fleet"])
        assert result.exit_code == 1
        assert "No origin" in result.output

    def test_pulls_from_a_configured_origin(
        self, settings: ProvisionSettings, tmp_path: Path
    ) -> None:
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git("init", "-q", cwd=upstream)
        (upstream / "a").write_text("x\n")
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "first", cwd=upstream)

        profile = create_profile("shop-floor-fleet", "patch", settings)
        runner.invoke(app, ["origin", "patch", "shop-floor-fleet", str(upstream)])
        result = runner.invoke(app, ["pull", "patch", "shop-floor-fleet"])

        assert result.exit_code == 0
        assert "Pulled" in result.output
        assert (profile.repo.path / "a").exists()


class TestExport:
    def test_exports_head_to_a_drive_path(
        self, settings: ProvisionSettings, tmp_path: Path
    ) -> None:
        profile = create_profile("shop-floor-fleet", "patch", settings)
        (profile.repo.path / "marker").write_text("x\n")
        _git("add", "-A", cwd=profile.repo.path)
        _git("commit", "-q", "-m", "first", cwd=profile.repo.path)

        destination = tmp_path / "drive"
        result = runner.invoke(
            app, ["export", "patch", "shop-floor-fleet", str(destination)]
        )

        assert result.exit_code == 0
        assert "Exported" in result.output
        assert (destination / "HEAD").is_file()
