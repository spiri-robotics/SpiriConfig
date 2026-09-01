"""Tests for spiriconfig_users' contribution to provisioning: staging,
describing, and applying ``users/`` entries in a provisioning repo. See
``docs/provisioning.md`` for the interface this exercises.

Like test_users.py, nothing here touches the real account database:
``apply()``'s ``useradd``/``userdel``/``chpasswd`` calls are checked as
built commands, with ``getent`` stubbed, never run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig.commands import Command, Result
from spiriconfig_provision.contract import StagedRepo
from spiriconfig_users import provision as users_provision
from spiriconfig_users import users
from spiriconfig_users.config import UsersSettings
from spiriconfig_users.users import UserError
from spiriconfig_users.provision import (
    _parse_groups,
    _parse_ssh_keys,
    _stage_remove,
    _staged,
    _write_lines,
    apply,
    describe,
)

from tests.test_users import SAMPLE_PASSWD


@pytest.fixture
def settings() -> UsersSettings:
    return UsersSettings()


def _stub_getent(monkeypatch, passwd: str = SAMPLE_PASSWD) -> None:
    monkeypatch.setattr(
        users, "_getent", lambda settings, database, *keys: passwd if database == "passwd" else ""
    )


class TestWriteLines:
    def test_writes_newline_joined_content(self, tmp_path: Path) -> None:
        path = tmp_path / "groups"
        _write_lines(path, ["docker", "sudo"])
        assert path.read_text() == "docker\nsudo\n"

    def test_empty_list_removes_an_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "groups"
        path.write_text("docker\n")
        _write_lines(path, [])
        assert not path.exists()

    def test_empty_list_is_a_no_op_when_no_file_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "groups"
        _write_lines(path, [])  # must not raise
        assert not path.exists()


class TestParsing:
    def test_parse_groups_splits_on_commas_and_whitespace(self) -> None:
        assert _parse_groups("docker, sudo  wheel") == ["docker", "sudo", "wheel"]

    def test_parse_groups_blank_is_empty(self) -> None:
        assert _parse_groups("   ") == []

    def test_parse_ssh_keys_splits_on_lines_and_drops_blanks(self) -> None:
        text = "ssh-ed25519 AAAA a@b\n\n  \nssh-rsa BBBB c@d\n"
        assert _parse_ssh_keys(text) == [
            "ssh-ed25519 AAAA a@b",
            "ssh-rsa BBBB c@d",
        ]


class TestDescribe:
    def test_add_entry(self, tmp_path: Path) -> None:
        entry = tmp_path / "users" / "add" / "operator" / "password.hash"
        entry.parent.mkdir(parents=True)
        entry.write_text("hash\n")
        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, "users/add/operator/password.hash")
        assert change is not None
        assert change.verb == "add"
        assert "operator" in change.summary
        assert "added" in change.summary
        assert "operator" in change.detail

    def test_add_entry_whose_hash_was_deleted_is_a_withdrawal_not_an_add(
        self, tmp_path: Path
    ) -> None:
        """Regression: unstaging a previously-committed add (staging a
        remove over it, see `_stage_remove`'s call to `_unstage`) deletes
        ``users/add/<name>/password.hash`` -- `git status` reports that
        deletion as a changed path exactly like a new one. Describing it
        as "added" put a false add line right next to that same commit's
        own, real "removed" line for the same account -- see
        `_describe_patch`'s docstring."""
        repo = StagedRepo(path=tmp_path, remote="patch")  # no users/add/operator/... on disk
        change = describe(repo, "users/add/operator/password.hash")
        assert change is not None
        assert change.verb != "add"
        assert "added" not in change.summary
        assert "operator" in change.detail

    def test_remove_entry(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        change = describe(repo, "users/remove/alice")
        assert change is not None
        assert change.verb == "remove"
        assert "alice" in change.summary
        assert "removed" in change.summary
        assert "alice" in change.detail

    def test_state_entry_present_is_added(self, tmp_path: Path) -> None:
        """Regression: `summary` used to be the bare name (`"operator"`),
        not a verb phrase -- harmless in `detail`'s full sentence, but the
        one and only thing a single-change commit message would say. Now
        uses the same "added"/"removed" vocabulary as the patch side --
        see `describe`'s docstring for why there's no separate "certify"
        wording any more."""
        entry = tmp_path / "users" / "state" / "operator"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("hash\n")
        repo = StagedRepo(path=tmp_path, remote="baseline")
        change = describe(repo, "users/state/operator/password.hash")
        assert change is not None
        assert change.verb == "add"
        assert "operator" in change.summary
        assert "added" in change.summary

    def test_state_entry_absent_is_removed(self, tmp_path: Path) -> None:
        """Dropping an entry from `state/` is `state/`'s only removal
        mechanism (see `_drop_from_state`) -- there's no marker file, so
        `describe` has to tell "dropped" apart from "still declared" by
        whether `path` still exists on disk."""
        repo = StagedRepo(path=tmp_path, remote="baseline")
        change = describe(repo, "users/state/operator/password.hash")
        assert change is not None
        assert change.verb == "remove"
        assert "operator" in change.summary
        assert "removed" in change.summary

    def test_path_for_another_resource_is_none(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        assert describe(repo, "apps/add/whoami/env") is None

    def test_too_short_a_path_is_none(self, tmp_path: Path) -> None:
        repo = StagedRepo(path=tmp_path, remote="patch")
        assert describe(repo, "users/stores.toml") is None


class TestStaging:
    """The staging helpers `render_patch_block` drives -- exercised
    directly so a test doesn't need a browser to prove a file lands where
    it should."""

    async def test_stage_add_writes_hash_and_clears_staged_remove(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        def fake_run(command: Command, *, input: str | None = None, log=None) -> Result:
            assert input == "hunter2"
            return Result(command, 0, "$y$fakehash\n", "")

        monkeypatch.setattr(users_provision, "run", fake_run)

        users_dir = tmp_path / "users"
        (users_dir / "remove").mkdir(parents=True)
        (users_dir / "remove" / "bob").touch()

        await users_provision._stage_add(users_dir, settings, "bob", "hunter2")

        assert (users_dir / "add" / "bob" / "password.hash").read_text() == "$y$fakehash\n"
        # Staging an add for a name that had a staged removal cancels it --
        # add and remove of the same entry in one patch isn't a state either
        # verb means alone.
        assert not (users_dir / "remove" / "bob").exists()

    async def test_stage_add_rejects_mkpasswds_bogus_but_zero_exit_output(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        """Regression, reproduced against the real system (not a guess):
        the old `hash_password` built `mkpasswd --method yescrypt --stdin`
        as two separate argv elements, which whois's mkpasswd parses as
        `--method` with no value -- it prints its "Available methods"
        list to stdout and exits 0. `Result.check()` alone doesn't catch
        this (exit code really is 0), so `_stage_add` must also reject
        output that doesn't look like a hash, rather than writing it into
        a `password.hash` a repo might go on to sign and ship."""
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(
                command, 0, "Available methods:\nyescrypt\tYescrypt\n", ""
            ),
        )
        users_dir = tmp_path / "users"

        with pytest.raises(UserError):
            await users_provision._stage_add(users_dir, settings, "bob", "hunter2")

        assert not (users_dir / "add" / "bob").exists()

    async def test_stage_add_writes_groups_and_ssh_keys(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(command, 0, "$y$fakehash\n", ""),
        )
        users_dir = tmp_path / "users"

        await users_provision._stage_add(
            users_dir,
            settings,
            "bob",
            "hunter2",
            groups=["docker", "sudo"],
            ssh_keys=["ssh-ed25519 AAAA bob@laptop"],
        )

        entry = users_dir / "add" / "bob"
        assert entry.joinpath("groups").read_text() == "docker\nsudo\n"
        assert (
            entry.joinpath("authorized_keys").read_text()
            == "ssh-ed25519 AAAA bob@laptop\n"
        )

    async def test_stage_add_without_groups_or_keys_writes_neither_file(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(command, 0, "$y$fakehash\n", ""),
        )
        users_dir = tmp_path / "users"

        await users_provision._stage_add(users_dir, settings, "bob", "hunter2")

        entry = users_dir / "add" / "bob"
        assert not entry.joinpath("groups").exists()
        assert not entry.joinpath("authorized_keys").exists()

    async def test_restaging_add_with_fields_blank_clears_them(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        """Restaging fully re-declares the entry -- the same rule the
        password half of `_stage_add` already follows, extended to
        groups/authorized_keys: leaving a field blank on a restage clears
        whatever was staged for it before, rather than leaving it stale."""
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(command, 0, "$y$fakehash\n", ""),
        )
        users_dir = tmp_path / "users"
        await users_provision._stage_add(
            users_dir, settings, "bob", "hunter2", groups=["docker"],
            ssh_keys=["ssh-ed25519 AAAA bob@laptop"],
        )

        await users_provision._stage_add(users_dir, settings, "bob", "hunter2")

        entry = users_dir / "add" / "bob"
        assert not entry.joinpath("groups").exists()
        assert not entry.joinpath("authorized_keys").exists()

    def test_stage_remove_is_a_file_not_a_directory(self, tmp_path: Path) -> None:
        """Regression: an empty *directory* marker is invisible to git, so
        `remove/<name>` must be a bare file -- see NOTES-usb-provisioning.md's
        layout."""
        users_dir = tmp_path / "users"
        (users_dir / "add" / "bob").mkdir(parents=True)

        _stage_remove(users_dir, "bob")

        entry = users_dir / "remove" / "bob"
        assert entry.is_file()
        assert not (users_dir / "add" / "bob").exists()

    def test_staged_lists_add_directories_and_remove_files_separately(
        self, tmp_path: Path
    ) -> None:
        users_dir = tmp_path / "users"
        (users_dir / "add" / "alice").mkdir(parents=True)
        (users_dir / "remove").mkdir(parents=True)
        (users_dir / "remove" / "bob").touch()

        assert _staged(users_dir, "add") == ["alice"]
        assert _staged(users_dir, "remove") == ["bob"]


class TestBaselineStaging:
    """The helpers `render_baseline_block` drives -- exercised directly,
    same rationale as `TestStaging` above."""

    async def test_stage_state_writes_hash(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        def fake_run(command: Command, *, input: str | None = None, log=None) -> Result:
            assert input == "hunter2"
            return Result(command, 0, "$y$fakehash\n", "")

        monkeypatch.setattr(users_provision, "run", fake_run)

        state_dir = tmp_path / "users" / "state"
        await users_provision._stage_state(state_dir, settings, "bob", "hunter2")

        assert (state_dir / "bob" / "password.hash").read_text() == "$y$fakehash\n"

    async def test_stage_state_rejects_mkpasswds_bogus_but_zero_exit_output(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        """Same regression as `TestStaging`'s -- see its docstring."""
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(
                command, 0, "Available methods:\nyescrypt\tYescrypt\n", ""
            ),
        )
        state_dir = tmp_path / "users" / "state"

        with pytest.raises(UserError):
            await users_provision._stage_state(state_dir, settings, "bob", "hunter2")

        assert not (state_dir / "bob").exists()

    async def test_stage_state_writes_groups_and_ssh_keys(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(command, 0, "$y$fakehash\n", ""),
        )
        state_dir = tmp_path / "users" / "state"

        await users_provision._stage_state(
            state_dir,
            settings,
            "bob",
            "hunter2",
            groups=["docker", "sudo"],
            ssh_keys=["ssh-ed25519 AAAA bob@laptop"],
        )

        entry = state_dir / "bob"
        assert entry.joinpath("groups").read_text() == "docker\nsudo\n"
        assert (
            entry.joinpath("authorized_keys").read_text()
            == "ssh-ed25519 AAAA bob@laptop\n"
        )

    async def test_restaging_state_with_fields_blank_clears_them(
        self, tmp_path: Path, settings: UsersSettings, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            users_provision,
            "run",
            lambda command, *, input=None, log=None: Result(command, 0, "$y$fakehash\n", ""),
        )
        state_dir = tmp_path / "users" / "state"
        await users_provision._stage_state(
            state_dir, settings, "bob", "hunter2", groups=["docker"],
            ssh_keys=["ssh-ed25519 AAAA bob@laptop"],
        )

        await users_provision._stage_state(state_dir, settings, "bob", "hunter2")

        entry = state_dir / "bob"
        assert not entry.joinpath("groups").exists()
        assert not entry.joinpath("authorized_keys").exists()

    def test_drop_from_state_removes_the_directory(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "users" / "state"
        (state_dir / "bob").mkdir(parents=True)
        (state_dir / "bob" / "password.hash").write_text("hash\n")

        users_provision._drop_from_state(state_dir, "bob")

        assert not (state_dir / "bob").exists()

    def test_drop_from_state_of_an_absent_name_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "users" / "state"
        users_provision._drop_from_state(state_dir, "nobody")  # must not raise

    def test_staged_lists_state_directories(self, tmp_path: Path) -> None:
        users_dir = tmp_path / "users"
        (users_dir / "state" / "alice").mkdir(parents=True)

        assert _staged(users_dir, "state") == ["alice"]


class TestApplyPatch:
    def test_add_creates_account_and_sets_password(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _stub_getent(monkeypatch)  # SAMPLE_PASSWD has no "bob"
        entry = tmp_path / "users" / "add" / "bob"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("$y$hash\n")

        steps = apply(StagedRepo(path=tmp_path, remote="patch"))

        assert [str(s.command) for s in steps] == [
            "useradd --create-home bob",
            "chpasswd -e",
        ]
        assert steps[-1].input == "bob:$y$hash\n"

    def test_add_of_an_existing_account_only_resets_the_password(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """alice is already in SAMPLE_PASSWD -- re-adding her should not
        useradd an account that already exists."""
        _stub_getent(monkeypatch)
        entry = tmp_path / "users" / "add" / "alice"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("$y$hash\n")

        steps = apply(StagedRepo(path=tmp_path, remote="patch"))

        assert [str(s.command) for s in steps] == ["chpasswd -e"]

    def test_remove_deletes_an_existing_account(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _stub_getent(monkeypatch)  # alice is in SAMPLE_PASSWD
        remove_dir = tmp_path / "users" / "remove"
        remove_dir.mkdir(parents=True)
        (remove_dir / "alice").touch()

        steps = apply(StagedRepo(path=tmp_path, remote="patch"))

        assert [str(s.command) for s in steps] == ["userdel alice"]

    def test_remove_of_an_account_that_does_not_exist_is_a_no_op(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _stub_getent(monkeypatch)
        remove_dir = tmp_path / "users" / "remove"
        remove_dir.mkdir(parents=True)
        (remove_dir / "nobody-by-this-name").touch()

        steps = apply(StagedRepo(path=tmp_path, remote="patch"))

        assert steps == []

    def test_no_users_directory_is_a_no_op(self, tmp_path: Path, monkeypatch) -> None:
        _stub_getent(monkeypatch)
        assert apply(StagedRepo(path=tmp_path, remote="patch")) == []


class TestApplyBaseline:
    def test_declares_the_exact_set(self, tmp_path: Path, monkeypatch) -> None:
        """alice is on the live system but not declared; bob is declared but
        not on the live system -- both must move, and alice's removal does
        not depend on any `remove/` entry existing, because state/ is not
        relative to anything."""
        _stub_getent(monkeypatch)
        state_dir = tmp_path / "users" / "state"
        entry = state_dir / "bob"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("$y$hash\n")

        steps = apply(StagedRepo(path=tmp_path, remote="baseline"))

        assert [str(s.command) for s in steps] == [
            "userdel alice",
            "useradd --create-home bob",
            "chpasswd -e",
        ]
        assert steps[-1].input == "bob:$y$hash\n"

    def test_an_already_certified_account_is_left_alone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """alice is both declared and already on the live system -- nothing
        to reconcile for her specifically, but her password is still
        (re)set every apply, since state/ doesn't track what changed."""
        _stub_getent(monkeypatch)
        state_dir = tmp_path / "users" / "state"
        entry = state_dir / "alice"
        entry.mkdir(parents=True)
        (entry / "password.hash").write_text("$y$hash\n")

        steps = apply(StagedRepo(path=tmp_path, remote="baseline"))

        assert [str(s.command) for s in steps] == ["chpasswd -e"]
