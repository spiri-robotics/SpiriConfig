"""Compose projects, and the docker commands that act on them.

A *stack* is one subdirectory of the configured compose directory that contains
a compose file. That is the entire model. It maps exactly onto how ``docker
compose`` already works, so a user who has never run SpiriConfig can drop a
directory in, and a user who never opens the web UI can ``cd`` into one and run
``docker compose up -d`` themselves.

Every function here that acts on a stack returns a :class:`~spiriconfig.commands.Command`
rather than running it. Building and running are kept separate so the UI can show
the user what it is about to do, and so tests can assert on the command line
without a docker daemon anywhere in sight.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from spiriconfig.commands import Command, CommandError, run

from spiriconfig_docker.config import DockerSettings

#: Compose file names, in the precedence order docker compose itself uses.
COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

#: What a freshly created compose file starts as, when the user gives us no text of
#: their own. Deliberately a working stack and not an empty file: ``docker compose
#: config`` rejects a file with no services, so a blank template would fail the very
#: validation :func:`create` runs, and the first thing a new user saw would be an
#: error. ``whoami`` is tiny, real, and starts.
NEW_COMPOSE_TEMPLATE = """\
services:
  whoami:
    image: traefik/whoami
    ports:
      - "8080:80"
"""

#: Container states that mean "this is not coming back on its own".
#:
#: Deliberately a list of what is *dead* rather than what is alive: docker gains
#: new states over time, and an unrecognised one is far more likely to be some
#: flavour of "in progress" than a corpse. Guessing wrong in that direction shows
#: a transient "partial" instead of falsely reporting a running stack as stopped.
DEAD_STATES = frozenset({"exited", "dead", "removing"})

#: What :meth:`Stack.exec` runs when nobody says otherwise. See the method for why
#: it is ``sh`` and not ``bash``.
DEFAULT_EXEC_COMMAND = "/bin/sh"

#: Wraps a supervised ``exec`` so that :meth:`Stack.hangup` has something to aim at.
#: ``$0`` is the pidfile and ``$@`` the real command -- passed as arguments rather
#: than pasted into the script, so that a command with a space or a quote in it
#: cannot rewrite the shell line it travels in.
#:
#: The write is allowed to fail. A read-only container has nowhere to put a pidfile,
#: and the right answer there is a terminal that works and does not get reaped, not
#: a button that refuses to open. Hence ``2>/dev/null`` and no ``&&``: whatever
#: happens to the redirect, the ``exec`` still runs.
_RECORD_PID = 'echo $$ > "$0" 2>/dev/null; exec "$@"'

#: The other half: hang up on whatever that recorded, and tidy the file away.
#: Every failure here is ignored on purpose -- an exec that already exited leaves a
#: pid that is gone, and "kill: no such process" is the sound of there being nothing
#: to clean up. Success and "already clean" should look the same to the caller.
_HANGUP = 'kill -HUP "$(cat "$0" 2>/dev/null)" 2>/dev/null; rm -f "$0"'

log = logger.bind(plugin="docker")


class StackError(Exception):
    """Something is wrong with a stack, or with the request made of it."""


@dataclass(frozen=True, slots=True)
class Stack:
    """One compose project on disk."""

    name: str
    """The directory name, which is also the compose project name."""

    path: Path
    """The project directory."""

    compose_file: Path
    """The compose file within :attr:`path`."""

    settings: DockerSettings

    # -- command construction -------------------------------------------------
    #
    # Note the shape of every command: we cd into the project directory and name
    # the compose file explicitly. `-p <name>` pins the project name so that our
    # containers are the same containers the user gets when they run compose in
    # that directory by hand -- if these disagreed, the UI would be managing a
    # parallel set of containers and the user would never find them.

    def _compose(self, *args: str) -> Command:
        return Command(
            argv=[
                self.settings.docker_bin,
                "compose",
                "-p",
                self.name,
                "-f",
                self.compose_file.name,
                *args,
            ],
            cwd=self.path,
        )

    def up(self) -> Command:
        """Create and start the stack, in the background."""
        return self._compose("up", "-d")

    def down(self) -> Command:
        """Stop and remove the stack's containers."""
        return self._compose("down")

    def restart(self) -> Command:
        """Restart the stack's containers."""
        return self._compose("restart")

    def pull(self) -> Command:
        """Pull the stack's images."""
        return self._compose("pull")

    def logs(self, *, follow: bool = False, tail: int = 200) -> Command:
        """Show the stack's logs."""
        args = ["logs", f"--tail={tail}"]
        if follow:
            args.append("--follow")
        return self._compose(*args)

    def ps(self) -> Command:
        """List the stack's containers, as JSON."""
        return self._compose("ps", "--all", "--format", "json")

    def exec(
        self, service: str, command: Sequence[str] = (), *, pidfile: str | None = None
    ) -> Command:
        """Run a command inside a service's *running* container.

        The command defaults to :data:`DEFAULT_EXEC_COMMAND`, which is a shell,
        because "give me a prompt in there" is what people overwhelmingly want. It
        is only a default: this is ``docker compose exec``, so anything the image
        can run is fair game, and the UI hands the user a box to say so.

        ``/bin/sh`` rather than ``bash``: sh is the one a container is actually
        likely to have. Alpine -- which half the images on a device are built from
        -- ships busybox and no bash at all, and a button that dies with "executable
        file not found" on half the apps is worse than a plainer shell that works on
        all of them. Somebody who wants bash types bash.

        No TTY flag, because there is none to pass: unlike ``docker exec``, which
        wants ``-it``, ``docker compose exec`` allocates a TTY *by default* and the
        only related flag it has is ``-T``, to turn one off. The shell on the far
        side gets a terminal without our asking for one.

        ``pidfile`` is what the web terminal passes, and it is the whole of our
        answer to :meth:`hangup` -- see there for why it has to exist at all. The
        command line stops being the one a person would type when it is set, which
        is precisely why it is *optional*: the CLI and the line shown on the page
        are built without it, and only the session that has a browser tab to lose
        is wrapped.
        """
        argv = list(command) or [DEFAULT_EXEC_COMMAND]
        if pidfile is None:
            return self._compose("exec", service, *argv)
        return self._compose("exec", service, "sh", "-c", _RECORD_PID, pidfile, *argv)

    def hangup(self, service: str, pidfile: str) -> Command:
        """Kill what a ``pidfile``'d :meth:`exec` left running in the container.

        This exists because **docker will not do it for us**. That is a strong claim
        about somebody else's software, so it is worth writing down what was actually
        measured, because every plausible easier answer is wrong:

        - It is not a missing ``-it``. Unlike ``docker exec``, ``docker compose exec``
          allocates a TTY *by default*; the only flag it has is ``-T``, to turn one
          off. Passing ``-it`` to a plain ``docker exec`` leaks identically.
        - It is not a pty we forgot to close. With the client verifiably reaped and
          nothing on the host holding the slave device open, the process inside the
          container carries on regardless.
        - It is not a signal we failed to send. SIGHUP kills the client and leaks;
          SIGKILL leaks; SIGTERM and SIGINT do not even *kill the client*, which
          sits in raw mode forwarding them as bytes. There is no polite signal that
          makes it clean up, because there is nothing listening for one.

        The reason is that an exec'd process is not the client's child. It is
        parented inside the container, and the daemon does not tear an exec down when
        the hijacked API stream drops -- so the shell gets no EOF, no SIGHUP, and no
        timeout. It simply carries on, forever, until the container stops. The docker
        API can start an exec and inspect an exec, and offers no way whatsoever to
        kill one; hence a pidfile, which is the shape everybody who has hit this ends
        up at. Twelve years and counting:

        - https://github.com/moby/moby/issues/9098  (open since 2014: killing the
          ``docker exec`` client does not terminate the spawned process)
        - https://github.com/moby/moby/issues/29700 (the orphan, reparented to
          PPID 0, when the client disconnects)
        - https://github.com/moby/moby/issues/35703 (the request for a
          ``docker exec kill`` that would make this method unnecessary)

        Nobody meets this at a real terminal, because at a real terminal you type
        ``exit`` -- and a shell that exits is clean. A browser tab has no ``exit``;
        closing it *is* the hangup. So the rare accident becomes the ordinary path,
        and we have to do by hand what the tty would have done for us.

        Killing the shell is enough to kill what it was running. It is a session
        leader with a controlling terminal, so the kernel SIGHUPs the foreground
        process group when it dies -- the runaway ``ping`` goes with it, exactly as
        when a real terminal window closes.

        ``-T`` because this one has no terminal and wants none: it is a command we
        run *at* the container, not a session anybody is sitting in.
        """
        return self._compose("exec", "-T", service, "sh", "-c", _HANGUP, pidfile)

    def attach(self, service: str) -> Command:
        """Attach to a running container's main process, stdin included.

        The difference from :meth:`exec` is worth being clear about, because the
        buttons sit next to each other and only one of them is safe to be careless
        with. ``exec`` starts a *new* process alongside the app; you can exit it and
        nothing has happened to the container. ``attach`` connects you to the
        process the container already *is* -- pid 1, the app itself. It is how you
        talk to a REPL an app is serving on its stdin, and how you see output that
        goes nowhere near the logs.

        Deliberately plain: no ``--sig-proxy``, no ``--detach-keys``. Whatever
        ``docker compose attach`` does with a signal is docker's business and its
        documented behaviour, and a person who typed this line into a shell would
        get exactly what this button gives them. Bolting on flags the user did not
        ask for, however well meant, is the point at which the UI stops being a face
        over the command line and starts being a different program.
        """
        return self._compose("attach", service)

    def validate(self) -> Command:
        """Ask docker compose to parse the file and say nothing if it is valid."""
        return self._compose("config", "--quiet")

    # -- state ----------------------------------------------------------------

    def containers(self) -> list[dict]:
        """Return the stack's containers as reported by ``docker compose ps``.

        Returns an empty list if docker is unreachable or the project has never
        been created, because "I cannot tell you about this stack" and "this
        stack has no containers" look the same to a user, and neither is worth
        crashing a page render over. A machine with no docker installed should
        still get a UI that loads and tells them so.
        """
        try:
            result = run(self.ps(), timeout=self.settings.command_timeout, log=log)
        except CommandError as exc:
            # Missing binary, or a hung daemon.
            log.warning("could not reach docker for {!r}: {}", self.name, exc)
            return []
        if not result.ok:
            log.warning("could not list containers for {!r}", self.name)
            return []
        return _parse_ps(result.stdout)

    def status(self) -> str:
        """A one-word summary: ``running``, ``partial``, ``stopped``, or ``down``.

        The three-way split matters more than it looks. ``docker compose up -d``
        returns once it has *started* the containers, which is a moment before the
        daemon reports them as ``running`` -- so a status check immediately after
        an up can legitimately see ``created``. Calling that "stopped" would be a
        lie, and one a script could act on. Anything that is neither running nor
        dead is therefore ``partial``: in flux, ask again shortly.
        """
        containers = self.containers()
        if not containers:
            return "down"

        states = [c.get("State", "") for c in containers]
        if all(state == "running" for state in states):
            return "running"
        if all(state in DEAD_STATES for state in states):
            return "stopped"
        return "partial"

    def running_services(self) -> list[str]:
        """The services with a container up right now, sorted, for exec and attach.

        Running, and not merely declared in the compose file, because both of the
        things this feeds are things you can only do to a *process*. Offering a
        person the name of a service that is not up would be offering them a button
        whose only possible outcome is docker telling them so.

        Which also means this is a snapshot and not a promise: a container can exit
        between the menu being drawn and the user picking from it. The failure is
        then docker's to report, which it does perfectly well -- there is no race
        worth closing here, only one worth not pretending we have closed.
        """
        return sorted(
            {
                service
                for container in self.containers()
                if container.get("State") == "running"
                and (service := container.get("Service"))
            }
        )

    # -- the compose file itself ----------------------------------------------

    def read(self) -> str:
        """Return the compose file's raw text."""
        return self.compose_file.read_text()

    def write(self, text: str) -> None:
        """Write the compose file, refusing to save something invalid.

        Two checks, cheapest first: it must parse as YAML, and docker compose
        itself must accept it. The second is what actually matters -- valid YAML
        that compose rejects would leave a stack the user cannot start.

        The file is only replaced once both checks pass, so a rejected save
        leaves the previous, working file untouched.
        """
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise StackError(f"not valid YAML: {exc}") from exc

        # compose can only validate a file on disk, so the new text has to be
        # written before it can be checked. Every path out of here from this
        # point must therefore put the original back if the check does not pass.
        original = self.read()
        self.compose_file.write_text(text)
        try:
            result = run(self.validate(), timeout=self.settings.command_timeout, log=log)
        except CommandError as exc:
            self.compose_file.write_text(original)
            raise StackError(f"could not run docker compose to check the file: {exc}") from exc

        if not result.ok:
            self.compose_file.write_text(original)
            raise StackError(
                f"docker compose rejected the file, so it was not saved:\n"
                f"{result.stderr.strip()}"
            )
        log.info("wrote {}", self.compose_file)


