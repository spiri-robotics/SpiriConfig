"""Reverse-proxy a container's web UI under a path prefix.

This is the transport for out-of-process plugins (see
``NOTES-out-of-process-plugins.md``): a plugin is a container that serves HTTP on
some port, and we surface it inside the shell by proxying it at a path on *us*
rather than a port on the box. Same origin is the whole point -- cookies, the PAM
gate, and ``window.parent`` all work only because the plugin lives at a path under
the origin the browser already trusts.

A target registered as ``name`` -> ``http://host:port`` is served at
``/plugin/<name>/...``. The upstream sees the request at its own root, with the
sub-path intact and ``X-Forwarded-Prefix: /plugin/<name>`` set, so a framework that
honours that header (NiceGUI does, thoroughly) emits correct URLs with no idea it
is behind us.

Two kinds of traffic cross the proxy:

* **HTTP** -- forwarded with :mod:`httpx`, both bodies streamed so a large download
  or an SSE stream is not buffered whole. This part is boring.
* **WebSocket** -- the only real code. NiceGUI rides socket.io over a websocket, so
  a live UI does not work without it. We accept the browser's upgrade, open our own
  upstream socket, and pump frames both ways until either end closes.

This is not a sandbox. A plugin is trusted code the operator installed; the proxy
forwards its cookies and inherits whatever auth the shell has in front of it. It is
plumbing, not a boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

import httpx
import websockets
from loguru import logger
from nicegui import app
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

log = logger.bind(component="proxy")

#: The path segment a proxied plugin's *upstream content* is served under -- the
#: iframe's src. Raw bytes from the container come back here.
MOUNT = "/plugin"

#: The path segment the *shell page* for a plugin lives under -- what the address bar
#: shows and the sidebar links to. Distinct from :data:`MOUNT` because the two return
#: different things (the shell page frames the upstream in an iframe), and prefixed
#: rather than a bare ``/<name>`` so one parametric route can serve every plugin --
#: discovered or not -- without a top-level catch-all shadowing ``/_nicegui`` and the
#: like. ``shell.js`` maps one prefix to the other. "app", because that is what a
#: proxied plugin is: an app surfaced through us, not a sysadmin tool over a CLI.
APP_PREFIX = "/app"

#: Per-RFC-9110 these headers describe a single connection hop and must not be
#: carried across the proxy. ``content-length`` is dropped from the *request* too
#: (below): we re-stream the body, so httpx must frame it, not us.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

#: Request headers we never forward upstream: the hop-by-hop set, plus ``host``
#: (the upstream's own host is what matters) and ``content-length`` (httpx reframes
#: the streamed body).
_STRIP_REQUEST = _HOP_BY_HOP | {"host", "content-length"}

#: Response headers we drop before relaying: the hop-by-hop set, plus ``date`` and
#: ``server`` -- our own uvicorn stamps those on the way out, so forwarding the
#: upstream's too would send each one twice.
_STRIP_RESPONSE = _HOP_BY_HOP | {"date", "server"}


@dataclass(frozen=True, slots=True)
class Target:
    """One proxied plugin: where it lives and how the shell should name it."""

    name: str
    upstream: str
    title: str
    icon: str = "web"


# Two sources of targets, kept apart so one cannot clobber the other. Discovery
# owns _DISCOVERED wholesale and replaces it every scan; _MANUAL holds anything
# registered by hand (a test, or the dev env stub). A manual target wins a name
# clash -- it was asked for explicitly.
_MANUAL: dict[str, Target] = {}
_DISCOVERED: dict[str, Target] = {}

# One client for every upstream. follow_redirects is off on purpose: a 301/302 from
# the plugin is the *browser's* to follow, at the proxied URL, so we pass it through
# untouched. No read timeout, or an SSE/long-poll response would be cut off mid-stream.
_client = httpx.AsyncClient(
    follow_redirects=False,
    timeout=httpx.Timeout(30.0, read=None),
)


def register_target(
    name: str, upstream: str, *, title: str, icon: str = "web"
) -> Target:
    """Build a :class:`Target`. Does not register it -- discovery collects these."""
    return Target(name=name, upstream=upstream.rstrip("/"), title=title, icon=icon)


def register(name: str, upstream: str, *, title: str, icon: str = "web") -> Target:
    """Register a target by hand, served at ``/plugin/<name>``. For tests and dev."""
    target = register_target(name, upstream, title=title, icon=icon)
    _MANUAL[name] = target
    log.debug("proxy target {!r} -> {} (manual)", name, target.upstream)
    return target


def set_discovered(discovered: list[Target]) -> None:
    """Replace the discovered target set. Called by :mod:`spiriconfig.discovery`."""
    global _DISCOVERED
    new = {t.name: t for t in discovered}
    if new.keys() != _DISCOVERED.keys():
        log.info("plugins: {}", ", ".join(sorted(new)) or "(none)")
    _DISCOVERED = new


def _all() -> dict[str, Target]:
    """The merged view; a manual target shadows a discovered one of the same name."""
    return {**_DISCOVERED, **_MANUAL}


def get(name: str) -> Target | None:
    """The target for ``name``, or None if nothing is registered under it."""
    return _all().get(name)


def targets() -> list[Target]:
    """Every registered target, sorted by name, for the shell to render nav for."""
    return sorted(_all().values(), key=lambda t: t.name)


def _prefix(name: str) -> str:
    """The public path prefix a target is served under (``/plugin/<name>``)."""
    return f"{MOUNT}/{name}"


async def _http(request: Request) -> Response:
    """Forward one HTTP request to its upstream and stream the response back."""
    name = request.path_params["name"]
    target = get(name)
    if target is None:
        return PlainTextResponse(f"no proxy target {name!r}", status_code=404)

    sub = request.path_params.get("path", "")
    url = f"{target.upstream}/{sub}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST
    }
    headers["X-Forwarded-Prefix"] = _prefix(name)
    headers["X-Forwarded-Proto"] = request.url.scheme
    if host := request.headers.get("host"):
        headers["X-Forwarded-Host"] = host

    upstream_req = _client.build_request(
        request.method, url, headers=headers, content=request.stream()
    )
    try:
        upstream_resp = await _client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        log.warning("proxy {!r}: upstream request failed: {}", name, exc)
        return PlainTextResponse(
            f"proxy target {name!r} is unreachable", status_code=502
        )

    # Rebuild the response headers, dropping the hop-by-hop set but keeping the
    # framing (content-length, content-encoding) that matches the raw bytes we relay.
    # Set-Cookie is carried separately so multiple cookies survive -- a dict would
    # collapse them to one.
    out = StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        background=BackgroundTask(upstream_resp.aclose),
    )
    out.raw_headers = [
        (k.encode(), v.encode())
        for k, v in upstream_resp.headers.multi_items()
        if k.lower() not in _STRIP_RESPONSE
    ]
    return out


async def _pump_client_to_upstream(
    ws: WebSocket, upstream: websockets.ClientConnection
) -> None:
    """Relay frames from the browser to the upstream until the browser hangs up."""
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (text := message.get("text")) is not None:
                await upstream.send(text)
            elif (data := message.get("bytes")) is not None:
                await upstream.send(data)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        return


async def _pump_upstream_to_client(
    ws: WebSocket, upstream: websockets.ClientConnection
) -> None:
    """Relay frames from the upstream to the browser until the upstream closes."""
    try:
        async for message in upstream:
            if isinstance(message, str):
                await ws.send_text(message)
            else:
                await ws.send_bytes(message)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        return


async def _ws(ws: WebSocket) -> None:
    """Proxy one websocket: accept the browser, dial the upstream, pump both ways."""
    name = ws.path_params["name"]
    target = get(name)
    if target is None:
        await ws.close(code=1011)
        return

    sub = ws.path_params.get("path", "")
    scheme = "wss" if target.upstream.startswith("https") else "ws"
    authority = target.upstream.split("://", 1)[1]
    url = f"{scheme}://{authority}/{sub}"
    if ws.url.query:
        url = f"{url}?{ws.url.query}"

    # Only the headers that carry application state -- the browser's own upgrade
    # headers (Sec-WebSocket-*, Connection, Upgrade) are the websockets client's to
    # generate for the new hop, not ours to copy.
    extra = {"X-Forwarded-Prefix": _prefix(name), "X-Forwarded-Proto": ws.url.scheme}
    if cookie := ws.headers.get("cookie"):
        extra["cookie"] = cookie

    subprotocols = ws.scope.get("subprotocols") or None
    try:
        upstream = await websockets.connect(
            url, additional_headers=extra, subprotocols=subprotocols, open_timeout=10
        )
    except (OSError, websockets.InvalidHandshake, asyncio.TimeoutError) as exc:
        log.warning("proxy {!r}: upstream websocket failed: {}", name, exc)
        await ws.close(code=1011)
        return

    await ws.accept(subprotocol=upstream.subprotocol)
    async with upstream:
        both = [
            asyncio.create_task(_pump_client_to_upstream(ws, upstream)),
            asyncio.create_task(_pump_upstream_to_client(ws, upstream)),
        ]
        # The first side to finish (either end closing) ends the session; cancel the
        # other pump so it is not left awaiting a socket that will never speak again,
        # and let the cancellation settle before we tear the upstream down.
        _, pending = await asyncio.wait(both, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # Best-effort close of the browser side. The common ending is the browser hanging
    # up first -- a page navigation tears down its socket -- and once it is gone,
    # sending a close frame raises. So only close a client that is still connected,
    # and still guard it: the state can change under us between the check and the send.
    if ws.client_state == WebSocketState.CONNECTED:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await ws.close()


#: Served at ``/plugin-sdk/shell.js``. A proxied plugin includes one ``<script>`` tag
#: pointing here; the script mirrors the plugin's path up to the shell's address bar,
#: so a deep link, a reload, and the back button track where the user actually is.
#: Same origin, so the child reaches ``parent.history`` with no postMessage handshake.
#: replaceState, not pushState, so we ride the plugin's own history rather than
#: double-stacking a second entry for every navigation.
_SHELL_JS = """\
(function () {
  // We run inside the proxied iframe, at <MOUNT>/<name>/<sub>. The shell that frames
  // us is at <APP>/<name>/<sub>: the same tail with the mount prefix swapped for the
  // app prefix. Keep the parent's address bar on that, so a deep link, a reload, and
  // the back button all track where the user actually is.
  var MOUNT = "%MOUNT%", APP = "%APP%";
  function sync() {
    var here = window.location.pathname;
    if (here.indexOf(MOUNT + "/") !== 0) return;
    var shellPath = APP + here.slice(MOUNT.length) +
      window.location.search + window.location.hash;
    try {
      if (window.parent && window.parent !== window) {
        window.parent.history.replaceState(null, "", shellPath);
      }
    } catch (e) {
      // Cross-origin parent (should not happen -- we are same-origin by design).
    }
  }
  sync();
  // Also re-sync when the browser restores this document from the back/forward cache
  // (a history traversal), where the script does not otherwise re-run and the parent
  // address bar would be left pointing at the page we came from.
  window.addEventListener("pageshow", sync);
})();
""".replace("%MOUNT%", MOUNT).replace("%APP%", APP_PREFIX)


async def _shell_js(_request: Request) -> Response:
    return Response(_SHELL_JS, media_type="application/javascript")


_installed = False


def install() -> None:
    """Register the proxy's routes on the NiceGUI app. Idempotent.

    Inserted at the front of the router so ``/plugin/...`` wins ahead of any
    catch-all NiceGUI may hold, and so a websocket upgrade to a proxied path is
    matched by the WebSocketRoute rather than falling through to socket.io.
    """
    global _installed
    if _installed:
        return
    _installed = True

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    routes = [
        Route("/plugin-sdk/shell.js", _shell_js, methods=["GET"]),
        Route(f"{MOUNT}/{{name}}", _http, methods=methods),
        Route(f"{MOUNT}/{{name}}/{{path:path}}", _http, methods=methods),
        WebSocketRoute(f"{MOUNT}/{{name}}", _ws),
        WebSocketRoute(f"{MOUNT}/{{name}}/{{path:path}}", _ws),
    ]
    for route in reversed(routes):
        app.router.routes.insert(0, route)
    log.debug("proxy routes installed under {}", MOUNT)
