"""The web UI shell.

The shell owns the layout, the nav, and the index page. It knows nothing about
docker or any other plugin: it asks each discovered plugin to render itself at
``/<name>`` and gets out of the way.
"""

from __future__ import annotations

import secrets

from loguru import logger
from nicegui import app, ui

from spiriconfig import advanced
from spiriconfig.config import Settings
from spiriconfig.plugins import Plugin, discover


def _layout(plugins: list[Plugin], current: str | None = None) -> None:
    """The header and nav, shared by every page."""
    with ui.header().classes("items-center justify-between"):
        with ui.link(target="/").classes("no-underline text-white"):
            ui.label("SpiriConfig").classes("text-xl font-bold")
        with ui.row().classes("items-center gap-2"):
            for plugin in plugins:
                if not plugin.has_page:
                    continue
                button = ui.button(
                    plugin.title,
                    on_click=lambda p=plugin: ui.navigate.to(f"/{p.name}"),
                ).props("flat color=white")
                if plugin.name == current:
                    button.props("outline")

            # Never inside advanced.only(): a toggle you can only see once you
            # have turned it on is a trap with no way back.
            advanced.toggle().props("color=white keep-color")


def _index(plugins: list[Plugin]) -> None:
    """The landing page: one card per plugin."""
    ui.label("Plugins").classes("text-2xl font-bold")

    if not plugins:
        ui.label(
            "No plugins are installed. Install one, and it will appear here."
        ).classes("text-gray-500")
        return

    with ui.column().classes("w-full gap-2"):
        for plugin in plugins:
            with ui.card().classes("w-full"):
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
        storage_secret=_storage_secret(config),
    )
