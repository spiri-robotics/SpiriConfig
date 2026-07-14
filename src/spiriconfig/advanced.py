"""Advanced mode: a filter on what the web UI *shows*.

Advanced mode hides clutter. It is not a permission system, and it must never be
used as one -- a hidden button is still a reachable capability, and the CLI does
everything regardless of what the UI is currently showing. That is deliberate:
the CLI is the escape hatch that makes progressive enhancement true, so gating it
would undercut the whole project. If you ever need "this person may not restart
containers", that is authorisation, and it belongs in front of the *command*, not
in front of the button.

So: advanced mode decides what a page renders. Nothing else.

Plugins use it like this::

    from spiriconfig import advanced

    ui.button("Up", on_click=...)          # everyone sees this

    with advanced.only():
        ui.button("Edit", on_click=...)    # only developers see this

Elements inside :func:`only` are *bound* to the setting rather than conditionally
created, so flipping the toggle shows and hides them instantly, with no page
rebuild and no lost state in whatever the user was doing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from loguru import logger
from nicegui import app, binding, context, ui
from nicegui.element import Element

from spiriconfig import theme
from spiriconfig.config import settings
from spiriconfig.preferences import preferences

#: Key this setting is stored under, in whatever store `preferences()` resolves to.
PREFERENCE_KEY = "advanced"

#: Key for the per-connection state object in NiceGUI's client storage.
_STATE_KEY = "advanced_state"


@binding.bindable_dataclass
class AdvancedState:
    """The live setting for one connected client.

    A bindable dataclass, so that assigning to :attr:`enabled` immediately
    propagates to every element bound to it.
    """

    enabled: bool = False


def state() -> AdvancedState:
    """Return the advanced-mode state for the client being served right now.

    Seeded on first access from the person's stored preference, falling back to
    ``SPIRICONFIG_ADVANCED`` -- so a developer image can ship with advanced mode
    on by default, and a customer image with it off, from the same code.

    The live object is held per connection; the *durable* value lives in the
    preference store, which is the thing that will become per-user.
    """
    client_storage = app.storage.client
    if _STATE_KEY not in client_storage:
        default = settings().advanced
        try:
            stored = preferences().get(PREFERENCE_KEY, default)
        except Exception:  # noqa: BLE001 - a broken store must not break the page
            logger.exception("could not read the advanced-mode preference")
            stored = default
        client_storage[_STATE_KEY] = AdvancedState(enabled=bool(stored))
    return client_storage[_STATE_KEY]


def enabled() -> bool:
    """Whether advanced mode is on for the client being served right now."""
    return state().enabled


def set_enabled(value: bool) -> None:
    """Turn advanced mode on or off, and remember the choice.

    The live state updates first so the UI responds even if persistence fails --
    a preference we could not save is a much smaller problem than a toggle that
    appears not to work.
    """
    state().enabled = value
    try:
        preferences().set(PREFERENCE_KEY, value)
    except Exception:  # noqa: BLE001
        logger.exception("could not save the advanced-mode preference")


def mark(element: Element) -> Element:
    """Show ``element`` only in advanced mode. Returns it, so it chains.

    Also makes it *look* advanced, which is the same act: an element cannot be
    advanced-only and yet fail to say so, because this is the only way to make it
    advanced-only in the first place.

    How it says so depends on what it is. A button takes the purple as its Quasar
    ``color`` prop -- purple lettering on a flat button, a purple face on a solid
    one -- because Quasar's own stylesheet sets ``outline: 0`` on ``.q-btn``, and
    a ring drawn on a button is a fight with the framework that we would be
    re-fighting at every upgrade. Everything else gets the dashed ring.
    """
    element.classes(add=theme.ADVANCED_CLASS)
    if isinstance(element, ui.button):
        element.props(f"color={theme.ADVANCED}")
    return element.bind_visibility_from(state(), "enabled")


@contextmanager
def only() -> Iterator[None]:
    """Show everything created inside this block only in advanced mode.

    Binds the elements that appear in the current slot, rather than wrapping them
    in a container: a wrapper element would sit inside the parent's flex or grid
    layout and quietly change how the visible siblings are arranged.
    """
    slot = context.slot
    before = len(slot.children)
    try:
        yield
    finally:
        for element in slot.children[before:]:
            mark(element)


def toggle() -> ui.switch:
    """A switch for advanced mode. Always visible -- it is the way back.

    Purple when it is on, and grey when it is off, wearing the same colour as the
    ring around everything it reveals: the switch is the legend for the marks.
    """
    switch = ui.switch(
        "Advanced",
        value=enabled(),
        on_change=lambda event: set_enabled(event.value),
    ).props(f"color={theme.ADVANCED}")
    return switch.tooltip("Show developer features: raw commands, file editing")
