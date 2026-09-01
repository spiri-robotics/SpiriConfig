"""Tests for spiriconfig_provision.repo: creating and listing an
operator's named profiles, discovering contributors, and reading what's
pending in a profile's remote.

Not testing the real plugin registry -- a fake `Plugin` per test stands in
for a contributor, so these tests exercise `repo.py`'s own logic (dedup,
git-init, `git status` parsing) rather than whatever plugins happen to be
installed alongside this suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig.plugins import Plugin
from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import Change, StagedRepo
from spiriconfig_provision.repo import (
    Profile,
    _changed_paths,
    contributors,
    create_profile,
    list_profiles,
    open_profile,
    pending_changes,
)


class _FakeContributor(Plugin):
    """A minimal Plugin that owns `provision_resource`, for repo.py tests."""

    def __init__(self, name: str, provision_resource: str, *, describe=None) -> None:
        self.name = name
        self.title = name.title()
        self.provision_resource = provision_resource
        self._describe = describe or (
            lambda repo, path: Change("add", f"stage {path}", f"Changed **{path}**")
        )

    def provision_describe(self, repo: StagedRepo, path: str) -> Change | None:
        return self._describe(repo, path)


@pytest.fixture
def settings(tmp_path: Path) -> ProvisionSettings:
    return ProvisionSettings(profiles_dir=tmp_path / "profiles")


class TestCreateProfile:
    def test_creates_a_patch_profile(self, settings: ProvisionSettings) -> None:
        profile = create_profile("shop-floor-fleet", "patch", settings)
        assert (profile.repo.path / ".git").is_dir()
        assert profile.repo.remote == "patch"
        assert profile.repo.path == settings.profiles_dir / "patch" / "shop-floor-fleet"

    def test_creates_a_baseline_profile(self, settings: ProvisionSettings) -> None:
        profile = create_profile("compliance-only", "baseline", settings)
        assert (profile.repo.path / ".git").is_dir()
        assert profile.repo.remote == "baseline"
        assert profile.repo.path == settings.profiles_dir / "baseline" / "compliance-only"

    def test_patch_and_baseline_are_independent_namespaces(
        self, settings: ProvisionSettings
    ) -> None:
        """The same name in both namespaces is allowed and creates two
        unrelated working trees -- there's no data linking them."""
        patch = create_profile("shop-floor-fleet", "patch", settings)
        baseline = create_profile("shop-floor-fleet", "baseline", settings)
        assert patch.repo.path != baseline.repo.path

    def test_a_duplicate_name_in_the_same_namespace_is_refused(
        self, settings: ProvisionSettings
    ) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        with pytest.raises(ValueError, match="already exists"):
            create_profile("shop-floor-fleet", "patch", settings)

    @pytest.mark.parametrize("bad_name", ["", ".", "..", "a/b", "a\\b"])
    def test_an_unsafe_name_is_refused(
        self, bad_name: str, settings: ProvisionSettings
    ) -> None:
        with pytest.raises(ValueError):
            create_profile(bad_name, "patch", settings)


class TestListProfiles:
    def test_empty_when_the_directory_does_not_exist_yet(
        self, settings: ProvisionSettings
    ) -> None:
        assert list_profiles("patch", settings) == []

    def test_lists_only_the_named_remote(self, settings: ProvisionSettings) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        create_profile("dev-kits", "patch", settings)
        create_profile("compliance-only", "baseline", settings)

        assert [p.name for p in list_profiles("patch", settings)] == [
            "dev-kits",
            "shop-floor-fleet",
        ]
        assert [p.name for p in list_profiles("baseline", settings)] == ["compliance-only"]

    def test_a_directory_with_no_git_tree_is_not_a_profile(
        self, settings: ProvisionSettings
    ) -> None:
        (settings.profiles_dir / "patch").mkdir(parents=True)
        (settings.profiles_dir / "patch" / "not-a-profile").mkdir()
        assert list_profiles("patch", settings) == []


