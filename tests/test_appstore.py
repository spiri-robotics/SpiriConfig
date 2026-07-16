"""Tests for app stores: discovery, install, and the update dance.

These build real git repositories in a tmpdir, because the thing under test *is*
git behaviour -- what a merge does to a file the user edited is not something a
mock can tell us, and a mock that claimed to know would be the only thing here
worth deleting. No network and no docker daemon is involved: a store is a local
path, which git clones perfectly happily.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import User

from spiriconfig import preferences, web
from spiriconfig.commands import run
from spiriconfig.plugins import Plugin
from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_appstore.installs import install_command, installed, uninstall
from spiriconfig_appstore.stores import (
    StoreError,
    find_app,
    remote_url,
    slug_for,
    store_for_url,
    stores,
    update_plan,
)

git_required = pytest.mark.skipif(
    shutil.which("git") is None, reason="needs git"
)

pytestmark = git_required

WHOAMI = """\
x-spiri-config-version: "1.10.1"
services:
  whoami:
    image: traefik/whoami:v1.10.1
    ports:
      - "8080:80"
"""

NEXTCLOUD = """\
services:
  nextcloud:
    image: nextcloud:29
"""


def _git(repo: Path, *args: str) -> None:
    """Run git in ``repo``, with an identity, failing the test if it errors."""
    subprocess.run(
        [
            "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A git repo shaped like an app store: one directory per app."""
    root = tmp_path / "upstream"
    (root / "whoami").mkdir(parents=True)
    (root / "whoami" / "compose.yaml").write_text(WHOAMI)
    (root / "nextcloud").mkdir()
    (root / "nextcloud" / "compose.yaml").write_text(NEXTCLOUD)

    # A top-level directory that is not an app: must be ignored, not crashed on.
    (root / "docs").mkdir()
    (root / "docs" / "README.md").write_text("not an app")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial apps")
    return root


@pytest.fixture
def compose_root(tmp_path: Path) -> Path:
    root = tmp_path / "compose"
    root.mkdir()
    return root


@pytest.fixture
def settings(upstream: Path, tmp_path: Path) -> AppStoreSettings:
    return AppStoreSettings(
        stores=[str(upstream)],
        store_dir=tmp_path / "stores",
    )


@pytest.fixture
def store(settings: AppStoreSettings):
    """The store, cloned -- i.e. the state right after `appstore sync`."""
    only = stores(settings)[0]
    only.path.parent.mkdir(parents=True, exist_ok=True)
    run(only.clone_command()).check()
    return only


def _bump_upstream(upstream: Path, old: str, new: str, message: str) -> None:
    """Make a change to the store, as its maintainer would."""
    compose = upstream / "whoami" / "compose.yaml"
    compose.write_text(compose.read_text().replace(old, new))
    _git(upstream, "commit", "-qam", message)


async def _until(condition, *, timeout: float = 5.0) -> None:
    """Wait for ``condition`` to come true, or fail the test saying it never did.

    A button click in the UI only *starts* the work -- the commands stream in the
    background -- so a test that asserts on the filesystem immediately afterwards
    is racing them.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition never came true within {timeout}s")


class TestSlugs:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/spiri/spiri-apps.git", "spiri-apps"),
            ("https://github.com/spiri/spiri-apps", "spiri-apps"),
            ("git@github.com:spiri/spiri-apps.git", "spiri-apps"),
            ("/srv/local-store/", "local-store"),
        ],
    )
    def test_derives_a_directory_name(self, url: str, expected: str) -> None:
        assert slug_for(url) == expected


class TestDiscovery:
    def test_finds_the_apps(self, store) -> None:
        """Sorted, so a store's listing does not depend on directory order."""
        assert [a.name for a in store.apps()] == ["nextcloud", "whoami"]

    def test_ignores_directories_without_a_compose_file(self, store) -> None:
        assert "docs" not in [a.name for a in store.apps()]

    def test_an_uncloned_store_has_no_apps(self, settings: AppStoreSettings) -> None:
        """Not an error: it is just a store nobody has synced yet."""
        assert stores(settings)[0].apps() == []

    def test_unknown_app_names_the_ones_that_exist(self, store) -> None:
        with pytest.raises(StoreError, match="nextcloud, whoami"):
            store.app("nodered")

    def test_cannot_escape_the_store(self, store) -> None:
        with pytest.raises(StoreError):
            store.app("../../etc")


