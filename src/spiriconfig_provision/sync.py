"""Git remote management for a provisioning working tree: an `origin` to
push to and pull from, ahead/behind status against it, and exporting HEAD
to a destination that isn't necessarily `origin` at all.

Read off git fresh every time -- `remote get-url`, `rev-list
--left-right --count` -- never tracked separately, the same "no state of
ours" stance every other file in this package takes.

See "The Provisioning page manages several profiles" and "Export has two
destinations behind one action" in ``NOTES-usb-provisioning.md`` for the
design this narrows to one profile's worth of plumbing -- more than one
named profile (`shop-floor-fleet`, `warehouse-b`, ...) isn't built yet;
this module is what a profile's `origin` and `Export` would be built on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from spiriconfig.commands import Command, run

from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import StagedRepo

log = logger.bind(plugin="provision")

#: A URL scheme (`https://`, `ssh://`, ...) or an SCP-style
#: `user@host:path` -- the two shapes `git push`/`git remote` accept that
#: are not a plain filesystem path. Anything else is a path.
_NOT_A_PATH = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://|^[\w.-]+@[\w.-]+:")


def _current_branch(repo: StagedRepo, settings: ProvisionSettings) -> str | None:
    """The checked-out branch's name, even on a brand new repo with no
    commits yet -- `git symbolic-ref`, not `rev-parse --abbrev-ref HEAD`,
    which fails on exactly that unborn-branch case a freshly `git
    init`ed working tree is always in until its first commit."""
    result = run(
        Command(argv=[settings.git_bin, "symbolic-ref", "--short", "HEAD"], cwd=repo.path),
        timeout=settings.command_timeout,
        log=log,
    )
    return result.stdout.strip() if result.ok else None


def origin_url(repo: StagedRepo, settings: ProvisionSettings) -> str | None:
    """`origin`'s configured URL, or `None` if this working tree has none."""
    result = run(
        Command(argv=[settings.git_bin, "remote", "get-url", "origin"], cwd=repo.path),
        timeout=settings.command_timeout,
        log=log,
    )
    return result.stdout.strip() if result.ok else None


def set_origin(repo: StagedRepo, url: str, settings: ProvisionSettings) -> None:
    """Point `origin` at `url`, adding it if this working tree has none yet."""
    verb = "set-url" if origin_url(repo, settings) is not None else "add"
    run(
        Command(argv=[settings.git_bin, "remote", verb, "origin", url], cwd=repo.path),
        timeout=settings.command_timeout,
        log=log,
    ).check()


@dataclass(frozen=True, slots=True)
class SyncStatus:
    """Where `HEAD` stands against `origin`'s matching branch, as of the
    last time anyone fetched from it."""

    ahead: int
    behind: int

    @property
    def clean(self) -> bool:
        return self.ahead == 0 and self.behind == 0


def status(repo: StagedRepo, settings: ProvisionSettings) -> SyncStatus | None:
    """Ahead/behind counts against `origin/<current branch>`, or `None`
    if there's no `origin`, or nothing has ever been fetched from it.

    Deliberately doesn't fetch first -- this reads whatever `origin/...`
    already resolves to locally, the same boundary that leaves reaching
    the network to :func:`pull`, the one function here that actually does.
    A status the operator hasn't pulled since is a status that might be
    stale, which is correct: it's the operator's own decision when to pay
    for a network round trip, not this function's to make for them.
    """
    if origin_url(repo, settings) is None:
        return None
    branch = _current_branch(repo, settings)
    if branch is None:
        return None
    result = run(
        Command(
            argv=[
                settings.git_bin,
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...origin/{branch}",
            ],
            cwd=repo.path,
        ),
        timeout=settings.command_timeout,
        log=log,
    )
    if not result.ok:
        return None
    ahead_s, behind_s = result.stdout.split()
    return SyncStatus(ahead=int(ahead_s), behind=int(behind_s))


def pull(repo: StagedRepo, settings: ProvisionSettings) -> None:
    """Fast-forward this working tree to `origin`'s `HEAD`.

    Fetch and merge as two steps, against `origin/HEAD`, rather than one
    `git pull` -- a working tree opened fresh by
    `open_patch_repo`/`open_baseline_repo` has no upstream configured for
    its current branch, and a plain `git pull origin` refuses to guess
    one. `origin/HEAD` sidesteps that: it names whatever branch `origin`
    itself considers current, populated by the plain `git fetch` below
    regardless of what the local branch happens to be called.

    Never a merge commit: a working tree with commits that diverged from
    `origin` needs a person to look at it, not an automatic reconciliation
    on their behalf, so this refuses (git's own fast-forward-only
    rejection) rather than risk quietly merging two operators' edits.
    """
    run(
        Command(argv=[settings.git_bin, "fetch", "-q", "origin"], cwd=repo.path),
        timeout=settings.fetch_timeout,
        log=log,
    ).check()
    run(
        Command(
            argv=[settings.git_bin, "merge", "--ff-only", "-q", "origin/HEAD"],
            cwd=repo.path,
        ),
        timeout=settings.command_timeout,
        log=log,
    ).check()


def _is_a_path(destination: str) -> bool:
    """True for a plain filesystem path (a drive); false for a URL or an
    SCP-style `user@host:path` -- the one distinction :func:`export_to`
    needs, since only a path destination might not exist as a git repo yet."""
    return not _NOT_A_PATH.match(destination)


def _ensure_bare(destination: Path, settings: ProvisionSettings) -> None:
    """Make `destination` a bare repo if it isn't one already.

    Bare, not a plain `git init`: pushing into a checked-out branch is
    refused by git by default, and rightly so here -- nothing should
    silently rewrite a working tree's files out from under whatever has
    that drive mounted elsewhere. Bare is also exactly the shape
    `apply.fetch` expects to `git fetch` from later, on the receiving
    device -- a USB export and a device's local applied clone speak the
    same git-remote shape on both ends.
    """
    if (destination / "HEAD").is_file() and (destination / "objects").is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(
            f"{destination} exists, is not empty, and is not already a bare git repo"
        )
    run(
        Command(argv=[settings.git_bin, "init", "-q", "--bare", str(destination)]),
        timeout=settings.command_timeout,
        log=log,
    ).check()


def export_to(repo: StagedRepo, destination: str, settings: ProvisionSettings) -> None:
    """Send `HEAD` to `destination` -- a drive path or a URL, the same
    operation either way (see "Export has two destinations behind one
    action" in ``NOTES-usb-provisioning.md``). Does not touch `origin` or
    require one to be configured; `destination` can be anything, a one-off
    export included.
    """
    if _is_a_path(destination):
        _ensure_bare(Path(destination), settings)
    branch = _current_branch(repo, settings)
    if branch is None:
        raise ValueError(f"{repo.path} has no branch checked out (detached HEAD)")
    run(
        Command(
            argv=[settings.git_bin, "push", "-q", destination, f"HEAD:refs/heads/{branch}"],
            cwd=repo.path,
        ),
        timeout=settings.fetch_timeout,
        log=log,
    ).check()


__all__ = [
    "SyncStatus",
    "export_to",
    "origin_url",
    "pull",
    "set_origin",
    "status",
]