class TestOpenProfile:
    def test_finds_an_existing_profile(self, settings: ProvisionSettings) -> None:
        create_profile("shop-floor-fleet", "patch", settings)
        found = open_profile("shop-floor-fleet", "patch", settings)
        assert isinstance(found, Profile)
        assert found.repo.remote == "patch"

    def test_an_unknown_name_raises_key_error(
        self, settings: ProvisionSettings
    ) -> None:
        with pytest.raises(KeyError):
            open_profile("nonexistent", "patch", settings)

    def test_a_name_that_only_exists_in_the_other_namespace_raises_key_error(
        self, settings: ProvisionSettings
    ) -> None:
        create_profile("shop-floor-fleet", "baseline", settings)
        with pytest.raises(KeyError):
            open_profile("shop-floor-fleet", "patch", settings)


class TestContributors:
    def test_dedupes_nothing_when_resources_are_distinct(self, monkeypatch) -> None:
        found = [_FakeContributor("users", "users"), _FakeContributor("apps", "apps")]
        monkeypatch.setattr("spiriconfig_provision.repo.discover", lambda: found)
        assert {c.provision_resource for c in contributors()} == {"users", "apps"}

    def test_a_resource_claimed_twice_drops_both(self, monkeypatch) -> None:
        found = [
            _FakeContributor("users-a", "users"),
            _FakeContributor("users-b", "users"),
            _FakeContributor("apps", "apps"),
        ]
        monkeypatch.setattr("spiriconfig_provision.repo.discover", lambda: found)
        result = contributors()
        assert [c.name for c in result] == ["apps"]

    def test_a_plugin_with_no_provision_resource_is_excluded(self, monkeypatch) -> None:
        plain = Plugin.__new__(Plugin)
        plain.name = "plain"
        found = [plain, _FakeContributor("users", "users")]
        monkeypatch.setattr("spiriconfig_provision.repo.discover", lambda: found)
        assert [c.name for c in contributors()] == ["users"]


class TestChangedPaths:
    def test_lists_every_untracked_file_individually(
        self, settings: ProvisionSettings
    ) -> None:
        """Regression: default `git status` collapses a wholly-untracked
        directory to its top-level name, which would make every file
        inside it invisible to `provision_describe`."""
        profile = create_profile("shop-floor-fleet", "patch", settings)
        entry = profile.repo.path / "users" / "add" / "alice"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("hash\n")

        assert _changed_paths(profile.repo, settings) == [
            "users/add/alice/password.hash"
        ]


class TestPendingChanges:
    def test_routes_a_changed_path_to_its_owning_contributor(
        self, settings: ProvisionSettings
    ) -> None:
        profile = create_profile("shop-floor-fleet", "patch", settings)
        entry = profile.repo.path / "users" / "add" / "alice"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("hash\n")

        changes = pending_changes(
            profile.repo, settings, contributors_=[_FakeContributor("users", "users")]
        )

        assert len(changes) == 1
        assert changes[0].detail == "Changed **users/add/alice/password.hash**"

    def test_the_same_change_from_multiple_paths_is_only_listed_once(
        self, settings: ProvisionSettings
    ) -> None:
        """A bundle under `apps/images/` (see `spiriconfig_appstore.images`)
        can touch dozens of blob paths for one logical event -- a
        contributor describing all of them as one aggregate `Change`
        must not turn into one row per blob in Review & Sign."""
        profile = create_profile("shop-floor-fleet", "patch", settings)
        for name in ("index.json", "blobs/sha256/aaa", "blobs/sha256/bbb"):
            entry = profile.repo.path / "apps" / "images" / "oci" / name
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("x")

        same_change = Change("images", "1 container image bundled", "bundled")
        changes = pending_changes(
            profile.repo,
            settings,
            contributors_=[
                _FakeContributor("appstore", "apps", describe=lambda repo, path: same_change)
            ],
        )

        assert changes == [same_change]

    def test_falls_back_to_the_bare_path_when_nobody_claims_it(
        self, settings: ProvisionSettings
    ) -> None:
        profile = create_profile("shop-floor-fleet", "patch", settings)
        (profile.repo.path / "network").mkdir()
        (profile.repo.path / "network" / "add").mkdir()
        (profile.repo.path / "network" / "add" / "shop-floor.nmconnection").write_text("x")

        changes = pending_changes(profile.repo, settings, contributors_=[])

        assert len(changes) == 1
        assert changes[0].verb == "?"
        assert changes[0].detail == "network/add/shop-floor.nmconnection"
