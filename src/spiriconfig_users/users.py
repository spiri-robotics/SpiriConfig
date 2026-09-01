"""System login accounts, and the commands that manage them.

The source of truth is the host's own user database, read through ``getent`` so
the answer is whatever NSS is configured to serve -- the local ``/etc/passwd``,
and LDAP or the like if the box uses it -- which is exactly the list ``getent
passwd`` prints in a shell. We keep no copy of it. This is the same stance the
docker plugin takes with ``docker ps``: read reality, never mirror it.

Every function that *changes* a user returns a :class:`~spiriconfig.commands.Command`
rather than running it, so the UI can show the exact line before it runs and tests
can assert on it without a real account database anywhere in sight. Reads
(``getent``) run immediately, because a read has nothing to show and nothing to
undo.

The write commands are shadow-utils -- ``useradd``, ``userdel``, ``chpasswd``,
``gpasswd`` -- and they need root, because only root may edit ``/etc/passwd`` and
``/etc/shadow``. When SpiriConfig is not root they fail with the OS's own "only
root" message, which :func:`~spiriconfig.commands.run` surfaces unchanged; we do
not pretend to a power the kernel will not grant us.

Portability, deliberately unfinished: a busybox-only rootfs (some of the Yocto
drone images will be) has ``adduser``/``deluser``/``addgroup`` applets with
different flags rather than shadow-utils. The binaries are
:class:`~spiriconfig_users.config.UsersSettings` fields so a deployment can point
them elsewhere, but a genuine busybox backend -- which would build a *different*
argv, not just a different path -- is not written yet. This is the same
"OS-level abstraction, several backends" problem the hostname work has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from spiriconfig.commands import Command, run

from spiriconfig_users.config import UsersSettings

log = logger.bind(plugin="users")

#: ``useradd``'s own default for a valid name (its ``chkname.c`` ``NAME_REGEX``):
#: start with a lower-case letter or underscore, then letters, digits, ``_`` or
#: ``-``, with an optional trailing ``$`` for machine accounts. We check it before
#: building a command so a bad name fails with a sentence rather than a shell error.
_NAME_RE = re.compile(r"[a-z_][a-z0-9_-]*\$?")

#: ``nobody``. Above :attr:`UsersSettings.uid_max` anyway, but named because it is
#: the one high-uid account that is genuinely universal.
NOBODY_UID = 65534

#: Login-shell basenames that mean "not for logging in". Setting an account's shell
#: to one of these is the conventional way ``/etc/passwd`` marks a service account,
#: and it is the only reliable tell for one that a uid band misses -- NixOS's
#: ``nixbld`` builders sit at uid 30000+, inside every sane login range, and are
#: distinguishable only by their ``nologin`` shell. An *empty* shell is deliberately
#: not here: it means "use the system default", which is login-capable, and a
#: freshly ``useradd``ed account can have one, so hiding it would make new users
#: vanish from the very list that just created them.
_NOLOGIN_SHELLS = frozenset({"nologin", "false"})


def _is_login_shell(shell: str) -> bool:
    """Whether ``shell`` is one a person could actually log in with."""
    return shell.rsplit("/", 1)[-1] not in _NOLOGIN_SHELLS


class UserError(Exception):
    """Something is wrong with a user, or with the request made of one."""


@dataclass(frozen=True, slots=True)
class User:
    """One row of ``getent passwd``: a login account as the host reports it."""

    name: str
    uid: int
    gid: int
    gecos: str
    """The comment field. Its first comma-separated part is the person's name."""
    home: str
    shell: str

    @property
    def full_name(self) -> str:
        """The human name, from the first field of GECOS. May be empty."""
        return self.gecos.split(",", 1)[0].strip()

    def is_login(self, settings: UsersSettings) -> bool:
        """Whether this is a person's account rather than the OS's own.

        The band ``[uid_min, uid_max]`` is the login range every distribution
        carves out; everything outside it is a daemon account, ``nobody``, or a
        systemd dynamic user -- machinery, not people. A ``nologin``/``false``
        shell then catches the service accounts that squat inside the band anyway
        (NixOS's ``nixbld`` builders are the case in point).
        """
        return (
            settings.uid_min <= self.uid <= settings.uid_max
            and _is_login_shell(self.shell)
        )


@dataclass(frozen=True, slots=True)
class Group:
    """One row of ``getent group``."""

    name: str
    gid: int
    members: tuple[str, ...]
    """The *supplementary* members. A user's primary group lists them by gid, not here."""


