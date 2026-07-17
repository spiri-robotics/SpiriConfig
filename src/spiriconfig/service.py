"""Installing and updating SpiriConfig itself -- the one thing not in a container.

The app that manages the containers cannot itself be a container: it has to live
on the host, start on boot, and be updatable in place. ``spiriconfig install`` and
``spiriconfig update`` are how, and -- like everything else here -- they work by
building the commands a person could have run by hand. ``uv tool install``, a
systemd unit, ``systemctl enable``: each is shown before it runs and is copyable,
and none of it is machinery of ours.

It rests on ``uv tool install`` on purpose. That hands us an isolated environment,
a stable executable, ``uv tool upgrade`` for self-update, and a *receipt* recording
how the tool was installed -- so "how do I update?" is a question we put to uv, not
to a manifest of our own. Same "no state that is ours" the app store makes about
symlinks.

Two scopes, and the difference is the whole security story (see
:mod:`spiriconfig.auth`):

* **root** installs a system service under ``/etc/systemd/system`` that runs as
  root. Root can verify any account's password, so this is the only install where
  the PAM login is genuinely multi-user and the users plugin can manage accounts.
* **a normal user** installs a ``systemctl --user`` service under ``~/.config``
  that runs as them. PAM can then only authenticate that one account -- a
  single-operator box. It also needs *linger*, or the service dies at logout,
  which on a headless drone means it never really runs.

Nothing in this module calls ``systemctl`` or ``uv``; it returns
:class:`~spiriconfig.commands.Command` objects and renders file contents as text.
The CLI is what runs them. That keeps the interesting part -- which commands, what
unit -- testable on any machine, including a NixOS one that will not let you drop a
system unit into ``/etc`` the ordinary way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from spiriconfig.commands import Command

#: The systemd unit, the uv tool, and the PyPI distribution all share this name.
SERVICE_NAME = "spiriconfig"
UNIT_FILENAME = f"{SERVICE_NAME}.service"

#: Installing with no source named means the latest published release.
PYPI_SOURCE = SERVICE_NAME

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


class ServiceError(Exception):
    """An install or update cannot proceed, with a message fit to show."""


def _user_config_home() -> Path:
    """``$XDG_CONFIG_HOME`` or ``~/.config`` -- where a user's own units live."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _is_loopback(host: str) -> bool:
    """Whether ``host`` keeps the UI on the box, so ``auth=none`` is not exposure.

    The same test :func:`spiriconfig.web.serve` makes, duplicated rather than
    imported: the install path must not drag in nicegui just to check a string.
    """
    return host in {"localhost", "::1"} or host.startswith("127.")


def executable_path() -> Path:
    """Where ``uv tool install`` puts the ``spiriconfig`` entry point.

    uv writes tool executables into ``$XDG_BIN_HOME``, falling back to
    ``~/.local/bin``. Computed rather than read from ``uv tool dir`` so a
    ``--show`` can render the unit's ``ExecStart`` before uv has run at all.
    """
    xdg_bin = os.environ.get("XDG_BIN_HOME")
    base = Path(xdg_bin) if xdg_bin else Path.home() / ".local" / "bin"
    return base / SERVICE_NAME