def _parse_ps(stdout: str) -> list[dict]:
    """Parse ``docker compose ps --format json`` output.

    Compose is inconsistent here: depending on version it emits either a single
    JSON array, or one JSON object per line. Handle both.
    """
    text = stdout.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(parsed, list):
            return parsed
        return [parsed]

    containers = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("could not parse compose ps line: {!r}", line)
    return containers


def find_compose_file(directory: Path) -> Path | None:
    """Return the compose file in ``directory``, or None if there is not one."""
    for name in COMPOSE_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def discover(settings: DockerSettings) -> list[Stack]:
    """Find every compose project in the configured directory, sorted by name.

    A missing compose directory is not an error: it just means there is nothing
    to manage yet, which is the state of a fresh machine.
    """
    root = settings.compose_dir
    if not root.is_dir():
        log.warning("compose directory does not exist: {}", root)
        return []

    stacks = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        compose_file = find_compose_file(child)
        if compose_file is None:
            log.debug("skipping {}: no compose file", child)
            continue
        stacks.append(
            Stack(
                name=child.name,
                path=child,
                compose_file=compose_file,
                settings=settings,
            )
        )
    return stacks


def get(settings: DockerSettings, name: str) -> Stack:
    """Return the named stack, or raise :class:`StackError` if it is unknown.

    Names are matched against what is actually on disk, so a caller cannot reach
    outside the compose directory by passing something like ``../../etc``.
    """
    stacks = discover(settings)
    for stack in stacks:
        if stack.name == name:
            return stack
    known = ", ".join(s.name for s in stacks) or "none"
    raise StackError(f"no such stack: {name!r} (known stacks: {known})")


