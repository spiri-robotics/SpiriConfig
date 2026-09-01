"""Tests for spiriconfig_provision.sync: origin, ahead/behind status,
pull, and exporting HEAD to a drive or a URL."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import StagedRepo
from spiriconfig_provision.repo import create_profile
from spiriconfig_provision.sync import export_to, origin_url, pull, set_origin, status


def _patch_repo(settings: ProvisionSettings) -> StagedRepo:
    return create_profile("test-profile", "patch", settings).repo


_GIT_ENV = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_GIT_ENV, *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _commit_a_file(path: Path, name: str, message: str) -> None:
    (path / name).write_text("content\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture
def settings(tmp_path: Path) -> ProvisionSettings:
    return ProvisionSettings(profiles_dir=tmp_path / "profiles")


class TestOrigin:
    def test_is_none_when_unset(self, settings: ProvisionSettings) -> None:
        repo = _patch_repo(settings)
        assert origin_url(repo, settings) is None

    def test_set_then_read_back(self, tmp_path: Path, settings: ProvisionSettings) -> None:
        repo = _patch_repo(settings)
        url = str(tmp_path / "somewhere")
        set_origin(repo, url, settings)
        assert origin_url(repo, settings) == url

    def test_set_twice_updates_rather_than_erroring(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        set_origin(repo, str(tmp_path / "first"), settings)
        set_origin(repo, str(tmp_path / "second"), settings)
        assert origin_url(repo, settings) == str(tmp_path / "second")


class TestStatus:
    def test_is_none_with_no_origin(self, settings: ProvisionSettings) -> None:
        repo = _patch_repo(settings)
        assert status(repo, settings) is None

    def test_is_none_before_anything_is_ever_fetched(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        set_origin(repo, str(tmp_path / "nonexistent"), settings)
        assert status(repo, settings) is None

    def test_reports_clean_right_after_a_pull(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git("init", "-q", cwd=upstream)
        _commit_a_file(upstream, "a", "first")

        repo = _patch_repo(settings)
        set_origin(repo, str(upstream), settings)
        pull(repo, settings)

        result = status(repo, settings)
        assert result is not None
        assert result.clean
        assert (result.ahead, result.behind) == (0, 0)

    def test_reports_behind_after_upstream_moves_on(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git("init", "-q", cwd=upstream)
        _commit_a_file(upstream, "a", "first")

        repo = _patch_repo(settings)
        set_origin(repo, str(upstream), settings)
        pull(repo, settings)

        _commit_a_file(upstream, "b", "second")
        _git("fetch", "-q", "origin", cwd=repo.path)

        result = status(repo, settings)
        assert result is not None
        assert (result.ahead, result.behind) == (0, 1)


class TestPull:
    def test_fast_forwards_from_origin(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git("init", "-q", cwd=upstream)
        _commit_a_file(upstream, "a", "first")

        repo = _patch_repo(settings)
        set_origin(repo, str(upstream), settings)
        pull(repo, settings)

        assert (repo.path / "a").exists()


class TestExportTo:
    def test_export_to_a_fresh_path_creates_a_bare_repo_and_pushes(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        _commit_a_file(repo.path, "marker", "first")
        destination = tmp_path / "drive"

        export_to(repo, str(destination), settings)

        assert (destination / "HEAD").is_file()
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        remote_rev = subprocess.run(
            ["git", "rev-parse", "refs/heads/master"],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert remote_rev == rev

    def test_export_twice_updates_the_same_destination(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        _commit_a_file(repo.path, "marker", "first")
        destination = tmp_path / "drive"
        export_to(repo, str(destination), settings)

        _commit_a_file(repo.path, "marker2", "second")
        export_to(repo, str(destination), settings)

        log = subprocess.run(
            ["git", "log", "--oneline", "refs/heads/master"],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        assert log.stdout.count("\n") == 2

    def test_export_to_a_nonempty_non_repo_path_refuses(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        _commit_a_file(repo.path, "marker", "first")
        destination = tmp_path / "occupied"
        destination.mkdir()
        (destination / "something").touch()

        with pytest.raises(ValueError, match="not empty"):
            export_to(repo, str(destination), settings)
