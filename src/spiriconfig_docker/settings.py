"""``x-spiri-settings``: the settings form an app declares for itself.

Not to be confused with :class:`spiriconfig_docker.config.DockerSettings`, which
is how *SpiriConfig* is configured. This is how an *app* says which of its knobs a
human is allowed to turn.

A compose file declares a list of fields under a top-level ``x-spiri-settings``
key, and each field names a ``.env`` variable and the NiceGUI widget that should
edit it::

    x-spiri-settings:
      - env: GRAFANA_PORT
        widget: number
        label: HTTP port
        help: The port the dashboard is served on.
        default: "3000"
        min: 1
        max: 65535

      - env: GRAFANA_ADMIN_PASSWORD
        widget: password
        label: Admin password
        required: true

      - env: GRAFANA_LOG_LEVEL
        widget: select
        label: Log level
        options: [debug, info, warn, error]
        default: info
        advanced: true

    services:
      grafana:
        image: grafana/grafana:11.1.0
        ports:
          - "${GRAFANA_PORT:-3000}:3000"
        environment:
          GF_SECURITY_ADMIN_PASSWORD: "${GRAFANA_ADMIN_PASSWORD:-admin}"

Three things about that are load-bearing.

**``x-`` means compose ignores it.** Extension fields are part of the compose
spec, so a compose file carrying a form is still an ordinary compose file. It
runs, unchanged, on a machine that has never heard of SpiriConfig -- which is the
rule the whole project is built on.

**The form writes a ``.env``, and nothing else.** Not a database of ours, not a
sidecar. ``.env`` is a file compose already reads (see
:mod:`spiriconfig_docker.env`), so the settings page is a *nicer way to edit a
file the user could have edited in vim*, and every value it sets is one they can
read back with ``cat``. There is no state here that outlives the file.

**The widget is named, not inferred.** ``widget: select`` means ``ui.select``.
The alternative -- declaring an abstract type and having us guess a widget from it
-- means an app author who wants a dropdown instead of a text box has to discover
which incantation of ``type:`` and ``enum:`` we happen to map to one. Naming the
widget is shorter, and it is honest about what it will do.

``advanced: true`` is the app author's way of saying *this knob is for a developer*
-- a log level, a debug flag, a tuning parameter nobody should have to read past on
their way to the port number. The field is then only rendered in advanced mode (see
:mod:`spiriconfig.advanced`), which makes a long form short for the person who did
not want it long. It hides the *widget*, and nothing else: the variable is still
read from the ``.env``, still written back on save, and the CLI still lists and sets
it. An app author choosing to declutter a form is not choosing who may configure
their app, and if this ever grew teeth it would be a permission system built out of
somebody's UI preference.

The form does not have to live in the compose file. It can sit in a sidecar --
``spiri-settings.yaml``, beside it -- and then the compose file needs no
``x-spiri-settings`` key, or indeed any edit at all::

    grafana/
    ├── compose.yaml            <- exactly as its author published it
    ├── spiri-settings.yaml     <- the form
    └── .env                    <- the answers

That is for the app store's benefit. An app is very often somebody else's compose
file, copied in as it was published, and every line a maintainer changes in it is a
line they get to re-merge by hand every time upstream moves. A sidecar adds a
settings form while touching nothing, so the app stays a clean copy of the thing it
came from. See :func:`_source` for the three forms and how they are chosen between.

The fallback in the compose file (``${GRAFANA_PORT:-3000}``) is not redundant with
``default:``. ``default:`` is what the *form* offers; the compose fallback is what
the *app* does when nobody has filled the form in. An app that only had the former
would not start until someone had visited a web page, which would be a poor thing
to require of a compose file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from spiriconfig.commands import CommandError, run

from spiriconfig_docker import env
from spiriconfig_docker.stacks import Stack

#: The compose key an app declares its settings form under.
SETTINGS_KEY = "x-spiri-settings"

#: The file the form writes, in the stack's own directory. Compose's own default,
#: not a name of ours -- which is why an app configured through the UI behaves
#: identically under a bare ``docker compose up``.
ENV_FILENAME = ".env"

#: Sidecar names, checked in order, for an app that keeps its settings in their own
#: file rather than in the compose file. See :func:`_source` for why it can.
SIDECAR_FILENAMES = ("spiri-settings.yaml", "spiri-settings.yml")

#: Every widget an app may ask for, and the NiceGUI element each one becomes.
#:
#: Kept here rather than in :mod:`spiriconfig_docker.widgets` so that the CLI and
#: the schema can be checked without importing NiceGUI. The two are held together
#: by a test that fails if a name here has no builder there.
WIDGETS = frozenset(
    {
        "input",
        "password",
        "textarea",
        "number",
        "slider",
        "switch",
        "checkbox",
        "select",
        "radio",
        "toggle",
        "color",
    }
)

#: Widgets that are a choice between fixed options, and so require ``options:``.
CHOICE_WIDGETS = frozenset({"select", "radio", "toggle"})

log = logger.bind(plugin="docker")


class SettingsError(Exception):
    """An app's ``x-spiri-settings`` is malformed, or a value for it is."""


