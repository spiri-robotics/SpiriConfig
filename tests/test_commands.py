"""Tests for the command layer.

The most important test in this file is the one asserting that ``str(Command)``
is copy-pasteable, because that is the promise the whole project rests on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig.commands import Command, CommandError, run, stream, stream_pty


class TestCommandRendering:
    def test_renders_as_a_shell_line(self) -> None:
        command = Command(argv=["docker", "compose", "up", "-d"])
        assert str(command) == "docker compose up -d"

    def test_includes_a_cd_when_there_is_a_working_directory(self) -> None:
        command = Command(argv=["docker", "compose", "up"], cwd=Path("/srv/compose/foo"))
        assert str(command) == "cd /srv/compose/foo && docker compose up"

    def test_includes_env_as_a_prefix(self) -> None:
        command = Command(argv=["docker", "ps"], env={"DOCKER_HOST": "tcp://x:1"})
        assert str(command) == "DOCKER_HOST=tcp://x:1 docker ps"

    def test_quotes_arguments_that_a_shell_would_mangle(self) -> None:
        command = Command(
            argv=["docker", "compose", "-f", "my compose.yaml", "logs"],
            cwd=Path("/srv/my stacks"),
        )
        # Without quoting, pasting this would cd into the wrong place and pass
        # two arguments where one was meant.
        assert str(command) == (
            "cd '/srv/my stacks' && docker compose -f 'my compose.yaml' logs"
        )

    def test_the_rendered_line_actually_runs_in_a_shell(self, tmp_path: Path) -> None:
        """The rendered string is not decorative; a shell must accept it."""
        import subprocess

        awkward = tmp_path / "a dir with spaces"
        awkward.mkdir()
        command = Command(argv=["echo", "it works"], cwd=awkward)

        proc = subprocess.run(
            str(command), shell=True, capture_output=True, text=True, check=True
        )
        assert proc.stdout.strip() == "it works"


class TestRun:
    def test_captures_output_of_a_successful_command(self) -> None:
        result = run(Command(argv=["echo", "hello"]))
        assert result.ok
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_reports_a_failing_command_without_raising(self) -> None:
        result = run(Command(argv=["sh", "-c", "exit 3"]))
        assert not result.ok
        assert result.returncode == 3

    def test_check_raises_on_failure(self) -> None:
        result = run(Command(argv=["sh", "-c", "echo nope >&2; exit 1"]))
        with pytest.raises(CommandError, match="exit code 1"):
            result.check()

    def test_check_returns_self_on_success(self) -> None:
        result = run(Command(argv=["true"]))
        assert result.check() is result

    def test_a_missing_executable_is_a_command_error_not_a_traceback(self) -> None:
        with pytest.raises(CommandError, match="executable not found"):
            run(Command(argv=["definitely-not-a-real-binary-xyz"]))

    def test_a_hung_command_times_out(self) -> None:
        with pytest.raises(CommandError, match="timed out"):
            run(Command(argv=["sleep", "10"]), timeout=0.2)

    def test_env_is_overlaid_on_the_real_environment(self) -> None:
        """Setting one variable must not blank out PATH and everything else."""
        result = run(
            Command(argv=["sh", "-c", "echo $SPIRI_TEST:$PATH"], env={"SPIRI_TEST": "x"})
        )
        stdout = result.stdout.strip()
        assert stdout.startswith("x:")
        assert len(stdout) > len("x:"), "PATH was lost when env was set"


class TestStream:
    async def test_yields_lines_as_they_arrive(self) -> None:
        command = Command(argv=["sh", "-c", "echo one; echo two; echo three"])
        lines = [line async for line in stream(command)]
        assert lines == ["one", "two", "three"]

    async def test_folds_stderr_into_the_output(self) -> None:
        """docker compose says most of what it has to say on stderr."""
        command = Command(argv=["sh", "-c", "echo out; echo err >&2"])
        lines = [line async for line in stream(command)]
        assert sorted(lines) == ["err", "out"]

    async def test_a_failure_is_visible_to_a_caller_that_only_reads_lines(self) -> None:
        command = Command(argv=["sh", "-c", "echo working; exit 2"])
        lines = [line async for line in stream(command)]
        assert lines == ["working", "[command exited with code 2]"]

    async def test_a_successful_command_adds_no_marker_line(self) -> None:
        lines = [line async for line in stream(Command(argv=["echo", "done"]))]
        assert lines == ["done"]


class TestStreamPty:
    """Streaming through a pseudo-terminal.

    The point of these is not "does output arrive" -- `stream` already did that.
    It is that the child program *believes it is talking to a terminal*, because
    docker changes what it says based on the answer, and everything nice about
    the output (colour, progress bars redrawn in place) is downstream of it.
    """

    async def test_the_child_thinks_it_has_a_terminal(self) -> None:
        command = Command(
            argv=["sh", "-c", "test -t 1 && echo yes-a-tty || echo no-just-a-pipe"]
        )
        output = b"".join([chunk async for chunk in stream_pty(command)])
        assert b"yes-a-tty" in output

    async def test_control_bytes_survive_intact(self) -> None:
        """Carriage returns and escape sequences are the output, not noise.

        `stream` would have rstripped and line-split these into nonsense. They go
        to xterm.js untouched, because it is the thing that knows what they mean.
        """
        command = Command(
            argv=["sh", "-c", r"printf 'aaa\rbbb\n\033[32mgreen\033[0m\n'"]
        )
        output = b"".join([chunk async for chunk in stream_pty(command)])

        assert b"\r" in output, "the carriage return was eaten"
        assert b"\x1b[32m" in output, "the colour escape was eaten"

    async def test_a_pty_translates_newlines_for_the_terminal(self) -> None:
        r"""A bare \n moves down a line without returning to the left margin.

        The pty's line discipline turns it into \r\n, which is why we do not have
        to, and why xterm is configured with convertEol off.
        """
        command = Command(argv=["printf", "one\ntwo\n"])
        output = b"".join([chunk async for chunk in stream_pty(command)])
        assert output == b"one\r\ntwo\r\n"

    async def test_a_failure_is_announced_in_the_stream(self) -> None:
        """Callers only render bytes, so a failure has to arrive as bytes."""
        command = Command(argv=["sh", "-c", "exit 3"])
        output = b"".join([chunk async for chunk in stream_pty(command)])
        assert b"[command exited with code 3]" in output

    async def test_a_missing_executable_raises(self) -> None:
        command = Command(argv=["definitely-not-a-real-binary-9c1f"])
        with pytest.raises(CommandError, match="executable not found"):
            [chunk async for chunk in stream_pty(command)]

    async def test_it_runs_in_the_commands_directory(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("here")
        command = Command(argv=["ls"], cwd=tmp_path)
        output = b"".join([chunk async for chunk in stream_pty(command)])
        assert b"marker.txt" in output
