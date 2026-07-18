"""The NiceGUI face of the system plugin: the host at a glance.

Unlike the other plugins, this page does not run commands you could have run
yourself -- it reads the machine through :mod:`psutil` (see
:mod:`spiriconfig_system.system` for why). So there is nothing to preview or copy
for the vitals; the one place a command appears is the required-tools card, whose
whole job is to show you the ``--version`` line that proves a tool is there.

The numbers refresh on a timer. The CPU sample and the tool probes block, so the
whole snapshot is gathered on a worker thread -- the page never waits on the
event loop for a reading.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import groupby

from loguru import logger
from nicegui import ui

from spiriconfig import advanced, theme
from spiriconfig.commands import Command

from spiriconfig_system import system
from spiriconfig_system.config import SystemSettings, system_settings

log = logger.bind(plugin="system")

#: How often the overview re-reads the machine. A few seconds is live enough for
#: watching a load spike settle, without spawning the tool probes constantly.
_REFRESH_SECONDS = 3.0

#: The disk/memory fill at which a bar turns red. Past this an operator should be
#: looking at freeing space, not admiring the graph.
_FULL_WARN = 0.9


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything the page draws, gathered in one off-thread pass."""

    release: system.Release
    cpu: system.Cpu
    memory: system.Memory
    disks: list[system.Disk]
    temperatures: list[system.Temperature]
    tools: list[system.ToolStatus]


def _collect(settings: SystemSettings) -> Snapshot:
    """Read every vital at once. Blocks (CPU sample, tool probes); run off-thread."""
    return Snapshot(
        release=system.release(),
        cpu=system.cpu(),
        memory=system.memory(),
        disks=system.disks(),
        temperatures=system.temperatures(),
        tools=system.tools(settings),
    )


def _meter(fraction: float) -> None:
    """A labelled fill bar, red once past :data:`_FULL_WARN`."""
    fraction = max(0.0, min(1.0, fraction))
    colour = "negative" if fraction >= _FULL_WARN else "primary"
    ui.linear_progress(value=fraction, show_value=False, size="12px").props(
        f"color={colour} track-color=grey-4"
    ).classes("w-full")


def _section(title: str, icon: str) -> ui.card:
    """A titled card for one group of readings."""
    card = ui.card().classes("w-full")
    with card, ui.row().classes("items-center gap-2"):
        ui.icon(icon).classes("text-xl text-gray-600")
        ui.label(title).classes("text-lg font-bold")
    return card


def _release_card(rel: system.Release) -> None:
    with _section("SpiriConfig", "widgets"):
        with ui.row().classes("w-full items-baseline gap-2"):
            ui.label(f"v{rel.version}").classes("text-2xl font-bold")
            ui.label(rel.scope).classes("text-sm text-gray-500")
        with ui.row().classes("w-full items-baseline gap-2"):
            ui.label("Installed as").classes("text-sm text-gray-500")
            ui.label(rel.method).classes("text-sm font-bold")
        # The checkout path or commit, when the install has one worth showing -- a
        # released package installed from an index does not.
        if rel.source:
            ui.label(rel.source).classes("text-sm text-gray-500 font-mono break-all")


