"""Users plugin settings, read from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class UsersSettings(BaseSettings):
    """Settings for the users plugin, prefixed ``SPIRICONFIG_USERS_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_USERS_",
        env_file=".env",
        extra="ignore",
    )

    uid_min: int = 1000
    """Lowest uid the page treats as a *login* account.

    ``SPIRICONFIG_USERS_UID_MIN``. Below this are the daemon and service accounts
    (``root``, ``daemon``, ``www-data`` …) that no one logs in as -- distributions
    put the first human user at 1000, which is what ``/etc/login.defs`` calls
    ``UID_MIN``. The page hides everything below this line unless asked, so the
    list is about people and not about the forty accounts the OS made for itself.
    """

    uid_max: int = 60000
    """Highest uid the page treats as a login account.

    ``SPIRICONFIG_USERS_UID_MAX``, matching ``/etc/login.defs``' ``UID_MAX``. It
    is what keeps ``nobody`` (65534) and systemd's dynamically-allocated service
    users (61184–65519) out of a list that is meant to be the humans.
    """

    # The shadow-utils binaries we drive. They are settings, not constants, for the
    # one deployment that does not have them: a busybox-only rootfs (some Yocto drone
    # images) ships adduser/deluser/addgroup applets with different flags instead. A
    # real busybox backend is not built yet -- pointing these elsewhere is the seam
    # where it would go. See the module docstring in users.py.
    getent_bin: str = "getent"
    useradd_bin: str = "useradd"
    userdel_bin: str = "userdel"
    chpasswd_bin: str = "chpasswd"
    gpasswd_bin: str = "gpasswd"

    install_bin: str = "install"
    """Creates ``~/.ssh`` and ``~/.ssh/authorized_keys`` with the right owner
    and mode when they don't already exist -- see
    :func:`spiriconfig_users.users.ensure_ssh_dir`/``ensure_authorized_keys``.
    Only ever asked to create, never to overwrite an existing file, so it's
    safe to call even when a previous run already did the work."""

    tee_bin: str = "tee"
    """Writes SSH public keys into an account's ``authorized_keys``, content
    on stdin -- ``tee -a`` to append, bare ``tee`` to replace the whole file.
    The same "secret/content never on the command line" discipline
    :attr:`chpasswd_bin` already follows, though a public key isn't a secret;
    it's just the natural way to hand a multi-line value to a command."""

    mkpasswd_bin: str = "mkpasswd"
    """Hashes a password for provisioning's ``password.hash`` files -- see
    :func:`spiriconfig_users.users.hash_password`. From the ``whois``
    package on Debian and derivatives; not needed for anything else this
    plugin does, since the live password-set path (:func:`~spiriconfig_users.users.set_password`)
    hands ``chpasswd`` a plaintext password directly."""

    password_hash_method: str = "yescrypt"
    """``mkpasswd --method``. Yescrypt is glibc's current default
    ``/etc/shadow`` hash on Debian 11+ and most modern distributions --
    matching that default means a hash generated here needs no special
    handling on the device that later applies it."""

    command_timeout: float = 30.0
    """Seconds before a user command is considered hung. ``SPIRICONFIG_USERS_COMMAND_TIMEOUT``."""


def users_settings() -> UsersSettings:
    """Load users plugin settings from the environment."""
    return UsersSettings()
