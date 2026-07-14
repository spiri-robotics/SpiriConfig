"""The terminal widgets: one to watch a command in, and one to type into.

SpiriConfig runs the command a human would have run, so the honest place to show
its output is the thing a human would have watched it in: a terminal. This module
is the xterm.js configuration, shared so that every plugin's output looks the same
and none of them has to think about it.

There are two of them, and the difference between them is not decoration:

:func:`terminal` is a **transcript**. A command was decided on, it ran, and this
shows what it said. It takes no input, and its size is fixed rather than fitted to
the browser window -- fitting means asking the browser how many columns it ended
up with and waiting for the answer before a single byte can be read, and it means
the same command wraps differently on a laptop and on a phone. A fixed 120x30 is
what the command sees, always, and the box scrolls if the window is smaller.

:func:`interactive` is a **terminal**, in the sense the word usually has. There is
a shell at the other end, so every one of those choices inverts: it takes input,
it blinks a cursor to say so, and it *does* fit the window, because a person
resizing their browser expects vim to still fill it. The round trip we refuse to
pay for a transcript is exactly what an interactive session is worth paying for,
since there is nobody waiting on the first byte -- the shell is waiting on them.

:func:`pump` is what makes an :func:`interactive` pane actually live: it settles
the size, starts the process, and copies bytes between the two until one end gives
up. It lives here rather than in any one plugin because there is now more than one
thing on the far end of an interactive terminal -- a login shell on the terminal
page, and ``docker compose exec`` or ``attach`` on the apps page -- and every one
of them has to get the hangup right or it leaks a process per browser tab.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from nicegui import ui
from nicegui.events import Handler, XtermDataEventArguments, XtermResizeEventArguments

from spiriconfig.commands import CommandError, PtySession

if TYPE_CHECKING:  # loguru only exports the Logger type to type checkers
    from loguru import Logger
    from nicegui import Client

#: The size of the pseudo-terminal commands run in, and of the widget that shows
#: them. These must agree: the pty tells the program how wide it is, and the
#: program wraps and draws progress bars to that width. If the widget were
#: narrower, docker's careful in-place redraws would land in the wrong columns.
TERMINAL_ROWS = 30
TERMINAL_COLUMNS = 120

#: What both terminals agree on. `convertEol` is off in both because the pty has
#: already turned every \n into \r\n -- see :mod:`spiriconfig.commands`.
_COMMON = {
    "scrollback": 5000,
    "fontSize": 12,
    "convertEol": False,
}

OPTIONS = {
    **_COMMON,
    "rows": TERMINAL_ROWS,
    "cols": TERMINAL_COLUMNS,
    # Nothing we show here is interactive -- these are transcripts of commands that
    # have already been decided on. A cursor that blinks in a read-only pane just
    # suggests it is waiting for you to type.
    "cursorBlink": False,
    "disableStdin": True,
}

INTERACTIVE_OPTIONS = {
    **_COMMON,
    # The opposite of the above, for the opposite reason: there is a shell at the
    # other end of this one, and it *is* waiting for you to type.
    "cursorBlink": True,
    "disableStdin": False,
}


def terminal() -> ui.xterm:
    """An xterm.js pane, sized to match :func:`spiriconfig.commands.stream_pty`.

    Write raw ``bytes`` straight from ``stream_pty`` into it. Do not decode them
    and do not clean them up: the carriage returns and escape sequences are the
    output, and xterm.js is the thing that knows what they mean.
    """
    return ui.xterm(options=OPTIONS).classes("w-full")


def interactive(
    *,
    on_data: Handler[XtermDataEventArguments],
    on_resize: Handler[XtermResizeEventArguments],
) -> ui.xterm:
    """An xterm.js pane wired for two-way traffic with a
    :class:`~spiriconfig.commands.PtySession`.

    Both handlers are required, because a terminal missing either one is broken in
    a way that looks like a hang. Without ``on_data`` the keystrokes go nowhere and
    the shell appears frozen; without ``on_resize`` the pty keeps insisting the
    window is its original size, and every full-screen program draws to the wrong
    one.

    The caller still has to call ``fit()`` once the browser has laid the element
    out, and hand the resulting size to the session before starting it.
    """
    return ui.xterm(
        options=INTERACTIVE_OPTIONS,
        on_data=on_data,
        on_resize=on_resize,
    ).classes("w-full")


async def _measure(view: ui.xterm, *, log: Logger) -> tuple[int, int] | None:
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
            "the browser did not say how big the terminal is; falling back to {}x{}",
            TERMINAL_ROWS,
            TERMINAL_COLUMNS,
        )
        return None


def _ending(code: int, what: str) -> str:
    """How the process ended, in words, for the last line in the pane.

    A signalled process comes back from ``wait()`` as a *negative* number, and
    "the shell exited with code -15" is not something a person should have to
    decode in order to learn that something killed it.
    """
    if code < 0:
        return f"{what} was killed by signal {-code}"
    return f"{what} exited with code {code}"


async def pump(
    session: PtySession,
    view: ui.xterm,
    client: Client,
    *,
    what: str = "the command",
    log: Logger = logger,
) -> None:
    """Start ``session`` at ``view``'s size and copy it into ``view`` until it ends.

    The size is settled before the process is spawned, not after. A shell that is
    born believing it has 80 columns and is corrected a moment later has already
    printed its prompt at the wrong width, and anything it drew is smeared -- so
    we pay for the round trip to the browser first, once, and let it open its eyes
    at the size it is actually going to live at.

    ``client`` is taken as an argument rather than read from
    :data:`nicegui.context` because this is called from a task, and the caller is
    the one that still definitely knows which browser it is serving. It is what the
    session is hung up on when that browser goes away -- and something has to be,
    because nobody sends us a goodbye when a laptop lid comes down. Without it every
    visit leaks a process, and a device that has been debugged a few times is
    quietly hosting a dozen of them.

    ``what`` names the thing in the closing line: "the shell exited with code 0".
    """
    if measured := await _measure(view, log=log):
        session.resize(*measured)

    try:
        await session.start()
    except CommandError as exc:
        log.error("could not start {}: {}", what, exc)
        view.write(f"\r\n[could not start {session.command}]\r\n{exc}\r\n")
        return

    # Registered only once the process is up, so that a disconnect arriving while
    # we were still measuring the window cannot close a session that does not exist.
    client.on_disconnect(session.close)

    async for chunk in session.output():
        view.write(chunk)

    # Reached when the process exits, and also when the *browser* went away and
    # close() ended the stream -- in which case there is nobody left to write to
    # and this just falls harmlessly on the floor. Either way the process is
    # reaped, which is the part that has to happen regardless of who is watching.
    view.write(f"\r\n[{_ending(await session.wait(), what)}]\r\n")


__all__ = [
    "INTERACTIVE_OPTIONS",
    "OPTIONS",
    "TERMINAL_COLUMNS",
    "TERMINAL_ROWS",
    "interactive",
    "pump",
    "terminal",
]
