"""Tests for the provisioning plugin's page.

Driven the way an operator drives it -- pick a profile, stage a change, see
it counted -- through nicegui's `user` harness, the same way
test_users_web.py drives the Users page. `getent` is stubbed so this is
about what the page draws and writes, not about the accounts on whatever
machine runs the suite.

Patch and baseline are two independent sections on this page, each with
its own profile picker -- see `spiriconfig_provision.web`'s module
docstring for why they're never one control.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from nicegui.testing import User

from spiriconfig import web
from spiriconfig_provision import ProvisionPlugin
from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.repo import create_profile
from spiriconfig_users import UsersPlugin, users
from spiriconfig_users.config import UsersSettings

from tests.test_users import SAMPLE_GROUP, SAMPLE_PASSWD

mkpasswd_required = pytest.mark.skipif(
    shutil.which("mkpasswd") is None, reason="needs mkpasswd (the whois package)"
)


def _stub_getent(monkeypatch) -> None:
    def fake(settings: UsersSettings, database: str, *keys: str) -> str:
        return SAMPLE_PASSWD if database == "passwd" else SAMPLE_GROUP

    monkeypatch.setattr(users, "_getent", fake)


@pytest.fixture(autouse=True)
def _profiles_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
    # Review & Sign actually runs `git commit` -- isolated from whatever
    # this machine's own ~/.gitconfig happens to have (a real signing key,
    # a different author) so these tests see only what they set themselves.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture
def settings(tmp_path: Path) -> ProvisionSettings:
    return ProvisionSettings(profiles_dir=tmp_path / "profiles")


def _profile(
    settings: ProvisionSettings, *, patch: bool = True, baseline: bool = True
) -> None:
    """A ready-to-use patch and/or baseline profile, both named
    ``shop-floor-fleet`` in their own namespace -- the pairing most tests
    need to already exist before opening the page. Two independent
    `create_profile` calls, not one profile with two remotes -- see
    `spiriconfig_provision.repo`'s module docstring."""
    if patch:
        create_profile("shop-floor-fleet", "patch", settings)
    if baseline:
        create_profile("shop-floor-fleet", "baseline", settings)


