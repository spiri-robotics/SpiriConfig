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

#: Container states that mean "this is not coming back on its own".
#:
#: Deliberately a list of what is *dead* rather than what is alive: docker gains
#: new states over time, and an unrecognised one is far more likely to be some
#: flavour of "in progress" than a corpse. Guessing wrong in that direction shows
#: a transient "partial" instead of falsely reporting a running stack as stopped.
DEAD_STATES = frozenset({"exited", "dead", "removing"})

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


__all__ = [
    "COMPOSE_FILENAMES",
    "Stack",
    "StackError",
    "discover",
    "find_compose_file",
    "get",
]
