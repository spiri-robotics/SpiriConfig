"""The users plugin: the machine's login accounts, managed as a human would.

Who may log in is a question about unix accounts, and this is where you answer it.
With the optional PAM login on (``SPIRICONFIG_AUTH=pam``), the accounts this page
manages are exactly the accounts that can sign into the web UI -- so creating a
user here, or deleting one, is how the login roster is kept. It grants no special
powers: everything is ``useradd``/``userdel``/``chpasswd``/``gpasswd``, the
commands an administrator already runs, and it needs root for the same reason they
do.

It is not advanced-only. Managing the people who use a machine is an operator's
job, not a developer's -- unlike the terminal next to it, which is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from spiriconfig.plugins import Plugin

from spiriconfig_users.cli import app as cli_app

if TYPE_CHECKING:
    from spiriconfig_provision.contract import ApplyStep, Change, StagedRepo


class UsersPlugin(Plugin):
    """The system's login accounts."""

    name = "users"
    title = "Users"
    description = "Add, remove, and manage system login accounts."
    icon = "manage_accounts"

    # See spiriconfig_users.provision and docs/provisioning.md: this plugin's
    # contribution to the provisioning plugin, kept in its own module so
    # `spiriconfig users list` never pays to import anything about it.
    provision_resource = "users"

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        # Imported lazily, like the other plugins: `spiriconfig users list` should
        # not pay to import a web framework to print a table.
        from spiriconfig_users import web

        web.page()

    def provision_patch_block(self, repo: StagedRepo) -> None:
        from spiriconfig_users.provision import render_patch_block

        render_patch_block(repo)

    def provision_baseline_block(self, repo: StagedRepo) -> None:
        from spiriconfig_users.provision import render_baseline_block

        render_baseline_block(repo)

    def provision_describe(self, repo: StagedRepo, path: str) -> Change | None:
        from spiriconfig_users.provision import describe

        return describe(repo, path)

    def provision_apply(self, repo: StagedRepo) -> list[ApplyStep]:
        from spiriconfig_users.provision import apply

        return apply(repo)


__all__ = ["UsersPlugin"]