class TestVersion:
    def test_prefers_the_declared_version(self, store) -> None:
        assert store.app("whoami").version() == "1.10.1"

    def test_falls_back_to_the_last_commit_date(self, store) -> None:
        """No x-spiri-config-version, so the date of the commit that touched it.

        The whole point of the fallback: a store maintainer who never thinks
        about versioning still gets one that moves when the app does.
        """
        version = store.app("nextcloud").version()
        assert version != "unknown"
        # An ISO date, from `git log --format=%cs`.
        assert len(version) == 10 and version.count("-") == 2


class TestInstall:
    def test_installs_a_symlink_into_the_compose_directory(
        self, store, compose_root: Path
    ) -> None:
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()

        link = compose_root / "whoami"
        assert link.is_symlink()
        assert link.resolve() == app.path.resolve()
        # And it is a usable compose project through the link.
        assert (link / "compose.yaml").read_text() == WHOAMI

    def test_the_command_is_a_plain_ln(self, store, compose_root: Path) -> None:
        command = install_command(store.app("whoami"), compose_root)
        assert command.argv[:2] == ["ln", "-s"]

    def test_can_install_under_another_name(self, store, compose_root: Path) -> None:
        run(install_command(store.app("whoami"), compose_root, "whoami-test")).check()
        assert (compose_root / "whoami-test").is_symlink()

    def test_refuses_to_install_twice(self, store, compose_root: Path) -> None:
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()
        with pytest.raises(StoreError, match="already installed"):
            install_command(app, compose_root)

    def test_refuses_to_clobber_a_real_directory(
        self, store, compose_root: Path
    ) -> None:
        """The user's own hand-made project of the same name is not ours to eat."""
        (compose_root / "whoami").mkdir()
        with pytest.raises(StoreError, match="not a symlink"):
            install_command(store.app("whoami"), compose_root)


