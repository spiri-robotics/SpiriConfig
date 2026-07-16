"""Tests for the users plugin's page.

Driven the way a person drives it -- open the page, see who is listed -- with
``getent`` stubbed so the test is about what the page draws, not about whatever
accounts the machine running the suite happens to have. Nothing here runs a write
command; the command-building is covered in test_users.py.
"""

from __future__ import annotations

import typer
from nicegui.testing import User

from spiriconfig import web
from spiriconfig.plugins import Plugin
from spiriconfig_users import users
from spiriconfig_users.config import UsersSettings

from tests.test_users import SAMPLE_GROUP, SAMPLE_PASSWD


class _UsersPage(Plugin):
    name = "users"
    title = "Users"

    def cli(self) -> typer.Typer:
        return typer.Typer()

    def page(self) -> None:
        from spiriconfig_users import web as users_web

        users_web.page(UsersSettings())


def _stub_getent(monkeypatch) -> None:
    """Make the page read our sample databases instead of the host's."""

    def fake(settings: UsersSettings, database: str, *keys: str) -> str:
        return SAMPLE_PASSWD if database == "passwd" else SAMPLE_GROUP

    monkeypatch.setattr(users, "_getent", fake)


async def test_lists_login_accounts_and_hides_system(
    user: User, monkeypatch
) -> None:
    _stub_getent(monkeypatch)
    web.build([_UsersPage()])
    await user.open("/users")
    # The human account is shown, with a group it belongs to.
    await user.should_see("alice")
    await user.should_see("docker")
    # A daemon account is not, until "show system accounts" is turned on.
    await user.should_not_see("nixbld1")


async def test_add_user_button_is_present(user: User, monkeypatch) -> None:
    _stub_getent(monkeypatch)
    web.build([_UsersPage()])
    await user.open("/users")
    await user.should_see("Add user")
