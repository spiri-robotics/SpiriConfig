"""The optional PAM login in front of the web UI.

Off unless ``SPIRICONFIG_AUTH=pam`` (see :class:`~spiriconfig.config.Settings`).
When on, every page redirects to ``/login`` until the browser has authenticated
against the host's PAM stack, exactly as ``login`` or ``sshd`` would.

This is only a login gate -- authentication, not authorization (see
:doc:`design </design>`). Once past it everyone shares the one process, which runs
as whoever launched it, so every authenticated user has the same access. It answers
"is this a person the machine trusts?", not "what may this particular person do?";
the second question has no answer here, because per-user permissions are not part
of the model.

Why the login rule below is shaped the way it is: only root can read
``/etc/shadow``, so only a root process can verify *another* user's password. A
non-root process can verify *its own* user's password and no one else's (PAM's
setuid ``unix_chkpwd`` helper is what lets it do even that). :func:`authenticate`
enforces exactly that boundary rather than pretending to a power the kernel will
not grant it.
"""

from __future__ import annotations

import base64
import grp
import http.cookies
import json
import os
import pwd
import urllib.parse
from dataclasses import dataclass

import itsdangerous
from loguru import logger
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from spiriconfig import theme
from spiriconfig.config import Settings

log = logger.bind(component="auth")


def is_root() -> bool:
    """Whether the process can verify any account's password, not just its own.

    A function, not a module constant, so a test can mock it -- and so the answer
    is read now rather than frozen at import.
    """
    return os.geteuid() == 0


def running_user() -> str:
    """The account this process runs as: the only login possible when not root."""
    return pwd.getpwuid(os.geteuid()).pw_name


@dataclass(frozen=True)
class AuthResult:
    """The outcome of one login attempt.

    ``error`` is written for a person to read on the login page, so it names what
    is wrong without leaking whether it was the username or the password that
    failed the PAM check itself -- that distinction is a gift to someone guessing.
    """

    ok: bool
    username: str | None = None
    error: str | None = None


def _in_group(username: str, group: str) -> bool:
    """Whether ``username`` belongs to ``group``, by membership or primary gid.

    A user's primary group does not list them in ``gr_mem``, so checking only the
    member list would miss someone whose *primary* group is the admin one. A group
    that does not exist is a misconfiguration, not a member: logged loudly, because
    the visible symptom is otherwise "nobody can log in" with no reason given.
    """
    try:
        entry = grp.getgrnam(group)
    except KeyError:
        log.error(
            "auth group {!r} does not exist; no one can log in until it does or "
            "SPIRICONFIG_AUTH_GROUP names a group that does",
            group,
        )
        return False
    if username in entry.gr_mem:
        return True
    try:
        return pwd.getpwnam(username).pw_gid == entry.gr_gid
    except KeyError:
        return False


def authenticate(username: str, password: str, config: Settings) -> AuthResult:
    """Decide a login, applying the who-may-I-even-check rule before touching PAM.

    The policy, and the only part of this module with logic worth testing on its
    own. PAM is imported lazily and last: a box where ``auth`` is ``none`` never
    loads libpam at all, and a box where libpam will not load fails one login with
    a clear message instead of crashing the process at import.
    """
    username = username.strip()
    if not username or not password:
        return AuthResult(False, error="Enter a username and a password.")

    if not is_root():
        # We can only verify our own account's password, so no other name could
        # succeed even if we tried it. Say so plainly rather than failing the PAM
        # check for a reason the user cannot act on.
        me = running_user()
        if username != me:
            return AuthResult(
                False,
                error=(
                    f"SpiriConfig is running as {me!r}, and a non-root process can "
                    f"only log in that one account. Log in as {me!r}."
                ),
            )
    elif not _in_group(username, config.auth_group):
        # Root can verify anyone, so without this gate every system account --
        # nobody, service users -- would be an admin login.
        return AuthResult(
            False,
            error=f"{username!r} is not a member of the {config.auth_group!r} group.",
        )

    try:
        import pamela
    except Exception as exc:  # noqa: BLE001 - libpam may be missing/unloadable
        log.error("PAM is unavailable, cannot authenticate: {}", exc)
        return AuthResult(False, error="PAM is unavailable on this host; cannot log in.")

    try:
        pamela.authenticate(username, password, service=config.auth_service)
    except pamela.PAMError as exc:
        # INFO, not WARNING: a failed login is a normal event, and the reason
        # (bad password vs. expired account) belongs in the log, not on the page.
        log.info("PAM rejected {!r} via service {!r}: {}", username, config.auth_service, exc)
        return AuthResult(False, error="Incorrect username or password.")

    log.info("{!r} logged in", username)
    return AuthResult(True, username=username)


