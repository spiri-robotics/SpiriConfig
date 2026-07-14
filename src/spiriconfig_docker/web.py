"""The NiceGUI face of the docker plugin.

Nothing here may do anything the CLI cannot. Each action opens a dialog that
shows the exact ``docker compose`` command being run, with a copy button, and
then streams its output -- so the UI is a demonstration of the command line
rather than a replacement for it.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from nicegui import ui

from spiriconfig import advanced, terminal, theme
from spiriconfig.commands import Command, stream_pty

from spiriconfig_docker import settings as app_settings
from spiriconfig_docker import widgets
from spiriconfig_docker.config import DockerSettings, docker_settings
from spiriconfig_docker.settings import SettingsError
from spiriconfig_docker.stacks import Stack, StackError, discover

log = logger.bind(plugin="docker")

#: Status word -> Quasar colour, for the badge on each stack.
STATUS_COLOURS = {
    "running": "positive",
    "partial": "warning",
    "stopped": "grey",
    "down": "grey-7",
}


async def _run_in_dialog(title: str, command: Command) -> None:
    """Show ``command``, stream it, and leave the output on screen to read.

    Returns only when the *user* closes the dialog, not when the command
    finishes. That is load-bearing, and the reason is worth writing down.

    Callers do ``await _run_in_dialog(...)`` and then ``refresh()``, and refresh
    clears the container this dialog was created inside -- which deletes the
    dialog. So a version of this that returned as soon as the command exited
    tore its own output off the screen a frame later. Nobody noticed for `up` or
    `pull`, which stream for long enough to read; `logs` finishes instantly, and
    the modal appeared and vanished.

    Waiting for the dismissal fixes it at the source: the output cannot be
    cleared away while the user is still looking at it, because we have not
    handed control back to the code that clears things.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")

        # Advanced only: the exact command line, to copy and run yourself. The
        # output below is shown to everyone -- a regular user still needs to see
        # what went wrong, they just do not need the invocation that caused it.
        with advanced.only(), ui.row().classes(
            f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"
        ):
            ui.label(str(command)).classes("font-mono text-xs grow break-all")
            ui.button(
                icon="content_copy",
                on_click=lambda: ui.clipboard.write(str(command)),
            ).props("flat dense round").tooltip("Copy command")

        output = terminal.terminal()
        close = ui.button("Close", on_click=dialog.close).props("flat")
        close.disable()

    dialog.open()
    try:
        async for chunk in stream_pty(
            command,
            log=log,
            rows=terminal.TERMINAL_ROWS,
            columns=terminal.TERMINAL_COLUMNS,
        ):
            output.write(chunk)
    except Exception as exc:  # noqa: BLE001 - surface any failure in the dialog
        log.exception("command failed: {}", command)
        output.write(f"\r\n[error] {exc}\r\n")
    finally:
        close.enable()

    # Block here until Close (or escape, or a click outside) dismisses it.
    await dialog

    # Then take it away. A closed dialog is still an element on the page, so
    # without this every button press leaves one behind, and a session spent
    # starting and stopping things accretes a pile of invisible modals holding
    # on to their output.
    dialog.delete()


async def _edit_dialog(stack: Stack, on_saved) -> None:
    """Edit a stack's compose file, refusing to save something compose rejects."""
    try:
        text = await asyncio.to_thread(stack.read)
    except OSError as exc:
        ui.notify(f"Could not read {stack.compose_file}: {exc}", type="negative")
        return

    # Asked for before the dialog is built rather than inside it: both of these are
    # round trips, and doing them up front keeps the awaits out of the slot the
    # elements are being created in.
    editor_theme = await theme.codemirror_theme()

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(f"{stack.name} — {stack.compose_file}").classes("text-lg font-bold")
        editor = ui.codemirror(
            text, language="YAML", theme=editor_theme
        ).classes("w-full h-96")

        async def save() -> None:
            # Validation lives in Stack.write, which restores the old file if
            # docker compose rejects the new one -- so a bad save cannot leave a
            # stack unstartable.
            try:
                await asyncio.to_thread(stack.write, editor.value)
            except (StackError, OSError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            ui.notify(f"Saved {stack.compose_file}", type="positive")
            dialog.close()
            on_saved()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).props("color=primary")

    dialog.open()


