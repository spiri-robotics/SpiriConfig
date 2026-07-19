"""Tests for compose project discovery and command construction."""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from spiriconfig.commands import Command, PtySession, Result, run
from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.stacks import (
    NEW_COMPOSE_TEMPLATE,
    Stack,
    StackError,
    Usage,
    _humanize_bytes,
    _parse_bytes,
    _parse_ps,
    _parse_usage,
    create,
    discover,
    find_compose_file,
    find_dev_override,
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

    def test_records_a_dev_override_when_present(self, compose_dir: Path) -> None:
        (compose_dir / "hello" / "compose.dev.yaml").write_text("services: {}\n")
        settings = DockerSettings(compose_dir=compose_dir)
        (stack,) = discover(settings)
        assert stack.dev_override is not None
        assert stack.dev_override.name == "compose.dev.yaml"

    def test_dev_override_is_none_without_one(self, settings: DockerSettings) -> None:
        assert discover(settings)[0].dev_override is None

    def test_a_lone_dev_override_is_not_a_stack(self, compose_dir: Path) -> None:
        """An override needs a base file to layer on; on its own it is not a stack."""
        orphan = compose_dir / "orphan"
        orphan.mkdir()
        (orphan / "compose.dev.yaml").write_text("services: {}\n")
        settings = DockerSettings(compose_dir=compose_dir)
        assert "orphan" not in [s.name for s in discover(settings)]
        assert find_dev_override(orphan) is not None  # the file is there
        assert find_compose_file(orphan) is None  # but there is no base to run


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

    def test_up_dev_layers_the_override_as_a_second_file(
        self, compose_dir: Path
    ) -> None:
        (compose_dir / "hello" / "compose.dev.yaml").write_text("services: {}\n")
        stack = get(DockerSettings(compose_dir=compose_dir), "hello")
        assert list(stack.up(dev=True).argv) == [
            "docker", "compose", "-p", "hello",
            "-f", "compose.yaml", "-f", "compose.dev.yaml", "up", "-d",
        ]

    def test_up_dev_is_a_noop_when_there_is_no_override(
        self, settings: DockerSettings
    ) -> None:
        """`--dev` on a stack that ships none behaves exactly like a plain up."""
        stack = get(settings, "hello")
        assert list(stack.up(dev=True).argv) == list(stack.up().argv)

    def test_up_without_dev_never_names_the_override(self, compose_dir: Path) -> None:
        (compose_dir / "hello" / "compose.dev.yaml").write_text("services: {}\n")
        stack = get(DockerSettings(compose_dir=compose_dir), "hello")
        assert "compose.dev.yaml" not in stack.up().argv

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


class TestExecAndAttach:
    def test_exec_runs_a_shell_by_default(self, settings: DockerSettings) -> None:
        argv = list(get(settings, "hello").exec("web").argv)
        assert argv[-3:] == ["exec", "web", "/bin/sh"]

    def test_exec_runs_whatever_it_is_given(self, settings: DockerSettings) -> None:
        """It is `docker compose exec`, not a shell button: anything the image has."""
        argv = list(get(settings, "hello").exec("web", ["ls", "-la", "/etc"]).argv)
        assert argv[-5:] == ["exec", "web", "ls", "-la", "/etc"]

    def test_exec_asks_for_no_tty_because_it_already_has_one(
        self, settings: DockerSettings
    ) -> None:
        """The `-it` trap. `docker exec` needs it; `docker compose exec` allocates a
        TTY by default and has no such flag -- its only related flag is `-T`, which
        turns the TTY *off*. Passing `-it` here would just be an error."""
        argv = list(get(settings, "hello").exec("web").argv)
        assert "-it" not in argv
        assert "-T" not in argv

    def test_attach_attaches(self, settings: DockerSettings) -> None:
        argv = list(get(settings, "hello").attach("web").argv)
        assert argv[-2:] == ["attach", "web"]

    def test_attach_is_left_exactly_as_docker_ships_it(
        self, settings: DockerSettings
    ) -> None:
        """No --sig-proxy, no --detach-keys. What attach does with a signal is
        documented behaviour of a real command, and bolting on flags the user never
        asked for is where a face over the command line becomes a different program.
        """
        argv = list(get(settings, "hello").attach("web").argv)
        assert not [a for a in argv if a.startswith("--sig-proxy")]
        assert not [a for a in argv if a.startswith("--detach-keys")]


class TestTheExecBabysitter:
    """`exec` grows a pidfile only when somebody has a browser tab to lose.

    The split is the point: the supervised form is what the *web session* runs, and
    the plain form is what we show, what the CLI runs, and what a person would type.
    See `Stack.hangup` for the evidence that the babysitter has to exist at all.
    """

    def test_the_plain_form_is_the_one_a_person_would_type(
        self, settings: DockerSettings
    ) -> None:
        command = get(settings, "hello").exec("web")
        assert str(command).endswith("exec web /bin/sh")
        assert "spiriconfig" not in str(command)

    def test_the_supervised_form_records_its_pid_and_then_execs_the_real_thing(
        self, settings: DockerSettings
    ) -> None:
        argv = list(get(settings, "hello").exec("web", pidfile="/tmp/pf").argv)

        assert argv[-7:-3] == ["exec", "web", "sh", "-c"]
        # The pidfile is $0 and the command is "$@", passed as *arguments* -- so a
        # command with a quote or a space in it cannot rewrite the script it rides in.
        assert argv[-2:] == ["/tmp/pf", "/bin/sh"]
        assert 'exec "$@"' in argv[-3]

    def test_the_supervised_form_still_runs_a_custom_command(
        self, settings: DockerSettings
    ) -> None:
        argv = list(
            get(settings, "hello").exec("web", ["ls", "-la"], pidfile="/tmp/pf").argv
        )
        assert argv[-3:] == ["/tmp/pf", "ls", "-la"]

    def test_a_container_that_cannot_be_written_to_still_gets_a_terminal(
        self, settings: DockerSettings
    ) -> None:
        """A read-only image has nowhere to put a pidfile, and the right answer there
        is a shell that works and does not get reaped -- not a button that refuses to
        open. So the write is allowed to fail and the `exec` runs regardless."""
        script = list(get(settings, "hello").exec("web", pidfile="/tmp/pf").argv)[-3]
        assert "2>/dev/null" in script
        assert "&&" not in script  # a failed write must not swallow the exec

    def test_hangup_kills_what_the_pidfile_names(self, settings: DockerSettings) -> None:
        argv = list(get(settings, "hello").hangup("web", "/tmp/pf").argv)
        assert argv[-7:-3] == ["exec", "-T", "web", "sh"]
        assert argv[-1] == "/tmp/pf"
        assert "kill -HUP" in argv[-2]

    def test_hangup_wants_no_terminal_of_its_own(
        self, settings: DockerSettings
    ) -> None:
        """It is a command we run *at* the container, not a session anyone sits in."""
        assert "-T" in list(get(settings, "hello").hangup("web", "/tmp/pf").argv)


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


class TestStatsCommand:
    """`docker stats`, not `docker compose stats` -- there is no such subcommand."""

    def test_names_the_given_containers(self, settings: DockerSettings) -> None:
        argv = list(get(settings, "hello").stats(["abc", "def"]).argv)
        assert argv == ["docker", "stats", "--no-stream", "abc", "def"]

    def test_json_is_opt_in_for_the_parser(self, settings: DockerSettings) -> None:
        """The CLI wants docker's own table; only the code that parses asks for JSON."""
        assert "--format" not in list(get(settings, "hello").stats(["abc"]).argv)
        argv = list(get(settings, "hello").stats(["abc"], as_json=True).argv)
        assert argv[-3:] == ["--format", "json", "abc"]

    def test_honours_a_custom_docker_binary(self, compose_dir: Path) -> None:
        settings = DockerSettings(compose_dir=compose_dir, docker_bin="/usr/bin/podman")
        assert list(get(settings, "hello").stats(["abc"]).argv)[0] == "/usr/bin/podman"


class TestParseUsage:
    """docker stats --format json is summed across a stack's containers."""

    def test_sums_cpu_and_the_used_side_of_memory(self) -> None:
        stdout = (
            '{"CPUPerc": "0.50%", "MemUsage": "42.98MiB / 61.95GiB"}\n'
            '{"CPUPerc": "1.50%", "MemUsage": "10MiB / 61.95GiB"}'
        )
        usage = _parse_usage(stdout)
        assert usage.cpu_percent == pytest.approx(2.0)
        # 42.98 MiB + 10 MiB, in bytes.
        assert usage.mem_bytes == pytest.approx(round(52.98 * 1024**2), rel=1e-6)

    def test_an_unreadable_row_costs_only_itself(self) -> None:
        """One weird row must not zero out the whole app's number."""
        stdout = (
            '{"CPUPerc": "1.00%", "MemUsage": "10MiB / 1GiB"}\n'
            '{"CPUPerc": "nonsense", "MemUsage": "also nonsense"}'
        )
        usage = _parse_usage(stdout)
        assert usage.cpu_percent == pytest.approx(1.0)
        assert usage.mem_bytes == 10 * 1024**2

    def test_empty_snapshot_is_zero(self) -> None:
        assert _parse_usage("") == Usage(cpu_percent=0.0, mem_bytes=0)


class TestSizeFormatting:
    """Docker prints memory in binary units; we parse and reprint them the same."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("42.98MiB", round(42.98 * 1024**2)),
            ("1GiB", 1024**3),
            ("512B", 512),
            # Decimal units appear on network/block IO; parse them too.
            ("5.67MB", round(5.67 * 1000**2)),
            ("100 kB", round(100 * 1000)),
        ],
    )
    def test_parse_bytes(self, text: str, expected: int) -> None:
        assert _parse_bytes(text) == expected

    def test_parse_bytes_rejects_nonsense(self) -> None:
        with pytest.raises(ValueError, match="not a docker size"):
            _parse_bytes("not a size")

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (round(43.0 * 1024**2), "43.0 MiB"),
            (1024**3, "1.0 GiB"),
        ],
    )
    def test_humanize_bytes(self, n: int, expected: str) -> None:
        assert _humanize_bytes(n) == expected


class TestUsage:
    """The end-to-end read: containers -> docker stats -> one summed Usage.

    Stack is a frozen, slotted dataclass, so fakes go on the class.
    """

    def test_sums_only_running_containers(
        self, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            Stack,
            "containers",
            lambda self: [
                {"ID": "run1", "State": "running"},
                {"ID": "dead1", "State": "exited"},
            ],
        )

        seen: list[list[str]] = []

        def fake_run(command: Command, **kwargs: object) -> Result:
            seen.append(list(command.argv))
            return Result(command, 0, '{"CPUPerc": "2.00%", "MemUsage": "5MiB / 1GiB"}', "")

        monkeypatch.setattr("spiriconfig_docker.stacks.run", fake_run)

        usage = get(settings, "hello").usage()
        assert usage == Usage(cpu_percent=2.0, mem_bytes=5 * 1024**2)
        # Only the running container's id was handed to docker stats.
        assert "run1" in seen[0] and "dead1" not in seen[0]

    def test_nothing_running_is_none_not_zero(
        self, settings: DockerSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stopped app has no usage to show, which is not the same as 0%."""
        monkeypatch.setattr(
            Stack, "containers", lambda self: [{"ID": "x", "State": "exited"}]
        )
        # run must never be reached -- there is nothing to ask docker stats about.
        monkeypatch.setattr(
            "spiriconfig_docker.stacks.run",
            lambda *a, **k: pytest.fail("docker stats should not run"),
        )
        assert get(settings, "hello").usage() is None


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


class TestRunningServices:
    """What exec and attach can be pointed at: services with a live process."""

    @docker_required
    def test_only_lists_services_that_are_actually_up(
        self, unique_stack: Stack
    ) -> None:
        """Running, not merely declared. Offering a service that is down would be
        offering a button whose only outcome is docker saying so."""
        assert unique_stack.running_services() == []

        run(unique_stack.up()).check()
        assert unique_stack.running_services() == ["hello"]

        run(unique_stack.down()).check()
        assert unique_stack.running_services() == []


def _processes(stack: Stack, needle: str) -> int:
    """How many processes in the stack's container match ``needle``.

    Asked from outside with `compose top`, rather than by exec'ing a `ps` in: an
    exec that goes hunting for leaked execs is a good way to find itself.
    """
    result = run(stack._compose("top", "hello"))
    return sum(needle in line for line in result.stdout.splitlines())


async def _eventually(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    """Wait for something to become true, or fail the test.

    Generously, because every poll here is a `docker compose top` -- a round trip to
    the daemon, on a machine that is also running a test suite. The alternative, a
    plausible-looking fixed sleep, passes on an idle laptop and fails at random in a
    full run, which is the worst of both worlds.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.2)
    pytest.fail(f"never became true within {timeout}s")


class TestAnExecDoesNotOutliveTheBrowserTab:
    """The rule the terminal page already keeps, now across the container boundary.

    `docker compose exec` orphans its process when the client goes away, and it has
    done since 2014 -- moby/moby#9098, still open. A closed browser tab *is* the
    client going away, so without `Stack.hangup` every visit to the Exec button
    would leave a shell running inside the user's container, for as long as that
    container lives. Nobody hits this at a real terminal because at a real terminal
    you type `exit`; a browser tab has no `exit`.
    """

    SLEEP = "31417"  # a duration nothing else on the machine will be sleeping for

    @docker_required
    async def test_hanging_up_kills_the_shell_and_what_it_was_running(
        self, unique_stack: Stack
    ) -> None:
        """The whole feature: the shell dies, *and* the runaway command dies with it.

        Through a real :class:`PtySession`, because the second half depends on there
        being a terminal. We only ever kill the shell -- but the shell is a session
        leader with a controlling tty, so the kernel SIGHUPs its foreground process
        group on the way out, and the `sleep` the user walked away from goes with it.
        Take the pty away and that mechanism is gone, and the test would be proving
        something the application does not do.
        """
        run(unique_stack.up()).check()
        pidfile = "/tmp/.spiriconfig-test-pf"

        session = PtySession(unique_stack.exec("hello", pidfile=pidfile))
        await session.start()
        try:
            # The runaway command, left in the foreground exactly as a user leaves it.
            session.write(f"sleep {self.SLEEP}\n")
            await _eventually(lambda: _processes(unique_stack, self.SLEEP) >= 1)
        finally:
            session.close()  # the closed browser tab
            await session.wait()

        # Which, on its own, leaves the sleep running: that is moby#9098, and the
        # canary below asserts it. This is the line that cleans up after docker.
        run(unique_stack.hangup("hello", pidfile)).check()

        await _eventually(lambda: _processes(unique_stack, self.SLEEP) == 0)

    @docker_required
    def test_hanging_up_twice_is_not_an_error(self, unique_stack: Stack) -> None:
        """Both endings can happen -- close the dialog, then close the tab -- and a
        pid that is already gone must look the same as a job well done."""
        run(unique_stack.up()).check()
        result = run(unique_stack.hangup("hello", "/tmp/.spiriconfig-nothing-here"))
        assert result.ok

    @docker_required
    def test_docker_still_has_not_fixed_this(self, unique_stack: Stack) -> None:
        """A canary, not a complaint. If this test ever *fails*, docker has fixed
        moby/moby#9098 -- and `Stack.hangup`, the pidfile, and the whole babysitter
        can be deleted. It is here so that the day that happens, we find out.

        It asserts the bug: an unsupervised exec, whose client we kill, leaves its
        process running inside the container.
        """
        run(unique_stack.up()).check()

        proc = subprocess.Popen(  # noqa: S603
            [*unique_stack.exec("hello", ["sleep", self.SLEEP]).argv],
            cwd=unique_stack.path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if _processes(unique_stack, self.SLEEP):
                    break
                time.sleep(0.1)
            else:
                pytest.fail("the sleep never started; the canary is not testing anything")

            # Kill the client, and give it every chance to clean up on its way out.
            proc.terminate()
            proc.wait(timeout=10)
            time.sleep(2)

            assert _processes(unique_stack, self.SLEEP) >= 1, (
                "docker appears to have fixed moby/moby#9098 -- an exec'd process no "
                "longer outlives its client. Stack.hangup and the pidfile that feeds "
                "it can now be deleted."
            )
        finally:
            proc.kill()


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


class TestCreate:
    def test_rejects_an_unsafe_name_without_making_anything(
        self, settings: DockerSettings
    ) -> None:
        before = set(settings.compose_dir.iterdir())
        for bad in ("", ".", "..", "a/b", ".hidden"):
            with pytest.raises(StackError, match="usable project name"):
                create(settings, bad)
        # Nothing new appeared in the compose directory.
        assert set(settings.compose_dir.iterdir()) == before

    def test_refuses_to_clobber_an_existing_directory(
        self, settings: DockerSettings
    ) -> None:
        # `hello` already exists in the fixture; creating it again must not touch it.
        original = (settings.compose_dir / "hello" / "compose.yaml").read_text()
        with pytest.raises(StackError, match="already exists"):
            create(settings, "hello")
        assert (settings.compose_dir / "hello" / "compose.yaml").read_text() == original

    def test_rejects_invalid_yaml_before_making_a_directory(
        self, settings: DockerSettings
    ) -> None:
        with pytest.raises(StackError, match="not valid YAML"):
            create(settings, "brand-new", "services: [unclosed")
        assert not (settings.compose_dir / "brand-new").exists()

    @docker_required
    def test_creates_a_project_and_returns_its_stack(
        self, settings: DockerSettings
    ) -> None:
        stack = create(settings, "made-here", HELLO_COMPOSE)
        assert stack.name == "made-here"
        assert stack.compose_file == settings.compose_dir / "made-here" / "compose.yaml"
        assert stack.read() == HELLO_COMPOSE
        # It is a real stack, discoverable like any other.
        assert get(settings, "made-here").compose_file == stack.compose_file

    @docker_required
    def test_a_rejected_file_leaves_no_directory_behind(
        self, settings: DockerSettings
    ) -> None:
        """We made the directory, so a compose rejection must clean it up."""
        with pytest.raises(StackError, match="docker compose rejected"):
            create(settings, "doomed", "services:\n  x:\n    not_a_real_key: true\n")
        assert not (settings.compose_dir / "doomed").exists()

    def test_the_default_template_is_valid_yaml(self) -> None:
        """A blank template would fail compose's 'must have services' check, so the
        starter has to be a real, startable stack -- assert it at least parses."""
        import yaml

        parsed = yaml.safe_load(NEW_COMPOSE_TEMPLATE)
        assert "services" in parsed
