"""App store's contribution to provisioning: staging, describing, and
applying ``apps/`` entries (installed apps, with optional per-app
settings) and ``apps/stores.toml`` (registered app stores) in a
provisioning repo.

See ``docs/provisioning.md`` for the interface this implements,
``NOTES-usb-provisioning.md`` for the decided on-disk layout, and its
"Mockup: the actual designed screens" section for the UI this renders --
a full list of every app in a configured store, an Add button and a
Remove button per row (not a single status pill or a mode toggle -- see
``render_patch_block``'s and ``_app_row``'s docstrings for why), not a
name-entry form.

Bundling declared apps' container images into ``apps/images/`` is
:mod:`spiriconfig_appstore.images` -- a "Bundle images" action here shows
and streams its plan the same way every other real-command-running
button in this codebase does (see :func:`_bundle_dialog`). Consuming a
bundle at apply time -- loading an image from the repo instead of
pulling it over the network -- is not implemented yet.

An app's settings reuse :mod:`spiriconfig_docker.settings` and
:mod:`spiriconfig_docker.widgets` wholesale -- the same fields, the same
widgets, the same validation the Docker page's own settings dialog uses.
The only thing that changes is the write target: instead of a live
stack's ``.env``, the rendered bytes land in the repo at
``apps/<mode>/<name>/env``. This is why ``apps/add/<name>`` and
``apps/state/<name>`` are *directories* here, unlike ``users/``'s
add/state entries -- there is real, optional content to hold. An entry
with no settings configured still gets an empty ``env`` file rather than
an empty directory, for the same "an empty directory is invisible to
git" reason ``users/remove/<name>`` is a marker file at all.

Nothing here touches this machine's own installs outside of :func:`apply`,
the one place expected to act on a real system -- and even then, only by
returning commands, never running them. See
:mod:`spiriconfig_appstore.installs`/:mod:`spiriconfig_appstore.stores` for
what "installed" and "a store" actually mean.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from nicegui import ui

from spiriconfig import advanced, terminal, theme
from spiriconfig.commands import Command, stream_pty

from spiriconfig_appstore import images, installs
from spiriconfig_appstore.config import AppStoreSettings, appstore_settings
from spiriconfig_appstore.stores import App, Store, StoreError, find_app, store_for_url
from spiriconfig_appstore.stores import stores as list_stores
from spiriconfig_docker import env as docker_env
from spiriconfig_docker import widgets
from spiriconfig_docker.config import docker_settings
from spiriconfig_docker.settings import Field, SettingsError, check_values, declared

if TYPE_CHECKING:
    from spiriconfig_provision.contract import ApplyStep, Change, StagedRepo

log = logger.bind(plugin="appstore")

#: Written at the top of an ``env`` file this creates, matching the wording
#: the design mockup shows for it -- distinct from
#: :data:`spiriconfig_docker.settings._HEADER`'s wording, since this file is
#: read by a *device applying the repo*, not by a developer editing a live
#: stack's ``.env`` directly.
_ENV_HEADER = "# written when this app is applied on the target device\n\n"


# ---------------------------------------------------------------------------
# apps/add, apps/remove, apps/state -- installed apps, by name
# ---------------------------------------------------------------------------


def _validate_name(name: str | None) -> str:
    """A safe, plain entry name -- no path separators, since that would
    turn one staged entry into nested directories and break
    :func:`describe`'s assumption that every entry is exactly
    ``apps/<verb>/<name>`` (or ``apps/<verb>/<name>/env``). An app name
    ambiguous across configured stores (see
    :func:`~spiriconfig_appstore.stores.find_app`) isn't resolvable here
    either way; it fails loudly at apply time instead.

    `name` is `str | None` because a `ui.select`'s value is `None` before
    anything is picked -- and, having rejected a value not in its
    `options` (see `render_baseline_block`'s dropdown), `None` again if
    one was set from outside a real pick.
    """
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name:
        raise ValueError(f"{name!r} is not a valid app name")
    return name


def _staged(apps_dir: Path, verb: str) -> list[str]:
    """Names currently staged under ``add/``, ``remove/``, or ``state/``.
    ``remove/`` entries are bare marker *files* (nothing to configure for
    an app being uninstalled); ``add/``/``state/`` entries are
    directories holding an ``env`` file, empty or not."""
    directory = apps_dir / verb
    if not directory.is_dir():
        return []
    is_entry = Path.is_file if verb == "remove" else Path.is_dir
    return sorted(p.name for p in directory.iterdir() if is_entry(p))


def _unstage(apps_dir: Path, verb: str, name: str) -> None:
    entry = apps_dir / verb / name
    if entry.is_dir():
        shutil.rmtree(entry)
    elif entry.is_file():
        entry.unlink()


def _env_entry(verb_dir: Path, name: str) -> Path:
    return verb_dir / name / "env"


def _stage_add(apps_dir: Path, name: str) -> None:
    """Mark ``name`` for install, with an empty ``env`` if it doesn't
    already have one -- see the module docstring for why this is a
    directory, not a bare file. Cancels a staged removal of the same
    name; leaves an existing ``env`` (from a previous stage-then-unstage)
    alone rather than clobbering it."""
    entry = _env_entry(apps_dir / "add", name)
    entry.parent.mkdir(parents=True, exist_ok=True)
    if not entry.exists():
        entry.touch()
    _unstage(apps_dir, "remove", name)


def _stage_remove(apps_dir: Path, name: str) -> None:
    """Mark ``name`` for removal. Cancels a staged add of the same name."""
    entry_dir = apps_dir / "remove"
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / name).touch()
    _unstage(apps_dir, "add", name)


def _stage_state_add(state_dir: Path, name: str) -> None:
    """Declare, or re-declare, one entry of the exact set. No "cancel a
    staged remove" step: `state/` has no `remove/` counterpart, there is
    only ever the current list."""
    entry = _env_entry(state_dir, name)
    entry.parent.mkdir(parents=True, exist_ok=True)
    if not entry.exists():
        entry.touch()


def _drop_from_state(state_dir: Path, name: str) -> None:
    """Remove `name` from the declared set -- the entire removal
    mechanism, the same as `users/state/`."""
    entry = state_dir / name
    if entry.is_dir():
        shutil.rmtree(entry)


# ---------------------------------------------------------------------------
# Per-app settings: apps/<add|state>/<name>/env, reusing spiriconfig_docker
# ---------------------------------------------------------------------------


def _has_settings(app: App) -> bool:
    """Whether `app` declares a settings form -- swallows a malformed
    declaration the same way :func:`spiriconfig_docker.settings.has_settings`
    does, so one broken app's typo doesn't take a whole list down."""
    try:
        return bool(declared(app.compose_file))
    except SettingsError as exc:
        log.warning("{} has a broken settings declaration: {}", app.name, exc)
        return False


def _render_env(fields: list[Field], original: str | None, values: dict[str, str]) -> str:
    """The exact bytes a settings save would write -- `check_values` for
    the same validation :meth:`spiriconfig_docker.settings.StackSettings.save`
    applies, `env.patch` for the same patch-in-place behaviour, and
    `_ENV_HEADER` only for a file that doesn't exist yet."""
    checked = check_values(fields, values)
    text = docker_env.patch(original or "", checked)
    if original is None and text:
        text = _ENV_HEADER + text
    return text


async def _settings_dialog(verb_dir: Path, app: App, refresh) -> None:
    """Edit `app`'s declared settings, writing into
    ``verb_dir/<app.name>/env`` in the repo instead of a live stack's
    ``.env``. Same fields, same widgets
    (:func:`spiriconfig_docker.widgets.form`/`values`), same validation
    as the Docker page's own settings dialog -- no new form-building code,
    only a different write target. Unlike
    :meth:`~spiriconfig_docker.settings.StackSettings.save`, there is no
    ``docker compose config`` verification: this repo may be authored on
    a laptop with no device, and no docker, in reach at all.
    """
    try:
        fields = declared(app.compose_file)
    except SettingsError as exc:
        ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
        return
    if not fields:
        ui.notify(f"{app.name} declares no settings.", type="warning")
        return

    env_path = _env_entry(verb_dir, app.name)
    original = env_path.read_text() if env_path.is_file() else None
    current_raw = docker_env.read(env_path)
    current = {f.env: current_raw.get(f.env, f.default) for f in fields}

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label(f"{app.name} — settings").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-4"):
            bound = widgets.form(fields, current)

        def do_save() -> None:
            try:
                text = _render_env(fields, original, widgets.values(bound))
            except SettingsError as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(text)
            dialog.close()
            ui.notify(f"Saved {env_path}", type="positive")
            refresh()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save to repo", on_click=do_save).props(
                "color=primary"
            ).mark("provision-apps-settings-save")
    dialog.open()


