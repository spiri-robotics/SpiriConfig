"""Tests for Review & Sign: staging everything pending and committing it,
signed with whatever git config already names."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import Change, StagedRepo
from spiriconfig_provision.repo import create_profile
from spiriconfig_provision.review import commit, suggested_message


def _patch_repo(settings: ProvisionSettings) -> StagedRepo:
    return create_profile("test-profile", "patch", settings).repo


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch) -> None:
    """Never let this machine's own ~/.gitconfig (a real signing key,
    say) decide what these tests see -- only what each test sets itself,
    repo-local, is allowed to matter."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture
def settings(tmp_path: Path) -> ProvisionSettings:
    return ProvisionSettings(profiles_dir=tmp_path / "profiles")


def _configure_identity(path: Path) -> None:
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


class TestSuggestedMessage:
    def test_subject_joins_summaries_with_commas(self) -> None:
        changes = [
            Change("add", "set password for alice", "..."),
            Change("remove", "remove bob", "..."),
        ]
        subject = suggested_message(changes).splitlines()[0]
        assert subject == "set password for alice, remove bob"

    def test_body_lists_each_change_s_detail(self) -> None:
        changes = [
            Change("add", "user alice added", "Adds **alice**"),
            Change("remove", "user bob removed", "Removes login account **bob**"),
        ]
        assert suggested_message(changes) == (
            "user alice added, user bob removed\n\n"
            "- Adds **alice**\n"
            "- Removes login account **bob**"
        )

    def test_empty_changes_is_an_empty_string(self) -> None:
        assert suggested_message([]) == ""


class TestCommit:
    def test_commits_whatever_is_pending(
        self, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        _configure_identity(repo.path)
        (repo.path / "users").mkdir()
        (repo.path / "users" / "add").mkdir()
        (repo.path / "users" / "add" / "alice").touch()

        result = commit(repo, "add alice", settings)

        log = subprocess.run(
            ["git", "log", "-1", "--format=%H %s"],
            cwd=repo.path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert log.stdout.strip() == f"{result.commit} add alice"

    def test_unsigned_when_no_signing_key_is_configured(
        self, settings: ProvisionSettings
    ) -> None:
        repo = _patch_repo(settings)
        _configure_identity(repo.path)
        (repo.path / "marker").touch()

        result = commit(repo, "stage marker", settings)

        assert result.signed is False
        assert result.signing_key is None

    def test_nothing_pending_raises(self, settings: ProvisionSettings) -> None:
        from spiriconfig.commands import CommandError

        repo = _patch_repo(settings)
        _configure_identity(repo.path)
        with pytest.raises(CommandError):
            commit(repo, "empty", settings)

    def test_signs_with_whatever_git_config_names(
        self, settings: ProvisionSettings, tmp_path: Path
    ) -> None:
        """Never invents or picks a key -- honours `user.signingkey`
        exactly the way `git commit -S` would by hand."""
        key_path = tmp_path / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)], check=True
        )

        repo = _patch_repo(settings)
        _configure_identity(repo.path)
        subprocess.run(
            ["git", "config", "gpg.format", "ssh"], cwd=repo.path, check=True
        )
        subprocess.run(
            ["git", "config", "user.signingkey", str(key_path)], cwd=repo.path, check=True
        )
        (repo.path / "marker").touch()

        result = commit(repo, "signed change", settings)

        assert result.signed is True
        assert result.signing_key == str(key_path)
        # `git commit -S` embeds the signature as a `gpgsig` header on the
        # commit object itself -- checking for that proves a signature was
        # actually attached, without needing an allowed_signers file set up
        # just to verify it.
        raw = subprocess.run(
            ["git", "cat-file", "commit", result.commit],
            cwd=repo.path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "gpgsig" in raw.stdout
