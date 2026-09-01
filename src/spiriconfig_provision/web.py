"""The NiceGUI face of the provisioning plugin.

Two independent sections, one per remote -- a picker for an operator's
named *patch* profiles, and a separate picker for their named *baseline*
profiles. They are deliberately not one control: a patch profile and a
baseline profile are unrelated data even when an operator gives them the
same name (see `spiriconfig_provision.repo`'s module docstring), and
staging a change / declaring an exact set are different enough actions
that showing them as two facets of one selection reads as one profile
doing two contradictory things.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from nicegui import ui

from spiriconfig import advanced, terminal, theme
from spiriconfig.commands import Command, CommandError, stream_pty

from spiriconfig_provision import repo, review, sync
from spiriconfig_provision.config import ProvisionSettings, provision_settings
from spiriconfig_provision.contract import ProvisioningContributor, StagedRepo
from spiriconfig_provision.repo import Profile, Remote

log = logger.bind(plugin="provision")

_TITLE = {"patch": "Patch", "baseline": "Baseline"}
_BLOCK_HOOK = {"patch": "provision_patch_block", "baseline": "provision_baseline_block"}
_BLURB = {
    "patch": "Everyday add/remove, staged one entry at a time.",
    "baseline": (
        "The exact set this profile's devices should have -- rare and "
        "deliberate. Saving resyncs the whole list, not just what changed."
    ),
}


def _status_text(staged_repo: StagedRepo, settings: ProvisionSettings) -> str:
    url = sync.origin_url(staged_repo, settings)
    if url is None:
        return "no origin"
    result = sync.status(staged_repo, settings)
    if result is None:
        return "never fetched"
    if result.clean:
        return "up to date"
    return f"{result.ahead} ahead, {result.behind} behind"


def _origin_dialog(
    staged_repo: StagedRepo, settings: ProvisionSettings, key: str, on_saved
) -> None:
    """A small dialog to point `staged_repo`'s `origin` at a URL or path."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label("Set origin").classes("text-lg font-bold")
        ui.label(
            "What Pull fetches from, and what a plain Export sends HEAD "
            "to by default."
        ).classes("text-sm text-gray-500")
        url_input = (
            ui.input("URL or path", value=sync.origin_url(staged_repo, settings) or "")
            .classes("w-full")
            .props("outlined")
            .mark(f"provision-{key}-origin-input")
        )

        def do_save() -> None:
            if not url_input.value:
                ui.notify("Enter a URL or path.", type="warning")
                return
            sync.set_origin(staged_repo, url_input.value, settings)
            dialog.close()
            on_saved()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=do_save).props("color=primary").mark(
                f"provision-{key}-origin-save"
            )
    dialog.open()


def _remote_row(staged_repo: StagedRepo, settings: ProvisionSettings, key: str) -> None:
    """`origin`'s URL and ahead/behind status, a Pull button when one's
    configured, and a way to set or change it. Read fresh from git on
    every render -- no state of its own, same as the pending summary
    below."""
    row = ui.row().classes("w-full items-center gap-2")

    def refresh() -> None:
        row.clear()
        with row:
            url = sync.origin_url(staged_repo, settings)
            if url is None:
                ui.label("No origin configured.").classes(
                    "text-sm text-gray-500 grow"
                )
            else:
                ui.label(f"origin: {url} ({_status_text(staged_repo, settings)})").classes(
                    "text-sm text-gray-500 grow font-mono"
                )

            ui.button(
                "Set origin" if url is None else "Change origin",
                icon="link",
                on_click=lambda: _origin_dialog(staged_repo, settings, key, refresh),
            ).props("flat dense").mark(f"provision-{key}-set-origin")

            if url is not None:

                async def do_pull() -> None:
                    try:
                        await asyncio.to_thread(sync.pull, staged_repo, settings)
                    except CommandError as exc:
                        ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                        return
                    ui.notify("Pulled.", type="positive")
                    refresh()

                ui.button("Pull", icon="download", on_click=do_pull).props(
                    "flat dense"
                ).mark(f"provision-{key}-pull")

    refresh()


