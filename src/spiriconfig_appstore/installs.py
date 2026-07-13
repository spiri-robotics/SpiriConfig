"""Installing an app: making, reading, and removing a symlink.

An installed app is a symlink in the compose directory pointing at an app
directory inside a store's checkout::

    /srv/compose/whoami -> /var/lib/spiriconfig/stores/spiri-apps/whoami

Which means the docker plugin needs to know nothing about any of this. It walks
the compose directory looking for directories with compose files in them, a
symlink to a directory *is* a directory, and so an installed app is simply a
stack. ``spiriconfig docker up whoami`` works on it, ``cd /srv/compose/whoami
&& docker compose up -d`` works on it, and neither has any idea a store exists.

The symlink is also the only thing we create, which is what keeps the promise in
:doc:`design` that we do not create or delete the user's project directories. An
uninstall removes a symlink; the app, and anything the user put beside it, is
still there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from spiriconfig.commands import Command

from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_appstore.stores import App, Store, StoreError, stores

log = logger.bind(plugin="appstore")


@dataclass(frozen=True, slots=True)
class Install:
    """An app installed into the compose directory, as seen from the symlink."""

    name: str
    """The symlink's name, which is the compose project name."""

    link: Path
    """The symlink itself, in the compose directory."""

    target: Path
    """Where it points: an app directory inside a store checkout."""

    store: Store
    app_name: str
    """Which app in which store. Read back off the symlink; we store it nowhere."""

    def uninstall_command(self) -> Command:
        """Remove the symlink.

        ``rm``, with no ``-r`` and no ``-f``: this deletes a link and cannot
        delete a directory, so the worst a bug here can do is unlink something.
        The app's files stay in the store, and so does anything the user left in
        it -- their data outlives an uninstall, which is the behaviour they will
        assume anyway and the one they cannot recover from if we get it wrong.
        """
        return Command(argv=["rm", str(self.link)])

    def adopt_commands(self) -> list[Command]:
        """Replace the symlink with a real copy of the app's directory.

        The escape hatch, and the reason nobody is trapped by the symlink design.
        An adopted app is an ordinary compose project: it never updates again, it
        has no store, and the user owns it completely. It is what they would have
        had if they had run ``cp -r`` themselves, which is exactly what this runs.
        """
        return [
            Command(argv=["rm", str(self.link)]),
            Command(argv=["cp", "-r", str(self.target), str(self.link)]),
        ]


def _resolve(link: Path, settings: AppStoreSettings) -> Install | None:
    """Turn a symlink in the compose directory into an :class:`Install`, or None.

    Returns None for anything that is not a symlink into a configured store: a
    real directory the user made by hand, or a symlink they pointed somewhere
    else entirely. Both are perfectly legal stacks -- they are just not ours to
    talk about, and an app store that claimed them would be lying.
    """
    if not link.is_symlink():
        return None

    try:
        target = link.resolve(strict=True)
    except OSError as exc:
        # A dangling symlink: the store was deleted out from under it. Worth a
        # warning, but not worth crashing a listing over.
        log.warning("{} does not resolve: {}", link, exc)
        return None

    for store in stores(settings):
        if not store.path.exists():
            continue
        try:
            relative = target.relative_to(store.path.resolve())
        except ValueError:
            continue
        # Exactly one level deep: <store>/<app>. Anything else is not an app.
        if len(relative.parts) != 1:
            continue
        return Install(
            name=link.name,
            link=link,
            target=target,
            store=store,
            app_name=relative.parts[0],
        )
    return None


def installed(settings: AppStoreSettings, compose_dir: Path) -> list[Install]:
    """Every store-installed app in the compose directory, sorted by name."""
    if not compose_dir.is_dir():
        log.warning("compose directory does not exist: {}", compose_dir)
        return []
    found = (_resolve(child, settings) for child in sorted(compose_dir.iterdir()))
    return [install for install in found if install is not None]


def install_command(app: App, compose_dir: Path, name: str | None = None) -> Command:
    """Build the ``ln -s`` that installs ``app``, refusing to clobber anything.

    Checked before the command is built rather than letting ``ln`` fail, because
    the failure we most want to prevent is the one where the target is the user's
    own hand-made project of the same name. ``ln`` would refuse anyway, but it
    would refuse with "File exists", and the user deserves to be told which of
    the several quite different things went wrong.
    """
    link = app.link_path(compose_dir, name)

    if link.is_symlink():
        raise StoreError(f"{link.name!r} is already installed ({link} -> {link.resolve()})")
    if link.exists():
        raise StoreError(
            f"{link} already exists and is not a symlink, so it is not ours to "
            f"replace. Install it under a different name, or move it aside."
        )
    if not compose_dir.is_dir():
        raise StoreError(f"compose directory does not exist: {compose_dir}")

    return app.install_command(compose_dir, name)


def uninstall(settings: AppStoreSettings, compose_dir: Path, name: str) -> Install:
    """Look up an installed app by name, or raise :class:`StoreError`.

    Refuses anything that is not one of our symlinks. A user who made
    ``/srv/compose/whoami`` themselves, or adopted it, gets told to remove it
    themselves -- we did not create that directory, so we will not delete it.
    """
    link = compose_dir / name
    if not link.exists() and not link.is_symlink():
        raise StoreError(f"not installed: {name!r}")

    resolved = _resolve(link, settings)
    if resolved is None:
        raise StoreError(
            f"{link} is not a store install -- it is a real directory, or points "
            f"outside the configured stores. SpiriConfig did not create it and "
            f"will not delete it; remove it yourself if you mean to."
        )
    return resolved


__all__ = [
    "Install",
    "install_command",
    "installed",
    "uninstall",
]