def _getent(settings: UsersSettings, database: str, *keys: str) -> str:
    """Run ``getent <database> [keys]`` and return its stdout.

    ``getent`` exits non-zero only when a *key* was asked for and not found; a
    bare dump of the whole database is always exit 0. So a lookup miss comes back
    as empty output rather than an error, which the callers here want -- "no such
    user" is a fact about the answer, not a failure of the question.
    """
    result = run(
        Command(argv=[settings.getent_bin, database, *keys]),
        timeout=settings.command_timeout,
        log=log,
    )
    return result.stdout


def _parse_passwd(text: str) -> list[User]:
    """Parse ``name:passwd:uid:gid:gecos:home:shell`` lines into users.

    A line we cannot make sense of -- too few fields, a non-numeric uid -- is
    skipped rather than raised on. ``getent`` does not emit malformed lines, but a
    hand-mangled ``/etc/passwd`` is exactly the machine on which this page should
    still load and show the accounts it *can* read.
    """
    users: list[User] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 7:
            continue
        name, _pw, uid, gid, gecos, home, shell = parts[:7]
        try:
            users.append(User(name, int(uid), int(gid), gecos, home, shell))
        except ValueError:
            continue
    return users


def _parse_group(text: str) -> list[Group]:
    """Parse ``name:passwd:gid:member,member`` lines into groups."""
    groups: list[Group] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        name, _pw, gid, members = parts[:4]
        try:
            gid_int = int(gid)
        except ValueError:
            continue
        member_names = tuple(m for m in members.split(",") if m)
        groups.append(Group(name, gid_int, member_names))
    return groups


def list_users(settings: UsersSettings, *, include_system: bool = False) -> list[User]:
    """Every login account, sorted by uid.

    Without ``include_system`` the daemon accounts (uid below
    :attr:`~spiriconfig_users.config.UsersSettings.uid_min`) and the high-uid
    machinery (``nobody``, systemd dynamic users) are dropped, so the list is the
    people who can log in. With it, the whole of ``getent passwd`` comes back --
    which is what a developer troubleshooting a service account wants.
    """
    users = _parse_passwd(_getent(settings, "passwd"))
    if not include_system:
        users = [u for u in users if u.is_login(settings)]
    return sorted(users, key=lambda u: u.uid)


def list_groups(settings: UsersSettings) -> list[Group]:
    """Every group, sorted by name."""
    return sorted(_parse_group(_getent(settings, "group")), key=lambda g: g.name)


def get(settings: UsersSettings, name: str) -> User:
    """Look up one account by name, or raise :class:`UserError` if there is none."""
    parsed = _parse_passwd(_getent(settings, "passwd", name))
    if not parsed:
        raise UserError(f"no such user: {name!r}")
    return parsed[0]


def groups_for(user: User, groups: list[Group]) -> list[str]:
    """The names of every group ``user`` belongs to, primary and supplementary.

    Cross-referenced from an already-fetched group list rather than shelling out
    per user (``id -nG``): the page draws a row per user and would otherwise run a
    command per row. The primary group is the one whose gid matches the user's;
    the rest are the groups that name the user among their members.
    """
    names = [g.name for g in groups if g.gid == user.gid or user.name in g.members]
    return sorted(set(names))


def validate_name(name: str) -> str:
    """Return ``name`` stripped, or raise :class:`UserError` if it is not a valid one.

    The same rule ``useradd`` enforces, checked here so a bad name is a readable
    sentence in the UI instead of a terse non-zero exit after the fact.
    """
    name = name.strip()
    if not name:
        raise UserError("a username is required.")
    if not _NAME_RE.fullmatch(name):
        raise UserError(
            f"{name!r} is not a valid unix username: start with a letter or "
            "underscore, then letters, digits, underscores or hyphens."
        )
    return name


def create(
    settings: UsersSettings,
    name: str,
    *,
    comment: str = "",
    shell: str = "",
    create_home: bool = True,
    groups: list[str] | None = None,
    system: bool = False,
) -> Command:
    """Build the ``useradd`` line for a new account.

    It does not set a password -- ``useradd`` leaves the account locked until one
    is, which is why the UI and CLI offer to run :func:`set_password` straight
    after. ``--create-home`` is the default because an account you cannot log into
    is rarely the intent; a service account that wants no home passes
    ``create_home=False``.
    """
    name = validate_name(name)
    argv = [settings.useradd_bin]
    if create_home:
        argv.append("--create-home")
    if system:
        argv.append("--system")
    if shell:
        argv += ["--shell", shell]
    if comment:
        argv += ["--comment", comment]
    if groups:
        argv += ["--groups", ",".join(groups)]
    argv.append(name)
    return Command(argv=argv)


