"""Tests for the users plugin: parsing getent, and building the write commands.

Like the docker suite, none of this touches the real account database. The
interesting logic is which command we build and how we read ``getent``'s output,
and both can be checked with sample text and no ``useradd`` in sight -- which is
also the only responsible way to test code whose real commands edit
``/etc/passwd``.
"""

from __future__ import annotations

import pytest

from spiriconfig_users import users
from spiriconfig_users.config import UsersSettings
from spiriconfig_users.users import (
    Group,
    User,
    UserError,
    _is_login_shell,
    _parse_group,
    _parse_passwd,
)

#: A cross-section of a real ``getent passwd``: root, a daemon account with a
#: nologin shell, a human at 1000, a high-uid service account that squats in the
#: login band (NixOS's nixbld), and nobody.
SAMPLE_PASSWD = """\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
alice:x:1000:1000:Alice Example,Room 1,,:/home/alice:/bin/bash
nixbld1:x:30001:30000:Nix build user 1:/var/empty:/run/current-system/sw/bin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
"""

SAMPLE_GROUP = """\
root:x:0:
sudo:x:27:alice,bob
docker:x:998:alice
alice:x:1000:
empty:x:1234:
"""


@pytest.fixture
def settings() -> UsersSettings:
    return UsersSettings()


class TestParsePasswd:
    def test_parses_all_fields(self) -> None:
        alice = next(u for u in _parse_passwd(SAMPLE_PASSWD) if u.name == "alice")
        assert alice == User(
            name="alice",
            uid=1000,
            gid=1000,
            gecos="Alice Example,Room 1,,",
            home="/home/alice",
            shell="/bin/bash",
        )

    def test_full_name_is_the_first_gecos_field(self) -> None:
        alice = next(u for u in _parse_passwd(SAMPLE_PASSWD) if u.name == "alice")
        assert alice.full_name == "Alice Example"

    def test_full_name_empty_when_gecos_is(self) -> None:
        assert User("x", 1, 1, "", "/", "/bin/sh").full_name == ""

    def test_skips_malformed_lines(self) -> None:
        text = "good:x:1000:1000::/home/good:/bin/sh\nnot-a-passwd-line\nalso:bad\n"
        assert [u.name for u in _parse_passwd(text)] == ["good"]

    def test_skips_non_numeric_uid(self) -> None:
        assert _parse_passwd("weird:x:notanumber:1000::/h:/bin/sh") == []

    def test_ignores_comments_and_blanks(self) -> None:
        text = "# a comment\n\nalice:x:1000:1000::/home/alice:/bin/sh\n"
        assert [u.name for u in _parse_passwd(text)] == ["alice"]


class TestParseGroup:
    def test_parses_members(self) -> None:
        sudo = next(g for g in _parse_group(SAMPLE_GROUP) if g.name == "sudo")
        assert sudo == Group(name="sudo", gid=27, members=("alice", "bob"))

    def test_empty_member_list_is_empty_tuple(self) -> None:
        empty = next(g for g in _parse_group(SAMPLE_GROUP) if g.name == "empty")
        assert empty.members == ()


