"""A throwaway upstream for the reverse-proxy spike.

Not shipped, not a plugin -- it stands in for the container a real out-of-process
plugin will be, so :mod:`spiriconfig.proxy` has something live to forward to before
label-discovery exists. Run it, point SpiriConfig at it, and click around:

    $ uv run python scripts/demo_target.py            # serves on :9002
    $ SPIRICONFIG_PROXY_DEMO=http://127.0.0.1:9002 uv run spiriconfig serve

Then open the SpiriConfig UI, click "Demo" in the sidebar, and exercise the two
things the spike exists to test (see NOTES-out-of-process-plugins.md):

* **subpath correctness** -- this app has no idea it is behind /plugin/demo. Every
  URL below is root-absolute or relative; they only resolve because NiceGUI reads
  the X-Forwarded-Prefix the proxy injects. If the socket.io upgrade or an asset
  comes back 404, the prefix threading is where to look.
* **history sync** -- the one <script> tag pulls in the shell SDK. Navigate a few
  pages deep, watch the address bar track, then reload and hit Back.

It deliberately mixes navigation styles: ui.navigate.to (goes through the JS that
honours the prefix) and ui.link (writes href straight to the DOM -- the known gap
the notes flag at link.py:29, and the reason ./relative targets are used here).
"""

from __future__ import annotations

from nicegui import ui

# The whole contract a proxied plugin signs up to (point 3 in the notes): one tag,
# served by the shell at a fixed URL, giving deep-link/history sync for free. Absolute
# path on purpose -- it is a shell URL, not a plugin one, so it must not be prefixed.
ui.add_head_html('<script src="/plugin-sdk/shell.js"></script>', shared=True)


def _nav() -> None:
    """A row of links to every page, so any page can reach any other."""
    with ui.row().classes("gap-4 items-center"):
        # Relative targets ('routes', not '/routes') so they resolve under whatever
        # prefix we are served at -- the safe form for a link that must stay inside.
        ui.link("Home", "/")
        ui.link("Routes", "routes")
        ui.link("Devices", "devices")
        ui.link("Deep", "devices/esc/0")


@ui.page("/")
def index() -> None:
    _nav()
    ui.label("Demo upstream").classes("text-2xl font-bold")
    ui.label(
        "A stand-in plugin. It does not know it is proxied; every URL here is "
        "resolved from the X-Forwarded-Prefix the proxy sets."
    )
    # A live counter proves the websocket relay: without socket.io over the proxied
    # websocket, this button does nothing.
    count = {"n": 0}
    label = ui.label("clicked 0 times")
    ui.button(
        "click me",
        on_click=lambda: (
            count.update(n=count["n"] + 1),
            label.set_text(f"clicked {count['n']} times"),
        ),
    )
    ui.button("Go to routes (ui.navigate)", on_click=lambda: ui.navigate.to("/routes"))


@ui.page("/routes")
def routes() -> None:
    _nav()
    ui.label("Routes").classes("text-2xl font-bold")
    for i in range(5):
        ui.label(f"route {i}: 10.0.0.{i}/24")


@ui.page("/devices")
def devices() -> None:
    _nav()
    ui.label("Devices").classes("text-2xl font-bold")
    for name in ("esc", "imu", "gps"):
        ui.link(f"{name} →", f"devices/{name}/0")


@ui.page("/devices/{kind}/{index}")
def device(kind: str, index: str) -> None:
    _nav()
    ui.label(f"{kind} #{index}").classes("text-2xl font-bold")
    ui.label(
        "A page three levels deep. Reload here: the shell should rebuild the frame "
        "at this exact path, not bounce you back to the plugin's root."
    )


ui.run(host="127.0.0.1", port=9002, title="Demo target", show=False, reload=False)
