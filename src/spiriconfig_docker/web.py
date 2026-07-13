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

from spiriconfig import advanced
from spiriconfig.commands import Command, stream

from spiriconfig_docker.config import DockerSettings, docker_settings
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
    """Show ``command``, stream it, and leave the output on screen to read."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")

        # Advanced only: the exact command line, to copy and run yourself. The
        # output below is shown to everyone -- a regular user still needs to see
        # what went wrong, they just do not need the invocation that caused it.
        with advanced.only(), ui.row().classes(
            "w-full items-center gap-2 bg-gray-100 p-2 rounded"
        ):
            ui.label(str(command)).classes("font-mono text-xs grow break-all")
            ui.button(
                icon="content_copy",
                on_click=lambda: ui.clipboard.write(str(command)),
            ).props("flat dense round").tooltip("Copy command")

        output = ui.log(max_lines=2000).classes("w-full h-96 font-mono text-xs")
        close = ui.button("Close", on_click=dialog.close).props("flat")
        close.disable()

    dialog.open()
    try:
        async for line in stream(command, log=log):
            output.push(line)
    except Exception as exc:  # noqa: BLE001 - surface any failure in the dialog
        log.exception("command failed: {}", command)
        output.push(f"[error] {exc}")
    finally:
        close.enable()


async def _edit_dialog(stack: Stack, on_saved) -> None:
    """Edit a stack's compose file, refusing to save something compose rejects."""
    try:
        text = await asyncio.to_thread(stack.read)
    except OSError as exc:
        ui.notify(f"Could not read {stack.compose_file}: {exc}", type="negative")
        return

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(f"{stack.name} — {stack.compose_file}").classes("text-lg font-bold")
        editor = ui.codemirror(
            text, language="YAML", theme="basicLight"
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


def _stack_card(stack: Stack, status: str, refresh) -> None:
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

        with ui.row().classes("gap-2"):
            ui.button("Up", icon="play_arrow", on_click=lambda: act(stack.up(), "up"))
            ui.button("Down", icon="stop", on_click=lambda: act(stack.down(), "down"))
            ui.button(
                "Restart", icon="restart_alt",
                on_click=lambda: act(stack.restart(), "restart"),
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

    ui.label("Docker Compose").classes("text-2xl font-bold")
    ui.label(f"Projects in {config.compose_dir}").classes("text-sm text-gray-500")

    container = ui.column().classes("w-full gap-2")

    async def render() -> None:
        container.clear()
        stacks = await asyncio.to_thread(discover, config)
        statuses = await _statuses(stacks)
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
                _stack_card(stack, statuses[stack.name], refresh)

    def refresh() -> None:
        ui.timer(0.1, render, once=True)

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=refresh).props("flat")

    refresh()