@dataclass(frozen=True, slots=True)
class Field:
    """One setting: a ``.env`` variable, and how to ask a human for it."""

    env: str
    """The ``.env`` variable this field reads and writes."""

    widget: str = "input"
    label: str = ""
    """Shown beside the widget. Defaults to a readable form of :attr:`env`."""

    help: str = ""
    """Shown under the widget. This is where an app author explains themselves."""

    default: str = ""
    """What the form offers when the ``.env`` does not set this variable."""

    options: list[str] = field(default_factory=list)
    """The choices, for :data:`CHOICE_WIDGETS`."""

    min: float | None = None
    max: float | None = None
    step: float | None = None
    required: bool = False
    pattern: str = ""
    """A regular expression the value must match. Checked in the form, and on save."""

    advanced: bool = False
    """Show this field only in advanced mode. Clutter, not secrecy.

    A hidden field is still filled in from the ``.env``, still written back when the
    form is saved, and still ``spiriconfig docker settings``'s business. See the
    module docstring, and :mod:`spiriconfig.advanced`, which says the same thing
    about every other advanced-only element on the page.
    """

    @property
    def title(self) -> str:
        """The label to render: what the author wrote, or a readable fallback.

        ``GRAFANA_ADMIN_PASSWORD`` becomes ``Grafana admin password``, so an author
        who cannot be bothered to write a label still gets something better than a
        shouting environment variable.
        """
        if self.label:
            return self.label
        return self.env.replace("_", " ").capitalize()


def _field_from(raw: Any, index: int, source: str = SETTINGS_KEY) -> Field:
    """Build one :class:`Field`, or say precisely what is wrong with it.

    The error messages are aimed at whoever wrote the declaration, which is usually
    a store author and is occasionally the user. They name the offending field by
    position, because a field with no ``env`` has no other name to give, and they
    name ``source`` -- the compose key or the sidecar's filename -- because with
    three places a form can come from, "which file is this in?" has become a real
    question.
    """
    where = f"{source}[{index}]"

    if not isinstance(raw, dict):
        raise SettingsError(f"{where} must be a mapping, not {type(raw).__name__}")

    unknown = set(raw) - {f.name for f in Field.__dataclass_fields__.values()}
    if unknown:
        known = ", ".join(sorted(f.name for f in Field.__dataclass_fields__.values()))
        raise SettingsError(
            f"{where} has unknown keys: {', '.join(sorted(unknown))}. Known keys: {known}"
        )

    name = raw.get("env")
    if not name or not isinstance(name, str):
        raise SettingsError(f"{where} needs an `env:` naming the variable it sets")

    widget = raw.get("widget", "input")
    if widget not in WIDGETS:
        raise SettingsError(
            f"{where} ({name}) asks for widget {widget!r}, which does not exist. "
            f"Available: {', '.join(sorted(WIDGETS))}"
        )

    options = raw.get("options", [])
    if not isinstance(options, list):
        raise SettingsError(f"{where} ({name}) has `options:` that is not a list")
    if widget in CHOICE_WIDGETS and not options:
        raise SettingsError(
            f"{where} ({name}) is a {widget}, so it needs `options:` to choose between"
        )

    # Everything reaching a .env is a string, so the schema's scalars are coerced
    # here rather than at every use. YAML turning `default: 3000` into an int, or
    # `default: true` into a bool, is the author writing what they meant -- it
    # should not become a type error three modules away.
    return Field(
        env=name,
        widget=widget,
        label=str(raw.get("label", "")),
        help=str(raw.get("help", "")),
        default=_scalar(raw.get("default", "")),
        options=[_scalar(o) for o in options],
        min=raw.get("min"),
        max=raw.get("max"),
        step=raw.get("step"),
        required=bool(raw.get("required", False)),
        pattern=str(raw.get("pattern", "")),
        advanced=bool(raw.get("advanced", False)),
    )


