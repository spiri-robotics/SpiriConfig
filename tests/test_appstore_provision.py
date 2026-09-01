"""Tests for spiriconfig_appstore's contribution to provisioning: staging,
describing, and applying ``apps/`` entries and ``apps/stores.toml`` in a
provisioning repo. See ``docs/provisioning.md`` for the interface this
exercises, and the module docstring of ``spiriconfig_appstore.provision``
for the reduced first-pass scope (no ``apps/images/``).

Like test_appstore.py, real git repos in a tmpdir stand in for a store --
what "already cloned" or "declares an app" means is a git question, not
something a mock should answer on git's behalf.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from nicegui.testing import User

from spiriconfig import web
from spiriconfig.commands import run
from spiriconfig_appstore import AppStorePlugin, installs, provision as appstore_provision
from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_appstore.provision import (
    DeclaredStore,
    _apply_baseline,
    _apply_patch,
    _apply_stores,
    _drop_from_state,
    _env_entry,
    _has_settings,
    _parse_stores_toml,
    _render_env,
    _stage_add,
    _stage_remove,
    _stage_state_add,
    _staged,
    _unstage,
    _validate_name,
    _write_stores_toml,
    describe,
)
from spiriconfig_appstore.stores import store_for_url
from spiriconfig_appstore.stores import stores as list_stores
from spiriconfig_provision import ProvisionPlugin
from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import StagedRepo
from spiriconfig_provision.repo import create_profile

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
pytestmark = git_required

WHOAMI = """\
services:
  whoami:
    image: traefik/whoami:v1.10.1
"""

NEXTCLOUD = """\
x-spiri-settings:
  - env: NEXTCLOUD_DOMAIN
    widget: input
    label: Domain
    required: true
  - env: NEXTCLOUD_PORT
    widget: number
    label: HTTP port
    default: "8443"

