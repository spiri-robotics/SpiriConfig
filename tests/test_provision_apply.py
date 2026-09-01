"""Tests for the device-side apply loop: fetch, fast-forward check,
tier-gate, reconcile.

`_make_source_repo` stands in for a USB drive or network remote -- an
ordinary git working tree on disk, since `fetch`'s whole point (see
"Mechanism: a git remote" in NOTES-usb-provisioning.md) is that the
transport is never special: a local path, `file://`, `https://`, `ssh://`
all resolve to the same `git fetch`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spiriconfig.commands import Command, CommandError
from spiriconfig.plugins import Plugin
from spiriconfig_provision.apply import (
    ApplyError,
    execute,
    fetch,
    last_applied,
    open_apply_repo,
    plan,
)
from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import ApplyStep, StagedRepo

_GIT_ENV = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *_GIT_ENV, *args], cwd=cwd, check=True, capture_output=True)


def _make_source_repo(tmp_path: Path, name: str = "source") -> Path:
    source = tmp_path / name
    source.mkdir()
    _git("init", "-q", cwd=source)
    (source / "marker").write_text("first\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-q", "-m", "first", cwd=source)
    return source


def _commit_more(source: Path, filename: str, message: str) -> None:
    (source / filename).write_text("more\n")
    _git("add", "-A", cwd=source)
    _git("commit", "-q", "-m", message, cwd=source)


class _FakeContributor(Plugin):
    """A `provision_apply` contributor returning canned steps."""

    def __init__(self, name: str, provision_resource: str, steps: list[ApplyStep]) -> None:
        self.name = name
        self.title = name.title()
        self.provision_resource = provision_resource
        self._steps = steps

    def provision_apply(self, repo: StagedRepo) -> list[ApplyStep]:
        return self._steps


@pytest.fixture
def settings(tmp_path: Path) -> ProvisionSettings:
    return ProvisionSettings(
        tier="trusted",
        apply_patch_dir=tmp_path / "apply-patch",
        apply_baseline_dir=tmp_path / "apply-baseline",
    )


@pytest.fixture(autouse=True)
def _no_real_contributors(monkeypatch) -> None:
    """Every real installed plugin is irrelevant here -- these tests exercise
    apply.py's own logic, not whatever `provision_apply` real plugins ship."""
    monkeypatch.setattr("spiriconfig_provision.repo.discover", lambda: [])


class TestFetch:
    def test_fetches_the_sources_head_commit(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        source = _make_source_repo(tmp_path)
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
        ).stdout.strip()

        staged = open_apply_repo("patch", settings)
        fetched = fetch(staged, str(source), "HEAD", settings)

        assert fetched == expected


