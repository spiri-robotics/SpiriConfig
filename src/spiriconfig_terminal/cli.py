"""``spiriconfig terminal`` -- the CLI face of the terminal.

There is a joke lurking here: the CLI face of a terminal is a terminal, and if you
are already in one, you did not need us. It is still worth existing, for two
reasons that are not jokes.

The first is the project's rule -- the UI must never be the only way to do
something -- and this is what keeping it costs when the feature *is* a shell.

The second is that ``--show`` prints the exact invocation the web page runs, which
is the honest answer to "what does that button actually give me?". You can read
it, and then run it yourself over SSH, and get the same shell.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from spiriconfig_terminal.config import terminal_settings
from spiriconfig_terminal.shell import shell_command

app = typer.Typer(
    name="terminal",
    help="Open a shell on this device.",
    no_args_is_help=True,
)


@app.command()
def shell(
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the command instead of running it."),
    ] = False,
) -> None:
    """Start the same shell the web terminal opens."""
    command = shell_command(terminal_settings())

    if show:
        print(command)  # noqa: T201 - the point of the command is its output
        return

    # exec, rather than spawning a child and waiting on it: this shell should be
    # talking to the terminal you are sitting at, with your job control and your
    # signals, and the surest way to arrange that is to stop being in the way.
    os.chdir(command.cwd or "/")
    os.execvp(command.argv[0], list(command.argv))  # noqa: S606 - argv is ours


__all__ = ["app"]