class TestHiddenBehindAdvancedMode:
    """Not a permission boundary -- see test_advanced.py -- just kept out of
    an ordinary operator's sidebar until there's more here than one
    hardcoded repo and no Review & Sign."""

    async def test_the_sidebar_hides_it_by_default(self, user: User) -> None:
        web.build([ProvisionPlugin()])
        await user.open("/")
        await user.should_not_see("Provisioning")

    async def test_turning_advanced_mode_on_reveals_it(self, user: User) -> None:
        web.build([ProvisionPlugin()])
        await user.open("/")
        user.find("Advanced").click()
        await user.should_see("Provisioning")

    async def test_the_page_itself_still_works_with_advanced_mode_off(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        """`advanced` only hides the nav entry -- the route, like every
        other plugin's, is never gated."""
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")
        await user.should_see("Provisioning")


class TestNoProfiles:
    async def test_says_so_in_both_sections(self, user: User) -> None:
        web.build([ProvisionPlugin()])
        await user.open("/provision")
        await user.should_see("Patch profiles")
        await user.should_see("Baseline profiles")
        await user.should_see("No profiles yet.")

    async def test_creating_a_patch_profile_makes_it_the_selected_one(
        self, user: User, monkeypatch
    ) -> None:
        _stub_getent(monkeypatch)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-patch-new-profile").click()
        await user.should_see("New patch profile")
        user.find(marker="provision-patch-new-profile-name").elements.pop().set_value(
            "shop-floor-fleet"
        )
        user.find(marker="provision-patch-new-profile-create").click()

        await user.should_see("shop-floor-fleet")
        await user.should_see("Nothing staged.")


class TestProfilePicker:
    async def test_each_section_lists_only_its_own_namespace(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        create_profile("shop-floor-fleet", "patch", settings)
        create_profile("compliance-only", "baseline", settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        await user.should_see("shop-floor-fleet")
        await user.should_see("compliance-only")

    async def test_switching_the_patch_profile_leaves_baseline_alone(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        create_profile("dev-kits", "patch", settings)
        create_profile("shop-floor-fleet", "patch", settings)
        create_profile("compliance-only", "baseline", settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")
        # Profiles list sorted by name -- "dev-kits" sorts before
        # "shop-floor-fleet" and is the one selected by default.
        await user.should_see("compliance-only")

        user.find(marker="provision-patch-profile-select").elements.pop().set_value(
            "shop-floor-fleet"
        )
        await user.should_see("compliance-only")  # baseline's own pick is untouched


class TestPatchStaging:
    async def test_renders_the_users_contributor_block(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")
        await user.should_see("Provisioning")
        await user.should_see("Users")  # the contributor block, attributed by title
        await user.should_see("Nothing staged.")

    @mkpasswd_required
    async def test_staging_add_shows_up_in_the_pending_count(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-name").elements.pop().set_value("newhire")
        user.find(marker="provision-users-password").elements.pop().set_value("hunter2")
        user.find(marker="provision-users-add").click()

        await user.should_see("newhire — users/add")
        user.find(marker="provision-patch-refresh").click()
        await user.should_see("1 uncommitted change")

    @mkpasswd_required
    async def test_staging_add_with_groups_and_ssh_keys_writes_both_files(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-name").elements.pop().set_value("newhire")
        user.find(marker="provision-users-password").elements.pop().set_value("hunter2")
        user.find(marker="provision-users-groups").elements.pop().set_value(
            "docker, sudo"
        )
        user.find(marker="provision-users-ssh-keys").elements.pop().set_value(
            "ssh-ed25519 AAAA newhire@laptop"
        )
        user.find(marker="provision-users-add").click()

        await user.should_see("newhire — users/add")

        entry = (
            settings.profiles_dir / "patch" / "shop-floor-fleet"
            / "users" / "add" / "newhire"
        )
        assert entry.joinpath("groups").read_text() == "docker\nsudo\n"
        assert (
            entry.joinpath("authorized_keys").read_text()
            == "ssh-ed25519 AAAA newhire@laptop\n"
        )

    async def test_staging_remove_needs_no_password_and_shows_up_pending(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-name").elements.pop().set_value("alice")
        user.find(marker="provision-users-remove").click()

        await user.should_see("alice — users/remove")
        user.find(marker="provision-patch-refresh").click()
        await user.should_see("1 uncommitted change")


class TestBaselineEditor:
    async def test_renders_the_baseline_block_for_its_own_profile(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings, patch=False)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")
        await user.should_see("Baseline profiles")
        await user.should_see("Nothing declared yet.")

    @mkpasswd_required
    async def test_adding_to_the_baseline_set_hashes_and_shows_up_declared(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings, patch=False)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-baseline-name").elements.pop().set_value(
            "operator"
        )
        user.find(marker="provision-users-baseline-password").elements.pop().set_value(
            "hunter2"
        )
        user.find(marker="provision-users-baseline-add").click()

        # Not "operator" alone -- that also matches the input field's still-typed
        # value before the async hash-and-write completes. This string only
        # exists once refresh_declared() has actually re-rendered the list.
        await user.should_see("operator — users/state")
        user.find(marker="provision-baseline-refresh").click()
        await user.should_see("1 uncommitted change")

    @mkpasswd_required
    async def test_declaring_groups_and_ssh_keys_writes_both_files(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings, patch=False)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-baseline-name").elements.pop().set_value(
            "operator"
        )
        user.find(marker="provision-users-baseline-password").elements.pop().set_value(
            "hunter2"
        )
        user.find(marker="provision-users-baseline-groups").elements.pop().set_value(
            "docker sudo"
        )
        user.find(
            marker="provision-users-baseline-ssh-keys"
        ).elements.pop().set_value("ssh-ed25519 AAAA operator@laptop")
        user.find(marker="provision-users-baseline-add").click()

        await user.should_see("operator — users/state")

        entry = (
            settings.profiles_dir / "baseline" / "shop-floor-fleet"
            / "users" / "state" / "operator"
        )
        assert entry.joinpath("groups").read_text() == "docker\nsudo\n"
        assert (
            entry.joinpath("authorized_keys").read_text()
            == "ssh-ed25519 AAAA operator@laptop\n"
        )


class TestOriginAndPull:
    async def test_no_origin_configured_says_so(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")
        await user.should_see("No origin configured.")

    async def test_setting_origin_updates_the_status_line(
        self, user: User, monkeypatch, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-patch-set-origin").click()
        await user.should_see("What Pull fetches from")  # the dialog's own text

        url = str(tmp_path / "upstream")
        user.find(marker="provision-patch-origin-input").elements.pop().set_value(url)
        user.find(marker="provision-patch-origin-save").click()

        await user.should_see(f"origin: {url}")

    async def test_pull_fast_forwards_and_reports_up_to_date(
        self, user: User, monkeypatch, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
        (upstream / "a").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=upstream, check=True)

        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-patch-set-origin").click()
        await user.should_see("What Pull fetches from")
        user.find(marker="provision-patch-origin-input").elements.pop().set_value(
            str(upstream)
        )
        user.find(marker="provision-patch-origin-save").click()
        await user.should_see("never fetched")

        user.find(marker="provision-patch-pull").click()
        await user.should_see("up to date")


class TestReviewAndSign:
    async def test_commits_pending_changes_and_clears_the_pending_count(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-name").elements.pop().set_value("alice")
        user.find(marker="provision-users-remove").click()
        await user.should_see("alice — users/remove")

        user.find(marker="provision-patch-review").click()
        await user.should_see("user alice removed")  # the suggested commit message

        user.find(marker="provision-patch-commit").click()
        await user.should_see("Commit & Sign")  # the run dialog, not a plain notify
        await asyncio.sleep(1.0)
        user.find("Close").click()

        await user.should_see("Nothing staged.")


class TestExport:
    async def test_exports_head_to_a_drive_path(
        self, user: User, monkeypatch, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-name").elements.pop().set_value("alice")
        user.find(marker="provision-users-remove").click()
        await user.should_see("alice — users/remove")
        user.find(marker="provision-patch-review").click()
        await user.should_see("user alice removed")
        user.find(marker="provision-patch-commit").click()
        await user.should_see("Commit & Sign")
        await asyncio.sleep(1.0)
        user.find("Close").click()
        await user.should_see("Nothing staged.")

        destination = tmp_path / "drive"
        user.find(marker="provision-patch-export-open").click()
        await user.should_see("Send this remote's HEAD")
        user.find(marker="provision-patch-export-destination").elements.pop().set_value(
            str(destination)
        )
        user.find(marker="provision-patch-export").click()
        await user.should_see("Exported.")

        assert (destination / "HEAD").is_file()


class TestCommitShowsTheCommandLine:
    """Regression: Review & Sign used to run `git add`/`git commit` in a
    background thread and only ever show a plain `ui.notify()` afterward
    -- breaking ``docs/design.md``'s "We shell out, on purpose" rule that
    a command must be something the user can *see* run, not just be told
    happened. It now goes through the same show-then-stream dialog every
    other action-running button in SpiriConfig uses."""

    async def test_the_literal_git_commit_line_is_shown_in_advanced_mode(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")
        user.find("Advanced").click()

        user.find(marker="provision-users-name").elements.pop().set_value("alice")
        user.find(marker="provision-users-remove").click()
        await user.should_see("alice — users/remove")

        user.find(marker="provision-patch-review").click()
        user.find(marker="provision-patch-commit").click()

        await user.should_see("git add -A")
        await user.should_see("git commit -q -m")

    async def test_the_commit_actually_lands_once_the_dialog_finishes(
        self, user: User, monkeypatch, settings: ProvisionSettings
    ) -> None:
        _stub_getent(monkeypatch)
        _profile(settings)
        profile_path = settings.profiles_dir / "patch" / "shop-floor-fleet"
        web.build([ProvisionPlugin(), UsersPlugin()])
        await user.open("/provision")

        user.find(marker="provision-users-name").elements.pop().set_value("alice")
        user.find(marker="provision-users-remove").click()
        await user.should_see("alice — users/remove")
        user.find(marker="provision-patch-review").click()
        user.find(marker="provision-patch-commit").click()
        await user.should_see("Commit & Sign")
        await asyncio.sleep(1.0)
        user.find("Close").click()

        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=profile_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "user alice removed" in log.stdout


class TestWorkingTreePath:
    """Regression: nothing on the page said where a profile's working
    tree actually lives on disk -- the CLI's `status` command already
    prints it (`spiriconfig_provision.cli._report`), but the page didn't,
    leaving no way to go look at it yourself (`git log`, a file browser)
    without already knowing `ProvisionSettings.profiles_dir`."""

    async def test_the_path_is_shown_once_a_profile_is_selected(
        self, user: User, settings: ProvisionSettings
    ) -> None:
        _profile(settings, baseline=False)
        web.build([ProvisionPlugin()])
        await user.open("/provision")

        await user.should_see(str(settings.profiles_dir / "patch" / "shop-floor-fleet"))
