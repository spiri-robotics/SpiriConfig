"""The NiceGUI face of the terminal: a shell, in the browser.

Every other plugin's page is a set of buttons that stand for commands. This one
skips the standing-for. That makes it the one page where the usual promise --
"nothing here does anything the CLI cannot" -- is not a discipline we have to keep
so much as a tautology: the thing on the page *is* the command line.

What it owes the rest of the project is the other direction. A shell in a browser
tab is a process on the device, and the tab can vanish without warning -- a closed
laptop, a dropped wifi, a reboot of the machine you are sitting at rather than the
one you are talking to. Nobody sends us a goodbye. So the session is tied to the
socket, and when the socket goes, the shell is hung up on: see
:meth:`~spiriconfig.commands.PtySession.close`. Otherwise every visit to this page
leaks a shell, and a device that has been debugged a few times is quietly hosting
a dozen of them.
"""

from __future__ import annotations

from loguru import logger
from nicegui import context, ui

from spiriconfig import advanced, terminal, theme
from spiriconfig.commands import Command, PtySession

from spiriconfig_terminal.config import TerminalSettings, terminal_settings
from spiriconfig_terminal.shell import shell_command

log = logger.bind(plugin="terminal")


def _command_line(command: Command) -> None:
    """The invocation, to copy -- the same one ``spiriconfig terminal shell`` runs.

    Kept even though this page is itself advanced-only, and not out of habit: a
    web terminal is precisely where somebody is most likely to be reaching for a
    real one, and this is the line that gets them the same shell over SSH.
    """
    with advanced.only(), ui.row().classes(
        f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"
    ):
        ui.label(str(command)).classes("font-mono text-xs grow break-all")
        ui.button(
            icon="content_copy",
            on_click=lambda: ui.clipboard.write(str(command)),
        ).props("flat dense round").tooltip("Copy command")


def page(settings: TerminalSettings | None = None) -> None:
    """Render the terminal page and open a shell on it."""
    config = settings or terminal_settings()
    command = shell_command(config)
    # The size it is born with, if the browser will not tell us a better one.
    session = PtySession(
        command,
        log=log,
        rows=terminal.TERMINAL_ROWS,
        columns=terminal.TERMINAL_COLUMNS,
    )

    with ui.column().classes("w-full gap-2"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("Terminal").classes("text-2xl font-bold")
            ui.space()
            # A shell you have exited is a dead pane with no way out of it, and a
            # shell that has wedged is worse, because it looks alive. Reloading the
            # page is the answer to both -- the old session is hung up on when this
            # socket closes, and the new page opens a new one.
            ui.button(
                "New session",
                icon="refresh",
                on_click=lambda: ui.navigate.reload(),
            ).props("flat").tooltip("Exit this shell and start a fresh one")

        _command_line(command)

        # A height, because xterm.js fits itself to the box it is given and a box
        # with no height is a terminal with no rows. Viewport-relative so that the
        # shell gets most of a laptop screen without running off the bottom of it.
        view = terminal.interactive(
            on_data=lambda event: session.write(event.data),
            on_resize=lambda event: session.resize(event.rows, event.cols),
        ).classes("h-[70vh]")

    # Not started here: page() runs while the page is still being *described*, and
    # there is no browser on the other end yet to measure or to write to. The timer
    # waits for the socket, then fires once. (NiceGUI's timers await the client
    # connection for us, which is the whole reason this is a timer and not a task.)
    client = context.client
    ui.timer(
        0,
        lambda: terminal.pump(session, view, client, what="the shell", log=log),
        once=True,
    )


__all__ = ["page"]
