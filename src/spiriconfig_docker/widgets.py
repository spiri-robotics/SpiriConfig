"""The widgets a settings form is made of, one per NiceGUI element.

``widget: select`` in a compose file means ``ui.select`` on the page. That is the
whole mapping, and keeping it that direct is a deliberate refusal of the usual
design, in which a schema declares an abstract *type* and the renderer guesses a
widget from it. Guessing is what forces an app author to work out that
``type: string`` plus ``enum:`` is the incantation for a dropdown, and it is what
leaves them stuck when they want a slider instead of a number box for the same
integer. Naming the element is shorter to write, obvious to read, and puts the
author in charge of their own form.

Every widget has to do three things, and they are separate because a ``.env``
holds text and a widget does not:

* **build** the NiceGUI element
* **parse** the text out of the ``.env`` into whatever the element's ``value`` is
* **format** that value back into text for the ``.env``

The round trip is where the bodies are buried. ``ui.number`` hands back a float,
so a port typed as ``3000`` would go into the file as ``3000.0`` and into the
container as a string docker cannot bind. ``ui.switch`` hands back a bool, and
``str(True)`` is ``"True"``, which is not how anything else in a container spells
it. So each widget owns its own conversions, and :func:`values` is the only thing
that reads them back.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nicegui import ui

from spiriconfig import advanced

from spiriconfig_docker.settings import Field

#: Where video capture nodes show up. A module-level constant, rather than the
#: literal inline, so a test can point it at a fake ``/dev`` instead of needing
#: an actual camera.
_DEV_DIR = Path("/dev")

#: Where the kernel names a ``/dev/videoN`` node, if it names it at all.
#:
#: Read rather than run: this is a file the v4l2 driver already writes, so there
#: is no tool to shell out to and nothing to parse out of a command's prose.
_V4L_SYSFS = Path("/sys/class/video4linux")

#: Text a ``.env`` might hold for a boolean widget. Anything else is false.
#:
#: Generous on the way in and strict on the way out: we write ``true``/``false``,
#: but a user who hand-edited their ``.env`` to say ``yes`` meant yes, and a
#: switch that silently showed their ``yes`` as off would be lying to them.
TRUTHY = frozenset({"true", "1", "yes", "on"})

#: NiceGUI marker put on each rendered widget, followed by the variable it sets:
#: ``setting-GRAFANA_PORT``. See :func:`render`.
MARKER_PREFIX = "setting-"

#: NiceGUI marker put on each field's reset button, followed by the variable it
#: resets: ``reset-GRAFANA_PORT``. See :func:`render`.
RESET_PREFIX = "reset-"


def _to_bool(text: str, _: Field) -> bool:
    return text.strip().lower() in TRUTHY


def _from_bool(value: Any) -> str:
    return "true" if value else "false"


def _to_number(text: str, _: Field) -> float | None:
    """Text to a number, or None for an empty box.

    Unparseable text becomes None rather than raising: it means somebody typed
    prose into a ``.env``, and the useful response is an empty box they can fix,
    not a page that will not render.
    """
    try:
        return float(text)
    except ValueError:
        return None


def _from_number(value: Any) -> str:
    """A number back to text, without the ``.0`` that would break a port binding.

    ``ui.number`` always yields a float, so ``3000`` comes back as ``3000.0``.
    Written out verbatim that gives ``ports: "3000.0:3000"``, which docker rejects
    -- so an integral float is written as an integer.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _to_text(text: str, _: Field) -> str:
    return text


def _from_text(value: Any) -> str:
    return "" if value is None else str(value)


