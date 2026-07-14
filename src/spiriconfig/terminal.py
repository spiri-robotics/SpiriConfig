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
"""

from __future__ import annotations

from nicegui import ui
from nicegui.events import Handler, XtermDataEventArguments, XtermResizeEventArguments

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


__all__ = [
    "INTERACTIVE_OPTIONS",
    "OPTIONS",
    "TERMINAL_COLUMNS",
    "TERMINAL_ROWS",
    "interactive",
    "terminal",
]