def _scalar(value: Any) -> str:
    """A YAML scalar as the string a ``.env`` will hold.

    ``True`` becomes ``"true"``, not ``"True"``: the value is going into a file
    that shell scripts and containers read, and every other tool in that world
    spells a boolean in lower case.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def declared(compose_file: Path) -> list[Field]:
    """The fields an app declares, or ``[]`` if it declares none.

    Reading the compose file as YAML is fine, and is what
    :meth:`spiriconfig_appstore.stores.App.version` already does. We never write it
    back -- the file the form edits is the ``.env`` beside it, precisely so that a
    settings page can never reformat somebody's compose file or eat its comments.

    Raises :class:`SettingsError` if the declaration is there but wrong, because a
    store author who typoed ``widgit:`` needs to hear about it. An app with no
    settings at all is not an error; it is simply an app with no settings, which is
    most of them.
    """
    raw, source = _source(compose_file)
    if raw is None:
        return []

    if not isinstance(raw, list):
        raise SettingsError(f"{source} must be a list of fields")

    fields = [_field_from(item, index, source) for index, item in enumerate(raw)]

    seen: dict[str, int] = {}
    for index, item in enumerate(fields):
        if item.env in seen:
            raise SettingsError(
                f"{source} sets {item.env} twice, at [{seen[item.env]}] and "
                f"[{index}]. Two widgets writing one variable cannot both be right."
            )
        seen[item.env] = index

    return fields


def _source(compose_file: Path) -> tuple[Any, str]:
    """Find an app's settings, wherever it keeps them. Returns (raw fields, label).

    Three ways to declare them, and the reason there is more than one is the app
    store. An app is often somebody else's compose file, copied in as it was
    published: the more of it you have to edit, the more of every future ``git
    merge`` is your own doing. So the sidecar exists to let a maintainer add a
    settings form *without touching the compose file at all*.

    In precedence order:

    1. ``x-spiri-settings:`` in the compose file, holding the list itself.
    2. ``x-spiri-settings: my-settings.yaml``, naming a file to read it from.
    3. No key at all, and a :data:`SIDECAR_FILENAMES` file sitting beside the
       compose file. Nothing in the compose file, which is the point.

    Declaring settings *twice* is an error rather than a precedence rule. Two
    sources of truth that can disagree is the thing this project exists not to do,
    and a store author who has left an old sidecar next to a new inline block would
    much rather be told than have us silently pick one.
    """
    inline = _from_compose(compose_file)
    sidecar = find_sidecar(compose_file.parent)

    if inline is not None and sidecar is not None:
        raise SettingsError(
            f"{compose_file.name} declares {SETTINGS_KEY} and {sidecar.name} exists "
            f"beside it. Settings must come from one place or the other -- delete "
            f"whichever is stale."
        )

    if isinstance(inline, str):
        return _load_sidecar(_referenced(compose_file.parent, inline)), inline
    if inline is not None:
        return inline, SETTINGS_KEY
    if sidecar is not None:
        return _load_sidecar(sidecar), sidecar.name
    return None, ""


def _from_compose(compose_file: Path) -> Any:
    """The raw ``x-spiri-settings`` value from the compose file, or None if absent.

    Reading the compose file as YAML is fine, and is what
    :meth:`spiriconfig_appstore.stores.App.version` already does. We never write it
    back -- the file the form edits is the ``.env`` beside it, precisely so that a
    settings page can never reformat somebody's compose file or eat its comments.
    """
    try:
        document = yaml.safe_load(compose_file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"could not read {compose_file}: {exc}") from exc

    if not isinstance(document, dict):
        return None
    return document.get(SETTINGS_KEY)


def _referenced(directory: Path, name: str) -> Path:
    """Resolve a sidecar named in the compose file, refusing to leave the app.

    A compose file is not always something the user wrote -- it can arrive from a
    store, over the network -- so ``x-spiri-settings: ../../../etc/shadow`` is a
    line somebody could write, and it must not be a line that makes us read it and
    put it on a web page. Checked the way :func:`spiriconfig_docker.stacks.get`
    checks a stack name: resolve it, and insist the answer is still inside.
    """
    target = (directory / name).resolve()
    if not target.is_relative_to(directory.resolve()):
        raise SettingsError(
            f"{SETTINGS_KEY} points at {name!r}, which is outside the app's "
            f"directory. It must name a file beside the compose file."
        )
    if not target.is_file():
        raise SettingsError(f"{SETTINGS_KEY} points at {name!r}, which does not exist")
    return target


def _load_sidecar(path: Path) -> Any:
    """Read a sidecar's fields.

    Accepts either a bare list of fields, or a mapping with ``x-spiri-settings:`` in
    it -- so a maintainer whose compose file has outgrown its settings block can cut
    the block out and paste it into a file, unchanged, and have it work.
    """
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"could not read {path}: {exc}") from exc

    if isinstance(document, dict):
        if SETTINGS_KEY not in document:
            raise SettingsError(
                f"{path.name} has no {SETTINGS_KEY} in it. A sidecar is either a "
                f"list of fields, or a mapping with {SETTINGS_KEY} holding one."
            )
        return document[SETTINGS_KEY]
    return document


def find_sidecar(directory: Path) -> Path | None:
    """The settings sidecar in ``directory``, or None if there is not one.

    The same shape as :func:`spiriconfig_docker.stacks.find_compose_file`, and for
    the same reason: a fixed list of names, checked in order, so that finding one is
    a question about the filesystem and not about configuration.
    """
    for name in SIDECAR_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class StackSettings:
    """A stack's declared form, bound to the ``.env`` it reads and writes."""

    stack: Stack
    fields: list[Field]

    @property
    def env_file(self) -> Path:
        return self.stack.path / ENV_FILENAME

    def values(self) -> dict[str, str]:
        """What the form should show: the ``.env``'s value, or the field's default.

        Only declared fields. Whatever else the user keeps in their ``.env`` is
        theirs, and none of the form's business -- it is not shown, and
        :meth:`save` will not touch it.
        """
        current = env.read(self.env_file)
        return {f.env: current.get(f.env, f.default) for f in self.fields}

    def read(self) -> str:
        """The ``.env``'s text, or ``""`` if the app has not got one yet.

        A missing ``.env`` is the ordinary state of an app nobody has configured,
        not an error -- and an empty buffer is the honest thing to show for one.
        """
        return self._original() or ""

    def preview(self, values: dict[str, str]) -> str:
        """The exact text :meth:`save` would write. Shown in the UI before it does.

        The same instinct as showing the command line before running it: a settings
        page that edits a file should be willing to say which bytes it is about to
        put in it. *Exact* is meant literally -- the advanced editor is filled with
        this text and will write back whatever it then holds, so a preview that
        differed from the write by so much as a header would be a preview that lied
        the moment somebody edited it.
        """
        return self._rendered(self._checked(values))

    def _original(self) -> str | None:
        """The ``.env`` as it stands, or None if there is not one.

        None and ``""`` are kept apart on purpose: they are different files to put
        back if a save is rejected, and only one of them wants a header.
        """
        try:
            return self.env_file.read_text()
        except FileNotFoundError:
            return None

    def _rendered(self, checked: dict[str, str]) -> str:
        """The file, with ``checked`` patched into it. What a form save writes."""
        original = self._original()
        text = env.patch(original or "", checked)
        if original is None and text:
            text = _HEADER + text
        return text

    def _checked(self, values: dict[str, str]) -> dict[str, str]:
        """Validate ``values`` against the schema, and drop anything undeclared.

        Undeclared keys are dropped rather than rejected: the caller is a form we
        built from the schema, so a key that is not in it is our bug, not the
        user's -- and writing it into their ``.env`` would be the worst way to find
        out. Values are checked here, on the way to the file, rather than only in
        the widgets, so that the CLI gets the same guarantees the web UI does.
        """
        checked: dict[str, str] = {}
        for item in self.fields:
            if item.env not in values:
                continue
            value = values[item.env]

            if item.required and not value:
                raise SettingsError(f"{item.title} ({item.env}) is required")
            if value and item.pattern and not re.search(item.pattern, value):
                raise SettingsError(
                    f"{item.title} ({item.env}) must match {item.pattern!r}, "
                    f"and {value!r} does not"
                )
            if value and item.widget in CHOICE_WIDGETS and value not in item.options:
                raise SettingsError(
                    f"{item.title} ({item.env}) must be one of "
                    f"{', '.join(item.options)}, not {value!r}"
                )
            checked[item.env] = value
        return checked

    def save(self, values: dict[str, str]) -> None:
        """Write the values to the ``.env``, refusing to leave a broken one behind.

        The keys the form declares are patched into the file; everything else in it
        -- the user's comments, their own variables, the order they chose -- comes
        through untouched. See :func:`spiriconfig_docker.env.patch`.
        """
        self.write(self._rendered(self._checked(values)))

    def write(self, text: str) -> None:
        """Write ``text`` to the ``.env`` as given, refusing to leave a broken one.

        The other door into the same file, for the advanced editor: bytes in, bytes
        on disk. No patching, no header, no schema -- a file somebody edited by hand
        is a file they own, and the form's opinion about which keys exist is not one
        it gets to impose on a text editor. What they typed is what compose will
        read, which is the entire reason to offer a text editor at all.

        The check is the same bargain :meth:`spiriconfig_docker.stacks.Stack.write`
        makes for the compose file, and for the same reason: compose has to be able
        to read this file, and a ``.env`` it rejects makes the stack unstartable --
        including by hand, from a shell, which is the escape hatch that must never
        close. So the new file goes down, ``docker compose config`` is asked whether
        it can still read the project, and the original is put back if it says no.
        A rejected save leaves the previous, working file exactly as it was.
        """
        original = self._original()

        self.env_file.write_text(text)

        try:
            result = run(
                self.stack.validate(),
                timeout=self.stack.settings.command_timeout,
                log=log,
            )
        except CommandError as exc:
            self._restore(original)
            raise SettingsError(
                f"could not run docker compose to check the file: {exc}"
            ) from exc

        if not result.ok:
            self._restore(original)
            raise SettingsError(
                f"docker compose rejected the settings, so they were not saved:\n"
                f"{result.stderr.strip()}"
            )
        log.info("wrote {}", self.env_file)

    def _restore(self, original: str | None) -> None:
        """Put the ``.env`` back exactly as it was, including not existing at all."""
        if original is None:
            self.env_file.unlink(missing_ok=True)
        else:
            self.env_file.write_text(original)