def _common(field: Field, element: Any) -> Any:
    """Label and validation that any element can carry.

    The validation is the same rule :meth:`StackSettings._checked` enforces on the
    way to the file, said twice on purpose: here it is a red message under the box
    as you type, and there it is a refusal to write. The form is the courtesy; the
    check before the write is the guarantee, and it is the one that also covers the
    CLI.
    """
    checks: dict[str, Callable[[Any], bool]] = {}
    if field.required:
        checks["Required"] = lambda value: bool(str(value or "").strip())
    if field.pattern:
        pattern = re.compile(field.pattern)
        checks[f"Must match {field.pattern}"] = (
            lambda value: not value or bool(pattern.search(str(value)))
        )
    if checks:
        element.validation = checks
    return element


def _input(field: Field, value: Any) -> Any:
    """A single-line text box."""
    return _common(
        field,
        ui.input(label=field.title, value=value, placeholder=field.default or None),
    )


def _password(field: Field, value: Any) -> Any:
    """A text box with the value masked, and a toggle to reveal it."""
    return _common(
        field,
        ui.input(
            label=field.title,
            value=value,
            password=True,
            password_toggle_button=True,
            placeholder=field.default or None,
        ),
    )


def _textarea(field: Field, value: Any) -> Any:
    """A multi-line text box."""
    return _common(
        field,
        ui.textarea(label=field.title, value=value, placeholder=field.default or None),
    )


def _number(field: Field, value: Any) -> Any:
    """A number box. Respects `min`, `max`, `step`."""
    element = ui.number(
        label=field.title,
        value=value,
        min=field.min,
        max=field.max,
        step=field.step,
    )
    return _common(field, element)


def _slider(field: Field, value: Any) -> Any:
    """A slider, plus the label and the readout it does not come with.

    ``ui.slider`` has no label of its own and shows no number, which is fine in a
    dashboard and useless in a settings form -- a bare rail that says neither what
    it sets nor what it is currently set to. Both are added here, and the readout is
    *bound* to the slider rather than refreshed by a handler, so it tracks the thumb
    as it is dragged.

    A slider with no bounds is a rail with no ends, so a field that forgot to say
    gets 0-100.
    """
    with ui.column().classes("w-full gap-0"):
        with ui.row().classes("w-full items-baseline justify-between"):
            ui.label(field.title).classes("text-sm")
            readout = ui.label().classes("text-sm font-mono text-gray-500")
        element = ui.slider(
            min=field.min if field.min is not None else 0,
            max=field.max if field.max is not None else 100,
            step=field.step if field.step is not None else 1,
            value=value,
        ).classes("w-full")
        readout.bind_text_from(element, "value", backward=_from_number)
    return element


def _switch(field: Field, value: Any) -> Any:
    """An on/off switch."""
    return ui.switch(field.title, value=value)


def _checkbox(field: Field, value: Any) -> Any:
    """A checkbox."""
    return ui.checkbox(field.title, value=value)


def _select(field: Field, value: Any) -> Any:
    """A dropdown, fixed to `options`."""
    return _common(
        field, ui.select(options=field.options, label=field.title, value=value)
    )


def _video_devices() -> dict[str, str]:
    """Every ``/dev/videoN`` on this machine, mapped to a label naming it.

    The label comes from ``/sys/class/video4linux/videoN/name``, which the kernel
    writes for every capture device -- a webcam shows up as something like ``HD
    Pro Webcam C920`` rather than the bare node a human cannot tell apart from a
    second one. A node without that file, or with one that fails to read, still
    gets listed under its own name rather than being dropped -- a form that hides
    a camera because of a sysfs hiccup is worse than one that shows it unnamed.

    Sorted numerically (``video2`` before ``video10``) rather than as text, which
    would put ``video10`` first.
    """
    devices: dict[str, str] = {}
    for path in sorted(
        _DEV_DIR.glob("video*"),
        key=lambda p: int(re.sub(r"\D", "", p.name) or 0),
    ):
        try:
            name = (_V4L_SYSFS / path.name / "name").read_text().strip()
        except OSError:
            name = path.name
        devices[str(path)] = f"{name} ({path})"
    return devices


