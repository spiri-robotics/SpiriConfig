"""``spiriconfig users`` -- the CLI face of the users plugin.

Every command is a thin wrapper over a shadow-utils invocation, and ``--show`` on
any of them prints that invocation instead of running it -- the same bargain the
docker plugin makes: the flag teaches you the command you could have run without
us, so this tool is never the only way.
"""

from __future__ import annotations

from typing import Annotated

import typer
from loguru import logger

from spiriconfig.commands import Command, CommandError, run

from spiriconfig_users import users
from spiriconfig_users.config import UsersSettings, users_settings
from spiriconfig_users.users import UserError

log = logger.bind(plugin="users")

app = typer.Typer(
    name="users",
    help="Manage system login accounts.",
    no_args_is_help=True,
)

group_app = typer.Typer(
    name="group",
    help="Add or remove a user's group membership.",
    no_args_is_help=True,
)
app.add_typer(group_app)

ShowOption = Annotated[
    bool,
    typer.Option("--show", help="Print the command instead of running it."),
]
NameArg = Annotated[str, typer.Argument(help="The account's login name.")]


def _settings() -> UsersSettings:
    return users_settings()


def _fail(message: str) -> typer.Exit:
    """Print ``message`` in red on stderr and return an Exit to raise."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _execute(command: Command, *, show: bool, input: str | None = None) -> None:
    """Run a command, or print it, then exit non-zero if it failed.

    ``input`` goes to the process's stdin and is never printed by ``--show`` --
    that is the whole reason a password can be set this way. See
    :func:`spiriconfig.commands.run`.
    """
    if show:
        typer.echo(str(command))
        return
    result = run(command, timeout=_settings().command_timeout, input=input, log=log)
    if result.stdout:
        typer.echo(result.stdout.rstrip())
    if result.stderr:
        typer.echo(result.stderr.rstrip(), err=True)
    if not result.ok:
        raise typer.Exit(result.returncode)


@app.command("list")
def list_users(
    all_: Annotated[
        bool,
        typer.Option(
            "--all", "-a", help="Include system and service accounts, not just logins."
        ),
    ] = False,
) -> None:
    """List login accounts, their uid, and the groups they are in."""
    settings = _settings()
    people = users.list_users(settings, include_system=all_)
    if not people:
        typer.echo("No accounts found.")
        return
    groups = users.list_groups(settings)
    width = max(len(u.name) for u in people)
    for user in people:
        member_of = ", ".join(users.groups_for(user, groups))
        typer.echo(f"{user.name:<{width}}  {user.uid:>6}  {member_of}")


@app.command()
def add(
    name: NameArg,
    comment: Annotated[
        str, typer.Option("--comment", "-c", help="The account's full name / GECOS.")
    ] = "",
    shell: Annotated[
        str, typer.Option("--shell", "-s", help="Login shell. Defaults to useradd's.")
    ] = "",
    group: Annotated[
        list[str] | None,
        typer.Option("--group", "-G", help="Supplementary group to add to. Repeatable."),
    ] = None,
    no_create_home: Annotated[
        bool, typer.Option("--no-create-home", help="Do not make a home directory.")
    ] = False,
    system: Annotated[
        bool, typer.Option("--system", help="Make a system account (low uid, no aging).")
    ] = False,
    show: ShowOption = False,
) -> None:
    """Create a new account. It has no password until you set one -- see ``passwd``."""
    settings = _settings()
    try:
        command = users.create(
            settings,
            name,
            comment=comment,
            shell=shell,
            create_home=not no_create_home,
            groups=group,
            system=system,
        )
    except UserError as exc:
        raise _fail(str(exc)) from exc
    _execute(command, show=show)
    if not show:
        typer.echo(f"Set a password with: spiriconfig users passwd {name}")


@app.command("del")
def delete(
    name: NameArg,
    remove_home: Annotated[
        bool,
        typer.Option("--remove-home", help="Also delete the home directory and mail spool."),
    ] = False,
    show: ShowOption = False,
) -> None:
    """Delete an account. Its home directory is kept unless you say otherwise."""
    _execute(users.delete(_settings(), name, remove_home=remove_home), show=show)


@app.command()
def passwd(
    name: NameArg,
    show: ShowOption = False,
) -> None:
    """Set an account's password.

    The password is read from a prompt, never from an argument -- an argument
    would land in your shell history and in the process list. It reaches
    ``chpasswd`` on stdin, so it appears in neither the command we run nor
    ``--show``.
    """
    settings = _settings()
    command = users.set_password(settings, name)
    if show:
        typer.echo(str(command))
        typer.echo(f"# then send '{name}:<password>' on its stdin")
        return

    # Confirm it exists first, so a typo'd name fails with "no such user" rather
    # than after the person has typed a password twice for nothing.
    try:
        users.get(settings, name)
    except UserError as exc:
        raise _fail(str(exc)) from exc

    password = typer.prompt(
        f"New password for {name}", hide_input=True, confirmation_prompt=True
    )
    try:
        run(
            command,
            timeout=settings.command_timeout,
            input=users.password_stdin(name, password),
            log=log,
        ).check()
    except CommandError as exc:
        raise _fail(
            exc.result.stderr.strip() or "chpasswd failed"
        ) from exc
    typer.echo(f"Password updated for {name}.")


@group_app.command("add")
def group_add(
    user: Annotated[str, typer.Argument(help="The account to add.")],
    group: Annotated[str, typer.Argument(help="The group to add them to.")],
    show: ShowOption = False,
) -> None:
    """Add a user to a group."""
    _execute(users.add_to_group(_settings(), user, group), show=show)


@group_app.command("remove")
def group_remove(
    user: Annotated[str, typer.Argument(help="The account to remove.")],
    group: Annotated[str, typer.Argument(help="The group to remove them from.")],
    show: ShowOption = False,
) -> None:
    """Remove a user from a group."""
    _execute(users.remove_from_group(_settings(), user, group), show=show)


__all__ = ["app"]
