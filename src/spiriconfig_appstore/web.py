"""The NiceGUI face of the app store.

Same bargain as the docker plugin: nothing here does anything the CLI cannot, and
every action shows the exact command it runs. An app store is the easiest place
in a tool like this to accumulate magic -- a button that "just installs it" and
leaves the user unable to say what changed -- so this page is deliberately built
to keep answering "we ran `ln -s`, here it is".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from nicegui import ui

from spiriconfig import advanced, terminal
from spiriconfig.commands import Command, run, stream_pty
from spiriconfig_docker.config import docker_settings

from spiriconfig_appstore.config import AppStoreSettings, appstore_settings
from spiriconfig_appstore.installs import Install, install_command, installed
from spiriconfig_appstore.stores import App, Store, StoreError, stores, update_plan

log = logger.bind(plugin="appstore")


async def _run_in_dialog(title: str, commands: list[Command]) -> None:
    """Show a sequence of commands, run them in order, and leave the output up.

    Stops at the first failure. An update is fetch-then-merge, and merging after
    a failed fetch would quietly report success for an update that never landed.

    Like the docker plugin's version, this returns when the *user* dismisses the
    dialog, not when the commands finish -- because the caller refreshes the page
    afterwards, and that clears the container this dialog lives in. See
    :func:`spiriconfig_docker.web._run_in_dialog`.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")

        with advanced.only(), ui.column().classes(
            "w-full gap-1 bg-gray-100 p-2 rounded"
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
                command,
                log=log,
                rows=terminal.TERMINAL_ROWS,
                columns=terminal.TERMINAL_COLUMNS,
            ):
                output.write(chunk)
                # stream_pty marks a failure with this synthetic trailer, which is
                # how we know to stop rather than run the next command in the plan.
                if b"[command exited with code " in chunk:
                    failed = True
            if failed:
                output.write("\r\n[stopped: the command above failed]\r\n")
                break
    except Exception as exc:  # noqa: BLE001 - surface any failure in the dialog
        log.exception("command failed")
        output.write(f"\r\n[error] {exc}\r\n")
    finally:
        close.enable()

    # Block here until the user dismisses it, or refresh() will clear the
    # container this dialog is in and take the output with it.
    await dialog

    # A closed dialog is still an element on the page. Without this, every action
    # leaves one behind.
    dialog.delete()


async def _diff_dialog(entry: App) -> None:
    """Show what the user changed and what the store changed, side by side.

    The whole point of the update story, and the thing a user actually needs
    before they press the button. Rendered as plain unified diff, because that is
    what it is, and a user who wants to check our work can run the same two git
    commands and compare.
    """
    settings = entry.store.settings

    async def capture(command: Command) -> str:
        result = await asyncio.to_thread(
            run, command, timeout=settings.command_timeout, log=log
        )
        return result.stdout.rstrip() or "(no changes)"

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl"):
        ui.label(f"{entry.name} — changes").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-4"):
            for title, command in (
                ("What you changed", entry.local_diff()),
                ("What the store changed since your version", entry.upstream_diff()),
            ):
                ui.label(title).classes("font-bold text-sm")
                ui.code(await capture(command), language="diff").classes(
                    "w-full text-xs"
                )
        ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()


