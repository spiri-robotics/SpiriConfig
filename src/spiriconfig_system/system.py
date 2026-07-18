"""Reading the host's vitals: CPU, memory, disk, temperatures, and tools.

This is the one plugin that reads the machine through :mod:`psutil` rather than
by shelling out to the command a human would run. The overview is a dashboard,
not an action -- there is nothing here to reproduce or audit, only numbers to
report -- and psutil turns per-core times, ``/proc/meminfo``, and the sysfs
thermal zones into one portable call each instead of a pile of platform-specific
parsing. The one exception is :func:`tools`, which *does* run real commands,
because "is this program installed" is answered by trying to run it.

Every function here is synchronous, and the blocking ones say so: :func:`cpu`
samples over a short interval and the probes in :func:`tools` spawn processes.
Callers on the event loop run them through :func:`asyncio.to_thread`, the same
way the docker plugin runs ``docker stats``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from urllib.parse import unquote, urlparse

import psutil
from loguru import logger

import spiriconfig
from spiriconfig.service import SERVICE_NAME, Scope

from spiriconfig.commands import Command, CommandError, run

from spiriconfig_system.config import SystemSettings, Tool

log = logger.bind(plugin="system")

#: Sampling window for :func:`cpu`. Long enough that the percentage is not noise,
#: short enough that a page refresh does not visibly stall on it.
_CPU_SAMPLE_SECONDS = 0.4

#: Filesystem types that are not real storage a person cares about the free space
#: of -- kernel bookkeeping, tmpfs, container overlays, squashfs snaps. Skipped so
#: the disk section is the handful of mounts an operator actually manages.
_PSEUDO_FSTYPES = frozenset(
    {
        "",
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fuse.portal",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)


@dataclass(frozen=True, slots=True)
class Cpu:
    """How busy the processors are, right now and lately."""

    percent: float
    """Overall use across all cores, 0-100, sampled over a short window."""

    cores: int
    """Number of logical cores, so a load average has something to be read against."""

    load: tuple[float, float, float]
    """The 1-, 5-, and 15-minute load averages."""


@dataclass(frozen=True, slots=True)
class Memory:
    """Physical and swap memory, in bytes, plus the percentage used."""

    total: int
    used: int
    available: int
    percent: float
    swap_total: int
    swap_used: int


@dataclass(frozen=True, slots=True)
class Disk:
    """One mounted filesystem's space, in bytes."""

    mount: str
    device: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


#: The band a real Celsius reading falls in. An unset nvme threshold comes back as
#: 65261.85 and a garbage sensor as a negative; anything outside is treated as "no
#: threshold" rather than shown as a 65261 C alarm line.
_CELSIUS_MIN = 0.0
_CELSIUS_MAX = 200.0

#: Human names for the kernel's hwmon chips, which are otherwise cryptic:
#: ``k10temp`` and ``thinkpad`` mean nothing to an operator. Best-effort -- an
#: unknown chip keeps its raw name, which is still better than a wrong guess. The
#: key is matched as a prefix, so ``coretemp`` covers ``coretemp-isa-0000`` too.
_CHIP_NAMES = {
    "k10temp": "CPU",
    "zenpower": "CPU",
    "coretemp": "CPU",
    "cpu_thermal": "CPU",
    "cpu-thermal": "CPU",
    "amdgpu": "GPU",
    "nouveau": "GPU",
    "nvidia": "GPU",
    "nvme": "NVMe drive",
    "acpitz": "Mainboard",
    "thinkpad": "ThinkPad",
    "ath12k_hwmon": "Wi-Fi",
    "ath11k_hwmon": "Wi-Fi",
    "iwlwifi": "Wi-Fi",
}


def friendly_chip(chip: str) -> str:
    """A readable name for a hwmon chip, or the raw name if we do not know it."""
    lowered = chip.lower()
    for prefix, name in _CHIP_NAMES.items():
        if lowered.startswith(prefix):
            return name
    return chip