def delete(settings: UsersSettings, name: str, *, remove_home: bool = False) -> Command:
    """Build the ``userdel`` line.

    ``remove_home`` adds ``--remove``, which deletes the home directory and mail
    spool too. It defaults off: removing an account and destroying its files are
    two different decisions, and the destructive one should be asked for, not
    assumed.
    """
    argv = [settings.userdel_bin]
    if remove_home:
        argv.append("--remove")
    argv.append(name)
    return Command(argv=argv)


def set_password(settings: UsersSettings, name: str) -> Command:
    """Build the ``chpasswd`` line for setting ``name``'s password.

    The password is **not** in this command. ``chpasswd`` reads ``user:password``
    from stdin, so the new secret travels on the input channel of
    :func:`~spiriconfig.commands.run` -- the one thing that is never logged and
    never rendered into the copy-pasteable line -- exactly as ``docker login
    --password-stdin`` does. Pair this with :func:`password_stdin`.

    The by-hand equivalent a person would type is ``passwd <name>``; we use
    ``chpasswd`` instead only because it takes the password without a terminal,
    which a web form has no way to offer.
    """
    return Command(argv=[settings.chpasswd_bin])


def password_stdin(name: str, password: str) -> str:
    """The stdin ``chpasswd`` expects for :func:`set_password`: ``name:password``."""
    return f"{name}:{password}\n"


def hash_password(settings: UsersSettings) -> Command:
    """Build the ``mkpasswd`` line that hashes a password for provisioning.

    Reads the plaintext on stdin and prints a ``/etc/shadow``-compatible
    hash on stdout -- the shape ``password.hash`` files in a provisioning
    repo store, and what :func:`set_password_hashed` later applies with
    ``chpasswd -e``. The password itself never touches argv or a log, the
    same discipline :func:`set_password` already follows; pass it as
    :func:`~spiriconfig.commands.run`'s ``input``, not paired with a name
    the way :func:`password_stdin` is -- ``mkpasswd`` hashes, it does not
    set anything, so there is no account to name yet.

    The by-hand equivalent is ``mkpasswd --method=yescrypt``, typed at a
    prompt so the password never appears on a command line either.

    ``--method`` and its value **must** be one argv element
    (``--method=yescrypt``), not two (``--method``, ``yescrypt``) --
    whois's ``mkpasswd`` parses the space-separated form as ``--method``
    with no value followed by a stray positional argument, silently prints
    its "Available methods" list to stdout instead of a hash, and still
    exits 0. A real, reproduced bug, not a hypothetical one -- see
    :func:`validate_password_hash`, which exists because exit code alone
    didn't catch it.
    """
    return Command(
        argv=[
            settings.mkpasswd_bin,
            f"--method={settings.password_hash_method}",
            "--stdin",
        ]
    )


def validate_password_hash(hashed: str) -> str:
    """Return ``hashed`` stripped, or raise :class:`UserError` if it
    doesn't look like a ``/etc/shadow``-style hash.

    :func:`hash_password` exiting 0 is not proof its stdout is a hash --
    see that function's docstring for the exact, real way it can print
    something else entirely while still succeeding. Every hash
    ``mkpasswd`` can produce starts with ``$``; nothing else it could
    print by mistake does, so this is a cheap, real check rather than
    trusting the exit code alone.
    """
    hashed = hashed.strip()
    if not hashed.startswith("$"):
        raise UserError(
            "mkpasswd did not produce a password hash -- got "
            f"{hashed.splitlines()[0] if hashed else '(empty output)'!r} "
            "instead. Check that mkpasswd is installed and working."
        )
    return hashed


def set_password_hashed(settings: UsersSettings, name: str) -> Command:
    """Build the ``chpasswd -e`` line for setting ``name``'s password from an
    *already-hashed* value -- what a provisioning repo's ``password.hash``
    holds, as opposed to :func:`set_password`'s plaintext. Pair with
    :func:`password_stdin`, passing the hash where it takes a password.
    """
    return Command(argv=[settings.chpasswd_bin, "-e"])


def add_to_group(settings: UsersSettings, name: str, group: str) -> Command:
    """Build the ``gpasswd --add`` line adding ``name`` to ``group``.

    ``gpasswd`` for a single group, not ``usermod --append --groups``: it changes
    exactly the one membership named and touches no others, so there is no
    ``-a``-vs-not footgun where forgetting a flag rewrites the user's whole group
    set.
    """
    return Command(argv=[settings.gpasswd_bin, "--add", name, group])


def remove_from_group(settings: UsersSettings, name: str, group: str) -> Command:
    """Build the ``gpasswd --delete`` line removing ``name`` from ``group``."""
    return Command(argv=[settings.gpasswd_bin, "--delete", name, group])