async def _run_in_dialog(title: str, commands: list[Command]) -> None:
    """Show a sequence of commands, run them in order, and leave the
    output up -- the same treatment every other action-running dialog in
    SpiriConfig gives a command before running it (see
    :func:`spiriconfig_docker.web._run_in_dialog`,
    :func:`spiriconfig_appstore.web._run_in_dialog`), which Review & Sign
    had not gotten yet: it used to run `git add`/`git commit` in a
    background thread and only ever show a plain `ui.notify()` after the
    fact, which is exactly the thing ``docs/design.md``'s "We shell out,
    on purpose" says not to do -- a command nobody can see run before it
    runs is unreproducible, whatever it says afterward.

    Stops at the first failure, so a failed `git add` doesn't march on
    into `git commit` as though nothing were staged.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")

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
                command,
                log=log,
                rows=terminal.TERMINAL_ROWS,
                columns=terminal.TERMINAL_COLUMNS,
            ):
                output.write(chunk)
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

    # Block here until the user dismisses it -- the caller refreshes the
    # page afterwards, and that clears the container this dialog lives
    # in, taking the output with it if we returned any sooner.
    await dialog
    dialog.delete()


def _review_dialog(
    staged_repo: StagedRepo, settings: ProvisionSettings, key: str, on_committed
) -> None:
    """Review & Sign: everything pending, a suggested commit message
    (editable), and the one button that actually commits it -- signed
    with whatever `git config user.signingkey` already names, or plainly
    not, see `spiriconfig_provision.review.commit`. The commit itself
    runs in :func:`_run_in_dialog`, showing the exact `git add`/`git
    commit` lines and their output, not a plain `ui.notify()` afterward.
    """
    changes = repo.pending_changes(staged_repo, settings)
    if not changes:
        ui.notify("Nothing staged.", type="warning")
        return

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("Review & Sign").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-1"):
            for change in changes:
                with ui.row().classes("items-center gap-2"):
                    ui.badge(change.verb).props("outline")
                    ui.label(change.detail).classes("text-sm")
        message_input = (
            ui.textarea("Commit message", value=review.suggested_message(changes))
            .classes("w-full")
            .props("outlined")
            .mark(f"provision-{key}-commit-message")
        )

        async def do_commit() -> None:
            message = message_input.value
            if not message.strip():
                ui.notify("Enter a commit message.", type="warning")
                return
            dialog.close()
            key_used = await asyncio.to_thread(review.signing_key, staged_repo, settings)
            commands = [
                review.add_command(staged_repo, settings),
                review.commit_command(staged_repo, message, key_used, settings),
            ]
            await _run_in_dialog("Commit & Sign", commands)
            on_committed()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Commit", icon="check", on_click=do_commit).props(
                "color=primary"
            ).mark(f"provision-{key}-commit")
    dialog.open()


def _export_dialog(staged_repo: StagedRepo, settings: ProvisionSettings, key: str) -> None:
    """Export: send this remote's HEAD to a drive path or a URL -- one
    dialog, one action, the same operation either way (see "Export has
    two destinations behind one action" in ``NOTES-usb-provisioning.md``)."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label("Export").classes("text-lg font-bold")
        ui.label(
            "Send this remote's HEAD to a drive path or a URL."
        ).classes("text-sm text-gray-500")
        dest_input = (
            ui.input("Destination", value=sync.origin_url(staged_repo, settings) or "")
            .classes("w-full")
            .props("outlined")
            .mark(f"provision-{key}-export-destination")
        )

        async def do_export() -> None:
            if not dest_input.value:
                ui.notify("Enter a destination.", type="warning")
                return
            try:
                await asyncio.to_thread(
                    sync.export_to, staged_repo, dest_input.value, settings
                )
            except (CommandError, ValueError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            ui.notify("Exported.", type="positive")
            dialog.close()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Export", icon="upload", on_click=do_export).props(
                "color=primary"
            ).mark(f"provision-{key}-export")
    dialog.open()


def _contributor_blocks(
    staged_repo: StagedRepo, active: list[ProvisioningContributor], render_hook: str
) -> None:
    blocks = [(c, getattr(c, render_hook)) for c in active if hasattr(c, render_hook)]
    if not blocks:
        with ui.card().classes("w-full"):
            ui.label("No plugin currently contributes here.").classes(
                "text-sm text-gray-500"
            )
        return
    for contributor, block in blocks:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.icon(contributor.icon)
                ui.label(contributor.title).classes("text-lg font-bold grow")
                if contributor.has_page:
                    ui.link(
                        f"Open {contributor.title}", f"/{contributor.name}"
                    ).classes("text-sm")
            block(staged_repo)


def _pending_summary_card(staged_repo: StagedRepo, settings: ProvisionSettings, key: str) -> None:
    """A count, not a list -- the itemised detail lives in the Review &
    Sign dialog, which is the only place it needs to be read in full."""
    card = ui.card().classes("w-full")

    def refresh() -> None:
        card.clear()
        with card, ui.row().classes("w-full items-center justify-between"):
            changes = repo.pending_changes(staged_repo, settings)
            if changes:
                plural = "" if len(changes) == 1 else "s"
                ui.label(f"{len(changes)} uncommitted change{plural}").classes(
                    "font-bold"
                )
            else:
                ui.label("Nothing staged.").classes("text-sm text-gray-500")
            with ui.row().classes("items-center gap-2"):
                ui.button("Refresh", icon="refresh", on_click=refresh).props(
                    "flat dense"
                ).mark(f"provision-{key}-refresh")
                ui.button(
                    "Review & Sign",
                    icon="rate_review",
                    on_click=lambda: _review_dialog(staged_repo, settings, key, refresh),
                ).props("flat dense").mark(f"provision-{key}-review")
                ui.button(
                    "Export",
                    icon="upload",
                    on_click=lambda: _export_dialog(staged_repo, settings, key),
                ).props("flat dense").mark(f"provision-{key}-export-open")

    refresh()


def _create_profile_dialog(remote: Remote, settings: ProvisionSettings, on_created) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label(f"New {_TITLE[remote].lower()} profile").classes("text-lg font-bold")
        name_input = (
            ui.input("Name")
            .classes("w-full")
            .props("outlined")
            .mark(f"provision-{remote}-new-profile-name")
        )

        def do_create() -> None:
            try:
                repo.create_profile(name_input.value, remote, settings)
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return
            dialog.close()
            on_created(name_input.value)

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=do_create).props("color=primary").mark(
                f"provision-{remote}-new-profile-create"
            )
    dialog.open()


