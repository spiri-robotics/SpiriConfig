"""Tests for the system plugin's readings.

The collectors are about *shaping* what psutil returns -- deduping bind mounts,
throwing out sentinel temperatures, judging a tool present or not -- so that is
what these exercise, with psutil stubbed. What psutil itself reports on the host
running the suite is not under test.
"""

from __future__ import annotations

from collections import namedtuple

from spiriconfig_system import system
from spiriconfig_system.config import SystemSettings, Tool

# The shapes psutil hands back, reproduced closely enough for the collectors.
_Part = namedtuple("_Part", "device mountpoint fstype opts maxfile maxpath")
_Usage = namedtuple("_Usage", "total used free percent")
_Temp = namedtuple("_Temp", "label current high critical")


def _part(device: str, mount: str, fstype: str = "ext4") -> _Part:
    return _Part(device, mount, fstype, "rw", 255, 4096)


def test_disks_dedupe_by_device_keeping_root_most(monkeypatch) -> None:
    # One device bind-mounted three times, plus a genuinely separate one.
    parts = [
        _part("/dev/sda1", "/var/lib/docker/x"),
        _part("/dev/sda1", "/"),
        _part("/dev/sda1", "/home"),
        _part("/dev/sdb1", "/boot"),
    ]
    monkeypatch.setattr(system.psutil, "disk_partitions", lambda all=False: parts)
    monkeypatch.setattr(
        system.psutil, "disk_usage", lambda mount: _Usage(100, 40, 60, 40.0)
    )

    disks = system.disks()

    # One row per device, and the device's row is its shortest mountpoint.
    assert [(d.device, d.mount) for d in disks] == [
        ("/dev/sdb1", "/boot"),
        ("/dev/sda1", "/"),
    ] or [(d.device, d.mount) for d in disks] == [
        ("/dev/sda1", "/"),
        ("/dev/sdb1", "/boot"),
    ]
    assert {d.device for d in disks} == {"/dev/sda1", "/dev/sdb1"}
    assert next(d for d in disks if d.device == "/dev/sda1").mount == "/"


def test_disks_skip_pseudo_filesystems(monkeypatch) -> None:
    parts = [
        _part("tmpfs", "/run", "tmpfs"),
        _part("overlay", "/var/lib/docker/overlay2/abc/merged", "overlay"),
        _part("/dev/sda1", "/", "ext4"),
    ]
    monkeypatch.setattr(system.psutil, "disk_partitions", lambda all=False: parts)
    monkeypatch.setattr(
        system.psutil, "disk_usage", lambda mount: _Usage(100, 40, 60, 40.0)
    )

    disks = system.disks()

    assert [d.mount for d in disks] == ["/"]


def test_temperatures_drop_sentinel_thresholds(monkeypatch) -> None:
    monkeypatch.setattr(
        system.psutil,
        "sensors_temperatures",
        lambda: {
            "nvme": [
                _Temp("Composite", 40.0, 82.0, 85.0),  # real thresholds, kept
                _Temp("Sensor 1", 40.0, 65261.85, 65261.85),  # sentinels, dropped
            ]
        },
    )

    temps = {t.label: t for t in system.temperatures()}

    assert temps["Composite"].high == 82.0
    assert temps["Sensor 1"].high is None
    assert temps["Sensor 1"].critical is None


def test_temperatures_alarm_only_past_the_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        system.psutil,
        "sensors_temperatures",
        lambda: {
            "cpu": [
                _Temp("cool", 50.0, 90.0, 95.0),
                _Temp("hot", 92.0, 90.0, 95.0),
                _Temp("unbounded", 200.0, None, None),  # no limit -> never alarms
            ]
        },
    )

    temps = {t.label: t for t in system.temperatures()}

    assert temps["cool"].alarming is False
    assert temps["hot"].alarming is True
    assert temps["unbounded"].alarming is False


def test_temperatures_empty_when_none_reported(monkeypatch) -> None:
    monkeypatch.setattr(system.psutil, "sensors_temperatures", dict)
    assert system.temperatures() == []


