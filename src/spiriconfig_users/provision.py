"""Users' contribution to provisioning: staging, describing, and applying
``users/`` entries in a provisioning repo.

See ``docs/provisioning.md`` for the interface this implements, and
``NOTES-usb-provisioning.md`` for why the repo layout looks the way it
does. Nothing here touches this machine's own accounts -- that's
:mod:`spiriconfig_users.web`, a live view of ``getent passwd``. This module
only ever reads and writes inside a provisioning repo's working tree, except
in :func:`apply`, which is the one place that's expected to act on a real
system -- and even then, only by returning commands, never running them.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from nicegui import ui

from spiriconfig.commands import run

from spiriconfig_users import users
from spiriconfig_users.config import UsersSettings, users_settings
from spiriconfig_users.users import UserError

if TYPE_CHECKING:
    from spiriconfig_provision.contract import ApplyStep, Change, StagedRepo

log = logger.bind(plugin="users")


def _staged(users_dir: Path, verb: str) -> list[str]:
    """Names currently staged under ``add/`` or ``remove/``, sorted.

    ``add/`` entries are directories (there's a ``password.hash`` to hold);
    ``remove/`` entries are bare empty *files* -- git tracks nothing about
    an empty directory, so an empty-directory marker would silently vanish
    the moment it's the only thing in a commit. See
    ``NOTES-usb-provisioning.md``'s layout: every ``remove/<name>`` across
    every resource type is a marker file, never a directory, for exactly
    this reason.
    """
    directory = users_dir / verb
    if not directory.is_dir():
        return []
    is_entry = Path.is_file if verb == "remove" else Path.is_dir
    return sorted(p.name for p in directory.iterdir() if is_entry(p))


def _unstage(users_dir: Path, verb: str, name: str) -> None:
    entry = users_dir / verb / name
    if entry.is_dir():
        shutil.rmtree(entry)
    elif entry.is_file():
        entry.unlink()


def _write_lines(path: Path, lines: list[str]) -> None:
    """Write `lines` newline-joined to `path`, or remove `path` if `lines`
    is empty.

    Used for a staged entry's ``groups`` and ``authorized_keys`` files.
    Removing rather than leaving an empty file behind means restaging with
    the field cleared actually clears what was declared before -- the same
    "restaging fully re-declares the entry" rule the password half of
    :func:`_stage_add`/:func:`_stage_state` already follows.
    """
    if lines:
        path.write_text("".join(f"{line}\n" for line in lines))
    elif path.exists():
        path.unlink()


def _parse_groups(text: str) -> list[str]:
    """Split a comma- or whitespace-separated Groups field into names."""
    return [g for g in re.split(r"[,\s]+", text.strip()) if g]


def _parse_ssh_keys(text: str) -> list[str]:
    """Split an SSH-keys textarea into one entry per non-blank line --
    already ``authorized_keys``'s own format, so no other parsing is
    needed."""
    return [line.strip() for line in text.splitlines() if line.strip()]


async def _stage_add(
    users_dir: Path,
    settings: UsersSettings,
    name: str,
    password: str,
    groups: list[str] | None = None,
    ssh_keys: list[str] | None = None,
) -> None:
    """Hash `password` and write ``users/add/<name>/password.hash``, plus
    ``groups`` and ``authorized_keys`` alongside it if given -- see
    :func:`_write_lines`.

    Adding cancels a previously staged removal of the same name in the same
    session -- add and remove of one entry in one patch isn't a state
    either verb means on its own.

    Raises :class:`~spiriconfig_users.users.UserError` if ``mkpasswd``
    exits 0 but its output doesn't actually look like a hash -- see
    :func:`~spiriconfig_users.users.validate_password_hash` for why that
    check exists and isn't redundant with :meth:`Result.check`.
    """
    result = await asyncio.to_thread(
        run, users.hash_password(settings), input=password, log=log
    )
    result.check()
    hashed = users.validate_password_hash(result.stdout)
    entry = users_dir / "add" / name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "password.hash").write_text(hashed + "\n")
    _write_lines(entry / "groups", groups or [])
    _write_lines(entry / "authorized_keys", ssh_keys or [])
    _unstage(users_dir, "remove", name)


def _stage_remove(users_dir: Path, name: str) -> None:
    """Mark ``name`` for removal: an empty file, not a directory -- see
    :func:`_staged`. Cancels a staged add of the same name."""
    remove_dir = users_dir / "remove"
    remove_dir.mkdir(parents=True, exist_ok=True)
    (remove_dir / name).touch()
    _unstage(users_dir, "add", name)


async def _stage_state(
    state_dir: Path,
    settings: UsersSettings,
    name: str,
    password: str,
    groups: list[str] | None = None,
    ssh_keys: list[str] | None = None,
) -> None:
    """Hash `password` and write ``users/state/<name>/password.hash``, plus
    ``groups`` and ``authorized_keys`` alongside it if given -- declaring,
    or re-declaring, one entry of the exact set. No "cancel a staged
    remove" step the way :func:`_stage_add` has: `state/` has no `remove/`
    counterpart, there is only ever the current list.

    Raises :class:`~spiriconfig_users.users.UserError` on the same
    "exited 0 but didn't hash anything" case :func:`_stage_add` guards
    against."""
    result = await asyncio.to_thread(
        run, users.hash_password(settings), input=password, log=log
    )
    result.check()
    hashed = users.validate_password_hash(result.stdout)
    entry = state_dir / name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "password.hash").write_text(hashed + "\n")
    _write_lines(entry / "groups", groups or [])
    _write_lines(entry / "authorized_keys", ssh_keys or [])


def _drop_from_state(state_dir: Path, name: str) -> None:
    """Remove `name` from the declared set. This *is* the entire removal
    mechanism -- state/ isn't relative to anything, so not listing an
    entry any more is what "removed" means; there's no separate marker."""
    entry = state_dir / name
    if entry.is_dir():
        shutil.rmtree(entry)


def render_baseline_block(repo: StagedRepo) -> None:
    """The exact-set editor for ``users/state/``, on the Provisioning page.

    Same list-builder shape as :func:`render_patch_block`'s Add side, with
    no Add/Remove toggle -- one polarity, being in the list -- and no
    session-scoped "staged" distinction: what's shown *is* what's declared,
    read straight off ``users/state/`` on every render, the same way
    `provision_apply`'s baseline branch reads it. See
    ``docs/provisioning.md``'s "Applying a repo" section for why this
    editor doesn't (and can't) show what a save will change on any given
    device -- that diff depends on a device's actual current state, which
    this page has no way to know.
    """
    settings = users_settings()
    state_dir = repo.path / "users" / "state"

    ui.label(
        "The login accounts this device should have. Saving removes any "
        "account not listed here -- there is no separate remove step."
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-end gap-2"):
        name_input = (
            ui.input("Account name")
            .classes("grow")
            .props("outlined")
            .mark("provision-users-baseline-name")
        )
        password_input = (
            ui.input("Password", password=True, password_toggle_button=True)
            .classes("grow")
            .props("outlined")
            .mark("provision-users-baseline-password")
        )
    with ui.row().classes("w-full items-end gap-2"):
        groups_input = (
            ui.input("Groups (space- or comma-separated, optional)")
            .classes("grow")
            .props("outlined")
            .mark("provision-users-baseline-groups")
        )
        ssh_keys_input = (
            ui.textarea("SSH public keys (optional, one per line)")
            .classes("grow")
            .props("outlined")
            .mark("provision-users-baseline-ssh-keys")
        )
    with ui.row().classes("w-full justify-end"):
        add_button = (
            ui.button("Add to set", icon="add")
            .props("color=primary")
            .mark("provision-users-baseline-add")
        )

    declared_column = ui.column().classes("w-full gap-1")

    def refresh_declared() -> None:
        declared_column.clear()
        names = _staged(state_dir.parent, "state")
        with declared_column:
            if not names:
                ui.label("Nothing declared yet.").classes("text-sm text-gray-500")
            for name in names:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{name} — users/state").classes("text-sm grow")
                    ui.button(
                        icon="close",
                        on_click=lambda n=name: (
                            _drop_from_state(state_dir, n),
                            refresh_declared(),
                        ),
                    ).props("flat dense round")

    async def do_add() -> None:
        try:
            name = users.validate_name(name_input.value)
        except UserError as exc:
            ui.notify(str(exc), type="negative")
            return
        if not password_input.value:
            ui.notify("Enter a password.", type="warning")
            return
        try:
            await _stage_state(
                state_dir,
                settings,
                name,
                password_input.value,
                groups=_parse_groups(groups_input.value),
                ssh_keys=_parse_ssh_keys(ssh_keys_input.value),
            )
        except UserError as exc:
            ui.notify(str(exc), type="negative")
            return
        name_input.value = ""
        password_input.value = ""
        groups_input.value = ""
        ssh_keys_input.value = ""
        refresh_declared()

    add_button.on_click(do_add)
    refresh_declared()


def render_patch_block(repo: StagedRepo) -> None:
    """The add/remove staging block for ``users/``, on the Provisioning page.

    This *is* the entire write path -- see
    :meth:`spiriconfig_provision.contract.ProvisioningContributor.provision_patch_block`.
    There is no callback into ``spiriconfig_provision``; staging happens the
    moment Add or Remove is pressed.

    Two explicit buttons, not a mode toggle -- the same call
    :mod:`spiriconfig_appstore.provision` made for its own patch block (see
    its ``_already_staged_dialog`` docstring): a toggle plus one shared
    "Stage" button reads as an unlabelled pair of tabs rather than two
    distinct actions, and direct feedback was that Add and Remove should
    each be their own button, here as there. Unlike an app row's Add/Remove
    (which relabel to a past-tense state once staged, since the row *is* a
    persistent catalog entry), these two buttons stay plain labels: `users/`
    has no catalog to list rows from, only the accumulating set of entries
    staged this session below, and every click here does something real --
    Add always (re)writes a password hash, even for a name already staged,
    so restaging is how you change a typo'd password before committing.
    """
    settings = users_settings()
    users_dir = repo.path / "users"

    ui.label(
        "Stage an account to create or delete. Nothing here touches this "
        "device -- only the accounts a repo built from this staging ends "
        "up declaring."
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-end gap-2"):
        name_input = (
            ui.input("Account name")
            .classes("grow")
            .props("outlined")
            .mark("provision-users-name")
        )
        password_input = (
            ui.input("New password", password=True, password_toggle_button=True)
            .classes("grow")
            .props("outlined")
            .mark("provision-users-password")
        )
    with ui.row().classes("w-full items-end gap-2"):
        groups_input = (
            ui.input("Groups (space- or comma-separated, optional)")
            .classes("grow")
            .props("outlined")
            .mark("provision-users-groups")
        )
        ssh_keys_input = (
            ui.textarea("SSH public keys (optional, one per line)")
            .classes("grow")
            .props("outlined")
            .mark("provision-users-ssh-keys")
        )
    ui.label(
        "Password, groups, and SSH keys are only used by Add; Remove ignores "
        "them."
    ).classes("text-xs text-gray-500")

    staged_column = ui.column().classes("w-full gap-1")

    # Translucent tints, not solid colours -- the same reasoning
    # `_app_row` in spiriconfig_appstore.provision gives for its own row
    # background: `color-mix` over the Quasar theme variable reads
    # correctly whether the page is light or dark, a fixed Tailwind shade
    # would not.
    _TINT = {"add": "var(--q-primary)", "remove": "var(--q-negative)"}

    def _staged_row(sign: str, verb: str, text: str, on_remove) -> None:
        row = ui.row().classes("items-center gap-2 py-1 px-2 rounded-borders")
        row.style(
            f"background-color: color-mix(in srgb, {_TINT[verb]} 12%, transparent)"
        )
        with row:
            ui.label(sign).classes("font-mono w-4")
            ui.label(text).classes("text-sm grow")
            ui.button(icon="close", on_click=on_remove).props("flat dense round")

    def refresh_staged() -> None:
        staged_column.clear()
        added = _staged(users_dir, "add")
        removed = _staged(users_dir, "remove")
        with staged_column:
            if not added and not removed:
                ui.label("Nothing staged this session.").classes(
                    "text-sm text-gray-500"
                )
            for name in added:
                _staged_row(
                    "+",
                    "add",
                    f"{name} — users/add",
                    lambda n=name: (_unstage(users_dir, "add", n), refresh_staged()),
                )
            for name in removed:
                _staged_row(
                    "−",
                    "remove",
                    f"{name} — users/remove",
                    lambda n=name: (
                        _unstage(users_dir, "remove", n),
                        refresh_staged(),
                    ),
                )

    async def do_add() -> None:
        try:
            name = users.validate_name(name_input.value)
        except UserError as exc:
            ui.notify(str(exc), type="negative")
            return
        if not password_input.value:
            ui.notify("Enter a password.", type="warning")
            return
        try:
            await _stage_add(
                users_dir,
                settings,
                name,
                password_input.value,
                groups=_parse_groups(groups_input.value),
                ssh_keys=_parse_ssh_keys(ssh_keys_input.value),
            )
        except UserError as exc:
            ui.notify(str(exc), type="negative")
            return
        name_input.value = ""
        password_input.value = ""
        groups_input.value = ""
        ssh_keys_input.value = ""
        refresh_staged()

    def do_remove() -> None:
        try:
            name = users.validate_name(name_input.value)
        except UserError as exc:
            ui.notify(str(exc), type="negative")
            return
        _stage_remove(users_dir, name)
        name_input.value = ""
        refresh_staged()

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button("Remove", icon="remove", on_click=do_remove).props(
            "outline color=negative no-caps"
        ).mark("provision-users-remove")
        ui.button("Add", icon="add", on_click=do_add).props(
            "color=primary no-caps"
        ).mark("provision-users-add")

    refresh_staged()


def _describe_patch(repo: StagedRepo, path: str, verb: str, name: str) -> Change | None:
    """Describe one ``users/add/`` or ``users/remove/`` entry -- the patch
    side's own vocabulary, kept apart from :func:`_describe_baseline` so
    neither has to branch around the other's shape on disk.

    An ``add/`` path can reach here even though it no longer exists, for
    the identical reason :func:`_describe_baseline` already checks
    existence for ``state/`` (see its own docstring): git reports a
    deletion as a changed path just like a new one, and unstaging a
    previously-committed add -- staging a remove over it, see
    :func:`_stage_remove`'s call to :func:`_unstage` -- deletes
    ``users/add/<name>/password.hash``. Left unchecked, that read as
    "user X added" sitting right next to that same commit's own "user X
    removed" line -- the patch side never applied the check the baseline
    side already had, and this is the mismatch that surfaced it (see
    :mod:`spiriconfig_appstore.provision`'s own ``_describe_patch`` for the
    identical fix on the apps side, hit by the same bug the same way).
    """
    from spiriconfig_provision.contract import Change

    if verb == "add":
        if not (repo.path / path).exists():
            return Change(
                "unstage",
                f"user {name} add withdrawn",
                f"**{name}** is no longer staged to be added -- its earlier "
                "users/add entry was withdrawn this patch",
            )
        return Change(
            "add",
            f"user {name} added",
            f"Adds **{name}**, or resets their password if already present",
        )
    if verb == "remove":
        return Change("remove", f"user {name} removed", f"Removes login account **{name}**")
    return None


def _describe_baseline(repo: StagedRepo, path: str, name: str) -> Change:
    """Describe one ``users/state/`` entry.

    Uses the same "added"/"removed" wording :func:`_describe_patch` does
    -- there's nothing about being on the baseline that a reviewer needs a
    different word for. Since ``state/`` has no ``remove/`` marker of its
    own (:func:`_drop_from_state` *is* the removal), whether an entry was
    added or dropped is read off whether ``path`` still exists on disk:
    git reports a dropped entry's ``password.hash`` as a changed path
    right up until the next commit, even though it's already gone.
    """
    from spiriconfig_provision.contract import Change

    if (repo.path / path).exists():
        return Change("add", f"user {name} added", f"**{name}** is in the certified account set")
    return Change(
        "remove",
        f"user {name} removed",
        f"**{name}** is removed from the certified account set",
    )


def describe(repo: StagedRepo, path: str) -> Change | None:
    """Describe one changed path under ``users/``. See the contract's
    ``provision_describe`` for the calling convention.

    Dispatches on `repo.remote` to :func:`_describe_patch` or
    :func:`_describe_baseline` -- the two shapes on disk (``add/``/
    ``remove/`` vs. ``state/``) never share a code path.
    """
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "users":
        return None
    verb, name = parts[1], parts[2]
    if repo.remote == "baseline":
        return _describe_baseline(repo, path, name) if verb == "state" else None
    return _describe_patch(repo, path, verb, name)


def _apply_baseline(repo: StagedRepo, settings: UsersSettings, current: set[str]) -> list[ApplyStep]:
    """Reconcile the live system to ``users/state/``'s declared set --
    kept apart from :func:`_apply_patch` since the two shapes on disk
    (an absolute declaration vs. an add/remove diff) reconcile
    differently, not just from a different directory."""
    from spiriconfig_provision.contract import ApplyStep

    state_dir = repo.path / "users" / "state"
    declared = (
        {p.name for p in state_dir.iterdir() if p.is_dir()} if state_dir.is_dir() else set()
    )
    steps = [ApplyStep(users.delete(settings, name)) for name in current - declared]
    # Every declared name gets its password (re)set, not just the ones
    # just created: state/ can't tell whether a live password already
    # matches without reading /etc/shadow, and re-setting an already
    # correct one is harmless -- the same reasoning `_apply_patch`'s add
    # branch follows, and for the same reason (see "state/ is not
    # relative to anything" in NOTES-usb-provisioning.md).
    for name in sorted(declared):
        if name not in current:
            steps.append(ApplyStep(users.create(settings, name)))
        hashed = (state_dir / name / "password.hash").read_text().strip()
        steps.append(
            ApplyStep(
                users.set_password_hashed(settings, name),
                users.password_stdin(name, hashed),
            )
        )
    return steps


def _apply_patch(repo: StagedRepo, settings: UsersSettings, current: set[str]) -> list[ApplyStep]:
    """Reconcile the live system to ``users/add/``/``users/remove/``'s
    diff -- kept apart from :func:`_apply_baseline`, see its docstring."""
    from spiriconfig_provision.contract import ApplyStep

    base = repo.path / "users"
    remove_dir, add_dir = base / "remove", base / "add"
    steps: list[ApplyStep] = []
    if remove_dir.is_dir():
        steps += [
            ApplyStep(users.delete(settings, entry.name))
            for entry in remove_dir.iterdir()
            if entry.is_file() and entry.name in current
        ]
    if add_dir.is_dir():
        for entry in add_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name not in current:
                steps.append(ApplyStep(users.create(settings, entry.name)))
            hashed = (entry / "password.hash").read_text().strip()
            steps.append(
                ApplyStep(
                    users.set_password_hashed(settings, entry.name),
                    users.password_stdin(entry.name, hashed),
                )
            )
    return steps


def apply(repo: StagedRepo) -> list[ApplyStep]:
    """Reconcile the live system's login accounts to what ``repo`` declares.

    See the contract's ``provision_apply`` for the calling convention --
    called after `repo` is already verified and fast-forwarded. Dispatches
    on `repo.remote` to :func:`_apply_baseline` or :func:`_apply_patch`,
    which never share a code path with each other.
    """
    settings = users_settings()
    current = {u.name for u in users.list_users(settings)}  # login accounts only
    if repo.remote == "baseline":
        return _apply_baseline(repo, settings, current)
    return _apply_patch(repo, settings, current)


__all__ = ["apply", "describe", "render_baseline_block", "render_patch_block"]