def _video_device_options(field: Field, value: Any) -> dict[str, str]:
    """Every option the video device dropdown offers, in the order it offers them.

    Three sources, layered: the cameras :func:`_video_devices` finds plugged in
    right now; then ``field.options``, an app author's declared list of URLs
    (an RTSP stream, an IP camera) that no filesystem scan would ever find; then
    the ``.env``'s current value, if it is neither, so a save is never seen to
    have silently forgotten it -- a device unplugged, or a URL an author has
    since dropped from ``options:``, still shows up rather than vanishing.
    """
    devices = _video_devices()
    for url in field.options:
        devices.setdefault(url, url)
    if value and value not in devices:
        devices = {value: f"{value} (not currently present)", **devices}
    return devices


def _video_device(field: Field, value: Any) -> Any:
    """A dropdown of video sources: cameras found on this machine, URLs an app
    author declared in ``options:``, or one typed in by hand.

    ``with_input`` and ``new_value_mode="add-unique"`` are what let the third
    kind exist -- neither a camera nor a declared URL is something every app can
    anticipate, so the box also accepts whatever text is typed into it, the same
    way :func:`_input` would.
    """
    return _common(
        field,
        ui.select(
            options=_video_device_options(field, value),
            label=field.title,
            value=value or None,
            with_input=True,
            new_value_mode="add-unique",
        ),
    )


def _radio(field: Field, value: Any) -> Any:
    """Radio buttons, fixed to `options`."""
    with ui.column().classes("gap-0"):
        ui.label(field.title).classes("text-sm")
        element = ui.radio(options=field.options, value=value).props("inline")
    return element


def _toggle(field: Field, value: Any) -> Any:
    """A button group, fixed to `options`."""
    with ui.column().classes("gap-0"):
        ui.label(field.title).classes("text-sm")
        element = ui.toggle(options=field.options, value=value)
    return element


def _color(field: Field, value: Any) -> Any:
    """A color picker."""
    return _common(field, ui.color_input(label=field.title, value=value))


@dataclass(frozen=True, slots=True)
class Widget:
    """How one ``widget:`` name is built, and how its value crosses the ``.env``."""

    build: Callable[[Field, Any], Any]
    parse: Callable[[str, Field], Any]
    """Text from the ``.env`` -> the element's ``value``."""

    format: Callable[[Any], str]
    """The element's ``value`` -> text for the ``.env``."""


#: Every widget name an app may ask for. The keys are exactly
#: :data:`spiriconfig_docker.settings.WIDGETS` -- a test asserts it, so a widget
#: added to one and forgotten in the other fails the suite rather than the page.
REGISTRY: dict[str, Widget] = {
    "input": Widget(_input, _to_text, _from_text),
    "password": Widget(_password, _to_text, _from_text),
    "textarea": Widget(_textarea, _to_text, _from_text),
    "number": Widget(_number, _to_number, _from_number),
    "slider": Widget(_slider, _to_number, _from_number),
    "switch": Widget(_switch, _to_bool, _from_bool),
    "checkbox": Widget(_checkbox, _to_bool, _from_bool),
    "select": Widget(_select, _to_text, _from_text),
    "radio": Widget(_radio, _to_text, _from_text),
    "toggle": Widget(_toggle, _to_text, _from_text),
    "color": Widget(_color, _to_text, _from_text),
    "video_device": Widget(_video_device, _to_text, _from_text),
}


@dataclass(frozen=True, slots=True)
class Bound:
    """A rendered field, and the element holding its answer."""

    field: Field
    element: Any
    widget: Widget

    @property
    def value(self) -> str:
        """What the user has put in, as the ``.env`` will hold it."""
        return self.widget.format(self.element.value)

    @property
    def default(self) -> Any:
        """The field's declared default, parsed into the element's ``value`` type.

        The same trip the ``.env``'s text takes on the way into the widget, so that
        comparing it against ``element.value`` is comparing like with like -- a
        number widget holds ``8080.0``, and ``field.default`` is the string
        ``"8080"``, and only one of those is what the widget will read back.
        """
        return self.widget.parse(self.field.default, self.field)

    def reset(self) -> None:
        """Put the widget back to the field's declared default.

        Only the widget -- nothing is written. The reset is undone by a Cancel and
        made real by a Save, exactly like typing into the box by hand, because that
        is all it is: :meth:`value` reads the element back whatever put the value
        there.
        """
        self.element.set_value(self.default)


