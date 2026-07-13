"""Tests for compose project discovery and command construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig.commands import run
from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.stacks import (
    Stack,
    StackError,
    _parse_ps,
    discover,
    find_compose_file,
    get,
)

from tests.conftest import HELLO_COMPOSE, docker_required


class TestDiscovery:
    def test_finds_the_project(self, settings: DockerSettings) -> None:
        assert [s.name for s in discover(settings)] == ["hello"]

    def test_ignores_directories_without_a_compose_file(
        self, settings: DockerSettings
    ) -> None:
        names = [s.name for s in discover(settings)]
        assert "not-a-stack" not in names

    def test_ignores_loose_files(self, settings: DockerSettings) -> None:
        names = [s.name for s in discover(settings)]
        assert "loose.yaml" not in names

    def test_a_missing_compose_directory_is_empty_not_an_error(
        self, tmp_path: Path
    ) -> None:
        """A fresh machine has no compose directory yet. That is not a crash."""
        settings = DockerSettings(compose_dir=tmp_path / "nope")
        assert discover(settings) == []

    def test_projects_are_sorted(self, compose_dir: Path) -> None:
        for name in ("zebra", "apple", "mango"):
            (compose_dir / name).mkdir()
            (compose_dir / name / "compose.yaml").write_text(HELLO_COMPOSE)
        settings = DockerSettings(compose_dir=compose_dir)
        assert [s.name for s in discover(settings)] == [
            "apple",
            "hello",
            "mango",
            "zebra",
        ]

    @pytest.mark.parametrize(
        "filename",
        ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"],
    )
    def test_accepts_every_compose_filename_docker_does(
        self, tmp_path: Path, filename: str
    ) -> None:
        project = tmp_path / "compose" / "thing"
        project.mkdir(parents=True)
        (project / filename).write_text(HELLO_COMPOSE)

        settings = DockerSettings(compose_dir=tmp_path / "compose")
        stacks = discover(settings)
        assert [s.name for s in stacks] == ["thing"]
        assert stacks[0].compose_file.name == filename

    def test_prefers_compose_yaml_like_docker_does(self, tmp_path: Path) -> None:
        project = tmp_path / "compose" / "thing"
        project.mkdir(parents=True)
        (project / "compose.yaml").write_text(HELLO_COMPOSE)
        (project / "docker-compose.yml").write_text(HELLO_COMPOSE)

        found = find_compose_file(project)
        assert found is not None
        assert found.name == "compose.yaml"


class TestGet:
    def test_returns_the_named_project(self, settings: DockerSettings) -> None:
        assert get(settings, "hello").name == "hello"

    def test_an_unknown_name_raises_and_says_what_is_known(
        self, settings: DockerSettings
    ) -> None:
        with pytest.raises(StackError, match="known stacks: hello"):
            get(settings, "nonexistent")

    def test_cannot_escape_the_compose_directory(self, settings: DockerSettings) -> None:
        """Names are matched against what is on disk, so traversal finds nothing."""
        with pytest.raises(StackError):
            get(settings, "../../etc")


class TestCommands:
    """The command line we build is the contract. Assert on it directly."""

    def test_up_runs_compose_up_detached_in_the_project_directory(
        self, settings: DockerSettings
    ) -> None:
        stack = get(settings, "hello")
        command = stack.up()
        assert list(command.argv) == [
            "docker", "compose", "-p", "hello", "-f", "compose.yaml", "up", "-d",
        ]
        assert command.cwd == stack.path

    def test_down(self, settings: DockerSettings) -> None:
        assert list(get(settings, "hello").down().argv)[-1] == "down"

    def test_restart(self, settings: DockerSettings) -> None:
        assert list(get(settings, "hello").restart().argv)[-1] == "restart"

    def test_logs_tails_by_default_and_does_not_follow(
        self, settings: DockerSettings
    ) -> None:
        argv = list(get(settings, "hello").logs().argv)
        assert argv[-2:] == ["logs", "--tail=200"]
        assert "--follow" not in argv

    def test_logs_can_follow(self, settings: DockerSettings) -> None:
        argv = list(get(settings, "hello").logs(follow=True, tail=10).argv)
        assert argv[-3:] == ["logs", "--tail=10", "--follow"]

    def test_the_project_name_pins_to_the_directory_name(
        self, settings: DockerSettings
    ) -> None:
        """If -p disagreed with a bare `docker compose up`, we would be managing
        containers the user could never find from the shell."""
        argv = list(get(settings, "hello").up().argv)
        assert argv[argv.index("-p") + 1] == "hello"

    def test_honours_a_custom_docker_binary(self, compose_dir: Path) -> None:
        settings = DockerSettings(compose_dir=compose_dir, docker_bin="/usr/bin/podman")
        assert list(get(settings, "hello").up().argv)[0] == "/usr/bin/podman"


class TestParsePs:
    """Compose emits either a JSON array or one object per line, by version."""

    def test_parses_a_json_array(self) -> None:
        assert _parse_ps('[{"Name": "a", "State": "running"}]') == [
            {"Name": "a", "State": "running"}
        ]

    def test_parses_newline_delimited_objects(self) -> None:
        stdout = '{"Name": "a", "State": "running"}\n{"Name": "b", "State": "exited"}'
        assert [c["Name"] for c in _parse_ps(stdout)] == ["a", "b"]

    def test_parses_a_single_bare_object(self) -> None:
        assert _parse_ps('{"Name": "a"}') == [{"Name": "a"}]

    def test_empty_output_is_no_containers(self) -> None:
        assert _parse_ps("") == []
        assert _parse_ps("   \n  ") == []

    def test_garbage_is_skipped_not_raised(self) -> None:
        """A daemon warning printed among the JSON must not blow up a page render."""
        stdout = 'not json at all\n{"Name": "a"}'
        assert _parse_ps(stdout) == [{"Name": "a"}]


class TestStatus:
    """Status is derived from container states, so fake the container list.

    Stack is a frozen, slotted dataclass, so the fake goes on the class rather
    than the instance.
    """

    @pytest.mark.parametrize(
        ("containers", "expected"),
        [
            ([], "down"),
            ([{"State": "running"}, {"State": "running"}], "running"),
            ([{"State": "running"}, {"State": "exited"}], "partial"),
            ([{"State": "exited"}, {"State": "exited"}], "stopped"),
            ([{"State": "dead"}], "stopped"),
            # `up -d` returns once containers are *started*, which is a moment
            # before the daemon calls them running. Reporting "stopped" here
            # would be a lie a script could act on.
            ([{"State": "created"}], "partial"),
            # A crash-looping container is not a stopped one.
            ([{"State": "restarting"}], "partial"),
            # An unknown or absent state is "in flux", never "running".
            ([{"State": "some-future-docker-state"}], "partial"),
            ([{}], "partial"),
        ],
    )
    def test_status(
        self,
        settings: DockerSettings,
        monkeypatch: pytest.MonkeyPatch,
        containers: list[dict],
        expected: str,
    ) -> None:
        monkeypatch.setattr(Stack, "containers", lambda self: containers)
        assert get(settings, "hello").status() == expected

    def test_unreachable_docker_reports_down_rather_than_raising(
        self, compose_dir: Path
    ) -> None:
        """A missing docker binary must not crash a page render."""
        settings = DockerSettings(
            compose_dir=compose_dir, docker_bin="definitely-not-a-real-binary-xyz"
        )
        assert get(settings, "hello").status() == "down"

    @docker_required
    def test_a_stack_that_was_just_started_is_never_reported_as_stopped(
        self, unique_stack: Stack
    ) -> None:
        """The bug this guards: `up -d` returns before the daemon says "running",
        so an immediate status check saw `created` and called the stack stopped."""
        run(unique_stack.up()).check()
        assert unique_stack.status() in {"running", "partial"}

    @docker_required
    def test_the_real_lifecycle(self, unique_stack: Stack) -> None:
        assert unique_stack.status() == "down"

        run(unique_stack.up()).check()
        assert unique_stack.status() in {"running", "partial"}

        run(unique_stack.down()).check()
        assert unique_stack.status() == "down"


class TestWrite:
    def test_rejects_invalid_yaml_without_touching_the_file(
        self, settings: DockerSettings
    ) -> None:
        stack = get(settings, "hello")
        with pytest.raises(StackError, match="not valid YAML"):
            stack.write("services: [unclosed")
        assert stack.read() == HELLO_COMPOSE

    @docker_required
    def test_rejects_yaml_that_compose_will_not_accept(
        self, settings: DockerSettings
    ) -> None:
        """Valid YAML that compose rejects would leave an unstartable stack."""
        stack = get(settings, "hello")
        with pytest.raises(StackError, match="docker compose rejected"):
            stack.write("services:\n  broken:\n    not_a_real_key: true\n")
        assert stack.read() == HELLO_COMPOSE, "the working file must survive"

    @docker_required
    def test_saves_a_valid_file(self, settings: DockerSettings) -> None:
        stack = get(settings, "hello")
        updated = HELLO_COMPOSE.replace("alpine:latest", "alpine:3.19")
        stack.write(updated)
        assert stack.read() == updated

    def test_preserves_comments_because_we_write_text_not_parsed_yaml(
        self, settings: DockerSettings
    ) -> None:
        """We never round-trip through a YAML parser, so the user's comments and
        formatting survive a save."""
        stack = get(settings, "hello")
        commented = "# keep me!\n" + HELLO_COMPOSE
        try:
            stack.write(commented)
        except StackError:  # no docker: the YAML check still passed
            pytest.skip("needs docker to complete the write")
        assert "# keep me!" in stack.read()
