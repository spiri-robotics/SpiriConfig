"""The app store plugin: install compose apps from a git repository.

An app store is a git repo with one top-level directory per app, each holding a
compose file. Installing an app symlinks it out of the store's checkout and into
the compose directory, where the docker plugin picks it up as an ordinary stack.

That symlink is the only thing this plugin creates, and it is also the only thing
it knows. Provenance, local edits, upstream changes, merges and conflicts are all
git's, on a working tree the user can ``cd`` into and drive by hand. See
:mod:`spiriconfig_appstore.stores`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from spiriconfig.plugins import Plugin

from spiriconfig_appstore.cli import app as cli_app

if TYPE_CHECKING:
    from spiriconfig_provision.contract import ApplyStep, Change, StagedRepo


class AppStorePlugin(Plugin):
    """Browse and install apps from git-hosted app stores."""

    name = "appstore"
    title = "App Store"
    description = "Install apps from a git-hosted app store."
    icon = "storefront"

    # See spiriconfig_appstore.provision and docs/provisioning.md: this
    # plugin's contribution to the provisioning plugin, kept in its own
    # module so `spiriconfig appstore list` never pays to import anything
    # about it.
    provision_resource = "apps"

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        # Imported lazily, for the reason the docker plugin gives: every plugin is
        # imported to build the CLI, and `spiriconfig appstore list` should not
        # pay to import a web framework it will never render with.
        from spiriconfig_appstore import web

        web.page()

    def provision_patch_block(self, repo: StagedRepo) -> None:
        from spiriconfig_appstore.provision import render_patch_block

        render_patch_block(repo)

    def provision_baseline_block(self, repo: StagedRepo) -> None:
        from spiriconfig_appstore.provision import render_baseline_block

        render_baseline_block(repo)

    def provision_describe(self, repo: StagedRepo, path: str) -> Change | None:
        from spiriconfig_appstore.provision import describe

        return describe(repo, path)

    def provision_apply(self, repo: StagedRepo) -> list[ApplyStep]:
        from spiriconfig_appstore.provision import apply

        return apply(repo)

    async def on_startup(self) -> None:
        # Fetch every cloned store once, so the "update available" markers are
        # honest the first time anyone opens the page. Launched as a background
        # task and returned from immediately: the fetch reaches the network, and
        # the server must not wait on a slow remote before it starts serving.
        from nicegui import background_tasks

        from spiriconfig_appstore import web

        background_tasks.create(
            web.fetch_on_startup(), name="appstore-startup-fetch",
        )


__all__ = ["AppStorePlugin"]
