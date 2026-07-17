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
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from spiriconfig import advanced, auth, theme, tls
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

        # Draws nothing unless someone is logged in, so it is safe with auth off:
        # a session that never authenticated carries no username to show.
        auth.header_account()


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
    but say so, because the consequence is otherwise a confusing little mystery.
    With auth on that consequence has teeth (every login drops on restart), so the
    warning sharpens to match.
    """
    if config.storage_secret:
        return config.storage_secret
    if config.auth != "none":
        logger.warning(
            "no SPIRICONFIG_STORAGE_SECRET set: using a temporary one, so everyone "
            "is logged out whenever this process restarts. Set it to something "
            "secret and stable for a real deployment"
        )
    else:
        logger.warning(
            "no SPIRICONFIG_STORAGE_SECRET set: using a temporary one, so per-person "
            "settings such as advanced mode will reset when this process restarts"
        )
    return secrets.token_urlsafe(32)


def _is_loopback(host: str) -> bool:
    """Whether ``host`` keeps the UI on the box, so no-auth is not exposure."""
    return host in {"localhost", "::1"} or host.startswith("127.")


class HstsMiddleware(BaseHTTPMiddleware):
    """Add ``Strict-Transport-Security`` -- only ever mounted for a validated cert.

    HSTS tells the browser to refuse this origin over anything but HTTPS, and to
    allow *no* click-through on a cert error. That is exactly right for a cert the
    browser can validate, and exactly wrong for a self-signed one: the operator has
    to click through a self-signed cert, and HSTS is precisely the instruction that
    forbids it -- so this is mounted only on the provided-cert path (see
    :func:`spiriconfig.tls.resolve`).
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        return response


def serve(config: Settings, plugins: list[Plugin] | None = None) -> None:
    """Build the UI and block, serving it."""
    found = discover() if plugins is None else plugins
    build(found)

    # Per-process startup, distinct from per-page render: a plugin may want to do
    # work once when the server comes up (the app store fetches its stores here, so
    # its "update available" markers are truthful on the first page load). The base
    # method is a no-op, so this costs nothing for the plugins that do not use it.
    for plugin in found:
        app.on_startup(plugin.on_startup)

    # Decide TLS before anything is mounted: it sets the scheme we log, whether the
    # session cookie may carry Secure, and whether HSTS is safe to send.
    tls_plan = tls.resolve(
        mode=config.tls,
        cert=config.tls_cert,
        key=config.tls_key,
        is_loopback=_is_loopback(config.host),
    )
    tls.ensure_selfsigned(tls_plan, config.host)

    if config.auth == "pam":
        # Order matters only in that both happen before ui.run starts the server:
        # the login route has to exist to be reachable, and the middleware has to
        # be mounted before the first request it is meant to gate.
        auth.login_page(config)
        app.add_middleware(auth.AuthMiddleware)
        # The middleware gates page renders; this gates the WebSocket those pages
        # run over, which no HTTP middleware ever sees. Without it the login is
        # only skin-deep -- see spiriconfig.auth.install_websocket_guard.
        auth.install_websocket_guard()
        logger.info("PAM login enabled (service {!r})", config.auth_service)
        if tls_plan.generate:
            # The whole reason TLS defaults on: with PAM the login sends a real host
            # password, and a self-signed cert keeps a passive sniffer off it. Say
            # what it does not do, so no one mistakes it for MITM protection.
            logger.info(
                "serving a self-signed cert: this encrypts against eavesdroppers "
                "but not an active MITM. For that, set SPIRICONFIG_TLS_CERT/_KEY to "
                "a cert the browser trusts"
            )
        elif not tls_plan.enabled and not _is_loopback(config.host):
            logger.warning(
                "PAM login is sending passwords over plain HTTP on {} (SPIRICONFIG_TLS"
                "=off): anyone on the network can read them. Terminate TLS in a proxy "
                "in front, or drop SPIRICONFIG_TLS=off to serve a self-signed cert",
                config.host,
            )
    elif not _is_loopback(config.host):
        # The one genuinely dangerous default: reachable off-box, no login. Docker
        # socket access is root-equivalent, so this is handing the machine out. TLS
        # does not change this -- an encrypted channel to an unauthenticated admin
        # UI is still an unauthenticated admin UI.
        logger.warning(
            "web UI on {} has no authentication (SPIRICONFIG_AUTH=none) and is "
            "reachable off this host; anyone who can connect has full control. "
            "Set SPIRICONFIG_AUTH=pam",
            config.host,
        )

    # HSTS is added last so it is the outermost middleware, stamping *every*
    # response -- including the redirect to /login that the auth middleware returns
    # before any inner middleware runs. Only the provided-cert path sets hsts.
    if tls_plan.hsts:
        app.add_middleware(HstsMiddleware)

    # Extra kwargs flow through NiceGUI to uvicorn (the ssl_* pair) and to the
    # session cookie (https_only -> the Secure flag, which is only honest to set
    # once the cookie actually travels over TLS).
    run_kwargs: dict[str, object] = {}
    if tls_plan.enabled:
        run_kwargs["ssl_certfile"] = str(tls_plan.certfile)
        run_kwargs["ssl_keyfile"] = str(tls_plan.keyfile)
        run_kwargs["session_middleware_kwargs"] = {"https_only": True}

    app.on_startup(
        lambda: logger.info(
            "web UI on {}://{}:{}", tls_plan.scheme, config.host, config.port
        )
    )
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
        **run_kwargs,
    )