class TestLastApplied:
    def test_is_none_before_anything_has_been_applied(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        staged = open_apply_repo("patch", settings)
        assert last_applied(staged, settings) is None

    def test_advances_after_a_successful_execute(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        source = _make_source_repo(tmp_path)
        computed = plan("patch", str(source), settings)
        execute(computed, settings)

        assert last_applied(computed.repo, settings) == computed.fetched


class TestPlanTierGate:
    def test_off_tier_refuses(self, tmp_path: Path) -> None:
        source = _make_source_repo(tmp_path)
        settings = ProvisionSettings(
            tier="off",
            apply_patch_dir=tmp_path / "apply-patch",
            apply_baseline_dir=tmp_path / "apply-baseline",
        )
        with pytest.raises(ApplyError, match="disabled"):
            plan("patch", str(source), settings)

    def test_signed_tier_refuses_because_trust_d_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        source = _make_source_repo(tmp_path)
        settings = ProvisionSettings(
            tier="signed",
            apply_patch_dir=tmp_path / "apply-patch",
            apply_baseline_dir=tmp_path / "apply-baseline",
        )
        with pytest.raises(ApplyError, match="trust.d"):
            plan("patch", str(source), settings)

    def test_signed_tier_refusal_happens_after_a_real_fetch(
        self, tmp_path: Path
    ) -> None:
        """Refusing signed-tier doesn't mean skipping the fetch -- the
        clone still ends up with the commit fetched, ready for whenever
        signed verification exists to check it."""
        source = _make_source_repo(tmp_path)
        settings = ProvisionSettings(
            tier="signed",
            apply_patch_dir=tmp_path / "apply-patch",
            apply_baseline_dir=tmp_path / "apply-baseline",
        )
        with pytest.raises(ApplyError):
            plan("patch", str(source), settings)
        assert (settings.apply_patch_dir / ".git" / "FETCH_HEAD").exists()


class TestPlan:
    def test_collects_steps_from_every_contributor(
        self, tmp_path: Path, settings: ProvisionSettings, monkeypatch
    ) -> None:
        source = _make_source_repo(tmp_path)
        step = ApplyStep(Command(argv=["true"]))
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover",
            lambda: [_FakeContributor("users", "users", [step])],
        )

        computed = plan("patch", str(source), settings)

        assert computed.steps == [step]

    def test_no_contributors_means_no_steps(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        source = _make_source_repo(tmp_path)
        computed = plan("patch", str(source), settings)
        assert computed.steps == []

    def test_checks_out_the_fetched_commit_into_the_working_tree(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        source = _make_source_repo(tmp_path)
        computed = plan("patch", str(source), settings)
        assert (computed.repo.path / "marker").read_text() == "first\n"

    def test_rejects_a_fetch_that_is_not_a_fast_forward(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        source = _make_source_repo(tmp_path)
        first = plan("patch", str(source), settings)
        execute(first, settings)

        # Rewrite the commit that's already applied instead of building on
        # top of it -- the new head shares no fast-forward path to it.
        subprocess.run(
            ["git", *_GIT_ENV, "commit", "--amend", "-q", "-m", "rewritten"],
            cwd=source,
            check=True,
            capture_output=True,
        )

        with pytest.raises(ApplyError, match="fast-forward"):
            plan("patch", str(source), settings)

    def test_a_second_fetch_that_is_a_fast_forward_is_accepted(
        self, tmp_path: Path, settings: ProvisionSettings
    ) -> None:
        source = _make_source_repo(tmp_path)
        first = plan("patch", str(source), settings)
        execute(first, settings)

        _commit_more(source, "marker2", "second")
        second = plan("patch", str(source), settings)

        assert second.fetched != first.fetched


class TestExecute:
    def test_runs_steps_in_order(
        self, tmp_path: Path, settings: ProvisionSettings, monkeypatch
    ) -> None:
        source = _make_source_repo(tmp_path)
        order_file = tmp_path / "order"
        steps = [
            ApplyStep(Command(argv=["sh", "-c", f"echo one >> {order_file}"])),
            ApplyStep(Command(argv=["sh", "-c", f"echo two >> {order_file}"])),
        ]
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover",
            lambda: [_FakeContributor("users", "users", steps)],
        )

        computed = plan("patch", str(source), settings)
        execute(computed, settings)

        assert order_file.read_text().splitlines() == ["one", "two"]

    def test_stops_on_first_failure_and_does_not_run_what_follows(
        self, tmp_path: Path, settings: ProvisionSettings, monkeypatch
    ) -> None:
        source = _make_source_repo(tmp_path)
        marker = tmp_path / "should-not-exist"
        steps = [
            ApplyStep(Command(argv=["false"])),
            ApplyStep(Command(argv=["sh", "-c", f"touch {marker}"])),
        ]
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover",
            lambda: [_FakeContributor("users", "users", steps)],
        )

        computed = plan("patch", str(source), settings)
        with pytest.raises(CommandError):
            execute(computed, settings)

        assert not marker.exists()

    def test_a_failed_execute_does_not_advance_last_applied(
        self, tmp_path: Path, settings: ProvisionSettings, monkeypatch
    ) -> None:
        source = _make_source_repo(tmp_path)
        steps = [ApplyStep(Command(argv=["false"]))]
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover",
            lambda: [_FakeContributor("users", "users", steps)],
        )

        computed = plan("patch", str(source), settings)
        with pytest.raises(CommandError):
            execute(computed, settings)

        assert last_applied(computed.repo, settings) is None

    def test_passes_input_through_to_the_command(
        self, tmp_path: Path, settings: ProvisionSettings, monkeypatch
    ) -> None:
        source = _make_source_repo(tmp_path)
        out_file = tmp_path / "stdin-out"
        steps = [
            ApplyStep(
                Command(argv=["sh", "-c", f"cat > {out_file}"]),
                input="a-secret-hash\n",
            )
        ]
        monkeypatch.setattr(
            "spiriconfig_provision.repo.discover",
            lambda: [_FakeContributor("users", "users", steps)],
        )

        computed = plan("patch", str(source), settings)
        execute(computed, settings)

        assert out_file.read_text() == "a-secret-hash\n"
