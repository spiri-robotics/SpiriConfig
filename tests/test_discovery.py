"""Tests for proxied-plugin discovery from docker labels."""

from __future__ import annotations

import json

import pytest

from spiriconfig import discovery, proxy
from spiriconfig.commands import Command, CommandError, Result


def _fake_docker(ids: str, inspect: list[dict]):
    """A stand-in for :func:`spiriconfig.commands.run` over docker.

    Answers ``docker ps`` with ``ids`` and ``docker inspect`` with ``inspect`` as
    JSON, branching on the subcommand, so a test never needs a daemon.
    """

    def fake_run(command: Command, **_: object) -> Result:
        sub = command.argv[1]
        if sub == "ps":
            return Result(command, 0, ids, "")
        if sub == "inspect":
            return Result(command, 0, json.dumps(inspect), "")
        raise AssertionError(f"unexpected docker subcommand: {sub}")

    return fake_run


def _container(**labels: str) -> dict:
    """A minimal inspected container carrying the given plugin labels and an IP."""
    return {
        "Config": {"Labels": {f"spiriconfig.plugin.{k}": v for k, v in labels.items()}},
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.2"}}},
        "State": {"Running": True},
    }


def test_scan_builds_a_target_from_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery,
        "run",
        _fake_docker(
            "abc123\n",
            [_container(name="probe", port="80", title="Probe", icon="science")],
        ),
    )
    (target,) = discovery.scan()
    assert target == proxy.Target(
        name="probe", upstream="http://172.17.0.2:80", title="Probe", icon="science"
    )


def test_scan_defaults_title_and_icon_to_sensible_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        discovery,
        "run",
        _fake_docker("abc123\n", [_container(name="bare", port="8080")]),
    )
    (target,) = discovery.scan()
    assert target.title == "bare"  # falls back to the name
    assert target.icon == "web"  # falls back to the generic app icon


def test_scan_skips_a_container_with_no_port_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        discovery, "run", _fake_docker("abc123\n", [_container(name="noport")])
    )
    assert discovery.scan() == []


def test_scan_skips_a_container_with_no_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _container(name="noip", port="80")
    container["NetworkSettings"]["Networks"] = {"bridge": {"IPAddress": ""}}
    monkeypatch.setattr(discovery, "run", _fake_docker("abc123\n", [container]))
    assert discovery.scan() == []


def test_scan_returns_nothing_when_no_containers_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "run", _fake_docker("", []))
    assert discovery.scan() == []


def test_scan_is_quiet_when_docker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(command: Command, **_: object) -> Result:
        raise CommandError(Result(command, 127, "", "docker: not found"))

    monkeypatch.setattr(discovery, "run", boom)
    assert discovery.scan() == []


def test_set_discovered_replaces_and_manual_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Work on a clean slate and leave one behind: these are module globals.
    monkeypatch.setattr(proxy, "_DISCOVERED", {})
    monkeypatch.setattr(proxy, "_MANUAL", {})

    proxy.set_discovered(
        [proxy.Target("a", "http://x:1", "A"), proxy.Target("b", "http://x:2", "B")]
    )
    assert {t.name for t in proxy.targets()} == {"a", "b"}

    # A later scan replaces the set wholesale -- "a" is gone, not merged.
    proxy.set_discovered([proxy.Target("b", "http://x:2", "B")])
    assert {t.name for t in proxy.targets()} == {"b"}

    # A hand-registered target shadows a discovered one of the same name.
    proxy.register("b", "http://manual:9", title="Manual B")
    assert proxy.get("b").upstream == "http://manual:9"
