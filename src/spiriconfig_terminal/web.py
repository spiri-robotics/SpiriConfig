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
from spiriconfig.commands import Command, CommandError, PtySession

from spiriconfig_terminal.config import TerminalSettings, terminal_settings
from spiriconfig_terminal.shell import shell_command

log = logger.bind(plugin="terminal")


async def _measure(view: ui.xterm) -> tuple[int, int] | None:
    """Ask the browser how big the terminal came out, or None if it will not say.

    Only the browser knows: the size depends on the font it chose and the window
    it has, neither of which ever reaches us. So this is a round trip, and a round
    trip is a thing that can fail to come back.

    When it does not, we do not have a page-load error worth showing anybody -- we
    have a size we could not confirm, and a person still waiting for a shell. So
    take the default and carry on, exactly as :func:`spiriconfig.theme.codemirror_theme`
    does with the colour it cannot ask about. A terminal that guessed 120 columns
    is a terminal; a terminal that refused to open because it could not measure
    itself is a bug report.
    """
    try:
        await view.fit()
        return await view.get_rows(), await view.get_columns()
    except (TimeoutError, RuntimeError):
        log.warning(
            "the browser did not say how big the terminal is; "
            "falling back to {}x{}",
            terminal.TERMINAL_ROWS,
            terminal.TERMINAL_COLUMNS,
        )
        return None


async def _run(session: PtySession, view: ui.xterm) -> None:
    """Start the shell, and pump it into the terminal until one end gives up.

    The size is settled before the shell is spawned, not after. A shell that is
    born believing it has 80 columns and is corrected a moment later has already
    printed its prompt at the wrong width, and anything it drew is smeared -- so
    we pay for the round trip to the browser first, once, and let the shell open
    its eyes at the size it is actually going to live at.
    """
    if measured := await _measure(view):
        session.resize(*measured)

    try:
        await session.start()
    except CommandError as exc:
        log.error("could not start the shell: {}", exc)
        view.write(f"\r\n[could not start {session.command}]\r\n{exc}\r\n")
        return

    # Registered only once the shell is up, so that a disconnect arriving while we
    # were still measuring the window cannot close a session that does not exist.
    context.client.on_disconnect(session.close)

    async for chunk in session.output():
        view.write(chunk)

    # Reached when the shell exits, and also when the *browser* went away and
    # close() ended the stream -- in which case there is nobody left to write to
    # and this just falls harmlessly on the floor. Either way the process is
    # reaped, which is the part that has to happen regardless of who is watching.
    view.write(f"\r\n[{_ending(await session.wait())}]\r\n")


def _ending(code: int) -> str:
    """How the shell ended, in words, for the last line in the pane.

    A signalled process comes back from ``wait()`` as a *negative* number, and
    "the shell exited with code -15" is not something a person should have to
    decode in order to learn that something killed it.
    """
    if code < 0:
        return f"the shell was killed by signal {-code}"
    return f"the shell exited with code {code}"


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
    ui.timer(0, lambda: _run(session, view), once=True)


__all__ = ["page"]