#: First token of a line ``ssh-keygen`` or an OpenSSH client would produce --
#: enough to catch a pasted passphrase or a stray comment, not an exhaustive
#: parse of every option OpenSSH's ``authorized_keys`` format allows.
_SSH_KEY_TYPES = frozenset({
    "ssh-ed25519",
    "ssh-rsa",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
})


def ssh_dir(user: User) -> Path:
    """``user``'s ``.ssh`` directory, where ``authorized_keys`` lives."""
    return Path(user.home) / ".ssh"


def authorized_keys_path(user: User) -> Path:
    """``user``'s ``authorized_keys`` file."""
    return ssh_dir(user) / "authorized_keys"


def list_ssh_keys(user: User) -> list[str]:
    """The public keys in ``user``'s ``authorized_keys``, one per line.

    Blank lines and comments are dropped. A read, so it happens immediately
    rather than building a :class:`Command` -- same stance :func:`_shells`
    takes on ``/etc/shells``, and the same reasoning ``users.py``'s module
    docstring gives for ``getent``: there's nothing to show or undo about
    looking at a file. A missing file (no ``.ssh`` yet, or none of this
    plugin's business to have created it) is an empty list, not an error.
    """
    try:
        text = authorized_keys_path(user).read_text()
    except OSError:
        return []
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def validate_ssh_key(key: str) -> str:
    """Return ``key`` stripped, or raise :class:`UserError` if it doesn't
    look like one OpenSSH public key line.

    Checked here for the same reason :func:`validate_name` checks a
    username before building a command: a bad paste should fail with a
    sentence, not a stray line in ``authorized_keys`` that ``sshd`` silently
    ignores.
    """
    key = key.strip()
    if not key:
        raise UserError("a public key is required.")
    if len(key.splitlines()) > 1:
        raise UserError("paste one public key at a time.")
    if key.split(" ", 1)[0] not in _SSH_KEY_TYPES:
        raise UserError(
            "doesn't look like an OpenSSH public key -- expected it to start "
            "with ssh-ed25519, ssh-rsa, ecdsa-sha2-..., or similar."
        )
    return key


def ensure_ssh_dir(settings: UsersSettings, user: User) -> Command:
    """Build the ``install`` line creating ``user``'s ``.ssh`` directory at
    mode 700, owned by them -- what ``sshd`` requires before it will trust
    anything inside. Only ever meant to be run when the directory doesn't
    already exist; ``install`` on one that does is harmless but pointless.
    """
    return Command(
        argv=[
            settings.install_bin, "-d", "-m", "700",
            "-o", user.name, "-g", str(user.gid),
            str(ssh_dir(user)),
        ]
    )


def ensure_authorized_keys(settings: UsersSettings, user: User) -> Command:
    """Build the ``install`` line creating an empty ``authorized_keys`` at
    mode 600, owned by ``user`` -- ``sshd`` refuses a key file writable by
    anyone else. Copies from ``/dev/null`` rather than touching an existing
    file in place, so this must only be called when the file is missing --
    calling it on one that already has keys in it would wipe them.
    """
    return Command(
        argv=[
            settings.install_bin, "-m", "600",
            "-o", user.name, "-g", str(user.gid),
            "/dev/null", str(authorized_keys_path(user)),
        ]
    )


def add_ssh_key(settings: UsersSettings, user: User) -> Command:
    """Build the ``tee -a`` line appending a key to ``user``'s
    ``authorized_keys``. The key itself is not in the command -- it travels
    on stdin, the same channel :func:`set_password` uses for a plaintext
    password, so a multi-line paste never has to survive being an argv
    element.
    """
    return Command(argv=[settings.tee_bin, "-a", str(authorized_keys_path(user))])


def replace_ssh_keys(settings: UsersSettings, user: User) -> Command:
    """Build the bare ``tee`` line overwriting ``user``'s ``authorized_keys``
    with whatever is given on stdin. There is no ``gpasswd --delete``
    equivalent for one key in a text file, so removing one is done by
    rewriting the file with everything but that line.
    """
    return Command(argv=[settings.tee_bin, str(authorized_keys_path(user))])


__all__ = [
    "Group",
    "User",
    "UserError",
    "add_ssh_key",
    "add_to_group",
    "authorized_keys_path",
    "create",
    "delete",
    "ensure_authorized_keys",
    "ensure_ssh_dir",
    "get",
    "groups_for",
    "list_groups",
    "list_ssh_keys",
    "list_users",
    "password_stdin",
    "remove_from_group",
    "replace_ssh_keys",
    "set_password",
    "ssh_dir",
    "validate_name",
    "validate_password_hash",
    "validate_ssh_key",
]