def test_temperatures_name_and_chip_are_readable(monkeypatch) -> None:
    monkeypatch.setattr(
        system.psutil,
        "sensors_temperatures",
        lambda: {
            "thinkpad": [
                _Temp("CPU", 56.0, None, None),
                _Temp("", 56.0, None, None),  # unlabelled -> "sensor 2"
            ]
        },
    )

    temps = system.temperatures()

    assert [t.name for t in temps] == ["CPU", "sensor 2"]
    # The cryptic chip name is turned into something an operator can read.
    assert {t.chip_name for t in temps} == {"ThinkPad"}


def test_temperatures_drop_unpopulated_zero_slots(monkeypatch) -> None:
    monkeypatch.setattr(
        system.psutil,
        "sensors_temperatures",
        lambda: {
            "thinkpad": [
                _Temp("CPU", 56.0, None, None),
                _Temp("", 0.0, None, None),  # unpopulated slot -> dropped
                _Temp("", 40.0, None, None),  # real, unlabelled -> kept
            ]
        },
    )

    temps = system.temperatures()

    # The 0 C slot is gone; the kept sensors keep their true sysfs index in the
    # name, so the surviving unlabelled one is "sensor 3", not "sensor 2".
    assert [t.name for t in temps] == ["CPU", "sensor 3"]


def test_friendly_chip_falls_back_to_raw_name() -> None:
    assert system.friendly_chip("k10temp") == "CPU"
    assert system.friendly_chip("coretemp-isa-0000") == "CPU"
    assert system.friendly_chip("some_unknown_chip") == "some_unknown_chip"


def test_probe_reports_missing_tool() -> None:
    tool = Tool(name="nope", probe=["definitely-not-a-real-binary-xyzzy"])
    status = system.probe(tool, SystemSettings())
    assert status.installed is False
    assert status.name == "nope"


def test_probe_reports_installed_tool() -> None:
    # `echo` exists on any unix; its stdout stands in for a version banner.
    tool = Tool(name="echoer", probe=["echo", "v1.2.3"])
    status = system.probe(tool, SystemSettings())
    assert status.installed is True
    assert status.detail == "v1.2.3"


class _FakeDist:
    """Stands in for an ``importlib.metadata`` distribution: just its receipt."""

    def __init__(self, direct_url: str | None) -> None:
        self._direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        return self._direct_url if name == "direct_url.json" else None


def _method(monkeypatch, direct_url: str | None) -> tuple[str, str | None]:
    monkeypatch.setattr(system, "distribution", lambda name: _FakeDist(direct_url))
    return system._install_method()


def test_install_method_released_when_no_receipt(monkeypatch) -> None:
    # An install from an index writes no direct_url.json; that is a plain release.
    assert _method(monkeypatch, None) == ("Released package", None)


def test_install_method_editable_carries_the_checkout_path(monkeypatch) -> None:
    receipt = '{"url":"file:///home/op/checkout","dir_info":{"editable":true}}'
    assert _method(monkeypatch, receipt) == ("Editable checkout", "/home/op/checkout")


def test_install_method_local_directory_not_editable(monkeypatch) -> None:
    receipt = '{"url":"file:///opt/build/spiriconfig","dir_info":{}}'
    assert _method(monkeypatch, receipt) == ("Local directory", "/opt/build/spiriconfig")


def test_install_method_version_control_shows_the_commit(monkeypatch) -> None:
    receipt = (
        '{"url":"https://example.com/spiriconfig.git",'
        '"vcs_info":{"vcs":"git","commit_id":"0123456789abcdef"}}'
    )
    method, source = _method(monkeypatch, receipt)
    assert method == "Version control"
    assert source.startswith("0123456789ab")


def test_install_method_missing_distribution_is_a_source_tree(monkeypatch) -> None:
    def _raise(name: str):
        raise system.PackageNotFoundError(name)

    monkeypatch.setattr(system, "distribution", _raise)
    assert system._install_method() == ("Source tree", None)


def test_scope_label_names_the_root_service() -> None:
    assert system._scope_label(system.Scope(system=True)) == "System-wide (root)"


def test_scope_label_per_user_names_the_account() -> None:
    label = system._scope_label(system.Scope(system=False))
    assert label.startswith("Per-user")


def test_release_reports_the_running_version() -> None:
    assert system.release().version == system.spiriconfig.__version__


def test_required_tools_override_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "SPIRICONFIG_SYSTEM_REQUIRED_TOOLS",
        '[{"name":"only-git","probe":["git","--version"]}]',
    )
    settings = SystemSettings()
    assert [t.name for t in settings.required_tools] == ["only-git"]
