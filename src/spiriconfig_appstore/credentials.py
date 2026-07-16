"""Credentials for private app stores: host logins, kept in the host's stores.

A store on a private host needs two logins that look like one to the person
adding it: git has to authenticate the ``clone``/``fetch``, and docker has to
authenticate the ``pull`` of the images the compose files reference. They are
genuinely separate credential systems -- ``~/.git-credentials`` and
``~/.docker/config.json`` -- and nothing makes them agree except the thing that
makes the private-Gitea case work at all: **they are both keyed by host**. A
Gitea serves the repository and its OCI registry from one host, so one
user/token written under that host key covers both, and stays in sync because
there is only ever one key.

That the key is a *host*, not a store, is the whole reason this is its own thing
rather than a field on the add-store dialog: one login is shared by every store
on a host, outlives any single store, and is removed on its own schedule. So the
model here is a small set of host logins you manage directly -- add one, list
them, forget one -- and adding or removing a store touches none of them.

We do not invent a SpiriConfig credential store. We write into the two stores the
host already has, keyed by host, and then forget the token. From then on ``git
clone`` and ``docker pull`` authenticate on their own, and the commands we print
stay secret-free -- the token travels on stdin (see
:func:`spiriconfig.commands.run`), never in an argv we would log or show.

The token is stored in cleartext at rest (``~/.git-credentials`` is plaintext;
docker's config.json is base64). That is why the UI asks for a *scoped access
token*, not an account password: on a single-box appliance there is no way to
avoid storing the thing, so the mitigation is that the thing is revocable and
narrow -- and :func:`forget_credentials` is the revoke-locally half of that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from spiriconfig.commands import Command, CommandError, run
from spiriconfig_docker.config import docker_settings

from spiriconfig_appstore.config import AppStoreSettings

log = logger.bind(plugin="appstore")

#: The git credential helper we read and write through. ``store`` keeps its
#: entries in ``~/.git-credentials`` as plaintext, one URL per line -- the same
#: file the user would get from ``git config credential.helper store`` by hand,
#: which is the point: we are not doing anything they could not.
GIT_HELPER = "store"

#: The scheme a bare host login is assumed to speak. Both git-over-https and every
#: OCI registry are https, so a host typed without one means https here.
DEFAULT_SCHEME = "https"


class CredentialError(Exception):
    """A credential operation failed, with a message fit to show."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """The host a store authenticates against, and how git should address it."""

    scheme: str
    """``https``, ``http``, or ``ssh`` -- the protocol git matches a credential on."""

    host: str
    """The bare host, e.g. ``gitea.example.com``. The key both stores share."""


@dataclass(frozen=True, slots=True)
class Login:
    """One stored host login, as read back out of git's credential store."""

    host: str
    username: str
    """Who the credential is for. The token is never read back, only the name."""


def endpoint_for(url: str) -> Endpoint | None:
    """Work out the host a store URL authenticates against, or None.

    Handles the two shapes a git remote comes in: a real URL
    (``https://gitea.example.com/org/store.git``) and the scp-like form
    (``git@gitea.example.com:org/store.git``). Returns None for a local path,
    which is every default in this package and needs no login at all. Used to
    *suggest* the host to log into for a store, never to store one -- storing is
    keyed on a host directly, because the login is the host's, not the store's.
    """
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https", "ssh"} and parts.hostname:
            return Endpoint(scheme=parts.scheme, host=parts.hostname)
        return None
    # scp-like: user@host:path. A colon before any slash is the giveaway; a bare
    # local path (./x, /srv/x) has no user@ and no host, so it falls through.
    if "@" in url and ":" in url:
        userhost, _, _ = url.partition(":")
        _, _, host = userhost.rpartition("@")
        if host:
            return Endpoint(scheme="ssh", host=host)
    return None


def _credential_description(scheme: str, host: str, username: str = "", token: str = "") -> str:
    """The key=value block git's credential protocol reads on stdin.

    Trailing blank line included: it is the terminator git waits for, and leaving
    it off makes ``git credential`` hang on a pipe that never closes.
    """
    lines = [f"protocol={scheme}", f"host={host}"]
    if username:
        lines.append(f"username={username}")
    if token:
        lines.append(f"password={token}")
    return "\n".join(lines) + "\n\n"


def _git(settings: AppStoreSettings, action: str) -> Command:
    """A ``git credential <action>`` fixed to write only to our store file.

    ``-c credential.helper=`` first clears whatever helper the host already had
    configured -- a system keyring, say -- so ``approve`` and ``reject`` touch our
    plaintext store and nothing else; then ``-c credential.helper=store`` names
    the one we mean.
    """
    return Command(
        argv=[
            settings.git_bin,
            "-c", "credential.helper=",
            "-c", f"credential.helper={GIT_HELPER}",
            "credential", action,
        ]
    )