# ---------------------------------------------------------------------------
# apps/stores.toml -- app stores this profile's devices should have cloned
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclaredStore:
    """One entry of ``apps/stores.toml``."""

    url: str
    ref: str | None = None


def _stores_path(repo_path: Path) -> Path:
    return repo_path / "apps" / "stores.toml"


def _parse_stores_toml(path: Path) -> list[DeclaredStore]:
    """Read `path`, or an empty list for a missing or unparsable file --
    same "degrade, don't crash" stance
    :meth:`~spiriconfig_appstore.stores.Store._capture` takes with a
    broken git command."""
    if not path.is_file():
        return []
    try:
        document = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not read {}: {}", path, exc)
        return []
    entries = document.get("store", [])
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            result.append(DeclaredStore(url=entry["url"], ref=entry.get("ref")))
    return result


def _write_stores_toml(path: Path, entries: list[DeclaredStore]) -> None:
    """Rewrite the whole file -- `apps/stores.toml` is a small declared
    list, edited as a whole, the same "no partial diff" stance
    `_stage_state_add`'s baseline callers take. `json.dumps` for the
    string values is a shortcut, not a hack: TOML basic strings follow
    JSON's escaping rules closely enough for a URL or a ref name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not entries:
        path.write_text("")
        return
    lines: list[str] = []
    for entry in entries:
        lines.append("[[store]]")
        lines.append(f"url = {json.dumps(entry.url)}")
        if entry.ref:
            lines.append(f"ref = {json.dumps(entry.ref)}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip("\n") + "\n")


def _add_store(path: Path, url: str, ref: str | None) -> None:
    entries = [e for e in _parse_stores_toml(path) if e.url != url]
    entries.append(DeclaredStore(url=url, ref=ref or None))
    _write_stores_toml(path, entries)


def _remove_store(path: Path, url: str) -> None:
    entries = [e for e in _parse_stores_toml(path) if e.url != url]
    _write_stores_toml(path, entries)


def _ensure_store_declared(apps_dir: Path, url: str) -> bool:
    """Register `url` in ``apps/stores.toml`` if it isn't already there.
    Returns whether it actually added one, so a caller only bothers
    refreshing the stores list on a real change.

    Staging an app for install without its store being declared would
    build a repo that can't actually be applied: a device reconciles
    ``apps/add/`` with `find_app`, which only sees stores
    `apps/stores.toml` told it to clone (see :func:`_apply_stores`) --
    an app whose store was only ever configured on *this* laptop, never
    declared in the repo, resolves to nothing on a fresh device.
    """
    path = apps_dir / "stores.toml"
    if any(entry.url == url for entry in _parse_stores_toml(path)):
        return False
    _add_store(path, url, None)
    return True


def _render_stores_block(repo: StagedRepo) -> Callable[[], None]:
    """Returns its own `refresh`, so :func:`render_patch_block` can call
    it after :func:`_ensure_store_declared` adds an entry from the app
    list above -- staging an app can change this list without anyone
    touching the "Add store" field."""
    path = _stores_path(repo.path)

    ui.label("App stores").classes("font-bold")
    ui.label(
        "Stores this profile's devices should have cloned. Saving rewrites "
        "the whole list -- there's no separate remove step."
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-end gap-2"):
        url_input = (
            ui.input("Store URL")
            .classes("grow")
            .props("outlined")
            .mark("provision-apps-store-url")
        )
        ref_input = (
            ui.input("Ref (optional)")
            .classes("w-32")
            .props("outlined")
            .mark("provision-apps-store-ref")
        )
        add_button = (
            ui.button("Add store", icon="add")
            .props("color=primary")
            .mark("provision-apps-store-add")
        )

    stores_column = ui.column().classes("w-full gap-1")

    def refresh() -> None:
        stores_column.clear()
        entries = _parse_stores_toml(path)
        with stores_column:
            if not entries:
                ui.label("No stores declared.").classes("text-sm text-gray-500")
            for entry in entries:
                with ui.row().classes("items-center gap-2"):
                    label = entry.url + (f" @ {entry.ref}" if entry.ref else "")
                    ui.label(label).classes("text-sm grow font-mono")
                    ui.button(
                        icon="close",
                        on_click=lambda u=entry.url: (_remove_store(path, u), refresh()),
                    ).props("flat dense round")

    def do_add() -> None:
        if not url_input.value:
            ui.notify("Enter a store URL.", type="warning")
            return
        _add_store(path, url_input.value, ref_input.value)
        url_input.value = ""
        ref_input.value = ""
        refresh()

    add_button.on_click(do_add)
    refresh()
    return refresh


# ---------------------------------------------------------------------------
# Bundling images: shared between patch and baseline
# ---------------------------------------------------------------------------


def _declared_apps(repo: StagedRepo, settings: AppStoreSettings) -> list[App]:
    """Every app currently declared in `repo`, resolved to real `App`s --
    ``apps/add/`` for patch, ``apps/state/`` for baseline, the same verb
    :func:`apply` reads. A name that no longer resolves (a store
    re-cloned to a revision that dropped it) is skipped with a warning
    rather than failing the whole bundle over one broken entry, the same
    "degrade, don't crash" stance :func:`_has_settings` already takes.
    """
    verb = "state" if repo.remote == "baseline" else "add"
    names = _staged(repo.path / "apps", verb)
    resolved = []
    for name in names:
        try:
            resolved.append(find_app(settings, name))
        except StoreError as exc:
            log.warning("could not resolve {} for image bundling: {}", name, exc)
    return resolved


async def _bundle_dialog(repo: StagedRepo, settings: AppStoreSettings) -> None:
    """Show, then run, the plan :func:`spiriconfig_appstore.images.bundle_commands`
    builds for every app `repo` currently declares -- the same
    show-the-command-then-stream-its-output treatment every other
    action-running dialog in SpiriConfig gives a command before running
    it (see ``spiriconfig_docker.web._run_in_dialog``,
    ``spiriconfig_appstore.web._run_in_dialog``). A local copy rather
    than a shared import: every plugin's own web-facing module keeps one,
    the established pattern here, not a coincidence to fold away.

    A separate, explicit action -- not wired into staging or Review &
    Sign -- because a bundle can mean real network transfer and real
    time; stopping to authorize a plan that big is exactly the "read
    before you run it" discipline this treatment exists for.
    """
    apps = _declared_apps(repo, settings)
    commands = images.bundle_commands(apps, repo.path, settings)
    if len(commands) <= len(images.lfs_setup_commands(repo.path, settings)):
        ui.notify("Nothing declared to bundle images for.", type="warning")
        return
    images.ensure_oci_dir(repo.path)

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label("Bundle images").classes("text-lg font-bold")

        with advanced.only(), ui.column().classes(
            f"w-full gap-1 {theme.COMMAND_CLASS} p-2"
        ):
            for command in commands:
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(str(command)).classes("font-mono text-xs grow break-all")
                    ui.button(
                        icon="content_copy",
                        on_click=lambda c=command: ui.clipboard.write(str(c)),
                    ).props("flat dense round").tooltip("Copy command")

        output = terminal.terminal()
        close = ui.button("Close", on_click=dialog.close).props("flat")
        close.disable()

    dialog.open()
    try:
        for command in commands:
            failed = False
            async for chunk in stream_pty(
                command, log=log, rows=terminal.TERMINAL_ROWS, columns=terminal.TERMINAL_COLUMNS
            ):
                output.write(chunk)
                if b"[command exited with code " in chunk:
                    failed = True
            if failed:
                output.write("\r\n[stopped: the command above failed]\r\n")
                break
    except Exception as exc:  # noqa: BLE001 - surface any failure in the dialog
        log.exception("image bundle failed")
        output.write(f"\r\n[error] {exc}\r\n")
    finally:
        close.enable()

    await dialog
    dialog.delete()


def _render_bundle_row(repo: StagedRepo, settings: AppStoreSettings) -> None:
    ui.label(
        "Bundle every declared app's container image into this repo via "
        "skopeo, deduplicated and stored through git-lfs -- see "
        "apps/images/oci/. A separate step, not automatic: it can mean "
        "real network transfer."
    ).classes("text-xs text-gray-500")
    ui.button(
        "Bundle images",
        icon="inventory_2",
        on_click=lambda: _bundle_dialog(repo, settings),
    ).props("flat dense").mark(f"provision-apps-bundle-{repo.remote}")


# ---------------------------------------------------------------------------
# Baseline: shared declared-list building blocks
# ---------------------------------------------------------------------------


def _catalog(settings: AppStoreSettings) -> dict[str, tuple[Store, App]]:
    """Every app across every configured, cloned store, keyed by name --
    the one source :func:`render_baseline_block` picks from. An app whose
    name is ambiguous across stores collides here the same way it does
    for :func:`~spiriconfig_appstore.stores.find_app`; that only matters
    once someone actually tries to stage it, the same "fails loudly at
    apply time, not here" stance :func:`_validate_name` already takes.
    """
    return {app.name: (store, app) for store in list_stores(settings) for app in store.apps()}


def _catalog_row(
    sign: str,
    name: str,
    entry: tuple[Store, App] | None,
    path_suffix: str,
    settings_dir: Path | None,
    on_dismiss: Callable[[], None],
    refresh: Callable[[], None],
    marker_prefix: str,
) -> None:
    """One row of a staged or declared list -- shared by
    :func:`render_patch_block`'s staged list and
    :func:`render_baseline_block`'s declared list: an optional +/− sign,
    the name, its store in faint text, the ``apps/...`` path it writes, an
    optional settings gear, and a dismiss button.

    `entry` is `None` for a name no longer in any configured store's
    catalog (the store was re-cloned to a revision that dropped it, say)
    -- the row still renders, just without a store label or a settings
    gear, rather than vanishing or crashing; the underlying entry is still
    there on disk either way, and dismissing it still works. `settings_dir`
    is `None` for a row with nothing to configure (a staged removal): no
    gear is shown regardless of what `entry` says.
    """
    with ui.row().classes("items-center gap-2"):
        if sign:
            ui.label(sign).classes("font-mono w-4")
        store, app = entry if entry is not None else (None, None)
        store_label = f" · {store.slug}" if store else ""
        ui.label(f"{name}{store_label} — {path_suffix}").classes("text-sm grow")
        if settings_dir is not None and app is not None and _has_settings(app):
            ui.button(
                icon="settings",
                on_click=lambda: _settings_dialog(settings_dir, app, refresh),
            ).props("flat dense round").mark(f"{marker_prefix}-settings-{name}")
        ui.button(icon="close", on_click=on_dismiss).props("flat dense round").mark(
            f"{marker_prefix}-dismiss-{name}"
        )


# ---------------------------------------------------------------------------
# Patch remote: staging block
# ---------------------------------------------------------------------------


def _already_staged_dialog(name: str, verb: str, apps_dir: Path, refresh: Callable[[], None]) -> None:
    """Shown when Add is pressed on an app already staged Add, or Remove
    on one already staged Remove.

    An earlier design let that click land on a mode toggle + a shared
    "Stage" button, which read as an unlabelled pair of tabs rather than
    two distinct actions -- direct feedback was that Add and Remove
    should each be their own button. But a per-row Add button that's
    already in the "added" state has nothing left to *do* on a plain
    click; rather than have it silently do nothing (which reads as
    broken, not as "already done"), it opens this: says so, and offers
    the one thing actually left to do here -- unstage.
    """
    label = "staged to be installed" if verb == "add" else "staged to be removed"
    with ui.dialog() as dialog, ui.card():
        ui.label(f"{name} is already {label}.")
        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=dialog.close).props("flat")

            def do_unstage() -> None:
                _unstage(apps_dir, verb, name)
                dialog.close()
                refresh()

            ui.button("Unstage", on_click=do_unstage).props("color=negative").mark(
                f"provision-apps-unstage-{name}"
            )
    dialog.open()


def _app_row(
    apps_dir: Path,
    store: Store,
    app: App,
    refresh: Callable[[], None],
    on_store_declared: Callable[[], None],
) -> None:
    """One row of the patch catalog list: name, store, an Add button and
    a Remove button, each relabelled to a past-tense state (``Added`` /
    ``Removed``) once it's the one in effect -- so the button reads as
    what's true right now, not as an action that's still waiting to be
    taken, which is what a bare ``Add`` label on an already-staged row
    was read as. See :func:`_already_staged_dialog` for what a click on
    the already-true one does instead of nothing.
    """
    status = (
        "add"
        if app.name in _staged(apps_dir, "add")
        else "remove"
        if app.name in _staged(apps_dir, "remove")
        else "none"
    )

    def stage(verb: str) -> None:
        if status == verb:
            _already_staged_dialog(app.name, verb, apps_dir, refresh)
            return
        if verb == "add":
            _stage_add(apps_dir, app.name)
            if _ensure_store_declared(apps_dir, store.url):
                on_store_declared()
        else:
            _stage_remove(apps_dir, app.name)
        refresh()

    # Translucent over the page's own background rather than a fixed
    # Tailwind shade -- `bg-blue-50`/`bg-red-50` are a light-mode colour
    # each, and this app runs with `dark=None` (follow the OS). Same
    # `color-mix` over a Quasar CSS variable `spiriconfig.theme`'s own
    # tint uses, so it reads correctly in both.
    tint = {"add": "var(--q-primary)", "remove": "var(--q-negative)"}.get(status)
    row = ui.row().classes("w-full items-center gap-3 py-2 px-3 border-b")
    if tint:
        row.style(f"background-color: color-mix(in srgb, {tint} 12%, transparent)")
    with row:
        with ui.column().classes("gap-0 grow"):
            ui.label(app.name).classes("text-sm font-semibold")
            ui.label(f"{store.slug} · {app.version()}").classes("text-xs text-gray-500")
        if status == "add" and _has_settings(app):
            ui.button(
                icon="settings",
                on_click=lambda: _settings_dialog(apps_dir / "add", app, refresh),
            ).props("flat dense round").mark(f"provision-apps-settings-{app.name}")
        add_props = "dense no-caps" + (" color=primary" if status == "add" else " outline")
        ui.button(
            "Added" if status == "add" else "Add", on_click=lambda: stage("add")
        ).props(add_props).mark(f"provision-apps-add-{app.name}")
        remove_props = "dense no-caps" + (" color=negative" if status == "remove" else " outline")
        ui.button(
            "Removed" if status == "remove" else "Remove", on_click=lambda: stage("remove")
        ).props(remove_props).mark(f"provision-apps-remove-{app.name}")


def render_patch_block(repo: StagedRepo) -> None:
    """The add/remove staging list, plus the store list, for ``apps/``,
    on the Provisioning page.

    This *is* the entire write path -- see
    :meth:`spiriconfig_provision.contract.ProvisioningContributor.provision_patch_block`.
    There is no callback into ``spiriconfig_provision``; staging happens
    the moment Add, Remove, or "Add store" is pressed. Every app in every
    configured store is listed, not just staged ones -- see
    ``NOTES-usb-provisioning.md``'s mockup section for why this isn't a
    name-entry form. Each row is built by :func:`_app_row`.
    """
    apps_dir = repo.path / "apps"
    settings = appstore_settings()
    catalog = [(store, app) for store in list_stores(settings) for app in store.apps()]

    ui.label(
        "Every app in a configured store — Add stages an install, Remove "
        "stages an uninstall. Nothing here touches this device -- only "
        "the apps a repo built from this staging ends up declaring."
    ).classes("text-sm text-gray-500")

    list_column = ui.column().classes("w-full gap-0 border rounded-borders")
    # Populated once `_render_stores_block` has rendered, below -- staging
    # Add can auto-register a store (see `_ensure_store_declared`), and
    # this is how that tells the stores list to redraw itself. A plain
    # dict rather than `nonlocal` so `refresh()` can read whatever's in
    # it at call time, including calls that happen before the stores
    # block exists yet (the initial `refresh()` a few lines down).
    refs: dict[str, Callable[[], None]] = {}

    def refresh() -> None:
        list_column.clear()
        with list_column:
            if not catalog:
                ui.label("No apps in any configured store.").classes(
                    "text-sm text-gray-500 p-3"
                )
            for store, app in catalog:
                _app_row(apps_dir, store, app, refresh, on_store_declared=lambda: refs["stores"]())

    refresh()

    ui.separator()
    refs["stores"] = _render_stores_block(repo)

    ui.separator()
    _render_bundle_row(repo, settings)


# ---------------------------------------------------------------------------
# Baseline remote: exact-set editor
# ---------------------------------------------------------------------------


def render_baseline_block(repo: StagedRepo) -> None:
    """The exact-set editor for ``apps/state/``, plus the store list, on
    the Provisioning page.

    Same shape as :func:`render_patch_block` above, minus the mode toggle
    -- one polarity, read straight off ``apps/state/`` on every render, no
    session-scoped "staged" distinction. Also declares its own
    ``apps/stores.toml``: baseline and patch are separate git repos (see
    ``docs/provisioning.md``'s "Profiles" section), so a baseline that
    declares an app needs its store registered here too, exactly as patch
    does -- a device applying only a baseline profile has no patch
    ``stores.toml`` to fall back on.
    """
    state_dir = repo.path / "apps" / "state"
    apps_dir = repo.path / "apps"
    settings = appstore_settings()
    catalog = _catalog(settings)

    ui.label(
        "The apps this device should have installed. Saving removes any "
        "app not listed here -- there is no separate remove step."
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-end gap-2"):
        name_input = (
            ui.select(sorted(catalog), label="App name", with_input=True)
            .classes("grow")
            .props("outlined")
            .mark("provision-apps-baseline-name")
        )
        add_button = (
            ui.button("Add to set", icon="add")
            .props("color=primary")
            .mark("provision-apps-baseline-add")
        )

    declared_column = ui.column().classes("w-full gap-1")
    refs: dict[str, Callable[[], None]] = {}

    def refresh_declared() -> None:
        declared_column.clear()
        names = _staged(apps_dir, "state")
        with declared_column:
            if not names:
                ui.label("Nothing declared yet.").classes("text-sm text-gray-500")
            for name in names:
                _catalog_row(
                    "",
                    name,
                    catalog.get(name),
                    "apps/state",
                    state_dir,
                    on_dismiss=lambda n=name: (
                        _drop_from_state(state_dir, n),
                        refresh_declared(),
                    ),
                    refresh=refresh_declared,
                    marker_prefix="provision-apps-baseline",
                )

    def do_add() -> None:
        try:
            name = _validate_name(name_input.value)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        # Belt and suspenders on top of the dropdown-only field above: the
        # catalog can change between page load and this click (a store
        # re-cloned, an app removed upstream), so re-check here rather
        # than trust that the select could only have offered a real name.
        if name not in catalog:
            ui.notify(
                f"{name!r} is not in any configured, cloned store.",
                type="negative",
            )
            return
        _stage_state_add(state_dir, name)
        if _ensure_store_declared(apps_dir, catalog[name][0].url):
            refs["stores"]()
        name_input.value = ""
        refresh_declared()

    add_button.on_click(do_add)
    refresh_declared()

    ui.separator()
    refs["stores"] = _render_stores_block(repo)

    ui.separator()
    _render_bundle_row(repo, settings)


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def _describe_settings(name: str, env_path: Path) -> str:
    """What `env_path` actually sets, rendered for a `Change.detail` --
    e.g. ``"NEXTCLOUD_DOMAIN=cloud.example.com, NEXTCLOUD_ADMIN_PASSWORD=(hidden)"``,
    or ``""`` for no file, an empty one, or an app with no settings.

    A password-widget field's value is never shown, only that it was set
    -- the same "never let a secret end up somewhere it might be read
    back, screenshotted, or grepped out of a log" stance
    :data:`spiriconfig_provision.contract.ApplyStep.input` exists for, and
    the reason ``users/``'s own describe never echoes a password either.
    Looking up which fields are secret means resolving `name` back to an
    app and its declared settings, which can fail (a store re-cloned to a
    different revision, an ambiguous name) -- swallowed here, the same
    "describe degrades, it does not crash" stance :func:`_has_settings`
    already takes, since a commit message is not the place to surface a
    lookup error.
    """
    if not env_path.is_file():
        return ""
    values = docker_env.read(env_path)
    if not values:
        return ""
    secret_envs: set[str] = set()
    try:
        app = find_app(appstore_settings(), name)
        secret_envs = {f.env for f in declared(app.compose_file) if f.widget == "password"}
    except (StoreError, SettingsError) as exc:
        log.warning("could not resolve {}'s settings fields for describe: {}", name, exc)
    return ", ".join(
        f"{key}={'(hidden)' if key in secret_envs else value}" for key, value in values.items()
    )


def _describe_patch(repo: StagedRepo, path: str, verb: str, name: str) -> Change | None:
    """Describe one ``apps/add/`` or ``apps/remove/`` entry -- kept apart
    from :func:`_describe_baseline` so neither has to branch around the
    other's shape on disk.

    An ``add/`` path can reach here even though it no longer exists:
    `git status` reports a deletion the same as a new path, and unstaging
    a previously-committed add -- dismissing it, or staging a remove over
    it, see :func:`_stage_remove`'s call to :func:`_unstage` -- deletes
    ``apps/add/<name>/env``. Describing that as "installed" would be
    backwards, and, worse, would sit right next to the freshly staged
    ``apps/remove/<name>`` entry's own, contradicting "removed" line in
    the same Review & Sign list -- exactly the "add whoami" / "remove
    whoami" pair a signer would (rightly) find alarming. So this checks
    the path still exists before claiming an install, the same "read
    reality off disk, don't infer it from which directory a path is
    under" check :func:`_describe_baseline` already makes for ``state/``.
    """
    from spiriconfig_provision.contract import Change

    if verb == "add":
        if not (repo.path / path).exists():
            return Change(
                "unstage",
                f"app {name} install withdrawn",
                f"**{name}** is no longer staged to install -- its earlier "
                "apps/add entry was withdrawn this patch",
            )
        settings = _describe_settings(name, repo.path / path)
        detail = f"Installs **{name}**"
        if settings:
            detail += f", setting {settings}"
        return Change("add", f"app {name} installed", detail)
    if verb == "remove":
        return Change("remove", f"app {name} removed", f"Removes app **{name}**")
    return None


def _describe_baseline(repo: StagedRepo, path: str, name: str) -> Change:
    """Describe one ``apps/state/`` entry. Same "added"/"removed"
    vocabulary as the patch side, and the same presence-on-disk trick
    ``users/state/`` describes with -- see
    :func:`spiriconfig_users.provision._describe_baseline`. `path` names
    the entry's ``env`` file, so "still exists" is checked one level
    below the app name itself."""
    from spiriconfig_provision.contract import Change

    if not (repo.path / path).exists():
        return Change(
            "remove",
            f"app {name} removed",
            f"**{name}** is removed from the certified app set",
        )
    settings = _describe_settings(name, repo.path / path)
    detail = f"**{name}** is in the certified app set"
    if settings:
        detail += f", setting {settings}"
    return Change("add", f"app {name} installed", detail)


def _describe_stores(repo: StagedRepo, path: str) -> Change:
    """Describe ``apps/stores.toml`` as a whole -- it has no per-entry
    add/remove marker of its own (see the module docstring), so there is
    exactly one `Change` for however many stores it declares, not one per
    store. Can't diff against the file's previous git revision either:
    `provision_describe` must not shell out (see the contract), so this
    only ever describes the file's current content."""
    from spiriconfig_provision.contract import Change

    entries = _parse_stores_toml(repo.path / path)
    if not entries:
        return Change("stores", "app stores cleared", "No app stores are declared any more")
    plural = "" if len(entries) == 1 else "s"
    urls = ", ".join(entry.url for entry in entries)
    return Change(
        "stores",
        f"{len(entries)} app store{plural} declared",
        f"apps/stores.toml declares: {urls}",
    )


def _describe_images(repo: StagedRepo) -> Change:
    """Describe the whole ``apps/images/oci/`` layout as one `Change` --
    like ``apps/stores.toml``, a bundle's blobs have no per-entry
    add/remove verb of their own, and there can be dozens of changed blob
    paths for one bundle run (a manifest plus every layer and config it
    references). One `Change` no matter how many of those paths changed
    -- :func:`describe` returns this same value for every one of them,
    and `spiriconfig_provision.repo.pending_changes` only keeps the first
    (see its own docstring).

    Reads ``index.json`` for a tag count; anything that stops that from
    parsing (missing, mid-write, corrupt) describes as "not readable"
    rather than raising -- `provision_describe` must not crash the whole
    Review & Sign list over one bundle's bookkeeping file.
    """
    from spiriconfig_provision.contract import Change

    index_path = repo.path / images.OCI_DIR / "index.json"
    if not index_path.is_file():
        return Change(
            "images", "bundled images cleared", "No container images are bundled any more"
        )
    try:
        index = json.loads(index_path.read_text())
        count = len(index.get("manifests", [])) if isinstance(index, dict) else 0
    except (OSError, json.JSONDecodeError):
        return Change(
            "images",
            "bundled images changed",
            f"{images.OCI_DIR}/index.json changed, but is not currently readable",
        )
    plural = "" if count == 1 else "s"
    return Change(
        "images",
        f"{count} container image{plural} bundled",
        f"{images.OCI_DIR}/ bundles {count} container image{plural}, deduplicated via git-lfs",
    )


def describe(repo: StagedRepo, path: str) -> Change | None:
    """Describe one changed path under ``apps/``. See the contract's
    ``provision_describe`` for the calling convention.

    Dispatches on `path`/`repo.remote` to :func:`_describe_patch`,
    :func:`_describe_baseline`, :func:`_describe_stores`, or
    :func:`_describe_images` -- the four shapes on disk never share a
    code path. ``apps/remove/<name>`` is a bare marker file (3 path
    segments); ``apps/add/<name>/env`` and ``apps/state/<name>/env`` are
    one level deeper, since those entries are directories holding a
    settings file; ``apps/images/...`` matches at any depth, since a
    bundle's blobs live arbitrarily deep under ``apps/images/oci/blobs/``.
    """
    parts = path.split("/")
    if not parts or parts[0] != "apps":
        return None
    if parts[1:2] == ["images"]:
        return _describe_images(repo)
    if len(parts) == 2 and parts[1] == "stores.toml":
        return _describe_stores(repo, path)
    if len(parts) == 3 and parts[1] == "remove":
        return _describe_patch(repo, path, "remove", parts[2])
    if len(parts) == 4 and parts[3] == "env" and parts[1] in ("add", "state"):
        verb, name = parts[1], parts[2]
        if repo.remote == "baseline":
            return _describe_baseline(repo, path, name) if verb == "state" else None
        return _describe_patch(repo, path, verb, name) if verb == "add" else None
    return None


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _env_write_step(env_path: Path, dest: Path) -> ApplyStep | None:
    """A step that writes `env_path`'s content to `dest` on the applying
    device, or `None` if there's nothing worth writing (no ``env`` file,
    or an empty one -- "no env file = install with defaults", per
    ``NOTES-usb-provisioning.md``). ``tee``, not a template in
    ``Command.argv``, for the reason ``ApplyStep.input`` exists at all:
    the rendered text can hold a plain-text secret (an app's password
    field), which must never end up in a copy-pasteable command line.
    """
    from spiriconfig_provision.contract import ApplyStep

    if not env_path.is_file():
        return None
    text = env_path.read_text()
    if not text.strip():
        return None
    return ApplyStep(Command(argv=["tee", str(dest)]), input=text)


def _apply_baseline(
    settings: AppStoreSettings,
    compose_dir: Path,
    state_dir: Path,
    current: dict[str, installs.Install],
) -> list[ApplyStep]:
    """Reconcile the live system to ``apps/state/``'s declared set --
    kept apart from :func:`_apply_patch`, see its docstring. Install is
    skipped for an already-installed name (nothing to redo there), but
    an entry's ``env`` is written every time it's declared with content,
    installed or not -- the same "can't tell whether a live value already
    matches, so re-write it, harmlessly, every time" reasoning
    `users/state/`'s password re-set already follows.
    """
    from spiriconfig_provision.contract import ApplyStep

    declared_dirs = (
        {p.name: p for p in state_dir.iterdir() if p.is_dir()} if state_dir.is_dir() else {}
    )
    declared = set(declared_dirs)
    steps = [
        ApplyStep(current[name].uninstall_command()) for name in current.keys() - declared
    ]
    for name in sorted(declared):
        if name not in current:
            app = find_app(settings, name)
            steps.append(ApplyStep(installs.install_command(app, compose_dir, name)))
        env_step = _env_write_step(declared_dirs[name] / "env", compose_dir / name / ".env")
        if env_step is not None:
            steps.append(env_step)
    return steps


def _apply_patch(
    settings: AppStoreSettings,
    compose_dir: Path,
    apps_dir: Path,
    current: dict[str, installs.Install],
) -> list[ApplyStep]:
    """Reconcile the live system to ``apps/add/``/``apps/remove/``'s diff
    -- kept apart from :func:`_apply_baseline`, see its docstring. Same
    "install only if missing, refresh settings regardless" split as
    :func:`_apply_baseline`.
    """
    from spiriconfig_provision.contract import ApplyStep

    remove_dir, add_dir = apps_dir / "remove", apps_dir / "add"
    steps: list[ApplyStep] = []
    if remove_dir.is_dir():
        steps += [
            ApplyStep(current[entry.name].uninstall_command())
            for entry in remove_dir.iterdir()
            if entry.is_file() and entry.name in current
        ]
    if add_dir.is_dir():
        for entry in sorted((p for p in add_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
            name = entry.name
            if name not in current:
                app = find_app(settings, name)
                steps.append(ApplyStep(installs.install_command(app, compose_dir, name)))
            env_step = _env_write_step(entry / "env", compose_dir / name / ".env")
            if env_step is not None:
                steps.append(env_step)
    return steps


def _apply_stores(settings: AppStoreSettings, apps_dir: Path) -> list[ApplyStep]:
    """Clone whichever declared stores aren't already cloned. Additive
    only, the same "read reality, never mirror it" stance
    :func:`spiriconfig_appstore.stores.stores` already takes -- a store
    dropped from ``apps/stores.toml`` is not un-cloned by this, since that
    would delete a working tree, including any local edits in it, that
    ``apps/stores.toml`` never asked to be made in the first place."""
    from spiriconfig_provision.contract import ApplyStep

    steps: list[ApplyStep] = []
    for entry in _parse_stores_toml(apps_dir / "stores.toml"):
        store = store_for_url(settings, entry.url)
        if store.is_cloned:
            continue
        steps.append(ApplyStep(store.clone_command()))
        if entry.ref:
            steps.append(
                ApplyStep(
                    Command(
                        argv=[settings.git_bin, "-C", str(store.path), "checkout", entry.ref]
                    )
                )
            )
    return steps


def apply(repo: StagedRepo) -> list[ApplyStep]:
    """Reconcile the live system's installed apps, and its cloned app
    stores, to what ``repo`` declares.

    See the contract's ``provision_apply`` for the calling convention --
    called after `repo` is already verified and fast-forwarded, returns
    steps rather than running them, and reads `repo.remote` to know
    whether to expect ``state/`` or ``add/``/``remove/`` on disk. Cloning
    declared stores applies on *both* remotes, not just patch -- each is
    its own repo with its own ``apps/stores.toml`` (see
    :func:`render_baseline_block`'s docstring), and a baseline applied on
    its own has no patch-side clone to borrow. A declared app name that
    resolves to no app in any configured store, or to more than one,
    raises :class:`~spiriconfig_appstore.stores.StoreError` -- a broken
    repo, surfaced loudly rather than silently skipped.
    """
    settings = appstore_settings()
    compose_dir = docker_settings().compose_dir
    current = {install.name: install for install in installs.installed(settings, compose_dir)}
    apps_dir = repo.path / "apps"

    if repo.remote == "baseline":
        steps = _apply_baseline(settings, compose_dir, apps_dir / "state", current)
    else:
        steps = _apply_patch(settings, compose_dir, apps_dir, current)
    steps += _apply_stores(settings, apps_dir)
    return steps


__all__ = ["apply", "describe", "render_baseline_block", "render_patch_block"]