async def _settings_dialog(stack: Stack) -> str | None:
    """Edit a stack's declared settings. Returns what the user chose to do.

    ``"applied"`` if they asked for the stack to be brought up with the new
    settings, ``"saved"`` if they only saved, and ``None`` if they dismissed it
    without saving.

    Returning the choice rather than acting on it is what keeps the "apply" path
    out of a nested dialog. The command has to be streamed into an output dialog,
    and building that one inside *this* one -- which is mid-teardown by then --
    is a good way to reinvent the disappearing-modal bug. The caller runs it in
    the card's slot instead, exactly as every other action on the card does.
    """
    try:
        config = await asyncio.to_thread(app_settings.for_stack, stack)
        current = await asyncio.to_thread(config.values)
    except (SettingsError, OSError) as exc:
        ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
        return None

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label(f"{stack.name} — settings").classes("text-lg font-bold")

        with ui.column().classes("w-full gap-4"):
            bound = widgets.form(config.fields, current)

        # Advanced only: the file, and the bytes we are about to put in it. The
        # settings page is a nicer way to edit a .env, and a developer should be
        # able to see through it to the file at any point -- including before the
        # write, which is the moment the preview is actually worth anything.
        with advanced.only(), ui.expansion(
            f"{config.env_file}", icon="description"
        ).classes(f"w-full {theme.COMMAND_CLASS}") as expansion:
            preview = ui.label().classes(
                "font-mono text-xs whitespace-pre-wrap break-all"
            )

            def show_preview() -> None:
                """Render the .env as it would be written, with today's answers in it.

                Recomputed when the panel is opened rather than bound to the
                widgets: a settings form is a handful of boxes, and re-rendering
                the whole file on every keystroke of a password field buys nothing
                anyone can see.
                """
                if not expansion.value:
                    return
                try:
                    preview.set_text(config.preview(widgets.values(bound)))
                except SettingsError as exc:
                    preview.set_text(f"# cannot preview: {exc}")

            expansion.on_value_change(show_preview)

        async def save(apply: bool) -> None:
            # Validation and the write both live in StackSettings.save, which puts
            # the old .env back if docker compose will not read the new one -- so a
            # bad save cannot leave a stack that will not start.
            try:
                await asyncio.to_thread(config.save, widgets.values(bound))
            except (SettingsError, OSError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            ui.notify(f"Saved {config.env_file}", type="positive")
            dialog.submit("applied" if apply else "saved")

        with ui.row().classes("w-full justify-end items-center"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=lambda: save(apply=False)).props("flat")
            # The one that does what the user actually came to do. A saved .env
            # changes nothing until compose reads it again, so a settings page
            # without this button is one that appears not to work: you change the
            # port, you save, and the app is still on the old one.
            ui.button(
                "Save & apply", icon="play_arrow", on_click=lambda: save(apply=True)
            ).props("color=primary").tooltip(
                "Save, then run docker compose up -d to restart with the new settings"
            )

    dialog.open()
    result = await dialog
    dialog.delete()
    return result


def _stack_card(stack: Stack, status: str, has_settings: bool, refresh) -> None:
    """One stack: its status, and the things you can do to it."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(stack.name).classes("text-lg font-bold")
                ui.label(str(stack.compose_file)).classes("text-xs text-gray-500")
            ui.badge(status, color=STATUS_COLOURS.get(status, "grey"))

        async def act(command: Command, title: str) -> None:
            await _run_in_dialog(f"{stack.name} — {title}", command)
            refresh()

        async def configure() -> None:
            """Open the settings form, and do whatever the user asked for next.

            The `up` runs out here rather than inside the dialog, in the card's own
            slot -- the same slot every other command on this card is streamed into.
            """
            result = await _settings_dialog(stack)
            if result == "applied":
                await _run_in_dialog(f"{stack.name} — up", stack.up())
            if result is not None:
                refresh()

        with ui.row().classes("gap-2"):
            ui.button("Up", icon="play_arrow", on_click=lambda: act(stack.up(), "up"))
            ui.button("Down", icon="stop", on_click=lambda: act(stack.down(), "down"))
            ui.button(
                "Restart", icon="restart_alt",
                on_click=lambda: act(stack.restart(), "restart"),
            ).props("flat")

            # Not advanced-only, and that is the point of the feature: an app
            # author decided which knobs are safe to turn, so turning them is an
            # ordinary thing for an ordinary user to do. Only shown for an app that
            # declares some -- a Settings button leading to an empty form would be
            # worse than no button at all.
            if has_settings:
                ui.button(
                    "Settings", icon="tune", on_click=configure
                ).props("flat")
            ui.button(
                "Pull", icon="download",
                on_click=lambda: act(stack.pull(), "pull"),
            ).props("flat")
            ui.button(
                "Logs", icon="article",
                on_click=lambda: act(stack.logs(), "logs"),
            ).props("flat")

            # Advanced only: hand-editing the compose file is the most dangerous
            # thing on this page. It stays fully available from the shell --
            # `$EDITOR "$(spiriconfig docker config <stack>)"` -- because hiding a
            # button is decluttering, not a permission.
            with advanced.only():
                ui.button(
                    "Edit", icon="edit",
                    on_click=lambda: _edit_dialog(stack, refresh),
                ).props("flat")


async def _statuses(stacks: list[Stack]) -> dict[str, str]:
    """Fetch every stack's status concurrently, off the event loop.

    Each status is a `docker compose ps`, which is slow enough that doing them
    one after another is noticeable on a machine with a handful of stacks.
    """
    results = await asyncio.gather(
        *(asyncio.to_thread(s.status) for s in stacks),
        return_exceptions=True,
    )
    statuses = {}
    for stack, result in zip(stacks, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("could not get status for {!r}: {}", stack.name, result)
            statuses[stack.name] = "down"
        else:
            statuses[stack.name] = result
    return statuses


def page(settings: DockerSettings | None = None) -> None:
    """Render the docker plugin's page."""
    config = settings or docker_settings()

    ui.label("Apps").classes("text-2xl font-bold")
    ui.label(f"Compose projects in {config.compose_dir}").classes(
        "text-sm text-gray-500"
    )

    container = ui.column().classes("w-full gap-2")

    async def render() -> None:
        container.clear()
        stacks = await asyncio.to_thread(discover, config)
        statuses = await _statuses(stacks)

        # Which stacks have a settings form, worked out in one hop off the event
        # loop. It is a small YAML parse per stack, but it is a disk read per
        # stack, and the list is drawn on every refresh.
        settings_flags = await asyncio.to_thread(
            lambda: {s.name: app_settings.has_settings(s) for s in stacks}
        )

        with container:
            if not stacks:
                with ui.card().classes("w-full"):
                    ui.label("No compose projects found.").classes("text-lg")
                    ui.label(
                        f"Create a directory with a compose file in "
                        f"{config.compose_dir} and it will show up here."
                    ).classes("text-sm text-gray-500")
                return
            for stack in stacks:
                _stack_card(
                    stack,
                    statuses[stack.name],
                    settings_flags[stack.name],
                    refresh,
                )

    def refresh() -> None:
        """Re-render the stack list, shortly.

        The timer is pinned to the slot `container` lives in, and that is not a
        detail -- it is the whole bug.

        A NiceGUI event handler runs with the *clicked element's* slot active. So
        a timer created here during a button press becomes a child of `container`,
        because the button is inside a card inside `container`. Then `render()`
        runs, calls `container.clear()`, and deletes the very timer whose callback
        is executing -- which cancels it, half-done, immediately after the clear
        and before anything is put back. The page goes blank.

        The first render works only by accident: `page()` calls this from the page
        slot, outside the container, so that one timer survives.
        """
        with container.parent_slot:
            ui.timer(0.1, render, once=True)

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=refresh).props("flat")

    refresh()
