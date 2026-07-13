"""App store settings, read from the environment."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppStoreSettings(BaseSettings):
    """Settings for the appstore plugin, prefixed ``SPIRICONFIG_APPSTORE_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_APPSTORE_",
        env_file=".env",
        extra="ignore",
    )

    stores: list[str] = ["test_data/example-store"]
    """Git URLs of app stores. ``SPIRICONFIG_APPSTORE_STORES``, as a JSON list.

    An app store is an ordinary git repository with one top-level directory per
    app, each containing a compose file. Nothing else. That is the whole format,
    which means anyone can host one, and a user can inspect one with ``git
    clone`` and read it with ``ls``.

    The default is the example store this repository ships, built by
    ``./scripts/test-data.sh``. A git URL and a local path are the same thing to
    ``git clone``, which is why the example store is a perfectly ordinary store
    and not a special case anywhere in the code.
    """

    store_dir: Path = Path("test_data/stores")
    """Where store clones live. ``SPIRICONFIG_APPSTORE_STORE_DIR``.

    Not a cache: installed apps are symlinks *into* these clones, and a user's
    edits to an installed app land here as changes in a git working tree. That
    is the point -- it is what lets ``git diff`` answer "what did I change?" and
    ``git merge`` answer "what happens when the store moves on?".

    Relative by default, for the reason
    :attr:`~spiriconfig_docker.config.DockerSettings.compose_dir` is: a checkout
    should stay inside its own directory. A deployment sets this to something
    like ``/var/lib/spiriconfig/stores``.
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