@dataclass(frozen=True, slots=True)
class Scope:
    """Where a service is installed, and how ``systemctl`` reaches it.

    ``system`` is the root install (a service for the whole machine); otherwise it
    is the per-user install. Everything that differs between the two -- unit path,
    config path, the ``--user`` flag, the target it is wanted by -- is derived from
    this one bool, so nothing downstream has to branch on it again.
    """

    system: bool

    @classmethod
    def detect(cls) -> Scope:
        """Root gets a system service; anyone else gets a user service."""
        return cls(system=os.geteuid() == 0)

    @property
    def name(self) -> str:
        return "system" if self.system else "user"

    @property
    def unit_path(self) -> Path:
        if self.system:
            return Path("/etc/systemd/system") / UNIT_FILENAME
        return _user_config_home() / "systemd" / "user" / UNIT_FILENAME

    @property
    def env_path(self) -> Path:
        if self.system:
            return Path("/etc/spiriconfig/config.env")
        return _user_config_home() / SERVICE_NAME / "config.env"

    @property
    def wanted_by(self) -> str:
        """The target the unit installs under. Users have no ``multi-user.target``."""
        return "multi-user.target" if self.system else "default.target"

    @property
    def systemctl(self) -> list[str]:
        """The ``systemctl`` argv prefix: user commands carry ``--user``."""
        return ["systemctl"] if self.system else ["systemctl", "--user"]


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """The answers to the install questions, rendered into an ``EnvironmentFile``.

    Auth defaults to ``pam`` on *every* install, user scope included: a login gate
    is the right default even for a single-operator box, where it simply
    authenticates that one account. The storage secret is required for those logins
    to survive a restart, and is passed in rather than generated here so this stays
    pure -- the CLI mints it.
    """

    compose_dir: Path
    storage_secret: str
    auth: str = "pam"
    auth_service: str = "login"
    auth_group: str = "wheel"
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def env(self) -> dict[str, str]:
        """The environment the service runs with, as ``SPIRICONFIG_*`` variables."""
        values = {
            "SPIRICONFIG_HOST": self.host,
            "SPIRICONFIG_PORT": str(self.port),
            "SPIRICONFIG_AUTH": self.auth,
            "SPIRICONFIG_STORAGE_SECRET": self.storage_secret,
            # The docker plugin's own setting: without it the service would default
            # to the checkout's throwaway test_data dir. An installed service is the
            # one place that default is exactly wrong, so install always names it.
            "SPIRICONFIG_DOCKER_COMPOSE_DIR": str(self.compose_dir),
        }
        if self.auth == "pam":
            values["SPIRICONFIG_AUTH_SERVICE"] = self.auth_service
            # Only meaningful as root, where PAM can verify anyone -- the group is
            # what stops every service account logging in. Harmless in a user
            # install, where PAM only ever sees the one account anyway.
            values["SPIRICONFIG_AUTH_GROUP"] = self.auth_group
        return values


def check_exposure(config: ServiceConfig) -> None:
    """Refuse the one install that hands the machine out: off-box with no login.

    The same danger :func:`spiriconfig.web.serve` warns about, escalated from a
    warning to a refusal because an install is durable -- a machine set up this way
    stays set up this way. Loopback with no auth is fine; a real address demands a
    login.
    """
    if config.auth == "none" and not _is_loopback(config.host):
        raise ServiceError(
            f"refusing to install a service on {config.host} with no login "
            "(SPIRICONFIG_AUTH=none): it would be reachable off this host with full "
            "control. Install with auth (the default) or bind to loopback."
        )


def render_env_file(config: ServiceConfig) -> str:
    """The ``EnvironmentFile`` body: one ``KEY=value`` per line.

    Not shell-quoted: systemd reads an ``EnvironmentFile`` itself and takes each
    value literally to the end of the line, so quoting it the way a shell would
    would put the quotes *into* the value. Sorted, so the file is stable to diff
    and the tests are not order-dependent.
    """
    lines = [
        "# Written by `spiriconfig install`. Edit and `systemctl restart spiriconfig`.",
        "# Every line is a plain SPIRICONFIG_* environment variable.",
    ]
    lines += [f"{key}={value}" for key, value in sorted(config.env().items())]
    return "\n".join(lines) + "\n"


def render_unit_file(scope: Scope, exec_path: Path) -> str:
    """The systemd unit, as text.

    ``Type=simple`` for the broadest compatibility, ``Restart=on-failure`` so a
    crash comes back, and the network ordering only for the system service -- a
    user manager has no ``network-online.target`` to wait on. ``ExecStart`` is the
    absolute path to the executable uv installed, not a bare name, because a system
    unit runs with a minimal ``PATH`` that will not have ``~/.local/bin`` on it.
    """
    unit = ["[Unit]", "Description=SpiriConfig configuration and container management"]
    if scope.system:
        unit += ["After=network-online.target", "Wants=network-online.target"]

    service = [
        "",
        "[Service]",
        "Type=simple",
        f"EnvironmentFile={scope.env_path}",
        f"ExecStart={exec_path} serve",
        "Restart=on-failure",
        "RestartSec=2",
        # Gives us $STATE_DIRECTORY -- /var/lib/spiriconfig (system) or
        # ~/.local/state/spiriconfig (user) -- created and owned by systemd. The
        # self-signed TLS cert lives there, so it has to outlast a restart and be
        # writable without us guessing a path. See spiriconfig.tls.state_dir.
        "StateDirectory=spiriconfig",
    ]

    install = ["", "[Install]", f"WantedBy={scope.wanted_by}", ""]
    return "\n".join(unit + service + install)


