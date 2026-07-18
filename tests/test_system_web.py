"""Tests for the system plugin's page.

Opened the way a person opens it, with psutil stubbed so the assertions are about
what the page draws rather than the vitals of whatever machine runs the suite.
"""

from __future__ import annotations

from collections import namedtuple

import typer
from nicegui.testing import User

from spiriconfig import web
from spiriconfig.plugins import Plugin
from spiriconfig_system import system
from spiriconfig_system.config import SystemSettings

_Part = namedtuple("_Part", "device mountpoint fstype opts maxfile maxpath")
_Usage = namedtuple("_Usage", "total used free percent")
_VMem = namedtuple("_VMem", "total used available percent")
_SMem = namedtuple("_SMem", "total used")
_Temp = namedtuple("_Temp", "label current high critical")


class _SystemPage(Plugin):
    name = "system"
    title = "Overview"

    def cli(self) -> typer.Typer:
        return typer.Typer()

    def page(self) -> None:
        from spiriconfig_system import web as system_web

        system_web.page(SystemSettings(required_tools=[]))


def _stub_psutil(monkeypatch) -> None:
    monkeypatch.setattr(system.psutil, "cpu_percent", lambda interval=None: 12.0)
    monkeypatch.setattr(system.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(
        system.psutil,
        "virtual_memory",
        lambda: _VMem(16_000_000_000, 4_000_000_000, 12_000_000_000, 25.0),
    )
    monkeypatch.setattr(system.psutil, "swap_memory", lambda: _SMem(0, 0))
    monkeypatch.setattr(
        system.psutil,
        "disk_partitions",
        lambda all=False: [_Part("/dev/sda1", "/", "ext4", "rw", 255, 4096)],
    )
    monkeypatch.setattr(
        system.psutil,
        "disk_usage",
        lambda mount: _Usage(500_000_000_000, 250_000_000_000, 250_000_000_000, 50.0),
    )
    monkeypatch.setattr(
        system.psutil,
        "sensors_temperatures",
        lambda: {"cpu": [_Temp("Package", 45.0, 90.0, 95.0)]},
    )


async def test_overview_shows_the_vitals(user: User, monkeypatch) -> None:
    _stub_psutil(monkeypatch)
    web.build([_SystemPage()])
    await user.open("/system")
    await user.should_see("Overview")
    await user.should_see("SpiriConfig")
    await user.should_see("Installed as")
    await user.should_see("CPU")
    await user.should_see("Memory")
    await user.should_see("Disk")
    await user.should_see("Temperatures")
    await user.should_see("Package")


async def test_overview_survives_a_host_with_no_sensors(
    user: User, monkeypatch
) -> None:
    _stub_psutil(monkeypatch)
    monkeypatch.setattr(system.psutil, "sensors_temperatures", dict)
    web.build([_SystemPage()])
    await user.open("/system")
    await user.should_see("No sensors reported.")
