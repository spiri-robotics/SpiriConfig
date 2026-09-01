"""Bundling declared apps' container images into a provisioning repo.

``skopeo copy`` into one **shared OCI image layout** per repo
(:data:`OCI_DIR`), never one destination per app or per image. That single
choice is what gives de-duplication for free: an OCI layout's blobs live at
``blobs/sha256/<digest>``, named by their own content, so two images (or two
versions of one image) that share a base layer only ever cause that layer's
blob to be written once -- skopeo itself skips writing a blob that is
already there under its digest. There is no digest-tracking or dedup logic
in this module; the storage format does the work.

The blobs are the part worth keeping out of ordinary git history -- a
provisioning repo otherwise stays small text, and a multi-hundred-megabyte
layer blob committed directly would make every clone, fetch, and diff pay
for it forever. git-lfs is the fix already built for exactly this, so
:data:`LFS_PATTERN` puts every blob through it (see :func:`lfs_setup_commands`).
Manifests and the layout's own small JSON files (also technically under
``blobs/``) go through LFS too -- there is no principled small/large split
worth codifying, an OCI blob is exactly as big as it is.

Nothing here runs anything. Every function returns :class:`~spiriconfig.commands.Command`\\ s,
same discipline as the rest of this codebase (see ``docs/design.md``'s "We
shell out, on purpose") -- a caller (the Provisioning page's streaming
dialog, or a CLI command) shows each line before running it.

Consuming a bundle at apply time -- loading an image out of the OCI layout
into the applying device's container runtime instead of pulling it from the
network -- is not implemented here yet. See ``NOTES-usb-provisioning.md``'s
``apps/images/`` note.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from loguru import logger

from spiriconfig.commands import Command

from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_appstore.stores import App

log = logger.bind(plugin="appstore")

#: Where the repo's one shared OCI image layout lives, relative to the
#: repo root. One layout for every bundled image, not one per app -- see
#: the module docstring for why that single choice is the de-duplication.
OCI_DIR = "apps/images/oci"

#: The ``.gitattributes`` line that routes every blob in the layout
#: through git-lfs.
LFS_PATTERN = f"{OCI_DIR}/blobs/** filter=lfs diff=lfs merge=lfs -text"

#: Characters a docker/OCI tag may not contain, collapsed to ``-``. The
#: real rule is ``[a-zA-Z0-9_][a-zA-Z0-9._-]*``; this is deliberately
#: stricter (no leading-character special case) since every tag this
#: module builds starts with an app name, which is already a safe,
#: plain directory name (see ``_validate_name`` in
#: :mod:`spiriconfig_appstore.provision`).
_INVALID_TAG = re.compile(r"[^A-Za-z0-9_.-]+")


def image_refs(app: App) -> list[tuple[str, str]]:
    """Every ``image:`` reference `app`'s compose file declares, as
    ``(service name, image reference)`` pairs.

    An app is very often more than one container -- a service plus its
    database -- and every one of them needs bundling, not just the
    first. Reading the compose file as YAML and never writing it back is
    the same stance :meth:`~spiriconfig_appstore.stores.App.version`
    already takes; a malformed file just means "no images found here",
    the same "describe degrades, it does not crash" instinct the rest of
    this plugin's provisioning code follows.
    """
    try:
        document = yaml.safe_load(app.compose_file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read images from {}: {}", app.compose_file, exc)
        return []
    if not isinstance(document, dict):
        return []
    services = document.get("services")
    if not isinstance(services, dict):
        return []
    refs: list[tuple[str, str]] = []
    for name, service in services.items():
        if isinstance(service, dict) and isinstance(service.get("image"), str):
            refs.append((str(name), service["image"]))
    return refs


def tag_for(app_name: str, service: str) -> str:
    """A valid OCI tag naming `service` within `app_name`.

    Stable across repeated bundling -- the same app and service always
    produce the same tag, so re-running the bundle updates that tag's
    manifest pointer in place rather than accumulating a new tag every
    time an image is refreshed. The service name is folded in only when
    it differs from the app's own name (the common case: an app with one
    service named after itself), so the ordinary case gets the plain,
    readable ``whoami`` rather than ``whoami-whoami``.
    """
    raw = app_name if service == app_name else f"{app_name}-{service}"
    tag = _INVALID_TAG.sub("-", raw).strip("-.")
    return tag or "image"


def lfs_setup_commands(repo_path: Path, settings: AppStoreSettings) -> list[Command]:
    """``git lfs install``/``git lfs track`` for `repo_path`.

    Both are idempotent -- ``install --local`` only touches this repo's
    own ``.git/config``, and ``track`` only adds :data:`LFS_PATTERN` to
    ``.gitattributes`` if it is not already there -- so a caller can
    always include these ahead of the actual copies rather than track
    whether a previous bundle already ran them.
    """
    return [
        Command(argv=[settings.git_bin, "lfs", "install", "--local"], cwd=repo_path),
        Command(argv=[settings.git_bin, "lfs", "track", LFS_PATTERN], cwd=repo_path),
    ]


def ensure_oci_dir(repo_path: Path) -> None:
    """Create `repo_path`'s OCI layout directory if it isn't there yet.

    ``skopeo copy ... oci:<path>:<tag>`` will create the leaf directory
    of `path` itself, but not the parents above it -- a fresh repo has no
    ``apps/images/`` at all until something creates it, and skopeo's own
    error for that (``lstat .../apps/images: no such file or directory``)
    does not say so in a way that points at the fix. A plain `mkdir`,
    not a shown `Command`: creating an empty directory needs no review
    the way a network copy or a git commit does, the same reasoning
    every other staging helper in :mod:`spiriconfig_appstore.provision`
    (e.g. ``_stage_add``) already applies to its own ``.mkdir(parents=True,
    exist_ok=True)`` calls.
    """
    (repo_path / OCI_DIR).parent.mkdir(parents=True, exist_ok=True)


def copy_command(ref: str, repo_path: Path, tag: str, settings: AppStoreSettings) -> Command:
    """The ``skopeo copy`` for one image, into `repo_path`'s shared OCI
    layout (see :data:`OCI_DIR`) under `tag`.

    Re-running this for an image already bundled is cheap past the first
    run: skopeo only writes a blob under its digest if that digest is not
    already in the layout, so an unchanged image (or one sharing layers
    with something already bundled) mostly re-confirms what is already
    there rather than re-copying it.
    """
    destination = f"oci:{repo_path / OCI_DIR}:{tag}"
    return Command(argv=[settings.skopeo_bin, "copy", f"docker://{ref}", destination])


def bundle_commands(
    apps: list[App], repo_path: Path, settings: AppStoreSettings
) -> list[Command]:
    """The full plan to bundle every image `apps` need into `repo_path`.

    LFS setup once, then one ``skopeo copy`` per ``(app, service)``
    image, in a stable order (apps as given, each app's own services in
    compose-file order) so the same declared set always produces the
    same plan. Returned, not run -- see the module docstring.
    """
    commands = lfs_setup_commands(repo_path, settings)
    for app in apps:
        for service, ref in image_refs(app):
            commands.append(copy_command(ref, repo_path, tag_for(app.name, service), settings))
    return commands


__all__ = [
    "LFS_PATTERN",
    "OCI_DIR",
    "bundle_commands",
    "copy_command",
    "ensure_oci_dir",
    "image_refs",
    "lfs_setup_commands",
    "tag_for",
]