async def _confirm_adopt(install: Install) -> bool:
    """Ask before the one action here that cannot be undone.

    Everything else on this page is reversible: install and uninstall are a
    symlink appearing and disappearing, and an update can be aborted or merged
    back. Adopt is the exception -- it is a one-way door, and afterwards
    SpiriConfig cannot even clean up after itself, because the directory it left
    behind is a real one that it did not create and will not delete.

    Being behind advanced mode is not protection. Advanced mode is self-service
    and a preference, not a boundary (see :doc:`advanced`), so the button is one
    click away for anybody who wants it. The confirmation is the actual guard,
    and it is the only one on this page precisely so that it still means
    something when it appears.

    The commands are shown to everyone, not gated on advanced mode like they are
    elsewhere: for an irreversible action, the commands *are* the explanation.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label(f"Adopt {install.name}?").classes("text-lg font-bold")
        ui.label(
            f"This replaces the symlink with a real copy of the files. "
            f"{install.name} stops tracking {install.store.slug} for good: it will "
            f"never show an update again, and nothing here will touch it."
        ).classes("text-sm")
        ui.label(
            "There is no undo. Afterwards SpiriConfig will refuse to remove the "
            "directory too — it did not create it — so getting rid of it means "
            "rm -rf in a shell."
        ).classes("text-sm text-gray-600")

        with ui.column().classes("w-full gap-1 bg-gray-100 p-2 rounded"):
            for command in install.adopt_commands():
                ui.label(str(command)).classes("font-mono text-xs break-all")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
            # "Adopt it", not "Adopt": the button that opened this dialog says
            # "Adopt", and a confirmation whose button reads the same as the one
            # you just pressed is a confirmation you click through on reflex.
            ui.button("Adopt it", on_click=lambda: dialog.submit(True)).props(
                "color=negative"
            )

    # A dismissed dialog (escape, click-away) submits None, which is a "no".
    answer = bool(await dialog)
    dialog.delete()
    return answer


def _app_card(
    entry: App,
    install: Install | None,
    modified: bool,
    updatable: bool,
    compose_dir: Path,
    refresh,
) -> None:
    """One app in a store, and the things you can do to it."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(entry.name).classes("text-lg font-bold")
                ui.label(f"{entry.store.slug} · {entry.version()}").classes(
                    "text-xs text-gray-500"
                )
            with ui.row().classes("items-center gap-1"):
                if install is not None:
                    label = (
                        "installed" if install.name == entry.name
                        else f"installed as {install.name}"
                    )
                    ui.badge(label, color="positive")
                if modified:
                    ui.badge("locally edited", color="warning")
                if updatable:
                    ui.badge("update available", color="info")

        async def do(commands: list[Command], title: str) -> None:
            await _run_in_dialog(f"{entry.name} — {title}", commands)
            refresh()

        # Every button says what it does on hover. None of these verbs mean what a
        # user would guess: "install" does not start anything, "uninstall" does not
        # delete anything, and "adopt" is a one-way door. A label cannot carry that,
        # and a user should not have to read the docs to find out which.
        with ui.row().classes("gap-2"):
            if install is None:
                async def do_install() -> None:
                    try:
                        command = install_command(entry, compose_dir)
                    except StoreError as exc:
                        ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                        return
                    await do([command], "install")

                ui.button("Install", icon="add", on_click=do_install).tooltip(
                    f"Symlink {entry.name} into the compose directory. "
                    f"Does not start it — use Apps for that."
                )
            else:
                ui.button(
                    "Uninstall", icon="link_off",
                    on_click=lambda: do([install.uninstall_command()], "uninstall"),
                ).props("flat").tooltip(
                    "Remove the symlink. Deletes no files and no data, and does "
                    "not stop the app if it is running."
                )

            if modified or updatable:
                ui.button(
                    "Changes", icon="difference",
                    on_click=lambda: _diff_dialog(entry),
                ).props("flat").tooltip(
                    "What you changed, and what the store changed, side by side."
                )

            # Advanced only: adopting is a decision a user needs a reason for, and
            # offering it to everyone invites cargo-culting it. That is decluttering
            # though, not a safeguard -- the safeguard is _confirm_adopt, because
            # this is the one button here that cannot be taken back.
            if install is not None:
                async def do_adopt() -> None:
                    if not await _confirm_adopt(install):
                        return
                    await do(install.adopt_commands(), "adopt")

                with advanced.only():
                    ui.button(
                        "Adopt", icon="content_copy", on_click=do_adopt
                    ).props("flat").tooltip(
                        f"Take {entry.name} out of the store and make it yours: "
                        f"the symlink becomes a real copy. It will never update "
                        f"again. Cannot be undone."
                    )


def _store_header(store: Store, refresh) -> None:
    """One store: where it came from, and the two things you can do to it."""
    with ui.row().classes("w-full items-center gap-2 mt-4"):
        ui.label(store.slug).classes("text-xl font-bold")
        ui.label(store.url).classes("text-xs text-gray-500 grow break-all")

        async def do(commands: list[Command], title: str) -> None:
            await _run_in_dialog(f"{store.slug} — {title}", commands)
            refresh()

        if not store.is_cloned:
            async def do_clone() -> None:
                store.path.parent.mkdir(parents=True, exist_ok=True)
                await do([store.clone_command()], "clone")

            ui.button("Clone", icon="download", on_click=do_clone)
            return

        if store.in_merge:
            # Everything else is hidden while a merge is unfinished. There is
            # exactly one thing to do here, and offering "Update" beside it would
            # be offering to make it worse.
            ui.badge("conflict", color="negative")
            return

        ui.button(
            "Sync", icon="sync",
            on_click=lambda: do([store.fetch_command()], "fetch"),
        ).props("flat").tooltip("Fetch from the remote. Changes no installed app.")

        async def do_update() -> None:
            try:
                plan = update_plan(store)
            except StoreError as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            await do(plan, "update")

        ui.button("Update", icon="upgrade", on_click=do_update).props("flat").tooltip(
            "Merge the store's changes into your copy, keeping your edits. "
            "Rewrites files; restarts nothing."
        )


