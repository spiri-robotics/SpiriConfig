"""Shared fixtures.

Most tests here never talk to a docker daemon: the interesting logic is in which
command we build, not in what docker does with it. Tests that do need docker are
marked ``docker`` and skipped when it is not available, so the suite passes on a
laptop with no daemon and still means something on one that has it.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.stacks import Stack, get

# NiceGUI's `user` harness renders pages in-process and resets its module globals
# between tests, which matters because routes are registered on a global app and
# would otherwise leak from one test into the next.
#
# Specifically user_plugin, not the full nicegui.testing.plugin: the latter also
# pulls in the selenium-driven `screen` fixture, and we are not going to make the
# test suite depend on a browser to assert that a page renders a label.
pytest_plugins = ["nicegui.testing.user_plugin"]

HELLO_COMPOSE = """\
services:
  hello:
    image: alpine:latest
    command: sh -c "echo hello && sleep 3600"
"""


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


docker_required = pytest.mark.skipif(
    not _docker_available(),
    reason="needs a working `docker compose`",
)


@pytest.fixture
def compose_dir(tmp_path: Path) -> Path:
    """A compose directory with one valid project and two things that are not."""
    root = tmp_path / "compose"

    hello = root / "hello"
    hello.mkdir(parents=True)
    (hello / "compose.yaml").write_text(HELLO_COMPOSE)

    # A directory with no compose file: must be ignored, not crashed on.
    (root / "not-a-stack").mkdir()
    (root / "not-a-stack" / "readme.txt").write_text("nothing to see here")

    # A loose file at the top level: must be ignored too.
    (root / "loose.yaml").write_text("services: {}")

    return root


@pytest.fixture
def settings(compose_dir: Path) -> DockerSettings:
    """Docker settings pointed at the temporary compose directory."""
    return DockerSettings(compose_dir=compose_dir)


@pytest.fixture
def unique_stack(compose_dir: Path) -> Iterator[Stack]:
    """A real stack, with a name no other project on this machine will have.

    Compose project names are global to the docker daemon, so a test project
    called "hello" would collide with the developer's own -- we would be starting
    and stopping *their* containers, and asserting on the result.

    Torn down unconditionally: a test suite must not leave containers running on
    the machine of whoever ran it, including when it fails.
    """
    name = f"spiri-test-{uuid.uuid4().hex[:8]}"
    project = compose_dir / name
    project.mkdir(parents=True)
    (project / "compose.yaml").write_text(HELLO_COMPOSE)

    stack = get(DockerSettings(compose_dir=compose_dir), name)
    try:
        yield stack
    finally:
        subprocess.run(
            ["docker", "compose", "-p", name, "down", "--timeout", "1"],
            cwd=project,
            capture_output=True,
        )