def render(field: Field, value: str) -> Bound:
    """Render one field, showing ``value``, and return a handle on it.

    An ``advanced:`` field is rendered like any other and then marked, so that the
    switch shows and hides it live -- and, crucially, so that it is *built* either
    way. The element exists whatever the switch says; :func:`values` reads it back
    whatever the switch says. Hiding a widget must not drop the variable out of the
    ``.env``, which is what skipping the build would quietly do to anyone who saved
    a form with advanced mode off.

    The whole column is marked rather than the widget alone. A field is a widget
    *and* its help text, and half a field left on the page would be worse than none.

    The widget stays a *direct* child of the column, with the help text and the
    reset button on a row of their own beneath it. That is not just tidy: the mark
    lands one level above the widget, and :func:`spiriconfig.advanced.mark` is on the
    column, so "the field" the switch reveals and "the field" a test reaches for from
    the widget are the same element either way.

    The reset button appears only when the value has drifted from the default (see
    :meth:`Bound.default`), and puts the widget back to it. A reset button on a field
    already at its default is a button that does nothing, and one on every field is
    clutter, so it shows itself when there is something to undo and hides again the
    moment the value is back.
    """
    widget = REGISTRY[field.widget]
    with ui.column().classes("w-full gap-1") as column:
        element = widget.build(field, widget.parse(value, field))
        element.classes("w-full")
        # Named after the variable it sets, which is the only name it is guaranteed
        # to have -- `label:` is optional, and two apps may well both call something
        # "Port". Tests find widgets by it, and so can anything else driving the page.
        element.mark(f"{MARKER_PREFIX}{field.env}")
        bound = Bound(field=field, element=element, widget=widget)

        with ui.row().classes("w-full items-center gap-2"):
            if field.help:
                ui.label(field.help).classes("text-xs text-gray-500")
            # `ml-auto` pins the reset to the right whether or not there is help text
            # to its left -- the alternative, an empty spacer when there is none, is a
            # second way to say the same thing and a second thing to keep in step.
            reset = (
                ui.button(icon="restart_alt", on_click=bound.reset)
                .props("flat dense round size=sm")
                .classes("ml-auto")
                .mark(f"{RESET_PREFIX}{field.env}")
            )
            reset.tooltip(_reset_tooltip(field))

            def sync_reset() -> None:
                reset.set_visibility(element.value != bound.default)

            # Fires on a hand edit and on the reset's own `set_value` alike, so the
            # button that undid the change is the same event that hides it again.
            element.on_value_change(sync_reset)
            sync_reset()
    if field.advanced:
        advanced.mark(column)
    return bound


def _reset_tooltip(field: Field) -> str:
    """The reset button's tooltip, naming the default unless it is a secret.

    Showing which value it restores is the useful thing -- except for a password,
    whose default is the one value not to print onto the page beside its own box.
    """
    if field.default and field.widget != "password":
        return f"Reset to default ({field.default})"
    return "Reset to default"


def form(fields: list[Field], values: dict[str, str]) -> list[Bound]:
    """Render the whole form, in the order the app declared it."""
    return [render(field, values.get(field.env, field.default)) for field in fields]


def values(bound: list[Bound]) -> dict[str, str]:
    """Read every rendered field back, as the text a ``.env`` will hold."""
    return {item.field.env: item.value for item in bound}


__all__ = [
    "MARKER_PREFIX",
    "REGISTRY",
    "RESET_PREFIX",
    "TRUTHY",
    "Bound",
    "Widget",
    "form",
    "render",
    "values",
]