def _conflict_banner(store: Store, refresh) -> None:
    """The only thing on screen when an update stopped on a conflict.

    A conflict is the one moment this plugin cannot resolve on the user's behalf,
    so the page stops pretending to be an app store and becomes a set of
    instructions. The Edit button on the Apps page is where they fix it -- and it
    already refuses to save a file compose will not accept, which is every file
    that still has markers in it.
    """
    with ui.card().classes("w-full border-2 border-red-500"):
        ui.label("This update needs you").classes("text-lg font-bold")
        ui.label(
            "Your edits and the store's changes touched the same lines, so git "
            "could not merge them on its own. These files have conflict markers "
            "in them:"
        ).classes("text-sm")
        for name in store.conflicts():
            ui.label(str(store.path / name)).classes("font-mono text-xs")
        ui.label(
            "Edit each one — the Apps page has an editor — keep the version you "
            "want, and delete the <<<<<<< ======= >>>>>>> lines. Nothing can "
            "start until you do: the markers are not valid YAML, so docker "
            "compose will refuse the file."
        ).classes("text-sm text-gray-600")

        async def do(commands: list[Command], title: str) -> None:
            await _run_in_dialog(f"{store.slug} — {title}", commands)
            refresh()

        async def do_resolve() -> None:
            try:
                plan = store.resolve_plan()
            except StoreError as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            await do(plan, "resolve")

        with ui.row().classes("gap-2"):
            ui.button("I have fixed them", icon="check", on_click=do_resolve)
            ui.button(
                "Undo the update", icon="undo",
                on_click=lambda: do([store.abort_command()], "abort"),
            ).props("flat").tooltip(
                "Put everything back as it was. Your own edits are kept."
            )


def _empty() -> None:
    with ui.card().classes("w-full"):
        ui.label("No app stores configured.").classes("text-lg")
        ui.label(
            "An app store is a git repository with one directory per app, each "
            "containing a compose file. Point SpiriConfig at one by setting "
            "SPIRICONFIG_APPSTORE_STORES to a JSON list of git URLs."
        ).classes("text-sm text-gray-500")
        with advanced.only():
            ui.code(
                'SPIRICONFIG_APPSTORE_STORES=\'["https://github.com/spiri/spiri-apps"]\'',
                language="bash",
            ).classes("w-full text-xs")


def page(
    settings: AppStoreSettings | None = None,
    compose_dir: Path | None = None,
) -> None:
    """Render the app store's page.

    Both arguments default to the real environment and are only passed in by
    tests, which need a page that talks about a tmpdir rather than /srv/compose.
    """
    config = settings or appstore_settings()
    compose_root = (compose_dir or docker_settings().compose_dir).expanduser().resolve()

    ui.label("App Store").classes("text-2xl font-bold")
    ui.label(
        f"Apps are symlinked from {config.store_dir} into {compose_root}"
    ).classes("text-sm text-gray-500")

    container = ui.column().classes("w-full gap-2")

    async def render() -> None:
        container.clear()
        configured = await asyncio.to_thread(stores, config)
        with container:
            if not configured:
                _empty()
                return

            links = {
                (i.store.slug, i.app_name): i
                for i in await asyncio.to_thread(installed, config, compose_root)
            }

            for store in configured:
                _store_header(store, refresh)
                if not store.is_cloned:
                    ui.label("Not cloned yet.").classes("text-sm text-gray-500")
                    continue

                if store.in_merge:
                    _conflict_banner(store, refresh)
                    continue

                apps = await asyncio.to_thread(store.apps)
                if not apps:
                    ui.label("This store has no apps in it.").classes(
                        "text-sm text-gray-500"
                    )
                    continue

                # Each of these is a git subprocess, and a store with twenty apps
                # would otherwise spend twenty round trips rendering one page.
                flags = await asyncio.gather(
                    *(asyncio.to_thread(_flags, entry) for entry in apps)
                )
                for entry, (modified, updatable) in zip(apps, flags, strict=True):
                    _app_card(
                        entry,
                        links.get((store.slug, entry.name)),
                        modified,
                        updatable,
                        compose_root,
                        refresh,
                    )

    def refresh() -> None:
        """Re-render the store list, shortly.

        Pinned to the slot `container` lives in, for the reason spelled out in
        :func:`spiriconfig_docker.web.page`: a handler runs with the clicked
        element's slot active, so a timer made here during a button press would
        land *inside* `container` -- and `render()` clears the container, deleting
        the running timer and leaving the page blank.
        """
        with container.parent_slot:
            ui.timer(0.1, render, once=True)

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=refresh).props("flat")

    refresh()


def _flags(entry: App) -> tuple[bool, bool]:
    return entry.is_modified(), entry.has_update()
