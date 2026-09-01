"""Review & Sign: turning what's pending in a working tree into a commit.

"Sign" means whatever ``git commit -S`` already means on this machine --
this module never manages a key of its own, the same "shell out to the
tool a human would use" discipline everything else in this codebase
follows. If ``user.signingkey`` is set in git config (repo-local or
global -- whichever ``git config`` itself would resolve), the commit is
signed with it; if not, it's committed anyway, plainly unsigned, and the
caller is told which happened so the UI can say so honestly rather than
implying every commit is signed.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from spiriconfig.commands import Command, run

from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import Change, StagedRepo

log = logger.bind(plugin="provision")


def suggested_message(changes: list[Change]) -> str:
    """A full commit message built from `changes`: a subject line of
    `summary`s joined with commas, then a blank line, then one bullet per
    change's `detail` -- the ordinary git shape of a short subject and a
    longer body, so the body is what makes this a real audit log rather
    than just a change list restated in Review & Sign. A contributor's
    `detail` is free to say more than its `summary` does -- an installed
    app's `detail` names the settings it was given (see
    `spiriconfig_appstore.provision._describe_settings`), a password
    field's value is never one of them -- and `spiriconfig_provision`
    never rewords either, see `Change.summary`/`Change.detail`'s own
    docstrings for why.

    `changes=[]` returns `""`, not a lone blank line: an empty repo has
    nothing to say, and the caller (a commit-message textarea, or the CLI
    printing what it's about to commit) should see an empty field, not
    two newlines.
    """
    if not changes:
        return ""
    subject = ", ".join(change.summary for change in changes)
    body = "\n".join(f"- {change.detail}" for change in changes)
    return f"{subject}\n\n{body}"


def signing_key(repo: StagedRepo, settings: ProvisionSettings) -> str | None:
    """Whatever ``git config user.signingkey`` resolves to inside `repo`
    -- repo-local config wins over global, the normal git resolution
    order, run from `repo.path` so a repo-specific key is honoured."""
    result = run(
        Command(argv=[settings.git_bin, "config", "--get", "user.signingkey"], cwd=repo.path),
        timeout=settings.command_timeout,
        log=log,
    )
    if not result.ok:
        return None
    return result.stdout.strip() or None


def add_command(repo: StagedRepo, settings: ProvisionSettings) -> Command:
    """``git add -A`` -- the staging half of :func:`commit`, broken out
    so a caller that wants to *show* the exact command line before
    running it (the web Review & Sign dialog) builds the identical
    `Command` rather than a copy of it that could drift out of sync with
    what :func:`commit` actually runs. See ``docs/design.md``'s "We shell
    out, on purpose" -- a command nobody can see run is exactly the thing
    this project exists not to do.
    """
    return Command(argv=[settings.git_bin, "add", "-A"], cwd=repo.path)


def commit_command(
    repo: StagedRepo, message: str, key: str | None, settings: ProvisionSettings
) -> Command:
    """The ``git commit`` command :func:`commit` runs, given whatever
    :func:`signing_key` already resolved -- taken as a parameter rather
    than resolved again here so a caller showing this command line before
    running it, and `commit` itself, agree on whether `-S` is in it
    without querying git config twice."""
    argv = [settings.git_bin, "commit", "-q", "-m", message]
    if key:
        argv.append("-S")
    return Command(argv=argv, cwd=repo.path)


@dataclass(frozen=True, slots=True)
class CommitResult:
    """What actually happened -- not just "it committed," but whether it
    was signed and with which key, so the UI can report the true thing
    rather than assume every commit here is signed."""

    commit: str
    signed: bool
    signing_key: str | None


def commit(repo: StagedRepo, message: str, settings: ProvisionSettings) -> CommitResult:
    """Stage everything pending in `repo` and commit it with `message`.

    Unsigned when no key is configured, not refused: an unsigned commit
    still means something on a `trusted`-tier device (see
    ``ProvisionSettings.tier``), and this function's job is to say
    plainly which one just happened, not to gate on it -- that gate lives
    at apply time, in the `signed` tier, not here.
    """
    run(add_command(repo, settings), timeout=settings.command_timeout, log=log).check()

    key = signing_key(repo, settings)
    run(
        commit_command(repo, message, key, settings),
        timeout=settings.command_timeout,
        log=log,
    ).check()

    head = run(
        Command(argv=[settings.git_bin, "rev-parse", "HEAD"], cwd=repo.path),
        timeout=settings.command_timeout,
        log=log,
    ).check()
    return CommitResult(commit=head.stdout.strip(), signed=bool(key), signing_key=key)


__all__ = [
    "CommitResult",
    "add_command",
    "commit",
    "commit_command",
    "signing_key",
    "suggested_message",
]
