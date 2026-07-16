"""``spiriconfig docker`` -- the CLI face of the docker plugin.

Every command here is a thin wrapper over a ``docker compose`` invocation, and
``--show`` on any of them prints that invocation instead of running it. That is
not a debugging aid, it is the point: it teaches the user the command they could
have run without us, so they are never dependent on this tool.
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

import typer
from loguru import logger

from spiriconfig.commands import Command, run, stream

from spiriconfig_docker import settings as app_settings
from spiriconfig_docker.config import docker_settings
from spiriconfig_docker.env import read as read_env
from spiriconfig_docker.settings import SettingsError, StackSettings
from spiriconfig_docker.stacks import (
    DEFAULT_EXEC_COMMAND,
    Stack,
    StackError,
    discover,
    get,
)

log = logger.bind(plugin="docker")

app = typer.Typer(
    name="docker",
    help="Manage docker compose projects.",
    no_args_is_help=True,
)

ShowOption = Annotated[
    bool,
    typer.Option("--show", help="Print the docker command instead of running it."),
]
StackArg = Annotated[str, typer.Argument(help="Name of the compose project.")]


def _stack(name: str) -> Stack:
    """Look up a stack, turning a miss into a clean CLI error."""
    try:
        return get(docker_settings(), name)
    except StackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _execute(command: Command, *, show: bool) -> None:
    """Run a command, or print it, then exit non-zero if it failed."""
    if show:
        typer.echo(str(command))
        return
    result = run(command, timeout=docker_settings().command_timeout, log=log)
    if result.stdout:
        typer.echo(result.stdout.rstrip())
    if result.stderr:
        typer.echo(result.stderr.rstrip(), err=True)
    if not result.ok:
        raise typer.Exit(result.returncode)


def _execute_streaming(command: Command, *, show: bool) -> None:
    """Run a long command, echoing its output as it arrives."""
    if show:
        typer.echo(str(command))
        return

    async def pump() -> None:
        async for line in stream(command, log=log):
            typer.echo(line)

    asyncio.run(pump())


@app.command("list")
def list_stacks() -> None:
    """List compose projects and whether they are running."""
    stacks = discover(docker_settings())
    if not stacks:
        typer.echo("No compose projects found.")
        return
    width = max(len(s.name) for s in stacks)
    for stack in stacks:
        typer.echo(f"{stack.name:<{width}}  {stack.status()}")


@app.command()
def up(stack: StackArg, show: ShowOption = False) -> None:
    """Start a compose project in the background."""
    _execute_streaming(_stack(stack).up(), show=show)


@app.command()
def down(stack: StackArg, show: ShowOption = False) -> None:
    """Stop a compose project and remove its containers."""
    _execute_streaming(_stack(stack).down(), show=show)


@app.command()
def restart(stack: StackArg, show: ShowOption = False) -> None:
    """Restart a compose project."""
    _execute_streaming(_stack(stack).restart(), show=show)


@app.command()
def pull(stack: StackArg, show: ShowOption = False) -> None:
    """Pull a compose project's images."""
    _execute_streaming(_stack(stack).pull(), show=show)


@app.command()
def logs(
    stack: StackArg,
    follow: Annotated[
        bool, typer.Option("-f", "--follow", help="Keep streaming new lines.")
    ] = False,
    tail: Annotated[int, typer.Option(help="Lines of history to show.")] = 200,
    show: ShowOption = False,
) -> None:
    """Show a compose project's logs."""
    _execute_streaming(_stack(stack).logs(follow=follow, tail=tail), show=show)


@app.command()
def ps(stack: StackArg, show: ShowOption = False) -> None:
    """Show a compose project's containers."""
    _execute(_stack(stack).ps(), show=show)


def _execute_interactively(command: Command, *, show: bool) -> None:
    """Hand our terminal to the command, and get out of the way.

    ``exec`` rather than spawning a child and waiting on it, exactly as
    ``spiriconfig terminal shell`` does: this is a program that wants to *be* the
    thing you are typing at, with your job control, your signals and your window
    size. Every layer we leave between it and the terminal is a layer that can get
    one of those wrong.
    """
    if show:
        typer.echo(str(command))
        return
    os.chdir(command.cwd or "/")
    os.execvp(command.argv[0], list(command.argv))  # noqa: S606 - argv is ours


ServiceArg = Annotated[str, typer.Argument(help="Service within the project.")]


@app.command("exec")
def exec_(
    stack: StackArg,
    service: ServiceArg,
    command: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[COMMAND]...",
            help=f"What to run. Defaults to {DEFAULT_EXEC_COMMAND}.",
        ),
    ] = None,
    show: ShowOption = False,
) -> None:
    """Run a command in a service's container -- a shell, unless you say otherwise.

    Put ``--`` before a command that has options of its own, or typer will read
    them as ours: ``spiriconfig docker exec grafana grafana -- ls -la /etc``.
    """
    _execute_interactively(_stack(stack).exec(service, command or []), show=show)


