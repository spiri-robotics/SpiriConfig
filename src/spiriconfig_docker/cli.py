"""``spiriconfig docker`` -- the CLI face of the docker plugin.

Every command here is a thin wrapper over a ``docker compose`` invocation, and
``--show`` on any of them prints that invocation instead of running it. That is
not a debugging aid, it is the point: it teaches the user the command they could
have run without us, so they are never dependent on this tool.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from loguru import logger

from spiriconfig.commands import Command, run, stream

from spiriconfig_docker.config import docker_settings
from spiriconfig_docker.stacks import Stack, StackError, discover, get

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


@app.command()
def config(stack: StackArg) -> None:
    """Print the path to a compose project's file.

    Deliberately prints the path rather than opening an editor: editing is
    ``$EDITOR "$(spiriconfig docker config foo)"``, which is a thing the user's
    own tools already do better than we would.
    """
    typer.echo(str(_stack(stack).compose_file))