class TestInstalled:
    def test_reads_the_store_and_app_back_off_the_symlink(
        self, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        """The symlink is the only record of the install, so it has to be enough."""
        run(install_command(store.app("whoami"), compose_root, "whoami-test")).check()

        [found] = installed(settings, compose_root)
        assert found.name == "whoami-test"
        assert found.app_name == "whoami"
        assert found.store.slug == store.slug

    def test_ignores_directories_we_did_not_install(
        self, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        (compose_root / "handmade").mkdir()
        assert installed(settings, compose_root) == []


class TestStoreManagement:
    """Adding and removing stores, and discovering them from disk.

    The model these exercise: a store *is* a git checkout under ``store_dir``. The
    seed list only seeds; the disk is the truth.
    """

    def test_a_seed_shows_up_not_cloned_until_it_is(
        self, settings: AppStoreSettings
    ) -> None:
        [only] = stores(settings)
        assert not only.is_cloned
        only.path.parent.mkdir(parents=True, exist_ok=True)
        run(only.clone_command()).check()
        [again] = stores(settings)
        assert again.is_cloned
        # Still one store, not the seed plus its clone.
        assert len(stores(settings)) == 1

    def test_discovers_a_checkout_that_was_never_seeded(
        self, upstream: Path, tmp_path: Path
    ) -> None:
        """A store the user cloned themselves, with an empty seed list, is first
        class -- disk is the source of truth."""
        settings = AppStoreSettings(stores=[], store_dir=tmp_path / "stores")
        target = store_for_url(settings, str(upstream))
        target.path.parent.mkdir(parents=True, exist_ok=True)
        run(target.clone_command()).check()

        [found] = stores(settings)
        assert found.slug == slug_for(str(upstream))
        assert found.is_cloned
        assert [a.name for a in found.apps()] == ["nextcloud", "whoami"]

    def test_reads_a_stores_url_back_off_its_remote(
        self, upstream: Path, tmp_path: Path
    ) -> None:
        settings = AppStoreSettings(stores=[], store_dir=tmp_path / "stores")
        target = store_for_url(settings, str(upstream))
        target.path.parent.mkdir(parents=True, exist_ok=True)
        run(target.clone_command()).check()

        assert remote_url(settings, target.path) == str(upstream)
        # And stores() surfaces that URL rather than inventing one.
        assert stores(settings)[0].url == str(upstream)

    def test_store_for_url_names_and_places_the_clone(
        self, tmp_path: Path
    ) -> None:
        settings = AppStoreSettings(stores=[], store_dir=tmp_path / "stores")
        store = store_for_url(settings, "https://example.com/team/apps.git")
        assert store.slug == "apps"
        assert store.path == (tmp_path / "stores").resolve() / "apps"

    def test_remove_command_is_an_rm_rf_of_the_checkout(
        self, store
    ) -> None:
        command = store.remove_command()
        assert command.argv == ["rm", "-rf", str(store.path)]

    def test_remove_deletes_the_checkout_and_it_leaves_the_listing(
        self, store, settings: AppStoreSettings
    ) -> None:
        assert store.is_cloned
        run(store.remove_command()).check()
        assert not store.path.exists()
        # The seed reappears as not-cloned; the clone is gone.
        assert all(not s.is_cloned for s in stores(settings))

    def test_ignores_symlinks_pointing_outside_the_stores(
        self, settings: AppStoreSettings, compose_root: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (compose_root / "other").symlink_to(elsewhere)
        assert installed(settings, compose_root) == []

    def test_uninstall_refuses_a_directory_we_did_not_create(
        self, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        (compose_root / "handmade").mkdir()
        with pytest.raises(StoreError, match="will not delete"):
            uninstall(settings, compose_root, "handmade")

    def test_uninstall_removes_only_the_link(
        self, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()
        found = uninstall(settings, compose_root, "whoami")
        run(found.uninstall_command()).check()

        assert not (compose_root / "whoami").exists()
        # The store's copy is untouched. An uninstall is not a delete.
        assert (app.path / "compose.yaml").read_text() == WHOAMI

    def test_adopt_leaves_a_real_directory_that_is_no_longer_ours(
        self, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        run(install_command(store.app("whoami"), compose_root)).check()
        found = uninstall(settings, compose_root, "whoami")
        for command in found.adopt_commands():
            run(command).check()

        link = compose_root / "whoami"
        assert link.is_dir() and not link.is_symlink()
        assert (link / "compose.yaml").read_text() == WHOAMI
        # It has left the store's orbit: nothing here will update it again.
        assert installed(settings, compose_root) == []


class TestUpdates:
    def test_notices_an_upstream_change(self, store, upstream: Path) -> None:
        app = store.app("whoami")
        assert not app.has_update()

        _bump_upstream(upstream, "whoami:v1.10.1", "whoami:v1.11.0", "bump")
        run(store.fetch_command()).check()

        assert app.has_update()
        assert app.changed_upstream() == ["whoami/compose.yaml"]

    def test_notices_a_local_edit(self, store, compose_root: Path) -> None:
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()
        assert not app.is_modified()

        # Edited through the symlink, which is how a user would do it.
        compose = compose_root / "whoami" / "compose.yaml"
        compose.write_text(compose.read_text().replace("8080:80", "9080:80"))

        assert app.is_modified()

    def test_untracked_data_is_not_a_local_edit(self, store, compose_root: Path) -> None:
        """docker creates bind-mount directories inside the checkout.

        Those are the user's data, not an edit to the app. Counting them would
        tell every user their apps were "modified" the moment they started one.
        """
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()
        (compose_root / "whoami" / "config").mkdir()
        (compose_root / "whoami" / "config" / "db.sqlite").write_text("data")

        assert not app.is_modified()

    def test_a_local_edit_survives_an_update(
        self, store, upstream: Path, compose_root: Path
    ) -> None:
        """The whole promise: the store moves, and the user's change is still there."""
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()

        compose = compose_root / "whoami" / "compose.yaml"
        compose.write_text(compose.read_text().replace("8080:80", "9080:80"))
        _bump_upstream(upstream, "whoami:v1.10.1", "whoami:v1.11.0", "bump")

        for command in update_plan(store):
            run(command).check()

        merged = compose.read_text()
        assert "9080:80" in merged, "the user's port was lost"
        assert "whoami:v1.11.0" in merged, "the store's change was not applied"
        assert not store.in_merge

    def test_discard_local_throws_the_edit_away(
        self, store, upstream: Path, compose_root: Path
    ) -> None:
        app = store.app("whoami")
        run(install_command(app, compose_root)).check()
        compose = compose_root / "whoami" / "compose.yaml"
        compose.write_text(compose.read_text().replace("8080:80", "9080:80"))

        for command in update_plan(store, discard_local=True):
            run(command).check()

        assert "9080:80" not in compose.read_text()

    def test_a_clean_store_does_not_get_a_pointless_commit(self, store) -> None:
        """Nothing edited, so there is nothing to preserve: fetch and merge only."""
        plan = update_plan(store)
        assert not any("commit" in c.argv for c in plan)


@pytest.fixture
def conflicted(store, upstream: Path, compose_root: Path):
    """A store mid-merge, because the user and the store changed the same line."""
    run(install_command(store.app("whoami"), compose_root)).check()
    compose = compose_root / "whoami" / "compose.yaml"
    compose.write_text(
        compose.read_text().replace("whoami:v1.10.1", "whoami:v1.10.1-custom")
    )
    _bump_upstream(upstream, "whoami:v1.10.1", "whoami:v1.11.0", "bump")

    for command in update_plan(store):
        run(command)  # the merge fails; that is the point
    assert store.in_merge
    return store, compose


class TestConflicts:
    def test_the_conflict_is_visible(self, conflicted) -> None:
        store, compose = conflicted
        assert store.conflicts() == ["whoami/compose.yaml"]
        assert "<<<<<<<" in compose.read_text()

    def test_a_second_update_refuses_to_pile_on(self, conflicted) -> None:
        store, _ = conflicted
        with pytest.raises(StoreError, match="unfinished update"):
            update_plan(store)

    def test_resolve_refuses_while_markers_remain(self, conflicted) -> None:
        """The regression test that matters.

        `git commit --all` *stages unmerged files*, so it will happily commit a
        file still full of conflict markers and report a successful update. The
        guard is ours, because git has none to offer here.
        """
        store, compose = conflicted
        with pytest.raises(StoreError, match="conflict markers"):
            store.resolve_plan()

        # And nothing was committed on the way to refusing.
        assert store.in_merge
        assert "<<<<<<<" in compose.read_text()

    def test_resolve_lands_the_merge_once_the_markers_are_gone(
        self, conflicted
    ) -> None:
        store, compose = conflicted
        compose.write_text(WHOAMI.replace("whoami:v1.10.1", "whoami:v1.11.0-custom"))

        for command in store.resolve_plan():
            run(command).check()

        assert not store.in_merge
        assert not store.conflicts()
        assert "whoami:v1.11.0-custom" in compose.read_text()

    def test_abort_restores_the_users_edit(self, conflicted) -> None:
        """Aborting is safe *because* the edit was committed before the merge."""
        store, compose = conflicted
        run(store.abort_command()).check()

        assert not store.in_merge
        assert "<<<<<<<" not in compose.read_text()
        assert "whoami:v1.10.1-custom" in compose.read_text()


class TestRelativePaths:
    """The defaults are relative (``test_data/...``), so relative must work.

    Relative paths are only meaningful against a working directory, and the two
    things an app store does -- clone, and symlink -- resolve them against a
    *different* directory than the one the user was standing in. Both of these
    failed before the paths were made absolute in :func:`stores`, and the symlink
    one failed silently: a link that dangles rather than an error.
    """

    @pytest.fixture
    def relative(self, upstream: Path, tmp_path: Path, monkeypatch):
        """Settings whose store path is relative, from a cwd that makes it work."""
        monkeypatch.chdir(tmp_path)
        return AppStoreSettings(
            stores=["upstream"],           # relative to tmp_path
            store_dir=Path("data/stores"),  # relative to tmp_path
        )

    def test_a_relative_store_is_resolved_to_an_absolute_path(self, relative) -> None:
        [store] = stores(relative)
        assert Path(store.url).is_absolute()
        assert store.path.is_absolute()

    def test_a_relative_store_can_be_cloned(self, relative) -> None:
        """The clone runs with no cwd of its own, so a relative URL would miss."""
        [store] = stores(relative)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        run(store.clone_command()).check()
        assert store.is_cloned
        assert [a.name for a in store.apps()] == ["nextcloud", "whoami"]

    def test_the_symlink_does_not_dangle(self, relative, tmp_path: Path) -> None:
        """The bug worth a test of its own.

        `ln -s data/stores/x/whoami compose/whoami` makes a link that resolves
        against `compose/`, not against the cwd -- so it points at nothing, and
        nothing says so until docker cannot find the compose file.
        """
        [store] = stores(relative)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        run(store.clone_command()).check()

        compose_root = tmp_path / "compose"
        compose_root.mkdir()
        run(install_command(store.app("whoami"), compose_root)).check()

        link = compose_root / "whoami"
        assert link.is_symlink()
        assert link.exists(), "the symlink dangles"
        assert (link / "compose.yaml").is_file()

    def test_a_real_url_is_left_alone(self, relative) -> None:
        """We rewrite paths, not URLs. A URL is not ours to touch."""
        settings = AppStoreSettings(
            stores=["https://github.com/spiri/spiri-apps.git", "git@github.com:o/r.git"],
            store_dir=Path("data/stores"),
        )
        urls = [s.url for s in stores(settings)]
        assert urls == [
            "https://github.com/spiri/spiri-apps.git",
            "git@github.com:o/r.git",
        ]


class TestFindApp:
    def test_finds_by_bare_name(self, settings: AppStoreSettings, store) -> None:
        assert find_app(settings, "whoami").name == "whoami"

    def test_finds_by_store_slash_name(self, settings: AppStoreSettings, store) -> None:
        assert find_app(settings, f"{store.slug}/whoami").name == "whoami"

    def test_unknown_app_raises(self, settings: AppStoreSettings, store) -> None:
        with pytest.raises(StoreError, match="no such app"):
            find_app(settings, "nodered")


class _PinnedPlugin(Plugin):
    """The appstore plugin, pinned to a test store instead of the real settings.

    The plugin's own ``page()`` reads settings from the environment, which in a
    test means /srv/compose and whatever stores the developer has configured.
    Passing settings in explicitly is the whole reason
    :func:`spiriconfig_appstore.web.page` takes them as an argument.
    """

    name = "appstore"
    title = "App Store"

    def __init__(self, settings: AppStoreSettings, compose_dir: Path) -> None:
        self._settings = settings
        self._compose_dir = compose_dir

    def page(self) -> None:
        from spiriconfig_appstore import web as appstore_web

        appstore_web.page(self._settings, self._compose_dir)


class TestThePage:
    """Rendered in-process: a page FastAPI cannot build takes the whole web app
    down at startup, so "does it render at all" is worth asserting, not checking
    by hand once. Same bargain tests/test_web.py makes.
    """

    async def test_it_renders_the_apps_in_a_store(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")
        await user.should_see("nextcloud")

    async def test_the_add_dialog_does_not_ask_for_a_login(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        """A login is a host's, not a store's, so Add is just a URL.

        It points at the logins section rather than carrying credential fields of
        its own.
        """
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")
        user.find("Add store").click()
        await user.should_see("Add an app store")
        await user.should_not_see("Access token")

    async def test_the_logins_section_is_hidden_until_advanced(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Managing host logins is a section on this page, behind advanced.

        Decluttering, not a boundary (see advanced.py): the section is hidden with
        advanced off and appears when it is on. HOME is redirected so the section
        reads a throwaway credential file, not the developer's.
        """
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
        (tmp_path / "fakehome").mkdir()
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")
        await user.should_not_see("App store logins")

        user.find("Advanced").click()
        await user.should_see("App store logins")

    async def test_an_installed_app_says_so(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        run(install_command(store.app("whoami"), compose_root)).check()
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("installed")

    async def test_a_conflicted_store_shows_the_way_out(
        self, user: User, conflicted, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        """Mid-merge, the page stops being a shop and becomes instructions."""
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("This update needs you")
        await user.should_see("Undo the update")

    async def test_the_page_survives_closing_a_dialog(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        """The same blank-page bug the docker plugin had.

        See tests/test_docker_web.py: refresh() scheduled its timer inside the
        container it was about to clear, so the render cancelled itself and the
        page went empty behind the closed dialog.
        """
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")

        # Sync, because there is exactly one of it -- every app has its own
        # Install button, so finding "Install" by label is ambiguous. Any dialog
        # exercises the same path: it refreshes the page when it closes.
        user.find("Sync").click()
        await user.should_see(f"{store.slug} — fetch")
        await asyncio.sleep(1.0)

        user.find("Close").click()
        await asyncio.sleep(1.0)

        # The store's apps are still listed behind it.
        await user.should_see("whoami")
        await user.should_see("nextcloud")

    async def test_no_stores_configured_says_so(
        self, user: User, tmp_path: Path, compose_root: Path
    ) -> None:
        settings = AppStoreSettings(stores=[], store_dir=tmp_path / "none")
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("No app stores yet")


class TestTheExampleStore:
    """The store this repo ships, in examples/store/.

    It is what a new user copies from and what `scripts/test-data.sh` builds, so
    it is worth knowing that it is a valid store and not just a directory of
    plausible YAML.
    """

    @pytest.fixture
    def examples(self) -> Path:
        root = Path(__file__).parent.parent / "examples" / "store"
        if not root.is_dir():
            pytest.skip("examples/store is not in this checkout")
        return root

    def test_every_app_has_a_parseable_compose_file(self, examples: Path) -> None:
        import yaml

        apps = [d for d in examples.iterdir() if d.is_dir() and d.name != "docs"]
        assert {d.name for d in apps} == {"whoami", "traefik", "nextcloud", "grafana"}

        for app_dir in apps:
            compose = app_dir / "compose.yaml"
            assert compose.is_file(), f"{app_dir.name} has no compose file"
            document = yaml.safe_load(compose.read_text())
            assert "services" in document, f"{app_dir.name} declares no services"

    def test_no_app_bind_mounts_into_the_checkout(self, examples: Path) -> None:
        """The store's own advice, enforced on the store itself.

        A relative bind mount in an installed app writes into the store's git
        checkout. The example store must not model the thing its README tells
        people not to do -- except the docker socket, which is an absolute path
        and the entire point of a reverse proxy.
        """
        import yaml

        for compose in examples.glob("*/compose.yaml"):
            document = yaml.safe_load(compose.read_text())
            for name, service in document["services"].items():
                for volume in service.get("volumes", []):
                    source = str(volume).split(":")[0]
                    assert not source.startswith("."), (
                        f"{compose.parent.name}/{name} bind-mounts {source!r} "
                        f"relative to the app directory, which lands in the "
                        f"store's git checkout"
                    )


class _AdvancedOn:
    """A preference store with advanced mode already on.

    The Adopt button is advanced-only, so it does not exist to click until this
    is registered. Note what that means, and why the confirmation dialog has to
    exist: getting here is one click for any user, in the browser.
    """

    def get(self, key: str, default: object) -> object:
        return True if key == "advanced" else default

    def set(self, key: str, value: object) -> None:
        pass


class TestAdoptAsks:
    """Adopt is the only irreversible action, so it is the only one that asks.

    Being behind advanced mode is not a safeguard -- advanced mode is a
    self-service preference, not a boundary -- so the confirmation is the guard,
    and it has to actually stop the thing when the answer is no.
    """

    @pytest.fixture(autouse=True)
    def _restore_preferences(self):
        """A test that swaps the store must not leak it into the next one."""
        yield
        preferences.reset()

    async def test_the_button_does_not_adopt_until_confirmed(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        run(install_command(store.app("whoami"), compose_root)).check()
        link = compose_root / "whoami"

        preferences.use(lambda: _AdvancedOn())  # the Adopt button is advanced-only
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")  # the cards render on a timer

        user.find("Adopt").click()
        await user.should_see("Adopt whoami?")

        # The dialog is up, and nothing has happened yet.
        assert link.is_symlink(), "adopted before the user was asked"

        user.find("Cancel").click()
        assert link.is_symlink(), "Cancel adopted it anyway"

    async def test_the_dialog_shows_the_commands_it_will_run(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        """For an irreversible action, the commands are the explanation."""
        run(install_command(store.app("whoami"), compose_root)).check()

        preferences.use(lambda: _AdvancedOn())
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")  # the cards render on a timer

        user.find("Adopt").click()
        await user.should_see("There is no undo")
        await user.should_see("cp -r")

    async def test_confirming_actually_adopts(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        run(install_command(store.app("whoami"), compose_root)).check()
        link = compose_root / "whoami"

        preferences.use(lambda: _AdvancedOn())
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")  # the cards render on a timer

        user.find("Adopt").click()
        await user.should_see("Adopt whoami?")

        # "Adopt it" is the dialog's button; "Adopt" is the card's. They are worded
        # differently on purpose -- a confirmation whose button reads the same as
        # the one you just pressed is one you click through on reflex.
        user.find("Adopt it").click()

        # Clicking only *starts* the rm and the cp; they stream in the background.
        # Polling the filesystem is the honest way to wait for them, because the
        # filesystem is the thing under test -- waiting on a label would let a
        # broken adopt pass as long as it rendered.
        await _until(lambda: not link.is_symlink())

        assert not link.is_symlink(), "still a symlink after confirming"
        assert link.is_dir(), "the real copy was not made"
        assert (link / "compose.yaml").read_text() == WHOAMI

    async def test_the_button_has_a_tooltip_saying_it_cannot_be_undone(
        self, user: User, store, settings: AppStoreSettings, compose_root: Path
    ) -> None:
        """Hovering must tell you what it does before you click it.

        "Adopt" is a warm word that does not, on its own, say that updates stop
        forever -- so the tooltip has to, and the confirmation behind it has to
        as well.
        """
        run(install_command(store.app("whoami"), compose_root)).check()

        preferences.use(lambda: _AdvancedOn())
        web.build([_PinnedPlugin(settings, compose_root)])
        await user.open("/appstore")
        await user.should_see("whoami")

        # NiceGUI does not nest a tooltip inside its button: it renders it as a
        # sibling whose `target` prop points at the button's id. So find the
        # button, then find the tooltip aimed at it.
        [button] = [b for b in user.find(ui.button).elements if b.text == "Adopt"]
        [tooltip] = [
            t
            for t in user.find(ui.tooltip).elements
            if t.props.get("target") == f"#{button.html_id}"
        ]
        assert "never update again" in tooltip.text
        assert "Cannot be undone" in tooltip.text