@dataclass(frozen=True, slots=True)
class Temperature:
    """One thermal sensor's reading, in degrees Celsius.

    A sensor belongs to a :attr:`chip` (``thinkpad``, ``nvme`` …) and may or may
    not carry a :attr:`label` of its own -- many do not, so :attr:`name` falls
    back to its position within the chip. Callers group by chip for display, so
    the six unlabelled ThinkPad sensors read as one "ThinkPad" group rather than
    six identical rows.
    """

    chip: str
    label: str
    index: int
    """Zero-based position within the chip, used to name an unlabelled sensor."""
    current: float
    high: float | None
    critical: float | None

    @property
    def chip_name(self) -> str:
        """The chip's human name, e.g. ``ThinkPad`` for ``thinkpad``."""
        return friendly_chip(self.chip)

    @property
    def name(self) -> str:
        """This sensor's own name: its label, or ``sensor N`` if it has none."""
        return self.label or f"sensor {self.index + 1}"

    @property
    def alarming(self) -> bool:
        """Whether this sensor is at or past the point worth colouring red.

        The ``high`` threshold is the chip's own "getting hot" line; if it does
        not publish one, ``critical`` is used, and if neither, nothing is alarming
        because there is nothing to compare against.
        """
        limit = self.high if self.high is not None else self.critical
        return limit is not None and self.current >= limit


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """Whether a required tool is installed, and what it reported."""

    name: str
    command: Command
    installed: bool
    detail: str
    """Its version line if installed, or the reason it was judged missing."""


@dataclass(frozen=True, slots=True)
class Release:
    """Which SpiriConfig this is, and how it got onto the machine.

    The :attr:`version` is the running package's own. :attr:`scope` is the half of
    "install method" that matters operationally -- a machine-wide root service, or a
    single-user one (see :class:`~spiriconfig.service.Scope`): the same bool the
    installer branches the whole security story on. :attr:`method` is the package
    origin ("Editable checkout", "Released package" …) and :attr:`source` its
    one-line detail -- the checkout path, or the commit -- when there is one.
    """

    version: str
    scope: str
    method: str
    source: str | None


def _file_url_path(url: str | None) -> str | None:
    """The filesystem path behind a ``file://`` install URL, for display.

    A local install records its origin as ``file:///home/you/checkout``; the bare
    path is what an operator recognises. Anything that is not a ``file://`` URL is
    handed back untouched -- a plain path already, or nothing to show.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return url
    return unquote(parsed.path) or None


def _install_method() -> tuple[str, str | None]:
    """Classify how the running ``spiriconfig`` distribution was installed.

    Read from pip's PEP 610 receipt (``direct_url.json`` in the ``.dist-info``),
    the same "ask the installer, keep no state of our own" the install path takes
    with ``uv tool list``. An install from an index (a published release) writes no
    such file; a local, VCS, or URL install does, and says which.
    """
    try:
        dist = distribution(SERVICE_NAME)
    except PackageNotFoundError:
        # Running from a source tree that was never installed as a distribution.
        return "Source tree", None

    raw = dist.read_text("direct_url.json")
    if raw is None:
        # No direct-URL receipt means it came from an index -- a normal release.
        return "Released package", None
    try:
        data = json.loads(raw)
    except ValueError:
        return "Unknown", None

    url = data.get("url")
    if isinstance(data.get("dir_info"), dict):
        path = _file_url_path(url)
        if data["dir_info"].get("editable"):
            return "Editable checkout", path
        return "Local directory", path
    vcs = data.get("vcs_info")
    if isinstance(vcs, dict):
        commit = vcs.get("commit_id")
        detail = f"{commit[:12]} · {url}" if commit else url
        return "Version control", detail
    return "Direct URL", url


def _scope_label(scope: Scope) -> str:
    """How this install is scoped, in the operator's terms rather than a bool.

    Root is the machine-wide service; anyone else is a single-operator install
    running as that account, so name the account -- it is the one PAM can log in.
    """
    if scope.system:
        return "System-wide (root)"
    try:
        import pwd

        return f"Per-user ({pwd.getpwuid(os.geteuid()).pw_name})"
    except (ImportError, KeyError):
        return "Per-user"


def release() -> Release:
    """The running SpiriConfig's version, scope, and how it was installed."""
    method, source = _install_method()
    return Release(
        version=spiriconfig.__version__,
        scope=_scope_label(Scope.detect()),
        method=method,
        source=source,
    )


def cpu() -> Cpu:
    """Sample overall CPU use and read the load averages. **Blocks briefly.**

    ``psutil.cpu_percent`` measures over the interval it is given -- a single
    call with no interval would return 0.0 the first time, having nothing to
    diff against -- so this sleeps for :data:`_CPU_SAMPLE_SECONDS`. That is why
    callers run it off the event loop.
    """
    percent = psutil.cpu_percent(interval=_CPU_SAMPLE_SECONDS)
    cores = psutil.cpu_count(logical=True) or 1
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        # No load average on this platform (Windows); report zeros rather than
        # inventing a number. Linux, the only target that matters, always has it.
        load = (0.0, 0.0, 0.0)
    return Cpu(percent=percent, cores=cores, load=load)


def memory() -> Memory:
    """Physical and swap memory use, in bytes."""
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return Memory(
        total=virtual.total,
        used=virtual.used,
        available=virtual.available,
        percent=virtual.percent,
        swap_total=swap.total,
        swap_used=swap.used,
    )