def _git_credentials_files() -> list[Path]:
    """The files the ``store`` helper reads, in the order it reads them.

    ``~/.git-credentials`` first, then the XDG location. We list both because a
    login could sit in either; we only ever *write* the first, which is the
    helper's own default.
    """
    files = [Path.home() / ".git-credentials"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    files.append(base / "git" / "credentials")
    return files


def logins() -> list[Login]:
    """Every host login currently stored, read back from git's credential file.

    Disk is the source of truth here exactly as it is for stores: we keep no list
    of our own, we read the one the ``store`` helper maintains. Each line is a URL
    with the credential embedded (``https://user:token@host``); we surface the
    host and the username and drop the token on the floor -- it is not ours to
    show, and nothing here needs it.

    Best-effort: a login made through a *different* git helper (a system keyring)
    leaves nothing in this file to find, and docker-only logins live in
    config.json, not here. What this lists is what we wrote, which is what there
    is to manage.
    """
    seen: dict[str, Login] = {}
    for path in _git_credentials_files():
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = urlsplit(line)
            if not parts.hostname:
                continue
            # First file wins, matching the helper's own read order.
            seen.setdefault(parts.hostname, Login(host=parts.hostname, username=parts.username or ""))
    return sorted(seen.values(), key=lambda login: login.host)


def _docker_login(host: str, username: str, token: str) -> None:
    """Log docker into the registry, so a later ``compose pull`` authenticates.

    ``--password-stdin`` rather than ``-p``: the argv (``docker login <host> -u
    <user> --password-stdin``) is safe to log and show because it holds no
    secret, and the token arrives on stdin. This writes ``~/.docker/config.json``
    for whatever user SpiriConfig runs as -- which is the user the ``pull`` will
    run as too, so the login lands where the pull will look.
    """
    command = Command(
        argv=[docker_settings().docker_bin, "login", host, "-u", username, "--password-stdin"]
    )
    try:
        run(command, input=token, log=log).check()
    except CommandError as exc:
        raise CredentialError(
            f"could not log docker into {host}: "
            f"{exc.result.stderr.strip() or 'docker login failed'}"
        ) from exc


def _docker_logout(host: str) -> None:
    """Drop the registry login from ``~/.docker/config.json``.

    Idempotent, deliberately: ``docker logout`` of a host you were never logged
    into exits cleanly, which is what "make sure I am logged out" should mean.
    """
    command = Command(argv=[docker_settings().docker_bin, "logout", host])
    try:
        run(command, log=log).check()
    except CommandError as exc:
        raise CredentialError(
            f"could not log docker out of {host}: "
            f"{exc.result.stderr.strip() or 'docker logout failed'}"
        ) from exc


def store_credentials(
    settings: AppStoreSettings,
    host: str,
    username: str,
    token: str,
    *,
    scheme: str = DEFAULT_SCHEME,
) -> None:
    """Save one host login to both git and docker, keyed on the host.

    One user/token, written to both stores under ``host``, so that the single
    combo a private Gitea issues covers the clone and the pull alike. Git
    credential storage only applies to http(s) -- an ssh remote authenticates with
    a key, so there is nothing for the helper to hold -- but the registry is https
    regardless, so docker is logged in either way.

    Raises :class:`CredentialError`, with a message fit to show, if either store
    rejects the credential. If git succeeds but docker fails the git half stands:
    a half-authenticated host still clones, and the user can fix the registry side
    without redoing the part that worked.
    """
    host = host.strip()
    if not host:
        raise CredentialError("a host is required -- e.g. gitea.example.com.")
    if not username or not token:
        raise CredentialError("both a username and a token are required.")

    if scheme in {"http", "https"}:
        command = _git(settings, "approve")
        description = _credential_description(scheme, host, username, token)
        try:
            run(command, input=description, log=log).check()
        except CommandError as exc:
            raise CredentialError(
                f"could not save the git credential for {host}: "
                f"{exc.result.stderr.strip() or 'git credential approve failed'}"
            ) from exc

    _docker_login(host, username, token)


def forget_credentials(
    settings: AppStoreSettings, host: str, *, scheme: str = DEFAULT_SCHEME
) -> None:
    """Remove a host login from both git and docker.

    The revoke-locally half of the token bargain: ``git credential reject`` drops
    the line from ``~/.git-credentials``, ``docker logout`` drops the entry from
    config.json. Keyed by host, so it clears the login for *every* store on that
    host -- which is the honest thing, because there was only ever one login to
    share. A leaked or over-scoped token has an in-tool way out.

    Both are idempotent: forgetting a host you were never logged into is not an
    error, it just does nothing, which is what "make sure I am logged out" should
    mean.
    """
    host = host.strip()
    if not host:
        raise CredentialError("a host is required -- e.g. gitea.example.com.")

    if scheme in {"http", "https"}:
        command = _git(settings, "reject")
        description = _credential_description(scheme, host)
        try:
            run(command, input=description, log=log).check()
        except CommandError as exc:
            raise CredentialError(
                f"could not remove the git credential for {host}: "
                f"{exc.result.stderr.strip() or 'git credential reject failed'}"
            ) from exc

    _docker_logout(host)


__all__ = [
    "CredentialError",
    "DEFAULT_SCHEME",
    "Endpoint",
    "GIT_HELPER",
    "Login",
    "endpoint_for",
    "forget_credentials",
    "logins",
    "store_credentials",
]