def _valid_name(name: str) -> bool:
    """Whether ``name`` is a single, safe directory name.

    One path component, no separators, no ``..``, not hidden. Same guard
    :meth:`get` leans on -- a name is only ever a directory *inside* the compose
    directory, so anything that could climb out of it is not a name.
    """
    return bool(name) and name not in {".", ".."} and "/" not in name and not name.startswith(".")


def create(settings: DockerSettings, name: str, text: str = NEW_COMPOSE_TEMPLATE) -> Stack:
    """Create a new compose project: a directory with a ``compose.yaml`` in it.

    The one place SpiriConfig makes a project directory rather than discovering
    one -- see :attr:`~spiriconfig_docker.config.DockerSettings.compose_dir`, which
    is otherwise a tree we only ever read. It is a deliberate, opt-in exception, and
    it earns its keep the same way :meth:`Stack.write` does: the file is validated
    with ``docker compose config`` before we call the project made, so a new stack
    cannot land unstartable.

    The asymmetry with the rest of the module is the point. We *created* this
    directory, so if compose rejects the file we delete the directory again and the
    machine is exactly as it was -- unlike a hand-made project, cleaning up after
    ourselves here is ours to do. That is the same bargain the app store's ``adopt``
    strikes in reverse: touch only what you made.
    """
    if not _valid_name(name):
        raise StackError(
            f"{name!r} is not a usable project name: it must be a single directory "
            f"name, with no '/' and no leading dot."
        )

    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StackError(f"not valid YAML: {exc}") from exc

    path = settings.compose_dir / name
    if path.exists():
        raise StackError(
            f"{path} already exists, so it is not ours to create. Pick another name, "
            f"or edit the existing project."
        )

    # From here on we have made something, so every failure path has to undo it.
    path.mkdir(parents=True)
    compose_file = path / "compose.yaml"
    stack = Stack(name=name, path=path, compose_file=compose_file, settings=settings)
    try:
        compose_file.write_text(text)
        result = run(stack.validate(), timeout=settings.command_timeout, log=log)
    except CommandError as exc:
        shutil.rmtree(path)
        raise StackError(f"could not run docker compose to check the file: {exc}") from exc
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        raise

    if not result.ok:
        shutil.rmtree(path)
        raise StackError(
            f"docker compose rejected the file, so nothing was created:\n"
            f"{result.stderr.strip()}"
        )

    log.info("created {}", compose_file)
    return stack


__all__ = [
    "COMPOSE_FILENAMES",
    "DEFAULT_EXEC_COMMAND",
    "NEW_COMPOSE_TEMPLATE",
    "Stack",
    "StackError",
    "create",
    "discover",
    "find_compose_file",
    "get",
]
