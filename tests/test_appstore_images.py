"""Tests for spiriconfig_appstore.images: building the skopeo/git-lfs plan
that bundles declared apps' container images into a provisioning repo.

No network, no live skopeo/git-lfs invocation -- these assert on the built
`Command`s, the same "test the command line, not a live daemon" convention
AGENTS.md describes for the docker plugin. `ensure_oci_dir` (a plain
filesystem side effect, not a command) gets one real-filesystem test.
"""

from __future__ import annotations

from pathlib import Path

from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_appstore.images import (
    LFS_PATTERN,
    OCI_DIR,
    bundle_commands,
    copy_command,
    ensure_oci_dir,
    image_refs,
    lfs_setup_commands,
    tag_for,
)
from spiriconfig_appstore.stores import App, Store

WHOAMI = """\
services:
  whoami:
    image: traefik/whoami:v1.10.1
"""

MULTI_SERVICE = """\
services:
  nextcloud:
    image: nextcloud:29
  db:
    image: postgres:16
"""

NO_IMAGE = """\
services:
  weird:
    build: .
"""


def _app(tmp_path: Path, name: str, compose_text: str) -> App:
    app_dir = tmp_path / name
    app_dir.mkdir()
    compose_file = app_dir / "compose.yaml"
    compose_file.write_text(compose_text)
    store = Store(
        slug="upstream",
        url="https://example.com/upstream.git",
        path=tmp_path,
        settings=AppStoreSettings(),
    )
    return App(store=store, name=name, path=app_dir, compose_file=compose_file)


class TestImageRefs:
    def test_a_single_service_app(self, tmp_path: Path) -> None:
        app = _app(tmp_path, "whoami", WHOAMI)
        assert image_refs(app) == [("whoami", "traefik/whoami:v1.10.1")]

    def test_a_multi_service_app_lists_every_image(self, tmp_path: Path) -> None:
        app = _app(tmp_path, "nextcloud", MULTI_SERVICE)
        assert image_refs(app) == [
            ("nextcloud", "nextcloud:29"),
            ("db", "postgres:16"),
        ]

    def test_a_service_with_no_image_is_skipped_not_crashed_on(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path, "weird", NO_IMAGE)
        assert image_refs(app) == []

    def test_an_unreadable_compose_file_is_an_empty_list(self, tmp_path: Path) -> None:
        app = _app(tmp_path, "broken", "not: valid: yaml: [")
        assert image_refs(app) == []


class TestTagFor:
    def test_a_service_named_after_its_app_is_the_plain_name(self) -> None:
        assert tag_for("whoami", "whoami") == "whoami"

    def test_a_different_service_is_qualified(self) -> None:
        assert tag_for("nextcloud", "db") == "nextcloud-db"

    def test_invalid_tag_characters_are_collapsed(self) -> None:
        assert tag_for("my app", "a/b") == "my-app-a-b"


class TestLfsSetupCommands:
    def test_install_then_track(self, tmp_path: Path) -> None:
        settings = AppStoreSettings(git_bin="git")
        commands = lfs_setup_commands(tmp_path, settings)
        assert [str(c.argv[1:3]) for c in commands] == [
            "['lfs', 'install']",
            "['lfs', 'track']",
        ]
        assert commands[0].argv == ["git", "lfs", "install", "--local"]
        assert commands[1].argv == ["git", "lfs", "track", LFS_PATTERN]
        assert all(c.cwd == tmp_path for c in commands)


class TestCopyCommand:
    def test_builds_a_skopeo_copy_into_the_shared_layout(self, tmp_path: Path) -> None:
        settings = AppStoreSettings(skopeo_bin="skopeo")
        command = copy_command("traefik/whoami:v1.10.1", tmp_path, "whoami", settings)
        assert command.argv == [
            "skopeo",
            "copy",
            "docker://traefik/whoami:v1.10.1",
            f"oci:{tmp_path / OCI_DIR}:whoami",
        ]

    def test_a_custom_skopeo_bin_is_honoured(self, tmp_path: Path) -> None:
        settings = AppStoreSettings(skopeo_bin="/opt/bin/skopeo")
        command = copy_command("busybox:1.36", tmp_path, "busybox", settings)
        assert command.argv[0] == "/opt/bin/skopeo"


class TestBundleCommands:
    def test_lfs_setup_first_then_one_copy_per_image(self, tmp_path: Path) -> None:
        settings = AppStoreSettings()
        whoami = _app(tmp_path, "whoami", WHOAMI)
        nextcloud = _app(tmp_path, "nextcloud", MULTI_SERVICE)

        commands = bundle_commands([whoami, nextcloud], tmp_path, settings)

        assert commands[0].argv[1:3] == ["lfs", "install"]
        assert commands[1].argv[1:3] == ["lfs", "track"]
        copies = commands[2:]
        assert len(copies) == 3
        assert "docker://traefik/whoami:v1.10.1" in copies[0].argv
        assert copies[0].argv[-1].endswith(":whoami")
        assert "docker://nextcloud:29" in copies[1].argv
        assert copies[1].argv[-1].endswith(":nextcloud")
        assert "docker://postgres:16" in copies[2].argv
        assert copies[2].argv[-1].endswith(":nextcloud-db")

    def test_no_apps_is_just_the_lfs_setup(self, tmp_path: Path) -> None:
        commands = bundle_commands([], tmp_path, AppStoreSettings())
        assert len(commands) == 2  # install, track -- no copies

    def test_every_command_is_shown_as_a_pasteable_line(self, tmp_path: Path) -> None:
        """The whole point of returning `Command`s rather than running
        anything -- `str(command)` must actually render, the same
        guarantee every other command-building function in this codebase
        is held to (see ``docs/design.md``'s "We shell out, on purpose")."""
        whoami = _app(tmp_path, "whoami", WHOAMI)
        commands = bundle_commands([whoami], tmp_path, AppStoreSettings())
        for command in commands:
            assert str(command)  # does not raise, is non-empty


class TestEnsureOciDir:
    def test_creates_the_parent_of_the_oci_leaf(self, tmp_path: Path) -> None:
        """Regression: `skopeo copy ... oci:<path>:<tag>` creates the
        leaf directory of `<path>` itself, but not the parents above it
        -- a fresh repo with nothing bundled yet has no `apps/images/`
        at all, and skopeo's own failure for that
        (`lstat .../apps/images: no such file or directory`) doesn't
        point at the fix."""
        ensure_oci_dir(tmp_path)
        assert (tmp_path / "apps" / "images").is_dir()
        assert not (tmp_path / OCI_DIR).exists()  # the leaf is skopeo's to create

    def test_is_idempotent(self, tmp_path: Path) -> None:
        ensure_oci_dir(tmp_path)
        ensure_oci_dir(tmp_path)  # does not raise
        assert (tmp_path / "apps" / "images").is_dir()
