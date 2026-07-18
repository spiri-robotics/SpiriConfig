"""The system plugin: the host's vitals, at a glance.

CPU, memory, disk, and temperatures for the machine itself -- distinct from the
docker plugin's per-app usage -- plus a check that the programs SpiriConfig leans
on (``docker``, ``docker compose``, ``git``) are actually installed. It is the
one place that answers "is this box healthy, and does it have what it needs".

This is the plugin that reads the machine through :mod:`psutil` rather than by
shelling out. The overview is a dashboard, not an action: there is nothing to
reproduce, only numbers to report. The commands the design is built around still
show up in the one place they belong -- the required-tools check, where the whole
question *is* "does running this work". See :mod:`spiriconfig_system.system`.

It is not advanced-only: knowing whether the disk is full is an operator's
question, not a developer's.
"""

from __future__ import annotations

import typer

from spiriconfig.plugins import Plugin

from spiriconfig_system.cli import app as cli_app


class SystemPlugin(Plugin):
    """The host's CPU, memory, disk, temperatures, and required tools."""

    name = "system"
    title = "Overview"
    description = "Host CPU, memory, disk, temperatures, and required tools."
    icon = "monitor_heart"

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        # Imported lazily, like the other plugins: `spiriconfig system` should not
        # pay to import a web framework to print a report.
        from spiriconfig_system import web

        web.page()


__all__ = ["SystemPlugin"]
