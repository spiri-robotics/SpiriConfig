"""The device side of provisioning: fetch, fast-forward check, tier-gate,
reconcile.

Step 1 of ``NOTES-usb-provisioning.md``'s phasing -- CLI only, one remote at
a time, no USB/UI. ``off`` and ``trusted`` tiers are real here; ``signed``
is refused with a clear error, because verifying a commit means checking it
against ``trust.d/``, and ``trust.d/`` doesn't exist as a resource type yet
(that's step 2). See :doc:`/provisioning`'s scope note and
``NOTES-usb-provisioning.md``'s "Mechanism: a git remote" for the full
design this narrows.

Nothing here is a permission check dressed up as a plan: :func:`plan` only
computes what *would* happen and never mutates the live system --
:func:`execute` is the one function that does, and it is the caller's
choice whether to call it at all, same as the CLI's own ``--yes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from spiriconfig.commands import Command, run

from spiriconfig_provision import repo
from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import ApplyStep, StagedRepo

log = logger.bind(plugin="provision")

#: `last-applied` lives as a ref, not a file -- atomic, and stored in the
#: same object database as everything it points into, so there's no way
#: for it to be updated and the commit it names not exist.
_LAST_APPLIED_REF = "refs/spiriconfig/last-applied"


class ApplyError(RuntimeError):
    """Refused to apply at all -- the tier forbids it, or the fetch isn't a
    fast-forward of what's already applied. Never raised for a step that
    ran and failed after being accepted; that's a plain `CommandError`
    from :func:`execute`, reported to whichever step it was."""


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """A fetched, accepted commit and the steps that would reconcile the
    live system to it -- computed, nothing run yet. :func:`execute` is
    what runs it."""

    repo: StagedRepo
    """This device's local applied clone, already checked out to `fetched`."""

    fetched: str
    """The commit `repo.path`'s working tree is now checked out to."""

    steps: list[ApplyStep]


def _clone_dir(remote: Literal["patch", "baseline"], settings: ProvisionSettings) -> Path:
    return settings.apply_patch_dir if remote == "patch" else settings.apply_baseline_dir


def open_apply_repo(
    remote: Literal["patch", "baseline"], settings: ProvisionSettings
) -> StagedRepo:
    """Open this device's persistent local clone for `remote`, ``git
    init``ing it if it's new.

    A different tree from `repo.open_patch_repo`/`open_baseline_repo`:
    those are an operator's own authoring working trees, staged from the
    web UI. This is the copy a device fetches into and applies from -- see
    `ProvisionSettings.apply_patch_dir`.
    """
    return repo.open_repo(_clone_dir(remote, settings), remote, settings)


def last_applied(staged: StagedRepo, settings: ProvisionSettings) -> str | None:
    """The commit this remote was last successfully applied at, or `None`
    if it never has been -- the bootstrap case, where any fetched commit
    is accepted since there is nothing yet to be a fast-forward of."""
    result = run(
        Command(argv=[settings.git_bin, "rev-parse", _LAST_APPLIED_REF], cwd=staged.path),
        timeout=settings.command_timeout,
        log=log,
    )
    return result.stdout.strip() if result.ok else None


def _set_last_applied(staged: StagedRepo, commit: str, settings: ProvisionSettings) -> None:
    run(
        Command(
            argv=[settings.git_bin, "update-ref", _LAST_APPLIED_REF, commit],
            cwd=staged.path,
        ),
        timeout=settings.command_timeout,
        log=log,
    ).check()


def fetch(staged: StagedRepo, source: str, ref: str, settings: ProvisionSettings) -> str:
    """``git fetch source ref`` into `staged`'s clone. Returns the fetched
    commit hash.

    The transport is never special here -- `source` is whatever ``git
    fetch`` itself accepts: ``file:///mnt/usb`` for a drive, ``https://``/
    ``ssh://`` for a network remote, a bare local path in tests. Nothing
    downstream of this asks how the bytes arrived, only what the commit DAG
    says -- see "Mechanism: a git remote"'s closing point in
    ``NOTES-usb-provisioning.md``.
    """
    run(
        Command(argv=[settings.git_bin, "fetch", "-q", source, ref], cwd=staged.path),
        timeout=settings.fetch_timeout,
        log=log,
    ).check()
    result = run(
        Command(argv=[settings.git_bin, "rev-parse", "FETCH_HEAD"], cwd=staged.path),
        timeout=settings.command_timeout,
        log=log,
    ).check()
    return result.stdout.strip()


def _is_ancestor(
    staged: StagedRepo, ancestor: str, descendant: str, settings: ProvisionSettings
) -> bool:
    return run(
        Command(
            argv=[settings.git_bin, "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=staged.path,
        ),
        timeout=settings.command_timeout,
        log=log,
    ).ok


def _checkout(staged: StagedRepo, commit: str, settings: ProvisionSettings) -> None:
    run(
        Command(
            argv=[settings.git_bin, "checkout", "-q", "--detach", commit], cwd=staged.path
        ),
        timeout=settings.command_timeout,
        log=log,
    ).check()


def plan(
    remote: Literal["patch", "baseline"],
    source: str,
    settings: ProvisionSettings,
    *,
    ref: str = "HEAD",
) -> ApplyPlan:
    """Fetch `source`, fast-forward- and tier-check it, and compute the
    steps that would reconcile the live system to it.

    Raises `ApplyError` for a tier that forbids applying at all, or a
    fetch that isn't a fast-forward of what's already applied -- both are
    refusals to proceed, checked before anything is run or even checked
    out. An empty `steps` list on the returned plan means the live system
    already matches, which is the common case and not an error, same as
    `provision_apply` itself.
    """
    if settings.tier == "off":
        raise ApplyError("provisioning is disabled (SPIRICONFIG_PROVISION_TIER=off)")

    staged = open_apply_repo(remote, settings)
    fetched = fetch(staged, source, ref, settings)
    baseline_commit = last_applied(staged, settings)

    if baseline_commit is not None and not _is_ancestor(
        staged, baseline_commit, fetched, settings
    ):
        raise ApplyError(
            f"{fetched} is not a fast-forward of the last-applied commit "
            f"{baseline_commit} -- refusing to apply (possible replay or "
            "rewritten history)"
        )

    if settings.tier == "signed":
        raise ApplyError(
            "SPIRICONFIG_PROVISION_TIER=signed needs trust.d/ to verify "
            "commits against, and trust.d/ isn't built yet -- see "
            "docs/provisioning.md's scope note"
        )

    _checkout(staged, fetched, settings)

    steps: list[ApplyStep] = []
    for contributor in repo.contributors():
        if hasattr(contributor, "provision_apply"):
            steps += contributor.provision_apply(StagedRepo(path=staged.path, remote=remote))

    return ApplyPlan(repo=staged, fetched=fetched, steps=steps)


def execute(plan_: ApplyPlan, settings: ProvisionSettings) -> None:
    """Run `plan_`'s steps in order, stopping at the first failure -- same
    discipline the contract promises: `run(...).check()` raises
    `CommandError` on the step that broke, and whatever ran before it
    stays done.

    `last-applied` only advances once every step has succeeded. A partial
    apply leaves the remote at its old baseline, so the next attempt
    recomputes the diff from there rather than marking a half-applied
    commit as done.
    """
    for step in plan_.steps:
        run(step.command, input=step.input, timeout=settings.command_timeout, log=log).check()
    _set_last_applied(plan_.repo, plan_.fetched, settings)


__all__ = [
    "ApplyError",
    "ApplyPlan",
    "execute",
    "fetch",
    "last_applied",
    "open_apply_repo",
    "plan",
]
