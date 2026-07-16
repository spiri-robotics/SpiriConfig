"""The optional PAM login in front of the web UI.

Off unless ``SPIRICONFIG_AUTH=pam`` (see :class:`~spiriconfig.config.Settings`).
When on, every page redirects to ``/login`` until the browser has authenticated
against the host's PAM stack, exactly as ``login`` or ``sshd`` would.

This is the *login* half of the model :doc:`design </design>` commits to --
"log the user in with PAM, then fork to that unix user". The fork half is not
here yet: once logged in, everyone shares the one process, which runs as whoever
launched it. So this gate answers "is this a person the machine trusts?", not
"what may this particular person do?" -- the second question is the OS's to
answer once the fork exists, and there is no role model of ours in the meantime.

Why the login rule below is shaped the way it is: only root can read
``/etc/shadow``, so only a root process can verify *another* user's password. A
non-root process can verify *its own* user's password and no one else's (PAM's
setuid ``unix_chkpwd`` helper is what lets it do even that). :func:`authenticate`
enforces exactly that boundary rather than pretending to a power the kernel will
not grant it.
"""

from __future__ import annotations

import grp
import os
import pwd
from dataclasses import dataclass

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
    "is_root",
    "login_page",
    "logout",
    "running_user",
]