services:
  nextcloud:
    image: nextcloud:29
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A git repo shaped like an app store: one directory per app --
    whoami declares no settings, nextcloud declares two fields."""
    root = tmp_path / "upstream"
    (root / "whoami").mkdir(parents=True)
    (root / "whoami" / "compose.yaml").write_text(WHOAMI)
    (root / "nextcloud").mkdir()
    (root / "nextcloud" / "compose.yaml").write_text(NEXTCLOUD)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial apps")
    return root


@pytest.fixture
def compose_dir(tmp_path: Path) -> Path:
    root = tmp_path / "compose"
    root.mkdir()
    return root


@pytest.fixture
def settings(upstream: Path, tmp_path: Path) -> AppStoreSettings:
    return AppStoreSettings(stores=[str(upstream)], store_dir=tmp_path / "stores")


@pytest.fixture
def cloned(settings: AppStoreSettings) -> AppStoreSettings:
    """Same settings, but with the seed store actually cloned -- i.e. the
    state right after `appstore check`."""
    only = list_stores(settings)[0]
    only.path.parent.mkdir(parents=True, exist_ok=True)
    run(only.clone_command()).check()
    return settings


class TestDescribe:
    def test_add_entry(self, tmp_path: Path) -> None:
        entry = tmp_path / "apps" / "add" / "whoami" / "env"
        entry.parent.mkdir(parents=True)
        entry.touch()
        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, "apps/add/whoami/env")
        assert change is not None
        assert change.verb == "add"
        assert "whoami" in change.summary
        assert "installed" in change.summary
        assert "whoami" in change.detail

    def test_add_entry_whose_env_was_deleted_is_a_withdrawal_not_an_install(
        self, tmp_path: Path
    ) -> None:
        """Regression: unstaging a previously-committed add (dismissing
        it, or staging a remove over it) deletes ``apps/add/<name>/env``
        without touching this describe path's structure -- `git status`
        reports that deletion as a changed path exactly like a new one.
        Describing it as "installed" put a false install line right next
        to that same commit's own, real "removed" line for the same app
        -- see `_describe_patch`'s docstring."""
        repo = StagedRepo(path=tmp_path, remote="patch")  # no apps/add/whoami/env on disk
        change = describe(repo, "apps/add/whoami/env")
        assert change is not None
        assert change.verb != "add"
        assert "installed" not in change.summary
        assert "whoami" in change.detail

    def test_add_entry_without_env_suffix_is_none(self, tmp_path: Path) -> None:
        """Regression: `apps/add/<name>` alone (no nested `env`) used to
        be the whole entry when it was a bare marker file; now it's a
        directory, and only the `env` path inside it is a describable
        change."""
        repo = StagedRepo(path=tmp_path, remote="patch")
        assert describe(repo, "apps/add/whoami") is None

    def test_remove_entry(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, "apps/remove/old-dashboard")
        assert change is not None
        assert change.verb == "remove"
        assert "old-dashboard" in change.summary
        assert "removed" in change.summary

    def test_state_entry_present_is_added(self, tmp_path: Path) -> None:
        entry = tmp_path / "apps" / "state" / "nextcloud" / "env"
        entry.parent.mkdir(parents=True)
        entry.touch()
        repo = StagedRepo(path=tmp_path, remote="baseline")
        change = describe(repo, "apps/state/nextcloud/env")
        assert change is not None
        assert change.verb == "add"
        assert "nextcloud" in change.summary
        assert "installed" in change.summary

    def test_state_entry_absent_is_removed(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="baseline")
        change = describe(repo, "apps/state/nextcloud/env")
        assert change is not None
        assert change.verb == "remove"
        assert "nextcloud" in change.summary
        assert "removed" in change.summary

    def test_path_for_another_resource_is_none(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        assert describe(repo, "users/add/whoami/password.hash") is None

    def test_too_short_a_path_is_none(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        assert describe(repo, "apps") is None

    def test_stores_toml_describes_the_whole_declared_list(self, tmp_path: Path) -> None:
        path = tmp_path / "apps" / "stores.toml"
        _write_stores_toml(
            path,
            [DeclaredStore(url="https://example.com/a.git"), DeclaredStore(url="https://example.com/b.git", ref="v2")],
        )
        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, "apps/stores.toml")
        assert change.verb == "stores"
        assert "2 app stores" in change.summary
        assert "example.com/a.git" in change.detail
        assert "example.com/b.git" in change.detail

    def test_stores_toml_missing_describes_as_cleared(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, "apps/stores.toml")
        assert change.verb == "stores"
        assert "cleared" in change.summary

    def test_images_index_describes_the_bundled_count(self, tmp_path: Path) -> None:
        from spiriconfig_appstore.images import OCI_DIR

        index_path = tmp_path / OCI_DIR / "index.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text(
            '{"manifests": [{"annotations": {"org.opencontainers.image.ref.name": "whoami"}}, '
            '{"annotations": {"org.opencontainers.image.ref.name": "nextcloud"}}]}'
        )
        repo = StagedRepo(path=tmp_path, remote="patch")

        change = describe(repo, f"{OCI_DIR}/index.json")

        assert change.verb == "images"
        assert "2 container images bundled" in change.summary

    def test_images_index_missing_describes_as_cleared(self, tmp_path: Path) -> None:
        from spiriconfig_appstore.images import OCI_DIR

        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, f"{OCI_DIR}/blobs/sha256/deadbeef")
        assert change.verb == "images"
        assert "cleared" in change.summary

    def test_every_blob_path_describes_identically(self, tmp_path: Path) -> None:
        """The point of one aggregate `Change` -- `pending_changes` dedups
        by equality, so every one of a bundle's many blob paths has to
        produce the exact same `Change`, not just an equivalent-looking
        one."""
        from spiriconfig_appstore.images import OCI_DIR

        index_path = tmp_path / OCI_DIR / "index.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text('{"manifests": []}')
        repo = StagedRepo(path=tmp_path, remote="patch")

        a = describe(repo, f"{OCI_DIR}/index.json")
        b = describe(repo, f"{OCI_DIR}/blobs/sha256/abc123")
        c = describe(repo, f"{OCI_DIR}/blobs/sha256/def456")

        assert a == b == c

    def test_add_entry_with_settings_lists_them_in_detail(
        self, tmp_path: Path, cloned: AppStoreSettings, monkeypatch
    ) -> None:
        """The whole point: a commit message that says "installed" is not
        an audit log of what was actually configured -- the settings a
        save wrote need to show up in `detail` too, not just that
        something was staged."""
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{cloned.stores[0]}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(cloned.store_dir))
        repo = StagedRepo(path=tmp_path, remote="patch")
        entry = tmp_path / "apps" / "add" / "nextcloud" / "env"
        entry.parent.mkdir(parents=True)
        entry.write_text("NEXTCLOUD_DOMAIN=cloud.example\n")

        change = describe(repo, "apps/add/nextcloud/env")

        assert change is not None
        assert "NEXTCLOUD_DOMAIN=cloud.example" in change.detail

    def test_add_entry_with_a_password_field_hides_its_value(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A settings value showing up in the commit message is the
        point (see above) -- a *secret* value showing up there is the
        opposite of the point. Never the value, only that it was set."""
        store = tmp_path / "upstream"
        (store / "grafana").mkdir(parents=True)
        (store / "grafana" / "compose.yaml").write_text(
            "x-spiri-settings:\n"
            "  - env: GRAFANA_ADMIN_PASSWORD\n"
            "    widget: password\n"
            "services:\n"
            "  grafana:\n"
            "    image: grafana/grafana:11.1.0\n"
        )
        _git(store, "init", "-q", "-b", "main")
        _git(store, "add", "-A")
        _git(store, "commit", "-qm", "grafana")
        store_dir = tmp_path / "stores"
        settings = AppStoreSettings(stores=[str(store)], store_dir=store_dir)
        only = list_stores(settings)[0]
        only.path.parent.mkdir(parents=True, exist_ok=True)
        run(only.clone_command()).check()
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{store}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(store_dir))

        repo = StagedRepo(path=tmp_path / "repo", remote="patch")
        entry = tmp_path / "repo" / "apps" / "add" / "grafana" / "env"
        entry.parent.mkdir(parents=True)
        entry.write_text("GRAFANA_ADMIN_PASSWORD=hunter2\n")

        change = describe(repo, "apps/add/grafana/env")

        assert change is not None
        assert "hunter2" not in change.detail
        assert "GRAFANA_ADMIN_PASSWORD=(hidden)" in change.detail


class TestValidateName:
    def test_none_is_rejected_not_crashed_on(self) -> None:
        """A `ui.select`'s value is `None` before anything is picked --
        regression, see TestBaselineDropdownIsStrict."""
        with pytest.raises(ValueError):
            _validate_name(None)

    def test_blank_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _validate_name("   ")


class TestStaging:
    def test_stage_add_creates_an_env_file_and_clears_staged_remove(
        self, tmp_path: Path
    ) -> None:
        apps_dir = tmp_path / "apps"
        (apps_dir / "remove").mkdir(parents=True)
        (apps_dir / "remove" / "whoami").touch()

        _stage_add(apps_dir, "whoami")

        assert (apps_dir / "add" / "whoami" / "env").is_file()
        assert not (apps_dir / "remove" / "whoami").exists()

    def test_stage_add_preserves_an_already_configured_env(self, tmp_path: Path) -> None:
        """Re-staging (e.g. toggling Remove then back to Add) must not
        clobber settings already saved via the settings dialog."""
        apps_dir = tmp_path / "apps"
        entry = _env_entry(apps_dir / "add", "nextcloud")
        entry.parent.mkdir(parents=True)
        entry.write_text("NEXTCLOUD_DOMAIN=cloud.example\n")

        _stage_add(apps_dir, "nextcloud")

        assert entry.read_text() == "NEXTCLOUD_DOMAIN=cloud.example\n"

    def test_stage_remove_is_a_file_not_a_directory(self, tmp_path: Path) -> None:
        apps_dir = tmp_path / "apps"
        _stage_add(apps_dir, "whoami")

        _stage_remove(apps_dir, "whoami")

        entry = apps_dir / "remove" / "whoami"
        assert entry.is_file()
        assert not (apps_dir / "add" / "whoami").exists()

    def test_staged_lists_add_directories_and_remove_files_separately(
        self, tmp_path: Path
    ) -> None:
        apps_dir = tmp_path / "apps"
        _stage_add(apps_dir, "whoami")
        (apps_dir / "remove").mkdir(parents=True)
        (apps_dir / "remove" / "old-dashboard").touch()

        assert _staged(apps_dir, "add") == ["whoami"]
        assert _staged(apps_dir, "remove") == ["old-dashboard"]

    def test_unstage_of_an_absent_name_is_a_no_op(self, tmp_path: Path) -> None:
        apps_dir = tmp_path / "apps"
        _unstage(apps_dir, "add", "nobody")  # must not raise


class TestBaselineStaging:
    def test_stage_state_add_creates_an_env_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "apps" / "state"
        _stage_state_add(state_dir, "nextcloud")
        assert (state_dir / "nextcloud" / "env").is_file()

    def test_drop_from_state_removes_the_directory(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "apps" / "state"
        _stage_state_add(state_dir, "nextcloud")

        _drop_from_state(state_dir, "nextcloud")

        assert not (state_dir / "nextcloud").exists()

    def test_drop_from_state_of_an_absent_name_is_a_no_op(self, tmp_path: Path) -> None:
        _drop_from_state(tmp_path / "apps" / "state", "nobody")  # must not raise

    def test_staged_lists_state_entries(self, tmp_path: Path) -> None:
        apps_dir = tmp_path / "apps"
        _stage_state_add(apps_dir / "state", "nextcloud")
        assert _staged(apps_dir, "state") == ["nextcloud"]


class TestStoresToml:
    def test_round_trips_url_and_ref(self, tmp_path: Path) -> None:
        path = tmp_path / "stores.toml"
        _write_stores_toml(
            path,
            [
                DeclaredStore(url="https://example.com/a.git"),
                DeclaredStore(url="https://example.com/b.git", ref="v2"),
            ],
        )
        entries = _parse_stores_toml(path)
        assert entries == [
            DeclaredStore(url="https://example.com/a.git", ref=None),
            DeclaredStore(url="https://example.com/b.git", ref="v2"),
        ]

    def test_missing_file_is_an_empty_list(self, tmp_path: Path) -> None:
        assert _parse_stores_toml(tmp_path / "nope.toml") == []

    def test_unparsable_file_is_an_empty_list_not_a_crash(self, tmp_path: Path) -> None:
        path = tmp_path / "stores.toml"
        path.write_text("this is not [ valid toml")
        assert _parse_stores_toml(path) == []

    def test_writing_an_empty_list_produces_an_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "stores.toml"
        _write_stores_toml(path, [DeclaredStore(url="https://example.com/a.git")])
        _write_stores_toml(path, [])
        assert path.read_text() == ""
        assert _parse_stores_toml(path) == []


class TestSettings:
    def test_has_settings_true_for_a_declaring_app(self, cloned: AppStoreSettings) -> None:
        app = list_stores(cloned)[0].app("nextcloud")
        assert _has_settings(app)

    def test_has_settings_false_for_an_app_with_none(self, cloned: AppStoreSettings) -> None:
        app = list_stores(cloned)[0].app("whoami")
        assert not _has_settings(app)

    def test_render_env_writes_a_header_for_a_new_file(self, cloned: AppStoreSettings) -> None:
        from spiriconfig_docker.settings import declared

        app = list_stores(cloned)[0].app("nextcloud")
        fields = declared(app.compose_file)
        text = _render_env(fields, None, {"NEXTCLOUD_DOMAIN": "cloud.example", "NEXTCLOUD_PORT": "8443"})
        assert text.startswith("# written when this app is applied on the target device")
        assert "NEXTCLOUD_DOMAIN=cloud.example" in text
        assert "NEXTCLOUD_PORT=8443" in text

    def test_render_env_preserves_an_existing_file_without_a_new_header(
        self, cloned: AppStoreSettings
    ) -> None:
        from spiriconfig_docker.settings import declared

        app = list_stores(cloned)[0].app("nextcloud")
        fields = declared(app.compose_file)
        text = _render_env(fields, "NEXTCLOUD_DOMAIN=old.example\n", {"NEXTCLOUD_DOMAIN": "new.example"})
        assert "# written when" not in text
        assert "NEXTCLOUD_DOMAIN=new.example" in text

    def test_render_env_rejects_a_missing_required_value(self, cloned: AppStoreSettings) -> None:
        from spiriconfig_docker.settings import SettingsError, declared

        app = list_stores(cloned)[0].app("nextcloud")
        fields = declared(app.compose_file)
        with pytest.raises(SettingsError):
            _render_env(fields, None, {"NEXTCLOUD_DOMAIN": "", "NEXTCLOUD_PORT": "8443"})


class TestApplyPatch:
    def test_add_installs_an_app_with_no_settings(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        apps_dir = tmp_path / "apps"
        _stage_add(apps_dir, "whoami")

        steps = _apply_patch(cloned, compose_dir, apps_dir, current={})

        assert len(steps) == 1
        assert "ln -s" in str(steps[0].command)
        assert str(compose_dir / "whoami") in str(steps[0].command)

    def test_add_with_configured_settings_also_writes_env(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        apps_dir = tmp_path / "apps"
        entry = apps_dir / "add" / "nextcloud" / "env"
        entry.parent.mkdir(parents=True)
        entry.write_text("NEXTCLOUD_DOMAIN=cloud.example\n")

        steps = _apply_patch(cloned, compose_dir, apps_dir, current={})

        assert len(steps) == 2
        assert "ln -s" in str(steps[0].command)
        assert str(steps[1].command) == f"tee {compose_dir / 'nextcloud' / '.env'}"
        assert steps[1].input == "NEXTCLOUD_DOMAIN=cloud.example\n"

    def test_add_of_an_already_installed_name_with_no_settings_is_a_no_op(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        app = list_stores(cloned)[0].app("whoami")
        run(installs.install_command(app, compose_dir, "whoami")).check()
        current = {i.name: i for i in installs.installed(cloned, compose_dir)}

        apps_dir = tmp_path / "apps"
        _stage_add(apps_dir, "whoami")

        steps = _apply_patch(cloned, compose_dir, apps_dir, current=current)

        assert steps == []

    def test_add_of_an_already_installed_name_still_refreshes_its_env(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        app = list_stores(cloned)[0].app("nextcloud")
        run(installs.install_command(app, compose_dir, "nextcloud")).check()
        current = {i.name: i for i in installs.installed(cloned, compose_dir)}

        apps_dir = tmp_path / "apps"
        entry = apps_dir / "add" / "nextcloud" / "env"
        entry.parent.mkdir(parents=True)
        entry.write_text("NEXTCLOUD_DOMAIN=cloud.example\n")

        steps = _apply_patch(cloned, compose_dir, apps_dir, current=current)

        assert len(steps) == 1
        assert str(steps[0].command) == f"tee {compose_dir / 'nextcloud' / '.env'}"

    def test_remove_uninstalls_an_existing_install(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        app = list_stores(cloned)[0].app("whoami")
        run(installs.install_command(app, compose_dir, "whoami")).check()
        current = {i.name: i for i in installs.installed(cloned, compose_dir)}

        remove_dir = tmp_path / "apps" / "remove"
        remove_dir.mkdir(parents=True)
        (remove_dir / "whoami").touch()

        steps = _apply_patch(cloned, compose_dir, tmp_path / "apps", current=current)

        assert len(steps) == 1
        assert str(steps[0].command) == f"rm {compose_dir / 'whoami'}"

    def test_remove_of_a_name_not_installed_is_a_no_op(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        remove_dir = tmp_path / "apps" / "remove"
        remove_dir.mkdir(parents=True)
        (remove_dir / "nobody-by-this-name").touch()

        steps = _apply_patch(cloned, compose_dir, tmp_path / "apps", current={})

        assert steps == []

    def test_no_apps_directory_is_a_no_op(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        assert _apply_patch(cloned, compose_dir, tmp_path / "apps", current={}) == []


class TestApplyBaseline:
    def test_declares_the_exact_set(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        """whoami is on the live system but not declared; nextcloud is
        declared but not installed -- both must move."""
        app = list_stores(cloned)[0].app("whoami")
        run(installs.install_command(app, compose_dir, "whoami")).check()
        current = {i.name: i for i in installs.installed(cloned, compose_dir)}

        state_dir = tmp_path / "apps" / "state"
        _stage_state_add(state_dir, "nextcloud")

        steps = _apply_baseline(cloned, compose_dir, state_dir, current)

        assert len(steps) == 2
        assert str(steps[0].command) == f"rm {compose_dir / 'whoami'}"
        assert "nextcloud" in str(steps[1].command)

    def test_an_already_installed_app_with_no_settings_needs_no_step(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        app = list_stores(cloned)[0].app("whoami")
        run(installs.install_command(app, compose_dir, "whoami")).check()
        current = {i.name: i for i in installs.installed(cloned, compose_dir)}

        state_dir = tmp_path / "apps" / "state"
        _stage_state_add(state_dir, "whoami")

        steps = _apply_baseline(cloned, compose_dir, state_dir, current)

        assert steps == []

    def test_an_already_installed_app_still_refreshes_its_declared_env(
        self, tmp_path: Path, compose_dir: Path, cloned: AppStoreSettings
    ) -> None:
        """Unlike users/state/'s password, an installed app itself needs
        no redo -- but declared settings are re-written every apply, same
        reasoning as users/state/'s password re-set (see
        _apply_baseline's docstring)."""
        app = list_stores(cloned)[0].app("nextcloud")
        run(installs.install_command(app, compose_dir, "nextcloud")).check()
        current = {i.name: i for i in installs.installed(cloned, compose_dir)}

        state_dir = tmp_path / "apps" / "state"
        entry = state_dir / "nextcloud" / "env"
        entry.parent.mkdir(parents=True)
        entry.write_text("NEXTCLOUD_DOMAIN=cloud.example\n")

        steps = _apply_baseline(cloned, compose_dir, state_dir, current)

        assert len(steps) == 1
        assert str(steps[0].command) == f"tee {compose_dir / 'nextcloud' / '.env'}"


class TestApplyStores:
    def test_clones_a_declared_store_not_yet_cloned(
        self, tmp_path: Path, settings: AppStoreSettings
    ) -> None:
        apps_dir = tmp_path / "apps"
        only = list_stores(settings)[0]
        _write_stores_toml(apps_dir / "stores.toml", [DeclaredStore(url=only.url)])

        steps = _apply_stores(settings, apps_dir)

        assert len(steps) == 1
        assert "clone" in str(steps[0].command)

    def test_an_already_cloned_store_needs_no_step(
        self, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        apps_dir = tmp_path / "apps"
        only = list_stores(cloned)[0]
        _write_stores_toml(apps_dir / "stores.toml", [DeclaredStore(url=only.url)])

        assert _apply_stores(cloned, apps_dir) == []

    def test_no_stores_toml_is_a_no_op(
        self, tmp_path: Path, settings: AppStoreSettings
    ) -> None:
        assert _apply_stores(settings, tmp_path / "apps") == []


class TestApplyEndToEnd:
    """`apply()` itself, which reads settings from the environment --
    unlike the `_apply_*` helpers above, which take them explicitly."""

    def test_installs_a_declared_patch_app_via_env_settings(
        self, tmp_path: Path, upstream: Path, monkeypatch
    ) -> None:
        compose_dir = tmp_path / "compose"
        compose_dir.mkdir()
        store_dir = tmp_path / "stores"
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{upstream}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(store_dir))
        monkeypatch.setenv("SPIRICONFIG_DOCKER_COMPOSE_DIR", str(compose_dir))

        repo_path = tmp_path / "repo"
        entry = repo_path / "apps" / "add" / "whoami" / "env"
        entry.parent.mkdir(parents=True)
        entry.touch()

        # Clone the store first -- `apply()` reconciles installed apps
        # against configured stores, it does not clone one for `apps/add/`.
        settings = AppStoreSettings(stores=[str(upstream)], store_dir=store_dir)
        only = list_stores(settings)[0]
        only.path.parent.mkdir(parents=True, exist_ok=True)
        run(only.clone_command()).check()

        steps = appstore_provision.apply(StagedRepo(path=repo_path, remote="patch"))

        assert len(steps) == 1
        assert "ln -s" in str(steps[0].command)

    def test_baseline_also_clones_a_declared_store(
        self, tmp_path: Path, upstream: Path, monkeypatch
    ) -> None:
        """Regression: `apply()` used to clone declared stores only for
        the patch remote -- a baseline repo's own `apps/stores.toml` was
        parsed by `describe()` but never actually reconciled by `apply()`.
        Baseline and patch are separate repos (see
        `render_baseline_block`'s docstring), so a device applying only a
        baseline profile had no patch-side clone to fall back on."""
        compose_dir = tmp_path / "compose"
        compose_dir.mkdir()
        store_dir = tmp_path / "stores"
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", "[]")
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(store_dir))
        monkeypatch.setenv("SPIRICONFIG_DOCKER_COMPOSE_DIR", str(compose_dir))

        repo_path = tmp_path / "repo"
        settings = AppStoreSettings(stores=[], store_dir=store_dir)
        store = store_for_url(settings, str(upstream))
        _write_stores_toml(
            repo_path / "apps" / "stores.toml", [DeclaredStore(url=store.url)]
        )

        steps = appstore_provision.apply(StagedRepo(path=repo_path, remote="baseline"))

        assert len(steps) == 1
        assert "clone" in str(steps[0].command)


