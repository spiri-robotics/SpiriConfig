"""Tests for the CLI.

``--show`` gets particular attention. It is the promise that the user is never
trapped: whatever the tool is about to do, it will tell you the command to do it
yourself. If it ever printed something that was not exactly what we run, that
promise would be a lie, so it is tested against the command we actually build.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spiriconfig.cli import app as root_app
from spiriconfig_docker.cli import app as docker_app
from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.stacks import get

from tests.conftest import HELLO_COMPOSE, docker_required

runner = CliRunner()


@pytest.fixture(autouse=True)
def point_at_the_test_compose_dir(
    compose_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI reads its settings from the environment, so set the environment."""
    monkeypatch.setenv("SPIRICONFIG_DOCKER_COMPOSE_DIR", str(compose_dir))


class TestRootCli:
    def test_mounts_the_docker_plugin_as_a_subcommand(self) -> None:
        result = runner.invoke(root_app, ["--help"])
        assert result.exit_code == 0
        assert "docker" in result.stdout

    def test_plugins_lists_the_docker_plugin(self) -> None:
        result = runner.invoke(root_app, ["plugins"])
        assert result.exit_code == 0
        assert "docker" in result.stdout

    def test_docker_subcommand_is_reachable_from_the_root(self) -> None:
        result = runner.invoke(root_app, ["docker", "--help"])
        assert result.exit_code == 0


class TestList:
    def test_lists_the_project(self) -> None:
        result = runner.invoke(docker_app, ["list"])
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_says_so_when_there_is_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_DOCKER_COMPOSE_DIR", str(tmp_path / "empty"))
        result = runner.invoke(docker_app, ["list"])
        assert result.exit_code == 0
        assert "No compose projects found" in result.stdout


class TestShow:
    """--show must print the command, run nothing, and be honest about both."""

    @pytest.mark.parametrize(
        "command", ["up", "down", "restart", "pull", "logs", "ps"]
    )
    def test_prints_the_exact_command_we_would_have_run(
        self, command: str, compose_dir: Path
    ) -> None:
        result = runner.invoke(docker_app, [command, "hello", "--show"])
        assert result.exit_code == 0

        stack = get(DockerSettings(compose_dir=compose_dir), "hello")
        expected = str(getattr(stack, command)())
        assert result.stdout.strip() == expected

    def test_the_printed_command_is_a_real_docker_compose_invocation(self) -> None:
        result = runner.invoke(docker_app, ["up", "hello", "--show"])
        printed = result.stdout.strip()
        assert "docker compose" in printed
        assert printed.endswith("up -d")

    @docker_required
    def test_show_does_not_actually_run_anything(
        self, compose_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: --show is safe to run on a production box.

        The project gets a unique name, because compose project names are global
        to the docker daemon -- asking it about a project called "hello" would
        happily find someone else's, and this test would be testing their
        containers instead of ours.
        """
        name = f"spiri-show-{uuid.uuid4().hex[:8]}"
        project = compose_dir / name
        project.mkdir()
        (project / "compose.yaml").write_text(HELLO_COMPOSE)
        monkeypatch.setenv("SPIRICONFIG_DOCKER_COMPOSE_DIR", str(compose_dir))

        result = runner.invoke(docker_app, ["up", name, "--show"])
        assert result.exit_code == 0

        ps = subprocess.run(
            ["docker", "compose", "-p", name, "ps", "-q"],
            capture_output=True,
            text=True,
        )
        assert ps.stdout.strip() == "", "--show started containers!"


class TestErrors:
    def test_an_unknown_project_exits_nonzero_and_says_what_exists(self) -> None:
        result = runner.invoke(docker_app, ["up", "nonexistent"])
        assert result.exit_code == 1
        assert "no such stack" in result.output
        assert "hello" in result.output, "should say what it does know about"


class TestConfig:
    def test_prints_the_path_so_the_shell_can_use_it(self, compose_dir: Path) -> None:
        """`$EDITOR "$(spiriconfig docker config hello)"` must work."""
        result = runner.invoke(docker_app, ["config", "hello"])
        assert result.exit_code == 0
        printed = Path(result.stdout.strip())
        assert printed == compose_dir / "hello" / "compose.yaml"
        assert printed.is_file()
