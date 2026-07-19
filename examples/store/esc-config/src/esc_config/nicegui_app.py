"""Example: a NiceGUI web app that SpiriConfig discovers and frames as a plugin.

Most small Spiri UIs -- a form to tune a drone's ESCs, a live sensor readout --
are a page or two of NiceGUI. This example is a complete one, wired so that when
its container runs on a machine with SpiriConfig, it appears in SpiriConfig's
sidebar and opens inside the shell with no extra work. That wiring lives in two
places:

- the ``spiriconfig.plugin.*`` labels on this service in ``compose.yaml``, which
  are how SpiriConfig *discovers* the app (it reads ``docker ps`` labels); and
- the ``shell.js`` script tag below, which SpiriConfig serves and which keeps the
  browser's address bar in step as you navigate inside the framed app.

Run standalone (``esc-config nicegui`` or ``docker compose up``) it is
just an ordinary NiceGUI app on its port; the ``shell.js`` request 404s
harmlessly and everything else works. Nothing here depends on SpiriConfig being
present -- it only takes advantage of it when it is.
"""

from __future__ import annotations

from datetime import datetime

from nicegui import ui
from pydantic_settings import BaseSettings, SettingsConfigDict

# The app binds every interface *inside its own container*. That is not the same
# as publishing a port on the host: this service declares no `ports:` in
# compose.yaml, so it is reachable only on the docker network. SpiriConfig runs on
# the host and reaches it there, by the container's own IP -- which is why a plugin
# author never has to pick a host port that might collide. The port below is fixed
# and must match the `spiriconfig.plugin.port` label in compose.yaml.
HOST = "0.0.0.0"  # noqa: S104 -- container-internal bind; no host port is published
PORT = 8080

# Included once, for every page. SpiriConfig serves this script at
# /plugin-sdk/shell.js when it frames the app; standalone it simply 404s.
ui.add_head_html('<script src="/plugin-sdk/shell.js"></script>', shared=True)


class Settings(BaseSettings):
    """Runtime configuration for the web app, read from the environment.

    Every field here is one env var, and every env var this example reads has a
    field here -- the same list that appears under this service's block in
    ``x-spiri-settings``. Keep the two in step and SpiriConfig's form and the app
    agree on what is configurable.
    """

    # env_prefix means the env var for ``greeting`` is ``ESC_CONFIG_GREETING``,
    # matching the names declared in compose.yaml.
    model_config = SettingsConfigDict(env_prefix="ESC_CONFIG_")

    greeting: str = "Hello from ESC Config"


def build(settings: Settings) -> None:
    """Register the app's pages. Split out from :func:`run` so tests can call it."""

    @ui.page("/")
    def index() -> None:
        with ui.column().classes("absolute-center items-center gap-4"):
            # No hard-coded ink colour: the label inherits the theme's text colour,
            # which flips with dark mode, so it stays legible on either background.
            ui.label(settings.greeting).classes("text-3xl font-bold")
            # A live element proves the websocket works -- including through
            # SpiriConfig's proxy, which relays it end to end. The clock ticks
            # from the server, not from browser-side JavaScript. `text-grey` is a
            # Quasar colour that reads on both themes (Tailwind's `text-gray-500`
            # would be a fixed mid-grey; fine here, but it does not adapt).
            clock = ui.label().classes("text-lg text-grey")
            ui.timer(1.0, lambda: clock.set_text(datetime.now().strftime("%H:%M:%S")))


def run(settings: Settings | None = None) -> None:
    """Serve the app until the process is stopped."""
    settings = settings or Settings()
    build(settings)
    # dark=True to match SpiriConfig's dark shell, which is where a plugin is
    # usually seen -- otherwise NiceGUI's default light theme paints dark text on
    # SpiriConfig's dark frame and the app looks broken. Standalone it is simply a
    # dark page. reload=False: in the container, compose.dev.yaml restarts the
    # process on a file change instead, so the two reload mechanisms do not fight.
    ui.run(
        host=HOST, port=PORT, title="ESC Config",
        dark=True, reload=False, show=False,
    )