def _profile_section(
    remote: Remote,
    settings: ProvisionSettings,
    active: list[ProvisioningContributor],
    current_name: str | None,
    on_switch,
    on_created,
) -> None:
    """One remote's whole section: a picker over its own named profiles,
    then whichever one is selected rendered inline. Wholly independent of
    the other remote's section -- no shared selection, no shared state."""
    profiles = repo.list_profiles(remote, settings)

    with ui.card().classes("w-full"):
        ui.label(f"{_TITLE[remote]} profiles").classes("text-lg font-bold")
        ui.label(_BLURB[remote]).classes("text-sm text-gray-500")

        with ui.row().classes("w-full items-center gap-3"):
            if profiles:
                select = (
                    ui.select(
                        {p.name: p.name for p in profiles},
                        value=current_name if current_name in {p.name for p in profiles} else None,
                        label="Profile",
                    )
                    .classes("min-w-[240px]")
                    .props("outlined dense")
                    .mark(f"provision-{remote}-profile-select")
                )
                select.on_value_change(lambda e: on_switch(e.value))
            else:
                ui.label("No profiles yet.").classes("text-sm text-gray-500")
            ui.button(
                "New profile",
                icon="add",
                on_click=lambda: _create_profile_dialog(remote, settings, on_created),
            ).props("flat dense").mark(f"provision-{remote}-new-profile")

    current = next((p for p in profiles if p.name == current_name), None)
    if current is not None:
        _profile_body(remote, current, settings, active)


def _path_row(staged_repo: StagedRepo, key: str) -> None:
    """Where `staged_repo` actually lives on disk -- an ordinary git
    working tree, and per ``docs/design.md``'s "anything SpiriConfig can
    do, the user must be able to do without it," there has to be a way to
    find it and go do exactly that (``git log``, a file browser, `cd` and
    look) without this page in the way. Nothing here reads or writes
    anything; it is only ever shown."""
    with ui.row().classes("w-full items-center gap-2"):
        ui.label(f"path: {staged_repo.path}").classes(
            "text-xs text-gray-500 font-mono grow break-all"
        )
        ui.button(
            icon="content_copy",
            on_click=lambda: ui.clipboard.write(str(staged_repo.path)),
        ).props("flat dense round").tooltip("Copy path").mark(f"provision-{key}-copy-path")


def _profile_body(
    remote: Remote,
    profile: Profile,
    settings: ProvisionSettings,
    active: list[ProvisioningContributor],
) -> None:
    _path_row(profile.repo, remote)
    _remote_row(profile.repo, settings, remote)
    _contributor_blocks(profile.repo, active, _BLOCK_HOOK[remote])
    _pending_summary_card(profile.repo, settings, remote)


def page(settings: ProvisionSettings | None = None) -> None:
    """Render the provisioning plugin's page.

    Patch and baseline each get their own tab rather than being stacked on
    screen together -- each section can grow long (contributor blocks, a
    pending-changes card, dialogs), and a reader only ever cares about one
    remote at a time. Independent tab panels, not independent pages: they
    still share this one route, the same "one route per plugin" convention
    every other page follows.
    """
    config = settings or provision_settings()
    active = repo.contributors()

    ui.label("Provisioning").classes("text-2xl font-bold")
    ui.label(
        "Build patch profiles that reset passwords or deliver apps, and "
        "separately, baseline profiles that certify a device's exact "
        "state -- nothing on this page touches this device."
    ).classes("text-sm text-gray-500")

    state: dict[str, str | None] = {"patch": None, "baseline": None, "tab": "patch"}
    container = ui.column().classes("w-full gap-4")

    def switch(remote: Remote, name: str | None) -> None:
        state[remote] = name
        render()

    def created(remote: Remote, name: str) -> None:
        state[remote] = name
        render()

    def render() -> None:
        container.clear()
        with container:
            with ui.tabs().classes("w-full") as tabs:
                tab_elements = {
                    remote: ui.tab(name=remote, label=_TITLE[remote]).mark(
                        f"provision-tab-{remote}"
                    )
                    for remote in ("patch", "baseline")
                }
            tabs.on_value_change(lambda e: state.__setitem__("tab", e.value))
            with ui.tab_panels(tabs, value=state["tab"]).classes("w-full"):
                for remote in ("patch", "baseline"):
                    names = {p.name for p in repo.list_profiles(remote, config)}
                    if state[remote] not in names:
                        state[remote] = next(iter(sorted(names)), None)
                    with ui.tab_panel(tab_elements[remote]).classes("gap-4"):
                        _profile_section(
                            remote,
                            config,
                            active,
                            state[remote],
                            lambda name, remote=remote: switch(remote, name),
                            lambda name, remote=remote: created(remote, name),
                        )

    render()


__all__ = ["page"]
