"""The NiceGUI face of the users plugin.

Nothing here does anything the CLI cannot, and every action shows the exact
shadow-utils command it runs, with a button to copy it -- so the page is a
demonstration of ``useradd``/``userdel``/``gpasswd``/``chpasswd`` rather than a
replacement for them.

These commands are quick and near-silent, unlike ``docker compose up``, so this
plugin does not stream into a terminal the way the docker one does. It runs the
command off the event loop, shows the line it ran, and reports success or the
OS's own error -- which for "only root may do this" is the message the user most
needs to see.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from nicegui import ui

from spiriconfig import advanced, theme
from spiriconfig.commands import Command, Result, run

from spiriconfig_users import users
from spiriconfig_users.config import UsersSettings, users_settings
from spiriconfig_users.users import Group, User, UserError

log = logger.bind(plugin="users")

#: Fallback login shells offered in the add-user form when ``/etc/shells`` cannot be
#: read. The real list comes from that file; this is what a bare-bones image has
#: anyway.
_FALLBACK_SHELLS = ["/bin/bash", "/bin/sh"]


def _shells() -> list[str]:
    """The login shells to offer, from ``/etc/shells`` if it is there.

    ``/etc/shells`` is the list the system itself considers valid, which is the
    honest thing to put in the dropdown -- but the box is editable, because
    ``useradd`` will accept a shell that is not in the file and a developer
    sometimes means to.
    """
    try:
        listed = [
            line.strip()
            for line in Path("/etc/shells").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    except OSError:
        listed = []
    return listed or _FALLBACK_SHELLS


def _copyable(command: Command) -> None:
    """The exact command line, with a button to take it away with you."""
    with ui.row().classes(f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"):
        ui.label(str(command)).classes("font-mono text-xs grow break-all")
        ui.button(
            icon="content_copy",
            on_click=lambda: ui.clipboard.write(str(command)),
        ).props("flat dense round").tooltip("Copy command")


async def _run(command: Command, *, input: str | None = None) -> Result:
    """Run a command off the event loop and notify the outcome.

    A shadow-utils failure is almost always one of two things -- "not run as root"
    or a name that does not exist -- and both come back on stderr, so that is what
    we surface. Returns the :class:`Result` so callers can decide what to do next
    (chain a password set onto a create, refresh a list) only when it worked.
    """
    result = await asyncio.to_thread(
        run, command, timeout=users_settings().command_timeout, input=input, log=log
    )
    if result.ok:
        ui.notify("Done.", type="positive")
    else:
        ui.notify(
            result.stderr.strip() or f"command failed (exit {result.returncode})",
            type="negative",
            multi_line=True,
            timeout=0,
        )
    return result


async def _add_dialog(settings: UsersSettings, on_done) -> None:
    """Create an account, and optionally set its first password in the same breath.

    The ``useradd`` line assembles itself as you type, shown below the form -- the
    whole idea of the project in one widget, since what you are watching build is
    exactly what you could paste into a root shell instead. A password typed here
    is applied with a second command (``chpasswd``) after the account exists,
    because ``useradd`` cannot take one; the password never reaches the preview.
    """
    groups = await asyncio.to_thread(users.list_groups, settings)
    group_names = [g.name for g in groups]

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("New user").classes("text-lg font-bold")

        name = ui.input("Username").classes("w-full").props("outlined").mark("new-user-name")
        comment = ui.input("Full name").classes("w-full").props("outlined")
        shell = (
            ui.select(_shells(), value=_shells()[0], label="Login shell", with_input=True)
            .classes("w-full")
            .props("outlined")
        )
        chosen_groups = (
            ui.select(group_names, label="Groups", multiple=True)
            .classes("w-full")
            .props("outlined")
        )
        password = (
            ui.input("Password (optional)", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("outlined")
        )
        create_home = ui.checkbox("Create home directory", value=True)

        with ui.row().classes(f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"):
            preview = ui.label().classes("font-mono text-xs grow break-all")

        def built() -> Command | None:
            """The useradd command for the current form, or None if the name is bad."""
            try:
                return users.create(
                    settings,
                    name.value,
                    comment=comment.value.strip(),
                    shell=shell.value or "",
                    create_home=create_home.value,
                    groups=list(chosen_groups.value or []),
                )
            except UserError:
                return None

        def show() -> None:
            command = built()
            preview.set_text(str(command) if command else "…")
            go.set_enabled(command is not None)

        async def do_create() -> None:
            try:
                name_value = users.validate_name(name.value)
            except UserError as exc:
                ui.notify(str(exc), type="negative")
                return
            command = built()
            if command is None:
                return
            if not (await _run(command)).ok:
                return
            # Only now that the account exists is there something to set a password
            # on. A create that worked but a password that did not is worth saying
            # so plainly -- the account is real, it just has no password yet.
            if password.value:
                await _run(
                    users.set_password(settings, name_value),
                    input=users.password_stdin(name_value, password.value),
                )
            dialog.close()
            on_done()

        for field in (name, comment, shell, chosen_groups, create_home):
            field.on_value_change(show)

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            go = (
                ui.button("Create", icon="person_add", on_click=do_create)
                .props("color=primary")
                .mark("new-user-create")
            )
        show()

    dialog.open()


async def _password_dialog(settings: UsersSettings, user: User) -> None:
    """Set one account's password.

    Two boxes that have to agree, because a password you cannot see is a password
    you can mistype. The line shown is a bare ``chpasswd`` -- honestly so: the
    secret is on its stdin, which is the point, and there is nothing about the
    password to render even if we wanted to.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label(f"{user.name} — set password").classes("text-lg font-bold")

        first = (
            ui.input("New password", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("outlined")
        )
        again = (
            ui.input("Confirm", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("outlined")
        )

        with advanced.only():
            _copyable(users.set_password(settings, user.name))
            ui.label(
                "The password is sent on chpasswd's stdin, so it is not in the "
                "command, the logs, or this page."
            ).classes("text-xs text-gray-500")

        async def submit() -> None:
            if not first.value:
                ui.notify("Enter a password.", type="warning")
                return
            if first.value != again.value:
                ui.notify("The two passwords do not match.", type="warning")
                return
            result = await _run(
                users.set_password(settings, user.name),
                input=users.password_stdin(user.name, first.value),
            )
            if result.ok:
                dialog.close()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Set password", on_click=submit).props("color=primary")

    dialog.open()


async def _groups_dialog(settings: UsersSettings, user: User, on_done) -> None:
    """Edit one account's supplementary group memberships, one command at a time.

    A chip per group the user is in, each with a remove button, and a picker to
    add another -- and every add or remove is a single ``gpasswd`` that runs at
    once. One action, one command, mirroring how the CLI does it; there is no
    "Save" that batches invisible changes, because a membership change is
    something you should see happen and be able to copy.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label(f"{user.name} — groups").classes("text-lg font-bold")
        body = ui.column().classes("w-full gap-3")

        async def render() -> None:
            body.clear()
            groups = await asyncio.to_thread(users.list_groups, settings)
            member_of = users.groups_for(user, groups)
            # The primary group cannot be dropped with gpasswd -- it is the user's
            # gid, not a supplementary membership -- so it is shown but not removable.
            primary = next((g.name for g in groups if g.gid == user.gid), None)

            with body:
                with ui.row().classes("w-full items-center gap-2"):
                    if not member_of:
                        ui.label("In no groups.").classes("text-sm text-gray-500")
                    for group_name in member_of:
                        chip = ui.chip(group_name).props("outline")
                        if group_name == primary:
                            chip.props("icon=star").tooltip("Primary group")
                        else:
                            chip.set_property("removable", True)
                            chip.on(
                                "remove",
                                lambda _, g=group_name: act(
                                    users.remove_from_group(settings, user.name, g)
                                ),
                            )

                addable = [
                    g.name for g in groups if g.name not in member_of
                ]
                picker = (
                    ui.select(addable, label="Add to group", with_input=True)
                    .classes("w-full")
                    .props("outlined")
                )

                async def add() -> None:
                    if picker.value:
                        await act(
                            users.add_to_group(settings, user.name, picker.value)
                        )

                ui.button("Add", icon="add", on_click=add).props("flat")

        async def act(command: Command) -> None:
            if (await _run(command)).ok:
                await render()
                on_done()  # the card behind the dialog shows chips too; keep it honest

        await render()

        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()


async def _delete_dialog(settings: UsersSettings, user: User, on_done) -> None:
    """Confirm deleting an account, and whether to take its home directory with it.

    The one destructive action here, so it is the one behind a confirmation. The
    ``userdel`` line changes as the "remove home" box is ticked, so what will run
    is on screen before it runs.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label(f"Delete {user.name}?").classes("text-lg font-bold")
        remove_home = ui.checkbox(f"Also delete {user.home}", value=False)

        with ui.row().classes(f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"):
            preview = ui.label().classes("font-mono text-xs grow break-all")

        def show() -> None:
            preview.set_text(
                str(users.delete(settings, user.name, remove_home=remove_home.value))
            )

        remove_home.on_value_change(show)
        show()

        async def do_delete() -> None:
            command = users.delete(settings, user.name, remove_home=remove_home.value)
            if (await _run(command)).ok:
                dialog.close()
                on_done()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", icon="delete", on_click=do_delete).props("color=negative")

    dialog.open()


def _user_card(
    settings: UsersSettings, user: User, groups: list[Group], refresh
) -> None:
    """One account: who it is, where it lives, and what you can do to it."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(user.name).classes("text-lg font-bold")
                    if user.full_name:
                        ui.label(user.full_name).classes("text-sm text-gray-500")
                ui.label(
                    f"uid {user.uid} · {user.shell} · {user.home}"
                ).classes("text-xs text-gray-500")
            with ui.row().classes("items-center gap-1 flex-wrap justify-end"):
                for group_name in users.groups_for(user, groups):
                    ui.chip(group_name).props("outline dense").classes("text-xs")

        with ui.row().classes("gap-2"):
            ui.button(
                "Password", icon="password",
                on_click=lambda: _password_dialog(settings, user),
            ).props("flat")
            ui.button(
                "Groups", icon="group",
                on_click=lambda: _groups_dialog(settings, user, refresh),
            ).props("flat")
            ui.button(
                "Delete", icon="delete",
                on_click=lambda: _delete_dialog(settings, user, refresh),
            ).props("flat color=negative")


def page(settings: UsersSettings | None = None) -> None:
    """Render the users plugin's page."""
    config = settings or users_settings()

    ui.label("Users").classes("text-2xl font-bold")
    ui.label("Login accounts on this device").classes("text-sm text-gray-500")

    # Advanced only: the service and daemon accounts. An operator manages people;
    # the forty accounts the OS made for itself are a developer's concern, and
    # `spiriconfig users list --all` shows them from the shell regardless.
    #
    # Made to read as advanced the way a flat button does -- purple *label text*,
    # via the theme's `text-advanced` utility class -- because that is the only
    # always-on signal. `advanced.mark` paints only buttons purple; a checkbox's
    # `color` prop tints just the checked box, so unchecked it would sit there
    # grey. The `color` still handles the ticked state, and `mark` adds the ring
    # and the "only in advanced mode" visibility.
    show_system = (
        ui.checkbox("Show system accounts")
        .props(f"color={theme.ADVANCED}")
        .classes("text-advanced")
    )
    advanced.mark(show_system)

    container = ui.column().classes("w-full gap-2")

    async def render() -> None:
        container.clear()
        people = await asyncio.to_thread(
            users.list_users, config, include_system=show_system.value
        )
        groups = await asyncio.to_thread(users.list_groups, config)
        with container:
            if not people:
                with ui.card().classes("w-full"):
                    ui.label("No accounts found.").classes("text-lg")
                return
            for user in people:
                _user_card(config, user, groups, refresh)

    def refresh() -> None:
        # Pinned to the container's parent slot, not the slot of whatever button
        # was clicked -- otherwise the timer becomes a child of `container`, and
        # render()'s clear() deletes the very timer whose callback is running,
        # blanking the page. The docker plugin's refresh() carries the same scar.
        with container.parent_slot:
            ui.timer(0.1, render, once=True)

    show_system.on_value_change(refresh)

    with ui.row().classes("items-center gap-2"):
        ui.button(
            "Add user", icon="person_add", on_click=lambda: _add_dialog(config, refresh)
        ).mark("add-user")
        ui.button("Refresh", icon="refresh", on_click=refresh).props("flat")

    refresh()