#: Written at the top of a ``.env`` we create, and never touched again after that.
#:
#: A file appearing in a user's project directory should say where it came from,
#: and -- more usefully -- that they are allowed to edit it. It goes in only on
#: creation: rewriting a header into a file the user already owns would be exactly
#: the sort of helpful vandalism this project is against.
_HEADER = (
    "# Settings for this app, read by `docker compose` and editable by hand.\n"
    "# SpiriConfig's settings page edits this file; it keeps your comments and\n"
    "# any other variables you put here.\n"
    "\n"
)


def for_stack(stack: Stack) -> StackSettings:
    """The settings form for ``stack``, empty if it declares none.

    Raises :class:`SettingsError` if it declares a malformed one.
    """
    return StackSettings(stack=stack, fields=declared(stack.compose_file))


def has_settings(stack: Stack) -> bool:
    """Whether ``stack`` declares a settings form that we could render.

    Swallows a malformed declaration, deliberately: this is called while drawing a
    list of every stack, and one app with a typo in it must not take the page down
    with it. The error is logged, and it is raised properly by :func:`for_stack`
    when someone actually opens that app's settings -- which is the moment they can
    do something about it.
    """
    try:
        return bool(declared(stack.compose_file))
    except SettingsError as exc:
        log.warning("{} has a broken {}: {}", stack.name, SETTINGS_KEY, exc)
        return False


def get(stack_settings: StackSettings, key: str) -> Field:
    """Look up a declared field by its variable name."""
    for item in stack_settings.fields:
        if item.env == key:
            return item
    known = ", ".join(f.env for f in stack_settings.fields) or "none"
    raise SettingsError(f"{stack_settings.stack.name} has no setting {key!r} (has: {known})")


__all__ = [
    "CHOICE_WIDGETS",
    "ENV_FILENAME",
    "SETTINGS_KEY",
    "SIDECAR_FILENAMES",
    "WIDGETS",
    "Field",
    "SettingsError",
    "StackSettings",
    "declared",
    "find_sidecar",
    "for_stack",
    "get",
    "has_settings",
]
