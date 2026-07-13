"""The terminal widget that plugins stream command output into.

SpiriConfig runs the command a human would have run, so the honest place to show
its output is the thing a human would have watched it in: a terminal. This module
is one xterm.js element and the size we agree to run commands at, shared so that
every plugin's output looks the same and none of them has to think about it.

The size is fixed rather than fitted to the browser window, and that is a choice
worth explaining. Fitting means asking the browser how many columns it ended up
with and then telling the pty to match -- a round trip, into JavaScript, before a
single byte of output can be read. It also means the same command produces
differently-wrapped output on a laptop and a phone. A fixed 120x30 is what the
command sees, always, and the box scrolls if the window is smaller.
"""

from __future__ import annotations

from nicegui import ui

#: The size of the pseudo-terminal commands run in, and of the widget that shows
#: them. These must agree: the pty tells the program how wide it is, and the
#: program wraps and draws progress bars to that width. If the widget were
#: narrower, docker's careful in-place redraws would land in the wrong columns.
TERMINAL_ROWS = 30
TERMINAL_COLUMNS = 120

OPTIONS = {
    "rows": TERMINAL_ROWS,
    "cols": TERMINAL_COLUMNS,
    "scrollback": 5000,
    "fontSize": 12,
    # Nothing we show is interactive -- these are transcripts of commands that
    # have already been decided on. A cursor that blinks in a read-only pane just
    # suggests it is waiting for you to type.
    "cursorBlink": False,
    "disableStdin": True,
    "convertEol": False,  # the pty already emits \r\n; see spiriconfig.commands
}


def terminal() -> ui.xterm:
    """An xterm.js pane, sized to match :func:`spiriconfig.commands.stream_pty`.

    Write raw ``bytes`` straight from ``stream_pty`` into it. Do not decode them
    and do not clean them up: the carriage returns and escape sequences are the
    output, and xterm.js is the thing that knows what they mean.
    """
    return ui.xterm(options=OPTIONS).classes("w-full")


__all__ = ["OPTIONS", "TERMINAL_COLUMNS", "TERMINAL_ROWS", "terminal"]