class TestLoginFilter:
    def test_human_account_is_a_login(self, settings: UsersSettings) -> None:
        alice = User("alice", 1000, 1000, "", "/home/alice", "/bin/bash")
        assert alice.is_login(settings)

    def test_daemon_below_uid_min_is_not(self, settings: UsersSettings) -> None:
        daemon = User("daemon", 1, 1, "", "/", "/usr/sbin/nologin")
        assert not daemon.is_login(settings)

    def test_high_uid_service_account_in_band_is_excluded_by_shell(
        self, settings: UsersSettings
    ) -> None:
        """The nixbld case: uid 30001 is inside the band, but its shell gives it away."""
        nixbld = User("nixbld1", 30001, 30000, "", "/var/empty", "/sbin/nologin")
        assert settings.uid_min <= nixbld.uid <= settings.uid_max
        assert not nixbld.is_login(settings)

    def test_nobody_above_uid_max_is_not(self, settings: UsersSettings) -> None:
        nobody = User("nobody", 65534, 65534, "", "/nonexistent", "/usr/sbin/nologin")
        assert not nobody.is_login(settings)

    @pytest.mark.parametrize(
        ("shell", "expected"),
        [
            ("/bin/bash", True),
            ("/bin/sh", True),
            ("", True),  # empty means "system default", which is login-capable
            ("/usr/sbin/nologin", False),
            ("/sbin/nologin", False),
            ("/run/current-system/sw/bin/nologin", False),
            ("/bin/false", False),
            ("/usr/bin/false", False),
        ],
    )
    def test_is_login_shell(self, shell: str, expected: bool) -> None:
        assert _is_login_shell(shell) is expected

    def test_list_users_hides_system_by_default(
        self, settings: UsersSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(users, "_getent", lambda s, db, *k: SAMPLE_PASSWD)
        assert [u.name for u in users.list_users(settings)] == ["alice"]

    def test_list_users_all_keeps_everything(
        self, settings: UsersSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(users, "_getent", lambda s, db, *k: SAMPLE_PASSWD)
        names = [u.name for u in users.list_users(settings, include_system=True)]
        assert names == ["root", "daemon", "alice", "nixbld1", "nobody"]


class TestGroupsFor:
    def test_includes_primary_by_gid_and_supplementary_by_membership(self) -> None:
        alice = User("alice", 1000, 1000, "", "/home/alice", "/bin/bash")
        groups = _parse_group(SAMPLE_GROUP)
        # sudo and docker list her; the `alice` group is her primary (gid 1000).
        assert users.groups_for(alice, groups) == ["alice", "docker", "sudo"]

    def test_no_groups_is_empty(self) -> None:
        loner = User("loner", 1001, 1001, "", "/home/loner", "/bin/bash")
        assert users.groups_for(loner, _parse_group(SAMPLE_GROUP)) == []


class TestValidateName:
    @pytest.mark.parametrize("name", ["alice", "_svc", "a", "user-1", "nixbld$"])
    def test_accepts_valid(self, name: str) -> None:
        assert users.validate_name(name) == name

    def test_strips_whitespace(self) -> None:
        assert users.validate_name("  alice  ") == "alice"

    @pytest.mark.parametrize("name", ["", "   ", "1alice", "Alice", "has space", "a:b"])
    def test_rejects_invalid(self, name: str) -> None:
        with pytest.raises(UserError):
            users.validate_name(name)


class TestCommands:
    def test_create_defaults_to_making_a_home(self, settings: UsersSettings) -> None:
        assert str(users.create(settings, "alice")) == "useradd --create-home alice"

    def test_create_with_everything(self, settings: UsersSettings) -> None:
        command = users.create(
            settings,
            "alice",
            comment="Alice Example",
            shell="/bin/bash",
            groups=["docker", "sudo"],
        )
        assert str(command) == (
            "useradd --create-home --shell /bin/bash --comment 'Alice Example' "
            "--groups docker,sudo alice"
        )

    def test_create_without_home_or_as_system(self, settings: UsersSettings) -> None:
        command = users.create(settings, "svc", create_home=False, system=True)
        assert str(command) == "useradd --system svc"

    def test_create_validates_the_name(self, settings: UsersSettings) -> None:
        with pytest.raises(UserError):
            users.create(settings, "1nope")

    def test_delete_keeps_home_by_default(self, settings: UsersSettings) -> None:
        assert str(users.delete(settings, "alice")) == "userdel alice"

    def test_delete_can_remove_home(self, settings: UsersSettings) -> None:
        assert str(users.delete(settings, "alice", remove_home=True)) == (
            "userdel --remove alice"
        )

    def test_set_password_command_holds_no_secret(self, settings: UsersSettings) -> None:
        """The whole point: the password is never in the command line."""
        command = users.set_password(settings, "alice")
        assert str(command) == "chpasswd"
        assert "alice" not in str(command)

    def test_password_stdin_is_user_colon_password(self) -> None:
        assert users.password_stdin("alice", "s3cr3t") == "alice:s3cr3t\n"

    def test_group_membership_commands(self, settings: UsersSettings) -> None:
        assert str(users.add_to_group(settings, "alice", "docker")) == (
            "gpasswd --add alice docker"
        )
        assert str(users.remove_from_group(settings, "alice", "docker")) == (
            "gpasswd --delete alice docker"
        )

    def test_hash_password_command_holds_no_secret(
        self, settings: UsersSettings
    ) -> None:
        command = users.hash_password(settings)
        assert str(command) == "mkpasswd --method=yescrypt --stdin"

    def test_hash_password_joins_method_and_value_in_one_argv_element(
        self, settings: UsersSettings
    ) -> None:
        """Regression: whois's mkpasswd parses `--method yescrypt` (two
        argv elements) as `--method` with no value plus a stray positional
        argument -- it prints its "Available methods" list to stdout
        instead of a hash, and still exits 0. Reproduced against the real
        `mkpasswd` binary, not a guess -- `--method=yescrypt` (one element)
        is the only form that actually hashes anything."""
        command = users.hash_password(settings)
        assert "--method" not in command.argv
        assert f"--method={settings.password_hash_method}" in command.argv

    def test_set_password_hashed_command_holds_no_secret(
        self, settings: UsersSettings
    ) -> None:
        """Same shape as set_password, but chpasswd -e: it expects an
        already-hashed value, not a plaintext one."""
        command = users.set_password_hashed(settings, "alice")
        assert str(command) == "chpasswd -e"
        assert "alice" not in str(command)


class TestValidatePasswordHash:
    def test_accepts_a_real_hash(self) -> None:
        hashed = "$y$j9T$8jBqWag4XrTf1iRjLbZgT0$Tb8veZhwuCqpCqY5V3DDzwTx.dIAQY/kABah9bpWbl8"
        assert users.validate_password_hash(hashed + "\n") == hashed

    def test_rejects_mkpasswds_available_methods_listing(self) -> None:
        """Regression: this is the literal, real output `mkpasswd
        --method yescrypt --stdin` (the old, broken two-argv-element form)
        prints on stdout while still exiting 0 -- see
        `test_hash_password_joins_method_and_value_in_one_argv_element`.
        `Result.check()` alone would have let this land in
        `password.hash` uncaught."""
        with pytest.raises(UserError):
            users.validate_password_hash("Available methods:\nyescrypt\tYescrypt\n")

    def test_rejects_empty_output(self) -> None:
        with pytest.raises(UserError):
            users.validate_password_hash("")


def _user(home: str) -> User:
    return User(name="alice", uid=1000, gid=1000, gecos="", home=home, shell="/bin/bash")


class TestSshKeys:
    def test_list_ssh_keys_reads_authorized_keys(self, tmp_path) -> None:
        user = _user(str(tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "authorized_keys").write_text(
            "# a comment\n\nssh-ed25519 AAAAtest alice@laptop\n"
        )
        assert users.list_ssh_keys(user) == ["ssh-ed25519 AAAAtest alice@laptop"]

    def test_list_ssh_keys_missing_file_is_empty(self, tmp_path) -> None:
        assert users.list_ssh_keys(_user(str(tmp_path))) == []

    def test_validate_ssh_key_accepts_a_real_looking_key(self) -> None:
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest alice@laptop"
        assert users.validate_ssh_key(key) == key

    def test_validate_ssh_key_strips_whitespace(self) -> None:
        assert users.validate_ssh_key("  ssh-rsa AAAA  \n") == "ssh-rsa AAAA"

    @pytest.mark.parametrize("key", ["", "   ", "not-a-key AAAA", "hunter2"])
    def test_validate_ssh_key_rejects_bad_input(self, key: str) -> None:
        with pytest.raises(UserError):
            users.validate_ssh_key(key)

    def test_validate_ssh_key_rejects_multiple_lines(self) -> None:
        with pytest.raises(UserError):
            users.validate_ssh_key("ssh-ed25519 AAAA\nssh-ed25519 BBBB")

    def test_ensure_ssh_dir_command(self, settings: UsersSettings) -> None:
        command = users.ensure_ssh_dir(settings, _user("/home/alice"))
        assert str(command) == "install -d -m 700 -o alice -g 1000 /home/alice/.ssh"

    def test_ensure_authorized_keys_command(self, settings: UsersSettings) -> None:
        command = users.ensure_authorized_keys(settings, _user("/home/alice"))
        assert str(command) == (
            "install -m 600 -o alice -g 1000 /dev/null "
            "/home/alice/.ssh/authorized_keys"
        )

    def test_add_ssh_key_command_holds_the_key_on_stdin_not_argv(
        self, settings: UsersSettings
    ) -> None:
        command = users.add_ssh_key(settings, _user("/home/alice"))
        assert str(command) == "tee -a /home/alice/.ssh/authorized_keys"

    def test_replace_ssh_keys_command(self, settings: UsersSettings) -> None:
        command = users.replace_ssh_keys(settings, _user("/home/alice"))
        assert str(command) == "tee /home/alice/.ssh/authorized_keys"
