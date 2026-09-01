"""Tests for ``spiriconfig provision apply`` -- the CLI face of the device
apply loop. ``status`` is exercised indirectly through `repo.py`'s own
tests; this file is about the plan/--yes gate and exit codes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spiriconfig.commands import Command
from spiriconfig.plugins import Plugin
from spiriconfig_provision.cli import app
from spiriconfig_provision.contract import ApplyStep, StagedRepo

runner = CliRunner()


class _FakeContributor(Plugin):
    """A `provision_apply` contributor with one canned, harmless step."""

    def __init__(self, marker: Path) -> None:
        self.name = "fake"
        self.title = "Fake"
        self.provision_resource = "fake"
        self._marker = marker

    def provision_apply(self, repo: StagedRepo) -> list[ApplyStep]:
        return [ApplyStep(Command(argv=["sh", "-c", f"touch {self._marker}"]))]

_GIT_ENV = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _make_source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(
        ["git", *_GIT_ENV, "init", "-q"], cwd=source, check=True, capture_output=True
    )
    (source / "marker").write_text("hello\n")
    subprocess.run(
        ["git", *_GIT_ENV, "add", "-A"], cwd=source, check=True, capture_output=True
    )
    subprocess.run(
        ["git", *_GIT_ENV, "commit", "-q", "-m", "first"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    return source


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPIRICONFIG_PROVISION_APPLY_PATCH_DIR", str(tmp_path / "apply-patch"))
    monkeypatch.setenv(
        "SPIRICONFIG_PROVISION_APPLY_BASELINE_DIR", str(tmp_path / "apply-baseline")
    )
    monkeypatch.setattr("spiriconfig_provision.repo.discover", lambda: [])


class TestApplyCommand:
    def test_an_unknown_remote_is_rejected(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["apply", "nonsense", "somewhere"])
        assert result.exit_code != 0
        assert "patch" in result.output

    def test_off_tier_is_the_default_and_refuses(self, tmp_path: Path, monkeypatch) -> None:
        source = _make_source_repo(tmp_path)
        result = runner.invoke(app, ["apply", "patch", str(source)])
        assert result.exit_code == 1
        assert "Refused" in result.output
        assert "off" in result.output

    def test_without_yes_shows_the_plan_and_applies_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_TIER", "trusted")
        source = _make_source_repo(tmp_path)

        result = runner.invoke(app, ["apply", "patch", str(source)])

        assert result.exit_code == 0
        assert "Nothing to apply" in result.output

    def test_yes_actually_applies(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_TIER", "trusted")
        source = _make_source_repo(tmp_path)
        marker = tmp_path / "ran"
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover", lambda: [_FakeContributor(marker)]
        )

        result = runner.invoke(app, ["apply", "patch", str(source), "--yes"])

        assert result.exit_code == 0
        assert "Applied." in result.output
        assert marker.exists()

    def test_without_yes_prints_the_plan_but_does_not_run_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("SPIRICONFIG_PROVISION_TIER", "trusted")
        source = _make_source_repo(tmp_path)
        marker = tmp_path / "should-not-run"
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover", lambda: [_FakeContributor(marker)]
        )

        result = runner.invoke(app, ["apply", "patch", str(source)])

        assert result.exit_code == 0
        assert "Pass --yes" in result.output
        assert not marker.exists()