def _cpu_card(cpu: system.Cpu) -> None:
    with _section("CPU", "speed"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(f"{cpu.percent:.0f}%").classes("text-2xl font-bold")
            ui.label(f"across {cpu.cores} cores").classes("text-sm text-gray-500")
        _meter(cpu.percent / 100)
        one, five, fifteen = cpu.load
        ui.label(
            f"Load average: {one:.2f} · {five:.2f} · {fifteen:.2f}  (1 · 5 · 15 min)"
        ).classes("text-sm text-gray-500").tooltip(
            "Runnable processes averaged over time; compare against the core count"
        )


def _memory_card(mem: system.Memory) -> None:
    with _section("Memory", "memory"):
        with ui.row().classes("w-full items-baseline gap-2"):
            ui.label(system.humanize_bytes(mem.used)).classes("text-2xl font-bold")
            ui.label(f"of {system.humanize_bytes(mem.total)} used").classes(
                "text-sm text-gray-500"
            )
        _meter(mem.percent / 100)
        ui.label(f"{system.humanize_bytes(mem.available)} available").classes(
            "text-sm text-gray-500"
        )
        # Swap is only worth a line when the machine has any; a swapless drone
        # image should not carry a "0 B of 0 B" row that means nothing.
        if mem.swap_total:
            ui.label(
                f"Swap: {system.humanize_bytes(mem.swap_used)} of "
                f"{system.humanize_bytes(mem.swap_total)}"
            ).classes("text-sm text-gray-500")


def _disk_card(disks: list[system.Disk]) -> None:
    with _section("Disk", "hard_drive"):
        if not disks:
            ui.label("No mounted filesystems reported.").classes("text-sm text-gray-500")
            return
        for disk in disks:
            with ui.column().classes("w-full gap-1"):
                with ui.row().classes("w-full items-baseline justify-between gap-2"):
                    ui.label(disk.mount).classes("font-mono text-sm").tooltip(
                        f"{disk.device} · {disk.fstype}"
                    )
                    ui.label(
                        f"{system.humanize_bytes(disk.used)} / "
                        f"{system.humanize_bytes(disk.total)}  ·  "
                        f"{system.humanize_bytes(disk.free)} free"
                    ).classes("text-sm text-gray-500")
                _meter(disk.percent / 100)


def _temperature_card(temps: list[system.Temperature]) -> None:
    with _section("Temperatures", "thermostat"):
        if not temps:
            # An honest empty state: many boards and most VMs expose no sensors.
            ui.label("No sensors reported.").classes("text-sm text-gray-500")
            return
        # Grouped by chip so the ThinkPad's six sensors read as one "ThinkPad"
        # group rather than six identical rows. psutil already lists a chip's
        # sensors together, and temperatures() keeps that order, so groupby holds.
        for chip, group in groupby(temps, key=lambda t: t.chip):
            group = list(group)
            with ui.column().classes("w-full gap-1"):
                header = system.friendly_chip(chip)
                label = ui.label(header).classes("text-sm font-bold text-gray-600")
                if header != chip:
                    label.tooltip(f"{chip} (kernel sensor chip)")
                with ui.grid().classes("w-full gap-x-6 gap-y-0 pl-3").style(
                    "grid-template-columns: 1fr auto"
                ):
                    for temp in group:
                        text = (
                            "text-red-600 font-bold" if temp.alarming else "text-gray-700"
                        )
                        ui.label(temp.name).classes(f"text-sm {text}")
                        limit = temp.high if temp.high is not None else temp.critical
                        suffix = f"  (limit {limit:.0f}°C)" if limit is not None else ""
                        ui.label(f"{temp.current:.0f}°C{suffix}").classes(
                            f"text-sm text-right {text}"
                        )


def _tool_row(tool: system.ToolStatus) -> None:
    """One required tool: a tick or a cross, its version, and how it was checked."""
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("w-full items-center gap-2"):
            if tool.installed:
                ui.icon("check_circle").classes("text-green-600")
            else:
                ui.icon("cancel").classes("text-red-600")
            ui.label(tool.name).classes("font-bold")
            ui.label(tool.detail).classes(
                "text-sm text-gray-500 grow break-all"
            )
        # The probe is the one real command on this page; advanced mode reveals it,
        # so "how do I check this myself" has an answer you can copy.
        with advanced.only():
            _copyable(tool.command)


def _copyable(command: Command) -> None:
    """The exact command line, with a button to take it away with you."""
    with ui.row().classes(f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"):
        ui.label(str(command)).classes("font-mono text-xs grow break-all")
        ui.button(
            icon="content_copy",
            on_click=lambda: ui.clipboard.write(str(command)),
        ).props("flat dense round").tooltip("Copy command")


def _tools_card(tools: list[system.ToolStatus]) -> None:
    with _section("Required tools", "build"):
        ui.label(
            "Programs SpiriConfig relies on to manage this machine."
        ).classes("text-sm text-gray-500")
        for tool in tools:
            _tool_row(tool)


def _render(container: ui.column, snapshot: Snapshot) -> None:
    """Draw a whole snapshot into the container."""
    container.clear()
    with container:
        _release_card(snapshot.release)
        _cpu_card(snapshot.cpu)
        _memory_card(snapshot.memory)
        _disk_card(snapshot.disks)
        _temperature_card(snapshot.temperatures)
        _tools_card(snapshot.tools)


def page(settings: SystemSettings | None = None) -> None:
    """Render the system plugin's page."""
    config = settings or system_settings()

    ui.label("Overview").classes("text-2xl font-bold")
    ui.label("This machine's CPU, memory, disk, temperatures, and tools").classes(
        "text-sm text-gray-500"
    )

    container = ui.column().classes("w-full gap-2")

    async def refresh() -> None:
        try:
            snapshot = await asyncio.to_thread(_collect, config)
        except Exception:  # noqa: BLE001 - a bad reading must not kill the timer
            log.exception("could not read the system overview")
            return
        _render(container, snapshot)

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=refresh).props("flat")

    # Pinned to the container's parent slot so render()'s clear() cannot delete the
    # timer whose callback is running -- the same scar the users and docker plugins
    # carry. The first tick fires immediately, so the page is never briefly empty.
    with container.parent_slot:
        ui.timer(_REFRESH_SECONDS, refresh)
    ui.timer(0.1, refresh, once=True)
