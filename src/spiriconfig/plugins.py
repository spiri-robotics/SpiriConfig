"""The plugin interface, and its discovery.

A plugin is a class that subclasses :class:`Plugin` and is registered under the
``spiriconfig.plugins`` entry point group::

    [project.entry-points."spiriconfig.plugins"]
    myplugin = "my_package:MyPlugin"

Installing the distribution makes the plugin available; uninstalling it removes
the plugin. There is no plugin registry to edit and no enable/disable state for
us to keep in sync with reality.

A plugin supplies two faces onto the same functionality:

* :meth:`Plugin.cli` -- a Typer app, mounted as ``spiriconfig <name> ...``
* :meth:`Plugin.page` -- a NiceGUI page body, mounted at ``/<name>``

Both are optional, but a plugin that offers a web page and no CLI is a bug in
spirit: the UI must not be the only way to do something.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from importlib.metadata import EntryPoint, entry_points

import typer
from loguru import logger

ENTRY_POINT_GROUP = "spiriconfig.plugins"


class Plugin(abc.ABC):
    """Base class for SpiriConfig plugins."""

    name: str
    """Short identifier. Used for the CLI subcommand, the URL, and the log field."""

    title: str
    """Human-readable name, shown in the web UI."""

    description: str = ""
    """One-line summary, shown in the CLI help and on the web UI's index."""

    def cli(self) -> typer.Typer | None:
        """Return a Typer app to mount at ``spiriconfig <name>``, or None."""
        return None

    def page(self) -> None:
        """Render this plugin's NiceGUI page body.

        Called inside a page route, so it may use ``ui.*`` freely. It must not
        be the only way to reach the plugin's functionality -- anything doable
        here must also be doable from :meth:`cli` or from the underlying tool.
        """
        raise NotImplementedError(f"plugin {self.name!r} has no web page")

    @property
    def has_page(self) -> bool:
        """Whether this plugin overrides :meth:`page`."""
        return type(self).page is not Plugin.page


def _load(ep: EntryPoint) -> Plugin | None:
    """Load and instantiate one entry point, or return None if it is broken.

    A single bad plugin must not take the whole application down with it, so
    failures here are logged and skipped. The user still gets a working UI, and
    a loud reason why their plugin is missing from it.
    """
    try:
        factory = ep.load()
    except Exception:  # noqa: BLE001 - third-party import, anything can happen
        logger.exception("failed to import plugin {!r} from {}", ep.name, ep.value)
        return None

    try:
        plugin = factory()
    except Exception:  # noqa: BLE001
        logger.exception("failed to instantiate plugin {!r}", ep.name)
        return None

    if not isinstance(plugin, Plugin):
        logger.error(
            "plugin {!r} is not a spiriconfig.plugins.Plugin (got {})",
            ep.name,
            type(plugin).__name__,
        )
        return None

    logger.debug("loaded plugin {!r} from {}", plugin.name, ep.value)
    return plugin


def discover() -> list[Plugin]:
    """Load every installed plugin, sorted by name.

    Broken plugins are logged and skipped rather than raising.
    """
    found: Iterator[EntryPoint] = iter(entry_points(group=ENTRY_POINT_GROUP))
    plugins = [p for ep in found if (p := _load(ep)) is not None]
    return sorted(plugins, key=lambda p: p.name)
