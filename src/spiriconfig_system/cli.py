"""``spiriconfig system`` -- the CLI face of the system plugin.

The web page shows the host's vitals; this prints them, so the overview is not
UI-only. There is no ``--show`` here as there is on the other plugins: this page
does not drive commands you could run instead (it reads the machine through
psutil), so there is no command line to reveal. The one exception is the tool
checks, which *are* commands -- ``--commands`` prints those.
"""

from __future__ import annotations

from itertools import groupby
from typing import Annotated

import typer

from spiriconfig_system import system
from spiriconfig_system.config import SystemSettings, system_settings

app = typer.Typer(
    name="system",
    help="Report this machine's CPU, memory, disk, temperatures, and required tools.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _settings() -> SystemSettings:
    return system_settings()


@app.callback()
def overview(
    ctx: typer.Context,
    commands: Annotated[
        bool,
        typer.Option(
            "--commands", help="Also print the command used to check each tool."
        ),
    ] = False,
) -> None:
    """Print a one-shot report of the host's vitals."""
    # A subcommand may be added later; only run the report for the bare invocation.
    if ctx.invoked_subcommand is not None:
        return

    settings = _settings()

    cpu = system.cpu()
    one, five, fifteen = cpu.load
    typer.echo(
        f"CPU     {cpu.percent:.0f}% of {cpu.cores} cores"
        f"   load {one:.2f} / {five:.2f} / {fifteen:.2f}"
    )

    mem = system.memory()
    typer.echo(
        f"Memory  {system.humanize_bytes(mem.used)} / "
        f"{system.humanize_bytes(mem.total)} ({mem.percent:.0f}%)"
        f"   {system.humanize_bytes(mem.available)} available"
    )
    if mem.swap_total:
        typer.echo(
            f"Swap    {system.humanize_bytes(mem.swap_used)} / "
            f"{system.humanize_bytes(mem.swap_total)}"
        )

    typer.echo("")
    typer.echo("Disk")
    disks = system.disks()
    if not disks:
        typer.echo("  (no mounted filesystems reported)")
    for disk in disks:
        typer.echo(
            f"  {disk.mount:<20} {system.humanize_bytes(disk.used):>10} / "
            f"{system.humanize_bytes(disk.total):>10}  {disk.percent:>5.1f}%"
        )

    typer.echo("")
    typer.echo("Temperatures")
    temps = system.temperatures()
    if not temps:
        typer.echo("  (no sensors reported)")
    for chip, group in groupby(temps, key=lambda t: t.chip):
        typer.echo(f"  {system.friendly_chip(chip)}")
        for temp in group:
            limit = temp.high if temp.high is not None else temp.critical
            suffix = f"  (limit {limit:.0f})" if limit is not None else ""
            mark = "  !!" if temp.alarming else ""
            typer.echo(f"    {temp.name:<18} {temp.current:>5.0f}°C{suffix}{mark}")

    typer.echo("")
    typer.echo("Required tools")
    for tool in system.tools(settings):
        mark = "ok " if tool.installed else "MISSING"
        typer.echo(f"  [{mark:^7}] {tool.name:<16} {tool.detail}")
        if commands:
            typer.echo(f"            $ {tool.command}")


__all__ = ["app"]
