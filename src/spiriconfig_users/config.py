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

    command_timeout: float = 30.0
    """Seconds before a user command is considered hung. ``SPIRICONFIG_USERS_COMMAND_TIMEOUT``."""


def users_settings() -> UsersSettings:
    """Load users plugin settings from the environment."""
    return UsersSettings()