# Routes reachable without a session. The login page has to be, or there is no
# way to get one; NiceGUI's own traffic is handled by prefix in the middleware.
unrestricted_page_routes = {"/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    """Send an unauthenticated request to ``/login``, remembering where it meant to go.

    Everything under ``/_nicegui`` is let through unconditionally: it is the
    framework's own assets and websocket, and the login page itself cannot render
    without them. Only whole-page navigations are gated -- which is all that needs
    to be, since a page is where a person actually arrives.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not app.storage.user.get("authenticated", False):
            path = request.url.path
            if path not in unrestricted_page_routes and not path.startswith("/_nicegui"):
                app.storage.user["referrer_path"] = path
                return RedirectResponse("/login")
        return await call_next(request)


# --- WebSocket session guard -------------------------------------------------
#
# AuthMiddleware above is an HTTP middleware, and HTTP middleware never sees the
# WebSocket. NiceGUI runs the whole UI over a socket.io connection (mounted at
# /_nicegui_ws/, which the "/_nicegui" allowance above lets straight through), and
# its own socket handlers authorise every frame by a single check: does this
# client_id name a live server-side client? (nicegui.nicegui._on_handshake,
# _on_event). They never look at the session cookie. So a *page render* is gated,
# but the socket that then drives that page is not -- and a client_id is not a
# secret: it rides in the WebSocket URL's query string, so it lands in proxy
# access logs, Referer headers and page source. Anyone who learns one can attach
# to, and act on, someone else's authenticated session without ever logging in.
#
# This binds the socket back to the session the HTTP gate already checked, in two
# checkpoints that mirror NiceGUI's own two phases:
#
#   attach  (connect, handshake): may this socket bind to this client_id at all?
#           Only if its session cookie is authenticated -- or the target is the
#           login page, whose socket has to work while logged out or no one could
#           ever log in.
#   action  (event, javascript_response, ack, log): a socket may only speak for a
#           client_id whose room it actually joined. This is what stops a logged-
#           out socket that legitimately attached to the login page from turning
#           around and emitting events for a victim's client_id.
#
# The handlers are wrapped, not replaced: each wrapper decides yes/no and delegates
# the real work down to NiceGUI's original. A checkpoint whose original handler has
# gone missing fails closed (deny), which is the safe direction if NiceGUI's socket
# API shifts under us.

#: The socket message handlers gated by room membership. Attach (connect,
#: handshake) is handled separately because it is what *grants* membership.
_GUARDED_SOCKET_EVENTS = ("event", "javascript_response", "ack", "log")

#: Matches Starlette's SessionMiddleware default, which NiceGUI does not override.
_SESSION_MAX_AGE = 14 * 24 * 60 * 60


def _session_id_from_cookie(cookie_header: str, secret: str | None) -> str | None:
    """The session id inside a signed Starlette session cookie, or ``None``.

    Mirrors ``starlette.middleware.sessions.SessionMiddleware`` exactly: a
    ``TimestampSigner`` over the storage secret, wrapping ``base64(JSON)``. Every
    way this can fail -- no cookie, wrong signature, expired, malformed -- collapses
    to ``None``, i.e. "not a session we issued", which every caller reads as "not
    authenticated". Fail closed.
    """
    if not secret:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get("session")
    if morsel is None:
        return None
    signer = itsdangerous.TimestampSigner(str(secret))
    try:
        raw = signer.unsign(morsel.value, max_age=_SESSION_MAX_AGE)
        session = json.loads(base64.b64decode(raw))
    except (itsdangerous.BadSignature, ValueError):
        return None
    session_id = session.get("id")
    return session_id if isinstance(session_id, str) else None


def _session_is_authenticated(environ: dict) -> bool:
    """Whether the socket's session cookie names a logged-in user.

    The ``authenticated`` flag is not in the cookie -- the cookie carries only a
    session id, and the flag lives in NiceGUI's server-side per-user storage keyed
    by that id (see :func:`login_page`, which writes ``app.storage.user``). So this
    verifies the cookie, then reads the flag out of that store by id -- the one
    lookup NiceGUI has no public API for, hence ``_users``.
    """
    from nicegui import core, storage

    session_id = _session_id_from_cookie(
        environ.get("HTTP_COOKIE", ""), storage.Storage.secret
    )
    if session_id is None:
        return False
    user = core.app.storage._users.get(session_id)  # noqa: SLF001 - no read-by-id API
    return bool(user is not None and user.get("authenticated"))


def _targets_unrestricted_page(client_id: str | None) -> bool:
    """Whether ``client_id`` is a client that was rendered for an unrestricted page.

    The login page runs over the socket like every other page, so a logged-out
    visitor's socket must be allowed to reach *its own* login client -- and nothing
    else. Any client whose route is not in :data:`unrestricted_page_routes` belongs
    to a page the HTTP gate would have redirected, so its socket needs a real login.
    """
    from nicegui import Client

    if not client_id:
        return False
    client = Client.instances.get(client_id)
    return client is not None and client.page.path in unrestricted_page_routes


def _may_attach(environ: dict, client_id: str | None) -> bool:
    """The attach rule: a real session, or the login page reaching itself."""
    return _session_is_authenticated(environ) or _targets_unrestricted_page(client_id)


def _query_client_id(environ: dict) -> str | None:
    """The ``client_id`` a connecting socket names in its URL, or ``None``."""
    values = urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).get("client_id")
    return values[0] if values else None


