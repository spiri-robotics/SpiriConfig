"""What the UI looks like: the purple, and the two classes that use it.

There is one idea here, and it is worth stating plainly: **purple means advanced**.
A developer-only control wears a purple ring, the switch that reveals those
controls turns purple when it is on, and the panels showing raw command lines are
tinted purple. Someone who flips the switch once should be able to see, at a
glance and forever after, which parts of a page are the ones they turned on.

The colour is registered with Quasar as a custom brand colour named ``advanced``,
which NiceGUI turns into three things at once: a ``--q-advanced`` CSS variable, a
pair of ``.text-advanced`` / ``.bg-advanced`` utility classes, and a colour name
that any Quasar ``color=`` prop will accept. So the hex below is the only place
this purple is written down -- the switch names it as a prop, and the stylesheet
names it as a variable, and neither hard-codes it.

Both classes below are deliberately dark-mode-safe, because the app runs with
``dark=None`` (follow the operating system). Anything defined as a fixed light
grey -- ``bg-gray-100``, say -- becomes white-on-white the moment the user's
machine is in dark mode, so tints here are *translucent* purple over whatever the
page background happens to be, and the text keeps its inherited colour.
"""

from __future__ import annotations

from loguru import logger
from nicegui import ui

#: The purple. Deep purple A400: vivid enough to read as a deliberate accent on a
#: white page, light enough not to vanish into a dark one.
ADVANCED_COLOR = "#7c4dff"

#: The Quasar brand-colour name :func:`apply` registers ``ADVANCED_COLOR`` under.
#: Pass it to any ``color=`` prop: ``ui.switch(...).props(f"color={ADVANCED}")``.
ADVANCED = "advanced"

#: Marks an element as advanced-only. Applied by :func:`spiriconfig.advanced.mark`
#: to everything inside an ``advanced.only()`` block; you should not need it by hand.
ADVANCED_CLASS = "advanced-only"

#: A block of shell commands, as shown in the plugins' command dialogs.
COMMAND_CLASS = "command-block"

#: ``outline`` rather than ``border``: an outline is painted outside the box and
#: takes up no space, so ringing an element cannot nudge its neighbours around.
#: A border would, and advanced mode is a toggle -- the layout must not move when
#: it is flipped, or turning it on becomes a jolt rather than a reveal.
#:
#: Buttons are excluded, and not as a matter of taste: Quasar's own stylesheet
#: declares ``outline: 0`` on ``.q-btn``, so a ring on a button does not paint.
#: They wear the purple as a Quasar ``color`` instead -- see
#: :func:`spiriconfig.advanced.mark`, which is where the two halves are chosen.
_CSS = f"""
.{ADVANCED_CLASS}:not(.q-btn) {{
    outline: 1px dashed color-mix(in srgb, var(--q-{ADVANCED}) 60%, transparent);
    outline-offset: 3px;
    border-radius: 4px;
}}

.{COMMAND_CLASS} {{
    background-color: color-mix(in srgb, var(--q-{ADVANCED}) 10%, transparent);
    border-radius: 4px;
}}
"""


def apply() -> None:
    """Register the palette and the stylesheet on the page being built.

    Called once per page by the shell's layout, before any plugin renders -- so a
    plugin gets the theme for free and never has to ask for it.
    """
    ui.colors(**{ADVANCED: ADVANCED_COLOR})
    ui.add_css(_CSS)


#: CodeMirror has no "follow the page" theme: an editor wears one of these two, and
#: somebody has to choose. :func:`codemirror_theme` is that somebody.
CODEMIRROR_LIGHT = "basicLight"
CODEMIRROR_DARK = "basicDark"

_PREFERS_DARK_JS = 'window.matchMedia("(prefers-color-scheme: dark)").matches'


async def codemirror_theme() -> str:
    """The CodeMirror theme that matches the page the caller is rendering into.

    This asks the browser, which looks like a lot of ceremony for a colour --
    until you notice that the browser is the only one who knows. The app runs with
    ``dark=None``, which means dark mode is resolved from the operating system's
    preference, on the client, and never travels to the server. So there is
    nothing here to consult: we have to go and ask, and that means a round trip
    and an ``await``.

    A browser that does not answer in time gets the light theme, because that is
    the failure worth having: a light editor on a dark page is ugly, whereas a
    dark editor on a light page is grey-on-white and genuinely hard to read. The
    editor is also how you fix a broken compose file, so it must render *something*
    legible even when the page is misbehaving badly enough to drop the round trip.
    """
    try:
        dark = await ui.run_javascript(_PREFERS_DARK_JS, timeout=1.0)
    except (TimeoutError, RuntimeError):
        logger.warning(
            "the browser did not say whether it is in dark mode; "
            "falling back to the light editor theme"
        )
        return CODEMIRROR_LIGHT
    return CODEMIRROR_DARK if dark else CODEMIRROR_LIGHT