class TestEditSettingsOfAnAlreadyStagedApp:
    """The settings gear isn't a one-shot "configure on add" action --
    reopening it for an app already staged (patch) or already declared
    (baseline) must show what was saved last time, and let it be
    changed, not just offer Remove/un-declare as the only next step."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, upstream: Path, monkeypatch) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{upstream}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(tmp_path / "stores"))

    async def test_reopening_settings_on_a_staged_patch_app_shows_saved_values(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-apps-add-nextcloud").click()
        user.find(marker="provision-apps-settings-nextcloud").click()
        await user.should_see("nextcloud — settings")
        user.find(marker="setting-NEXTCLOUD_DOMAIN").elements.pop().set_value(
            "cloud.example"
        )
        user.find(marker="provision-apps-settings-save").click()
        await user.should_see("Saved")

        # Reopen -- the app is still staged Add, not removed or unstaged,
        # and the gear must still be there to edit it again.
        user.find(marker="provision-apps-settings-nextcloud").click()
        await user.should_see("nextcloud — settings")
        domain_field = user.find(marker="setting-NEXTCLOUD_DOMAIN").elements.pop()
        assert domain_field.value == "cloud.example"

        domain_field.set_value("cloud2.example")
        user.find(marker="provision-apps-settings-save").click()
        await user.should_see("Saved")

        entry = tmp_path / "profiles" / "patch" / "shop-floor-fleet" / "apps" / "add" / "nextcloud" / "env"
        assert "NEXTCLOUD_DOMAIN=cloud2.example" in entry.read_text()
        # Still staged Add throughout -- editing settings never un-staged it.
        assert entry.is_file()

    async def test_reopening_settings_on_a_declared_baseline_app_shows_saved_values(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        create_profile("shop-floor-fleet", "baseline", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-tab-baseline").click()
        user.find(marker="provision-apps-baseline-name").elements.pop().set_value("nextcloud")
        user.find(marker="provision-apps-baseline-add").click()
        await user.should_see("nextcloud")

        user.find(marker="provision-apps-baseline-settings-nextcloud").click()
        await user.should_see("nextcloud — settings")
        user.find(marker="setting-NEXTCLOUD_DOMAIN").elements.pop().set_value(
            "cloud.example"
        )
        user.find(marker="provision-apps-settings-save").click()
        await user.should_see("Saved")

        user.find(marker="provision-apps-baseline-settings-nextcloud").click()
        await user.should_see("nextcloud — settings")
        domain_field = user.find(marker="setting-NEXTCLOUD_DOMAIN").elements.pop()
        assert domain_field.value == "cloud.example"


class TestStagingAddDeclaresTheStore:
    """Regression: staging an app for install used to say nothing about
    where it came from -- `apps/add/<name>` could name an app whose store
    was configured only on the authoring laptop, never declared in
    `apps/stores.toml`. A device applying the repo cloning exactly what
    `apps/stores.toml` lists would then have no store to resolve that
    name against. Staging Add now registers the app's store too."""

    def test_ensure_store_declared_adds_it_once(self, tmp_path: Path, settings: AppStoreSettings) -> None:
        from spiriconfig_appstore.provision import _ensure_store_declared

        apps_dir = tmp_path / "apps"
        only = list_stores(settings)[0]

        first = _ensure_store_declared(apps_dir, only.url)
        second = _ensure_store_declared(apps_dir, only.url)

        assert first is True
        assert second is False
        assert [e.url for e in _parse_stores_toml(apps_dir / "stores.toml")] == [only.url]

    async def test_staging_add_in_the_web_ui_registers_the_apps_store(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings, upstream: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{upstream}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(tmp_path / "stores"))

        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")
        await user.should_see("No stores declared.")

        user.find(marker="provision-apps-add-whoami").click()

        await user.should_see(str(upstream))  # the stores list redrew itself
        entries = _parse_stores_toml(profile.repo.path / "apps" / "stores.toml")
        assert [e.url for e in entries] == [str(upstream)]


