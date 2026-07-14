"""The ``.env`` file beside a compose file: reading it, and writing it back.

Docker compose already reads a ``.env`` from the project directory and
substitutes it into ``${VAR}`` in the compose file. That is why the settings form
has somewhere to put its answers that is not ours: an ordinary file, in a format
the user already knows, which keeps working exactly the same when SpiriConfig is
not running. :mod:`spiriconfig_docker.settings` describes the form; this module is
where the form's answers land.

Two things shape everything below.

**We patch lines, we do not rewrite the file.**

The same argument as compose files being text (see :doc:`design`). A ``.env`` is a
file the user also edits: it has their comments in it, their own variables, and an
order they chose. Round-tripping it through a parser and re-emitting it would
quietly eat all three. So :func:`patch` edits the *spans* belonging to the keys it
was asked to change, and every other byte of the file survives untouched.

**Quoting is not obvious, and getting it wrong silently loses data.**

Verified against ``docker compose`` (v5.1.4), because guessing here is how a
password ends up truncated:

===================== ===================================== =========================
Written               Compose reads it as                   Why
===================== ===================================== =========================
``K=a$bc``            ``a``                                 unquoted ``$`` interpolates
``K=bare val # x``    ``bare val``                          unquoted `` #`` is a comment
``K="a $x"``          ``a``                                 double quotes interpolate too
``K="a \\$x"``         ``a $x``                              ...but backslash escapes work
``K='a $x #y'``       ``a $x #y``                           single quotes are fully literal
``K='it's'``          *parse error*                         no escape exists for ``'``
``K=``                ``""``                                empty, and legal
===================== ===================================== =========================

So the encoder's rule is: leave the boring values bare (ports, tags, booleans --
which is nearly all of them, and a ``.env`` full of needless quotes is a ``.env``
nobody wants to hand-edit), single-quote anything with a character that would be
eaten, and fall back to double quotes with escapes only for the one case single
quotes cannot express -- a value containing an apostrophe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: A ``KEY=`` at the start of a line. Deliberately *not* accepting ``export KEY=``:
#: compose does not, and a line we understood but compose ignored would show the
#: user a value that is not actually in effect.
_ASSIGNMENT = re.compile(r"[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)\Z")

#: Values that need no quoting at all: no whitespace, no ``$``, no ``#``, no quote
#: characters, no backslash. Ports, image tags, hostnames, ``true``/``false``, and
#: numbers all land here, which is the overwhelming majority of what a form writes.
_BARE = re.compile(r"\A[A-Za-z0-9_.:/@%+,=-]+\Z")

#: Backslash escapes compose honours inside double quotes.
_UNESCAPE = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "$": "$",
}


@dataclass(frozen=True, slots=True)
class Entry:
    """One assignment in a ``.env``, and the lines it occupies.

    The line span is the point of this class. A quoted value may run across
    several lines, and a patcher that assumed one key meant one line would replace
    the first of them and leave the rest behind as loose garbage -- turning a
    multi-line certificate into a syntax error. Spans make that unrepresentable.
    """

    key: str
    value: str
    start: int
    """Index of the line the assignment begins on."""

    end: int
    """Index one past the line it ends on, so ``lines[start:end]`` is the whole of it."""


def _read_quoted(text: str, quote: str) -> tuple[str, int] | None:
    """Read a quoted value from the start of ``text``. Returns (value, chars consumed).

    ``None`` if the closing quote never arrives, which means the file is malformed;
    the caller treats the line as unparseable rather than guessing where it ended.

    Single quotes are literal, so the first ``'`` closes them -- there is no escape
    for an apostrophe inside them, because compose has none either. Double quotes
    honour backslash escapes.
    """
    out = []
    i = 1  # skip the opening quote
    while i < len(text):
        char = text[i]
        if char == quote:
            return "".join(out), i + 1
        if char == "\\" and quote == '"' and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(_UNESCAPE.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(char)
        i += 1
    return None


def _strip_bare(text: str) -> str:
    """Trim an unquoted value the way compose does: inline comment, then whitespace.

    A ``#`` only starts a comment when whitespace precedes it, which is why
    ``pa#ss`` survives as a password and ``3000 # the port`` does not keep its
    comment.
    """
    cut = re.search(r"(?:^|\s)#", text)
    if cut is not None:
        text = text[: cut.start()]
    return text.strip()


def scan(text: str) -> list[Entry]:
    """Every assignment in ``text``, in file order, with the lines each occupies.

    Anything that is not an assignment -- comments, blank lines, whatever the user
    left in there -- is simply not returned. It is still in the file, and
    :func:`patch` will not touch it.
    """
    lines = text.splitlines()
    entries: list[Entry] = []

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        match = _ASSIGNMENT.match(line)
        if match is None:
            index += 1
            continue

        key, remainder = match.group(1), match.group(2).lstrip()
        start = index

        if remainder[:1] in ("'", '"'):
            quote = remainder[0]
            # Keep pulling in lines until the quote closes. A value that never
            # closes is malformed -- compose refuses the whole file -- so we stop
            # rather than swallow the rest of it.
            buffer = remainder
            read = _read_quoted(buffer, quote)
            while read is None and index + 1 < len(lines):
                index += 1
                buffer = f"{buffer}\n{lines[index]}"
                read = _read_quoted(buffer, quote)
            if read is None:
                index = start + 1
                continue
            value = read[0]
        else:
            value = _strip_bare(remainder)

        entries.append(Entry(key=key, value=value, start=start, end=index + 1))
        index += 1

    return entries


def values(text: str) -> dict[str, str]:
    """The effective value of every key in ``text``.

    Later assignments win, which is what compose does with a duplicated key.
    """
    return {entry.key: entry.value for entry in scan(text)}


def encode(value: str) -> str:
    """Render ``value`` so that compose reads back exactly these characters.

    See the table in the module docstring. The three cases, in the order they are
    tried: bare when nothing would be eaten, single-quoted when something would be
    (which is literal, and needs no escaping at all), and double-quoted with
    escapes only when the value contains an apostrophe -- the one thing single
    quotes cannot hold.
    """
    if _BARE.match(value):
        return value
    if not value:
        return ""
    if "'" not in value:
        return f"'{value}'"

    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def patch(text: str, updates: dict[str, str]) -> str:
    """Return ``text`` with ``updates`` applied, and everything else left alone.

    An empty value *removes* the key rather than writing ``KEY=``. Compose's
    ``${VAR:-fallback}`` treats unset and empty alike, so the two are equivalent to
    the containers -- and given the choice between the two, the file that does not
    mention the key is the one that reads correctly: clearing a box in the form
    means "I do not want to set this", and the app falls back to whatever the
    compose file says it should do without it.

    Keys already in the file are rewritten in place, keeping their position and
    their neighbouring comments. Keys that are not are appended.
    """
    lines = text.splitlines()
    existing = scan(text)

    # Walk backwards so that removing a span cannot shift the indices of the spans
    # we have not dealt with yet.
    seen: set[str] = set()
    for entry in reversed(existing):
        if entry.key not in updates:
            continue
        value = updates[entry.key]
        if entry.key in seen:
            # A duplicate assignment of a key we have already rewritten further
            # down the file. The last one wins, so this earlier one is dead text;
            # leaving it would show the user a value that is not in effect.
            del lines[entry.start : entry.end]
            continue
        seen.add(entry.key)
        if value == "":
            del lines[entry.start : entry.end]
        else:
            lines[entry.start : entry.end] = [f"{entry.key}={encode(value)}"]

    # Appended plainly, with no blank line to set them off. A separator looks
    # tidier on the first save and is a bug on the second: the keys added last time
    # are ordinary lines now, so a rule like "pad before what we append" adds
    # another gap every time, and a file saved five times has five holes in it.
    lines.extend(
        f"{key}={encode(value)}"
        for key, value in updates.items()
        if key not in seen and value != ""
    )

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def read(path: Path) -> dict[str, str]:
    """Every value in the ``.env`` at ``path``, or ``{}`` if there is not one.

    A missing ``.env`` is the normal state of a stack nobody has configured yet,
    not an error.
    """
    try:
        return values(path.read_text())
    except FileNotFoundError:
        return {}


__all__ = ["Entry", "encode", "patch", "read", "scan", "values"]