# --- commands ---------------------------------------------------------------
#
# Each returns a Command; none runs one. The CLI runs them, shows them under
# --show, and (for the file writes, which are not commands) prints the path and
# content instead. Same build-then-run split the docker plugin is built on.


def install_tool_command(
    source: str = PYPI_SOURCE,
    *,
    editable: bool = False,
    force: bool = False,
    uv: str = "uv",
) -> Command:
    """``uv tool install`` for ``source``.

    ``source`` is anything uv accepts: ``spiriconfig`` (latest release),
    ``spiriconfig==1.2.0``, ``git+https://…/spiriconfig@main``, or ``.`` with
    ``editable`` for a working checkout -- which is how you dev-test the installer
    without publishing anything.
    """
    argv = [uv, "tool", "install"]
    if editable:
        argv.append("--editable")
    if force:
        argv.append("--force")
    argv.append(source)
    return Command(argv=argv)


def upgrade_tool_command(*, reinstall: bool = False, uv: str = "uv") -> Command:
    """``uv tool upgrade spiriconfig``.

    ``reinstall`` forces a refetch, which is what a git-tracked install wants: a
    plain upgrade re-resolves versions, but a moved branch is the same "version"
    and only ``--reinstall`` pulls the new commit.
    """
    argv = [uv, "tool", "upgrade"]
    if reinstall:
        argv.append("--reinstall")
    argv.append(SERVICE_NAME)
    return Command(argv=argv)


def daemon_reload_command(scope: Scope) -> Command:
    """Tell systemd to re-read unit files, after we have written one."""
    return Command(argv=[*scope.systemctl, "daemon-reload"])


def enable_command(scope: Scope) -> Command:
    """Enable the service and start it now."""
    return Command(argv=[*scope.systemctl, "enable", "--now", SERVICE_NAME])


def linger_command(user: str) -> Command:
    """Keep a user's services running when they are not logged in.

    Only for a user install, and the difference between a drone whose UI survives a
    reboot and one whose UI is gone the moment the setup session ends.
    """
    return Command(argv=["loginctl", "enable-linger", user])


def restart_command(scope: Scope, *, detached: bool = True) -> Command:
    """Restart the service -- by default via ``systemd-run``, so it outlives us.

    A self-update restarts the very process handling the request, so an in-line
    ``systemctl restart`` would kill the command mid-run and the caller would never
    hear how it went. ``systemd-run --on-active`` hands the restart to systemd to
    do a moment later, letting the request finish first. The same shape the netplan
    plugin will want, for the same reason: the session that asks for the change is
    the session the change takes down.
    """
    if not detached:
        return Command(argv=[*scope.systemctl, "restart", SERVICE_NAME])
    runner = ["systemd-run", *([] if scope.system else ["--user"]), "--on-active=2"]
    return Command(argv=[*runner, *scope.systemctl, "restart", SERVICE_NAME])


def parse_tool_version(text: str, name: str = SERVICE_NAME) -> str | None:
    """Pull one tool's version out of ``uv tool list`` output, or None if absent.

    ``uv tool list`` prints ``name vX.Y.Z`` per installed tool. Reading uv's own
    listing rather than keeping a version of our own is the same choice as reading
    ``docker ps``: the installer's state lives in uv, and we ask.
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name and parts[1].startswith("v"):
            return parts[1][1:]
    return None


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PYPI_SOURCE",
    "SERVICE_NAME",
    "UNIT_FILENAME",
    "Scope",
    "ServiceConfig",
    "ServiceError",
    "check_exposure",
    "daemon_reload_command",
    "enable_command",
    "executable_path",
    "install_tool_command",
    "linger_command",
    "parse_tool_version",
    "render_env_file",
    "render_unit_file",
    "restart_command",
    "upgrade_tool_command",
]