class TestPatchStaging:
    """The patch block's catalog list, each row with its own Add and
    Remove button -- `render_patch_block` briefly went through a
    dropdown + mode-toggle + Stage-button shape (to converge with
    `render_baseline_block`'s list-builder pattern), but direct feedback
    was that the mode toggle read as an unlabelled pair of tabs, and that
    Add and Remove should each be a distinct, always-visible button per
    row -- closer to the original design mockup, just with the status
    label rewritten so an already-staged row reads ``Added``/``Removed``
    (a state) rather than ``Add``/``Remove`` (which reads as an action
    still waiting to be taken, the thing that was actually unclear)."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, upstream: Path, monkeypatch) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{upstream}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(tmp_path / "stores"))

    async def test_add_writes_apps_add_and_relabels_to_added(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-apps-add-whoami").click()

        await user.should_see("Added")
        assert (profile.repo.path / "apps" / "add" / "whoami" / "env").is_file()

        user.find(marker="provision-patch-review").click()
        await user.should_see("app whoami installed")

    async def test_remove_writes_apps_remove_and_relabels_to_removed(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-apps-remove-whoami").click()

        await user.should_see("Removed")
        assert (profile.repo.path / "apps" / "remove" / "whoami").is_file()
        assert not (profile.repo.path / "apps" / "add").exists()

    async def test_clicking_add_again_on_an_added_app_opens_a_dialog_not_a_noop(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        """Regression waiting to happen: a button that's already in the
        state its own click would produce must not silently do nothing --
        that reads as broken, not as "already done"."""
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-apps-add-whoami").click()
        await user.should_see("Added")
        user.find(marker="provision-apps-add-whoami").click()

        await user.should_see("is already staged to be installed")
        # The redundant click didn't touch anything on disk.
        assert (profile.repo.path / "apps" / "add" / "whoami" / "env").is_file()

        user.find(marker="provision-apps-unstage-whoami").click()

        await user.should_see("Add")
        assert not (profile.repo.path / "apps" / "add" / "whoami").exists()

    async def test_switching_from_add_to_remove_needs_no_dialog(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-apps-add-whoami").click()
        await user.should_see("Added")
        user.find(marker="provision-apps-remove-whoami").click()

        await user.should_see("Removed")
        assert not (profile.repo.path / "apps" / "add" / "whoami").exists()
        assert (profile.repo.path / "apps" / "remove" / "whoami").is_file()


class TestBaselineDropdownIsStrict:
    """Regression: the baseline "App name" field used to accept and stage
    arbitrary typed text (`ui.select(..., new_value_mode="add-unique")`),
    so an operator could declare an app that exists in no configured
    store -- silently a no-op on every device that applies the baseline,
    since `find_app` has nothing to resolve it to. The field is now a
    strict pick from the catalog, checked again in `do_add` in case the
    catalog changed underneath the page."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, upstream: Path, monkeypatch) -> None:
        # `render_baseline_block` reads settings from the environment via
        # `appstore_settings()`, not from the `cloned`/`settings` fixture
        # objects directly -- these have to match the same store/path the
        # `cloned` fixture actually cloned into, or the page falls back to
        # this repo's own default example store instead.
        monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{upstream}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(tmp_path / "stores"))

    async def test_a_value_outside_the_catalog_is_rejected_not_crashed_on(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        """A `ui.select` with a fixed `options=` list can't actually hold
        text that isn't one of them -- `set_value` on an unlisted value
        resolves to `None` server-side, same as if nothing were ever
        picked. Regression: `_validate_name` used to crash on that `None`
        (`AttributeError: 'NoneType' object has no attribute 'strip'`)
        instead of showing a plain "enter a name" message."""
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "baseline", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-tab-baseline").click()
        user.find(marker="provision-apps-baseline-name").elements.pop().set_value(
            "not-a-real-app"
        )
        user.find(marker="provision-apps-baseline-add").click()

        await user.should_see("is not a valid app name")
        assert not (profile.repo.path / "apps" / "state").exists()

    async def test_picking_a_real_catalog_app_stages_it(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        profile = create_profile("shop-floor-fleet", "baseline", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-tab-baseline").click()
        user.find(marker="provision-apps-baseline-name").elements.pop().set_value("whoami")
        user.find(marker="provision-apps-baseline-add").click()

        await user.should_see("whoami · upstream — apps/state")
        assert (profile.repo.path / "apps" / "state" / "whoami" / "env").is_file()


class TestBundleImagesButton:
    """The "Bundle images" action, without ever actually invoking skopeo
    (no network in a test suite) -- only the safe path, where nothing is
    declared yet and the button is a no-op notify, plus that it renders
    on both remotes at all."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, upstream: Path, monkeypatch) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_PROFILES_DIR", str(tmp_path / "profiles"))
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORES", f'["{upstream}"]')
        monkeypatch.setenv("SPIRICONFIG_APPSTORE_STORE_DIR", str(tmp_path / "stores"))

    async def test_it_is_on_the_patch_page(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        await user.should_see("Bundle images")

    async def test_it_is_on_the_baseline_page(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        create_profile("shop-floor-fleet", "baseline", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")
        user.find(marker="provision-tab-baseline").click()

        await user.should_see("Bundle images")

    async def test_nothing_declared_notifies_instead_of_opening_a_dialog(
        self, user: User, tmp_path: Path, cloned: AppStoreSettings
    ) -> None:
        """No app staged means the plan is only the (never network-bound)
        lfs setup commands -- this must stop there and say so, not open
        a dialog and try to stream `git lfs install`/`track` alone."""
        settings = ProvisionSettings(profiles_dir=tmp_path / "profiles")
        create_profile("shop-floor-fleet", "patch", settings)
        web.build([ProvisionPlugin(), AppStorePlugin()])
        await user.open("/provision")

        user.find(marker="provision-apps-bundle-patch").click()

        await user.should_see("Nothing declared to bundle images for.")
        # The static caption mentions skopeo by name; only a `skopeo copy`
        # command line in an opened dialog would mean it tried to run one.
        await user.should_not_see("skopeo copy")
