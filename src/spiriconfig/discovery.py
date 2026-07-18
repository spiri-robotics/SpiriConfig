"""Find proxied plugins by asking docker what is running.

A plugin, out of process, is a compose app with labels on it (see
``NOTES-out-of-process-plugins.md``): a container that serves HTTP and declares
itself with ``spiriconfig.plugin.*`` labels. There is no plugin registry of ours to
keep in sync -- the source of truth is ``docker ps``, exactly as it is for the app
store's stacks. Install a plugin app and its container appears here; stop it and it
is gone, with nothing for us to reconcile.

This module turns those labels into :class:`~spiriconfig.proxy.Target` objects and
hands them to the proxy. It only *reads* docker, through the same
:class:`~spiriconfig.commands.Command` seam every other query uses, so the line it
runs is one a user could run themselves:

    $ docker ps -q --filter label=spiriconfig.plugin.name

The upstream address is the container's own IP on its docker network, at the port
the label names. SpiriConfig runs on the host, which can reach a container by that
IP directly -- so a plugin author publishes no host port and picks no number that
could collide, matching the "no port per plugin" decision the notes settled on.
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from spiriconfig import proxy
from spiriconfig.commands import Command, CommandError, run

log = logger.bind(component="discovery")

#: The label namespace a container uses to declare itself a plugin. The presence of
#: ``<prefix>.name`` is what marks a container as one of ours; the rest describe it.
LABEL_PREFIX = "spiriconfig.plugin"


def _label(container: dict, key: str) -> str | None:
    """Read one ``spiriconfig.plugin.<key>`` label off an inspected container."""
    labels = container.get("Config", {}).get("Labels") or {}
    return labels.get(f"{LABEL_PREFIX}.{key}")


def _container_ip(container: dict) -> str | None:
    """The container's first network IP, which the host can reach it at.

    A container can sit on several networks; any of their IPs is reachable from the
    host, so the first one with an address is as good as any. ``None`` when it has
    no address yet -- a container that is starting but not yet attached.
    """
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    for conf in networks.values():
        if ip := (conf or {}).get("IPAddress"):
            return ip
    return None


def _to_target(container: dict) -> proxy.Target | None:
    """Turn one inspected container into a proxy target, or None if it cannot be.

    A container missing the name or port label, or without an IP, is skipped with a
    reason rather than raising: one misconfigured plugin must not blank the sidebar
    for the rest.
    """
    name = _label(container, "name")
    if not name:
        return None  # not one of ours; the ps filter should already exclude it

    port = _label(container, "port")
    if not port:
        log.warning("plugin {!r} has no {}.port label; skipping", name, LABEL_PREFIX)
        return None

    ip = _container_ip(container)
    if not ip:
        log.warning("plugin {!r} has no container IP yet; skipping", name)
        return None

    return proxy.register_target(
        name=name,
        upstream=f"http://{ip}:{port}",
        title=_label(container, "title") or name,
        icon=_label(container, "icon") or "web",
    )


def scan(*, docker_bin: str = "docker", timeout: float = 10.0) -> list[proxy.Target]:
    """Return every running plugin container, as proxy targets.

    Two commands: ``docker ps`` to find the ids carrying our name label, then a
    single ``docker inspect`` to read each one's labels, network, and state. An
    unreachable or absent docker is not an error here -- a machine with no docker
    simply has no plugin containers -- so it logs and returns nothing rather than
    letting a page render fail.
    """
    ps = Command(
        argv=[docker_bin, "ps", "-q", "--filter", f"label={LABEL_PREFIX}.name"]
    )
    try:
        result = run(ps, timeout=timeout, log=log)
    except CommandError as exc:
        log.warning("could not reach docker to discover plugins: {}", exc)
        return []
    if not result.ok:
        return []

    ids = result.stdout.split()
    if not ids:
        return []

    inspect = Command(argv=[docker_bin, "inspect", *ids])
    try:
        result = run(inspect, timeout=timeout, log=log)
    except CommandError as exc:
        log.warning("could not inspect plugin containers: {}", exc)
        return []
    if not result.ok:
        return []

    try:
        containers = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("could not parse docker inspect output: {}", exc)
        return []

    return [target for c in containers if (target := _to_target(c)) is not None]


async def refresh(*, docker_bin: str = "docker", timeout: float = 10.0) -> None:
    """Scan once and publish the result to the proxy.

    The scan shells out to docker, which blocks, so it runs on a worker thread --
    discovery must never be the thing that freezes the event loop it is trying to
    keep a live UI on.
    """
    targets = await asyncio.to_thread(scan, docker_bin=docker_bin, timeout=timeout)
    proxy.set_discovered(targets)


async def run_forever(
    *, interval: float, docker_bin: str = "docker", timeout: float = 10.0
) -> None:
    """Rescan on a loop, so a plugin installed while we run appears without a restart.

    A failing scan is swallowed and retried on the next tick: docker restarting, a
    momentary daemon hiccup, or a container mid-start should cost one stale interval,
    not the discovery loop.
    """
    while True:
        try:
            await refresh(docker_bin=docker_bin, timeout=timeout)
        except Exception:  # noqa: BLE001 - the loop must outlive any single scan
            log.exception("plugin discovery scan failed")
        await asyncio.sleep(interval)