def disks() -> list[Disk]:
    """Every real storage device and how full it is, one row apiece.

    Pseudo filesystems (:data:`_PSEUDO_FSTYPES`) are dropped, and a mount we are
    not allowed to stat is skipped rather than raising -- a removable drive can
    vanish between listing the mounts and asking about one, and an unreadable
    mount is not worth taking the whole page down for.

    Deduplicated by device, keeping the shortest (root-most) mountpoint: a host
    can bind-mount one filesystem into a dozen places -- container setups and
    NixOS especially -- and the overview wants the *global* disk picture the todo
    asked for, which is one line per real device, not one per bind mount of it.
    """
    by_device: dict[str, Disk] = {}
    # Shortest mountpoint first, so the canonical mount of a device (``/`` over
    # ``/var/lib/docker``) is the one that wins the dedupe below.
    partitions = sorted(psutil.disk_partitions(all=False), key=lambda p: len(p.mountpoint))
    for part in partitions:
        if part.fstype.lower() in _PSEUDO_FSTYPES:
            continue
        if part.device in by_device:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError) as exc:
            log.debug("could not stat {}: {}", part.mountpoint, exc)
            continue
        by_device[part.device] = Disk(
            mount=part.mountpoint,
            device=part.device,
            fstype=part.fstype,
            total=usage.total,
            used=usage.used,
            free=usage.free,
            percent=usage.percent,
        )
    return sorted(by_device.values(), key=lambda d: d.mount)


def _threshold(value: float | None) -> float | None:
    """A sensor threshold, or None if it is missing or an obvious sentinel.

    psutil reports a chip's own high/critical lines, but an unset one is not
    absent -- it is whatever garbage the register held, classically an nvme's
    65261.85. Anything outside :data:`_PLAUSIBLE_CELSIUS` is treated as no
    threshold at all.
    """
    if value is None or not (_CELSIUS_MIN <= value <= _CELSIUS_MAX):
        return None
    return value


def temperatures() -> list[Temperature]:
    """Every thermal sensor the machine exposes, in Celsius.

    Returns an empty list when there are none -- a VM, a board with no sensors,
    or an OS that does not surface them (macOS). That is a fact worth showing
    plainly ("no sensors reported"), not an error.
    """
    try:
        by_chip = psutil.sensors_temperatures()
    except AttributeError:
        # psutil has no sensors_temperatures on this platform at all.
        return []

    readings: list[Temperature] = []
    for chip, entries in by_chip.items():
        for index, entry in enumerate(entries):
            # An unlabelled sensor reading exactly 0 C with no thresholds is an
            # unpopulated slot, not a component at freezing -- thinkpad_acpi lists
            # several. Drop them so the card is the sensors that are really there.
            if not entry.label and entry.current == 0.0 and entry.high is None:
                continue
            readings.append(
                Temperature(
                    chip=chip,
                    label=entry.label,
                    index=index,
                    current=entry.current,
                    high=_threshold(entry.high),
                    critical=_threshold(entry.critical),
                )
            )
    return readings


def probe(tool: Tool, settings: SystemSettings) -> ToolStatus:
    """Run one tool's version command and report whether it is installed.

    A tool that is not on ``PATH`` comes back from :func:`~spiriconfig.commands.run`
    as a :class:`CommandError` (exit 127, "executable not found"); a tool that is
    there but answered non-zero is still installed -- it ran -- so only a launch
    failure counts as missing. **Spawns a process; blocks.**
    """
    command = Command(tool.probe)
    try:
        result = run(command, timeout=settings.command_timeout, log=log)
    except CommandError as exc:
        return ToolStatus(
            name=tool.name,
            command=command,
            installed=False,
            detail=exc.result.stderr.strip() or "not installed",
        )
    # It launched, so it exists. The first line of stdout is the version banner;
    # some tools print it to stderr instead, so fall back to that.
    banner = (result.stdout or result.stderr).strip().splitlines()
    return ToolStatus(
        name=tool.name,
        command=command,
        installed=True,
        detail=banner[0] if banner else "installed",
    )


def tools(settings: SystemSettings) -> list[ToolStatus]:
    """Check every required tool. **Spawns a process per tool; blocks.**"""
    return [probe(tool, settings) for tool in settings.required_tools]


def humanize_bytes(n: int) -> str:
    """Render a byte count in binary units with one decimal, e.g. ``12.4 GiB``.

    A local copy rather than an import from the docker plugin: plugins do not
    depend on one another, and this is four lines.
    """
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"
