"""App store settings, read from the environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from spiriconfig import paths


class AppStoreSettings(BaseSettings):
    """Settings for the appstore plugin, prefixed ``SPIRICONFIG_APPSTORE_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_APPSTORE_",
        env_file=".env",
        extra="ignore",
    )

    stores: list[str] = []
    """*Seed* git URLs of app stores. ``SPIRICONFIG_APPSTORE_STORES``, a JSON list.

    An app store is an ordinary git repository with one top-level directory per
    app, each containing a compose file. Nothing else. That is the whole format,
    which means anyone can host one, and a user can inspect one with ``git
    clone`` and read it with ``ls``.

    Not the live list -- disk is (see :func:`spiriconfig_appstore.stores.stores`).
    These are stores to *offer* on a machine that has none yet: each one that is
    not already cloned shows up ready to clone, and once cloned it is an ordinary
    store discovered from disk like any the user added themselves. Removing a store
    in the UI deletes its checkout; a seed listed here then reappears as
    not-yet-cloned, because this list says what to offer, not what must exist.

    Empty by default: there is no store every machine should be offered. A
    checkout seeds the example store this repository ships, through the ``.env``
    ``./scripts/test-data.sh`` writes. A git URL and a local path are the same
    thing to ``git clone``, which is why the example store is a perfectly
    ordinary store and not a special case anywhere in the code.
    """

    store_dir: Path = Field(default_factory=paths.store_dir)
    """Where store clones live. ``SPIRICONFIG_APPSTORE_STORE_DIR``.

    Not a cache: installed apps are symlinks *into* these clones, and a user's
    edits to an installed app land here as changes in a git working tree. That
    is the point -- it is what lets ``git diff`` answer "what did I change?" and
    ``git merge`` answer "what happens when the store moves on?".

    Defaults to ``/var/lib/spiriconfig/stores`` as root and
    ``~/.local/share/spiriconfig/stores`` otherwise (see :mod:`spiriconfig.paths`);
    a checkout points it at ``test_data/stores`` through its ``.env``.
    """

    git_bin: str = "git"
    """The git executable. ``SPIRICONFIG_APPSTORE_GIT_BIN``."""

    command_timeout: float = 300.0
    """Seconds before a git command is considered hung."""

    commit_name: str = "SpiriConfig"
    commit_email: str = "spiriconfig@localhost"
    """Identity for the local commits made to preserve a user's edits on update.

    git refuses to commit without one, and the machine running SpiriConfig
    usually has no global git identity configured. Passed with ``-c`` on the
    command line rather than written into the repo's config, so the commands we
    print stay complete and copy-pasteable.
    """


def appstore_settings() -> AppStoreSettings:
    """Load appstore plugin settings from the environment."""
    return AppStoreSettings()
