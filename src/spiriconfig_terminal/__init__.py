"""The terminal plugin: a shell on the device, in the browser.

The plugin that admits the UI is not the whole story. Every other page is a set of
buttons standing in for commands, and each one is an argument that the command was
worth wrapping; this is the door you go through when the answer is no -- when the
thing you need to do next is not a button we thought of, and you would rather have
the machine than a picture of it.

It is advanced-only, and that is a statement about *audience*, not about safety.
The person who wants a shell here is a developer, and putting a terminal in a
customer's sidebar invites them into a room where nothing is labelled. It hides no
capability: the shell runs as the user SpiriConfig already runs as, doing what our
own plugins already do with that user, and advanced mode does not gate the route
(see :mod:`spiriconfig.advanced`). Access control is authentication's job, and
authentication is not built yet.
"""

from __future__ import annotations

import typer

from spiriconfig.plugins import Plugin

from spiriconfig_terminal.cli import app as cli_app


class TerminalPlugin(Plugin):
    """A shell on this device."""

    name = "terminal"
    title = "Terminal"
    description = "Open a shell on this device."
    icon = "terminal"
    advanced = True

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        # Imported lazily, like the other plugins: every plugin is imported to
        # build the CLI, and `spiriconfig terminal shell` should not pay to import
        # a web framework in order to hand you a shell.
        from spiriconfig_terminal import web

        web.page()


__all__ = ["TerminalPlugin"]
