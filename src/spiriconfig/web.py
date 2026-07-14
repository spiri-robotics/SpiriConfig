"""The web UI shell.

The shell owns the sidebar, the header, and the index page. It knows nothing
about docker or any other plugin: it asks each discovered plugin to render itself
at ``/<name>`` and gets out of the way.

The sidebar is the shell's only claim on the screen. Everything to the right of
it is the plugin's, in full: the shell puts no heading, no breadcrumb, and no
chrome of its own into the main area.
"""

from __future__ import annotations

import secrets

from loguru import logger
from nicegui import app, ui

from spiriconfig import advanced, theme
from spiriconfig.config import Settings
from spiriconfig.plugins import Plugin, discover


def _nav_item(plugin: Plugin, current: str | None) -> None:
    """One plugin's entry in the sidebar.

    A plugin that declares itself advanced is marked rather than skipped, so it
    appears and disappears with the toggle like everything else the toggle owns --
    and wears the purple while it is there, saying which switch put it there.
    """
    item = ui.item(on_click=lambda: ui.navigate.to(f"/{plugin.name}"))
    item.props("clickable v-ripple")
    if plugin.name == current:
        item.props("active active-class=text-primary")
    with item:
        with ui.item_section().props("avatar"):
            ui.icon(plugin.icon)
        with ui.item_section():
            ui.item_label(plugin.title)
    if plugin.advanced:
        advanced.mark(item)


def _sidebar(plugins: list[Plugin], current: str | None) -> ui.left_drawer:
    """The nav, and the advanced-mode toggle beneath it.

    The toggle sits at the bottom, away from the plugins: it is a property of the
    whole UI rather than of whatever you happen to be looking at.
    """
    drawer = ui.left_drawer(value=True, bordered=True).classes("justify-between p-0")
    drawer.mark("sidebar")
    with drawer:
        with ui.list().props("padding").classes("w-full"):
            for plugin in plugins:
                if plugin.has_page:
                    _nav_item(plugin, current)

        with ui.column().classes("w-full gap-0"):
            ui.separator()
            # Never inside advanced.only(): a toggle you can only see once you
            # have turned it on is a trap with no way back.
            advanced.toggle().classes("p-4")
    return drawer


def _layout(plugins: list[Plugin], current: str | None = None) -> None:
    """The header and sidebar, shared by every page.

    The theme goes on first, before the sidebar and before the plugin: a plugin
    that renders an advanced-only control gets the purple without asking for it.
    """
    theme.apply()
    drawer = _sidebar(plugins, current)

    with ui.header().classes("items-center gap-2"):
        # The sidebar collapses: on a small screen it is most of the window, and
        # on any screen a plugin is sometimes better off with the whole width.
        ui.button(icon="menu", on_click=drawer.toggle).props(
            "flat round dense color=white"
        ).mark("sidebar-toggle").tooltip("Show or hide the sidebar")
        with ui.link(target="/").classes("no-underline text-white"):
            ui.label("SpiriConfig").classes("text-xl font-bold")


def _index(plugins: list[Plugin]) -> None:
    """The landing page: one card per plugin.

    Only reached by clicking the title, since the sidebar takes you straight to a
    plugin. It is the place that admits to a plugin the sidebar cannot show you:
    a CLI-only one, which has no page to link to.
    """
    ui.label("Plugins").classes("text-2xl font-bold")

    if not plugins:
        ui.label(
            "No plugins are installed. Install one, and it will appear here."
        ).classes("text-gray-500")
        return

    with ui.column().classes("w-full gap-2"):
        for plugin in plugins:
            card = ui.card().classes("w-full")
            with card:
                with ui.row().classes("items-center gap-2"):
                    ui.icon(plugin.icon).classes("text-2xl text-gray-600")
                    ui.label(plugin.title).classes("text-lg font-bold")
                if plugin.description:
                    ui.label(plugin.description).classes("text-sm text-gray-500")
                if plugin.has_page:
                    ui.button(
                        "Open",
                        on_click=lambda p=plugin: ui.navigate.to(f"/{p.name}"),
                    ).props("flat")
                else:
                    ui.label("CLI only").classes("text-xs text-gray-400")
            # The index has to agree with the sidebar. An advanced plugin that was
            # hidden from the nav and then listed here anyway would just be a
            # confusing second door into the same room.
            if plugin.advanced:
                advanced.mark(card)


def _register(plugin: Plugin, found: list[Plugin]) -> None:
    """Register one plugin's route.

    The plugin is captured by closure rather than bound as a default argument.
    A page function's signature is a FastAPI route signature, so a `plugin: Plugin
    = plugin` default is read as a *request parameter* and FastAPI tries to build
    a Pydantic schema for it -- which fails at startup, taking the app with it.
    """

    @ui.page(f"/{plugin.name}")
    def _plugin_page() -> None:
        _layout(found, current=plugin.name)
        try:
            plugin.page()
        except Exception as exc:  # noqa: BLE001 - third-party plugin code
            logger.exception("plugin {!r} failed to render", plugin.name)
            with ui.card().classes("w-full bg-red-50"):
                ui.label(f"{plugin.title} failed to render").classes(
                    "text-lg font-bold text-red-900"
                )
                ui.label(str(exc)).classes("font-mono text-sm text-red-800")


def build(plugins: list[Plugin] | None = None) -> None:
    """Register the index and every plugin's page.

    A plugin whose page raises is caught and rendered as an error on its own
    page, rather than being allowed to take down the whole UI.
    """
    found = discover() if plugins is None else plugins

    @ui.page("/")
    def _index_page() -> None:
        _layout(found)
        _index(found)

    for plugin in found:
        if plugin.has_page:
            _register(plugin, found)


def _storage_secret(config: Settings) -> str:
    """The secret signing the cookie that per-person settings are keyed on.

    Generated if unset, so that a first run works with no configuration at all --
    but say so, because the consequence (everyone's advanced-mode setting resets
    on restart) is otherwise a confusing little mystery.
    """
    if config.storage_secret:
        return config.storage_secret
    logger.warning(
        "no SPIRICONFIG_STORAGE_SECRET set: using a temporary one, so per-person "
        "settings such as advanced mode will reset when this process restarts"
    )
    return secrets.token_urlsafe(32)


def serve(config: Settings, plugins: list[Plugin] | None = None) -> None:
    """Build the UI and block, serving it."""
    build(plugins)
    app.on_startup(lambda: logger.info("web UI on http://{}:{}", config.host, config.port))
    ui.run(
        host=config.host,
        port=config.port,
        title="SpiriConfig",
        favicon="🐳",
        show=False,
        reload=False,
        # None, not False: follow the operating system's light/dark setting. Any
        # colour written into a page has to survive both, which is why the theme
        # tints with translucency rather than naming a fixed light grey.
        dark=None,
        storage_secret=_storage_secret(config),
    )