def install_websocket_guard() -> None:
    """Enforce the login gate on the WebSocket, not only on page renders.

    Call once with auth on, after NiceGUI has registered its own socket handlers
    (any time after ``from nicegui import ui``) and before ``ui.run``. With auth
    off there is no session to bind to, so it must not run.
    """
    import socketio
    from nicegui import core

    sio = core.sio
    handlers = sio.handlers.get("/", {})
    original_connect = handlers.get("connect")
    original_handshake = handlers.get("handshake")

    async def guarded_connect(sid: str, environ: dict, auth=None):
        if not _may_attach(environ, _query_client_id(environ)):
            log.warning(
                "refused unauthenticated socket connect from {}",
                environ.get("REMOTE_ADDR", "?"),
            )
            raise socketio.exceptions.ConnectionRefusedError("authentication required")
        if original_connect is None:
            raise socketio.exceptions.ConnectionRefusedError("socket handler unavailable")
        return await original_connect(sid, environ, auth)

    async def guarded_handshake(sid: str, data: dict) -> bool:
        client_id = data.get("client_id") if isinstance(data, dict) else None
        if not _may_attach(sio.get_environ(sid) or {}, client_id):
            log.warning("refused unauthenticated socket handshake for client {}", client_id)
            return False
        return bool(original_handshake) and await original_handshake(sid, data)

    def guard_action(original):
        def guarded(sid: str, msg, *args):
            client_id = msg.get("client_id") if isinstance(msg, dict) else None
            # A socket only joins a room by attaching to that client_id, and attach
            # is gated above; so room membership is proof of an authorised attach.
            if client_id is None or client_id not in sio.rooms(sid):
                log.warning(
                    "dropped socket {!r} for client {} from a socket not attached to it",
                    getattr(original, "__name__", "?"),
                    client_id,
                )
                return None
            return original(sid, msg, *args)

        return guarded

    sio.on("connect", guarded_connect)
    sio.on("handshake", guarded_handshake)
    for event in _GUARDED_SOCKET_EVENTS:
        original = handlers.get(event)
        if original is not None:
            sio.on(event, guard_action(original))


def logout() -> None:
    """Drop the session and return to the login page."""
    username = app.storage.user.get("username")
    app.storage.user.clear()
    if username:
        log.info("{!r} logged out", username)
    ui.navigate.to("/login")


def header_account() -> None:
    """The 'you are X / log out' control for the shared header.

    Renders nothing when no one is authenticated, so the header can call it
    unconditionally: with ``auth`` off no session ever carries a username, so this
    simply draws nothing and the header is unchanged.
    """
    username = app.storage.user.get("username")
    if not username:
        return
    ui.space()
    ui.label(username).classes("text-white text-sm").mark("account-user")
    ui.button(icon="logout", on_click=logout).props(
        "flat round dense color=white"
    ).mark("logout").tooltip("Log out")


def login_page(config: Settings) -> None:
    """Register ``/login``. Called only when :attr:`~spiriconfig.config.Settings.auth` is on."""

    @ui.page("/login")
    def _login() -> None:
        theme.apply()

        # Already in? The middleware lets /login through, so a logged-in visitor
        # would otherwise sit staring at a login form. Send them on.
        if app.storage.user.get("authenticated", False):
            ui.navigate.to("/")
            return

        # Not root -> only running_user() can ever succeed, so name it and lock the
        # field. Making someone guess the one account that works would be a small
        # cruelty the PAM rule lets us avoid.
        fixed_user = None if is_root() else running_user()

        with ui.card().classes("absolute-center w-80 gap-3"):
            ui.label("SpiriConfig").classes("text-xl font-bold")
            username = ui.input("Username", value=fixed_user or "").classes("w-full")
            username.mark("username")
            if fixed_user is not None:
                username.props("readonly")
            password = ui.input(
                "Password", password=True, password_toggle_button=True
            ).classes("w-full")
            password.mark("password")

            def _submit() -> None:
                result = authenticate(username.value, password.value, config)
                if result.ok:
                    app.storage.user.update(authenticated=True, username=result.username)
                    ui.navigate.to(app.storage.user.pop("referrer_path", "/"))
                else:
                    ui.notify(result.error, type="negative")

            password.on("keydown.enter", _submit)
            ui.button("Log in", on_click=_submit).classes("w-full").mark("login")


__all__ = [
    "AuthMiddleware",
    "AuthResult",
    "authenticate",
    "header_account",
    "install_websocket_guard",
    "is_root",
    "login_page",
    "logout",
    "running_user",
]