@app.command()
def attach(stack: StackArg, service: ServiceArg, show: ShowOption = False) -> None:
    """Attach to a service's main process, stdin and all.

    Not the same as ``exec``, and the difference bites: ``exec`` starts a new
    process next to the app, while this connects you to the app itself. What your
    keystrokes and your Ctrl-C do to it is between you and that process.
    """
    _execute_interactively(_stack(stack).attach(service), show=show)


@app.command()
def config(stack: StackArg) -> None:
    """Print the path to a compose project's file.

    Deliberately prints the path rather than opening an editor: editing is
    ``$EDITOR "$(spiriconfig docker config foo)"``, which is a thing the user's
    own tools already do better than we would.
    """
    typer.echo(str(_stack(stack).compose_file))


@app.command()
def env(stack: StackArg) -> None:
    """Print the path to a compose project's ``.env`` file.

    The companion to ``config``, and the same idea: the settings page edits this
    file, so this is how you get at what it edited. ``cat "$(spiriconfig docker env
    grafana)"`` is the whole of "what did the UI just do to my machine".
    """
    typer.echo(str(_settings(stack).env_file))


def _settings(name: str) -> StackSettings:
    """Look up a stack's declared settings, turning a bad schema into a CLI error."""
    try:
        return app_settings.for_stack(_stack(name))
    except SettingsError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


#: Stand-in for a secret in ``settings`` output. See the ``--show-secrets`` flag.
MASK = "********"


@app.command()
def settings(
    stack: StackArg,
    assignments: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[KEY=VALUE]...",
            help="Settings to change. With none, the current ones are listed.",
        ),
    ] = None,
    reset: Annotated[
        list[str] | None,
        typer.Option(
            "--reset",
            help="Reset a setting to its declared default. Repeatable, and applied "
            "before any KEY=VALUE, so `--reset PORT PORT=9000` still ends at 9000.",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Run `up -d` afterwards, so the change takes effect."),
    ] = False,
    show_secrets: Annotated[
        bool,
        typer.Option("--show-secrets", help="Print password fields instead of masking them."),
    ] = False,
) -> None:
    """Show or change the settings an app declares in ``x-spiri-settings``.

    Writes the same ``.env`` the web UI's settings page writes, with the same
    validation in front of it -- the CLI is not a back door around the form's
    rules, it is the same door. ``--reset`` is that door's other handle: the form's
    per-field reset button, spelled for a shell, restoring a field to the ``default:``
    its app declared.
    """
    stack_settings = _settings(stack)

    if not stack_settings.fields:
        typer.echo(
            f"{stack} declares no settings. An app opts in by listing them under "
            f"`{app_settings.SETTINGS_KEY}` in its compose file."
        )
        return

    if not assignments and not reset:
        _list_settings(stack_settings, show_secrets=show_secrets)
        return

    values = dict(stack_settings.values())

    # Resets first, so a `--reset PORT PORT=9000` on the same line lands on 9000
    # rather than being undone by its own reset -- the explicit value is the later,
    # more specific word about what the user wants.
    for key in reset or []:
        try:
            field = app_settings.get(stack_settings, key)
        except SettingsError as exc:
            typer.secho(f"{exc}. Nothing was changed.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        values[key] = field.default

    for assignment in assignments or []:
        key, separator, value = assignment.partition("=")
        if not separator:
            typer.secho(
                f"{assignment!r} is not a KEY=VALUE. Nothing was changed.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        try:
            app_settings.get(stack_settings, key)
        except SettingsError as exc:
            typer.secho(f"{exc}. Nothing was changed.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        values[key] = value

    # Every assignment is checked before any of them is written, so a typo in the
    # third one does not leave the first two applied and the app half-configured.
    try:
        stack_settings.save(values)
    except (SettingsError, OSError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Wrote {stack_settings.env_file}")

    if apply:
        _execute_streaming(stack_settings.stack.up(), show=False)
    else:
        typer.echo(
            f"Run `spiriconfig docker up {stack}` to restart it with the new settings."
        )


def _list_settings(stack_settings: StackSettings, *, show_secrets: bool) -> None:
    """Print each declared setting, its value, and where that value came from.

    The provenance column is the useful one: "this is the app's default" and "this
    is what you set last week" look identical in a value column, and only one of
    them changes when the app is updated.

    Every field, including the ones the web form marks ``advanced:`` and hides. That
    is not an oversight: ``advanced:`` is an app author decluttering a *form*, and a
    CLI that took a UI hint as an instruction to withhold a setting would be hiding
    it from the one person who went looking. The note says which they are, so that
    "why can I not see this on the page?" has an answer here.
    """
    current = stack_settings.values()
    in_file = set(read_env(stack_settings.env_file))
    width = max(len(f.env) for f in stack_settings.fields)

    for field in stack_settings.fields:
        value = current[field.env]
        if field.widget == "password" and value and not show_secrets:
            value = MASK
        notes = []
        if field.env not in in_file:
            notes.append("default")
        if field.advanced:
            notes.append("advanced")
        origin = f"  ({', '.join(notes)})" if notes else ""
        typer.echo(f"{field.env:<{width}}  {value or '(unset)'}{origin}")

    typer.echo(f"\n{stack_settings.env_file}")
