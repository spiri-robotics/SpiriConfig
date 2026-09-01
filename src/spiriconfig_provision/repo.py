"""Opening and enumerating an operator's named provisioning profiles, and
reading what's pending in one of their remotes.

Nothing here is authoritative: a remote's working tree is the whole state
of its content. Patch and baseline are two entirely separate namespaces of
named profiles -- a patch profile named ``shop-floor-fleet`` and a baseline
profile of the same name are unrelated unless an operator chooses to use
the same name for both; neither implies or requires the other. "Read
reality, never mirror it" is the same stance :mod:`spiriconfig_appstore.installs`
takes with its symlinks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from spiriconfig.commands import Command, run
from spiriconfig.plugins import Plugin, discover

from spiriconfig_provision.config import ProvisionSettings
from spiriconfig_provision.contract import Change, ProvisioningContributor, StagedRepo

log = logger.bind(plugin="provision")

Remote = Literal["patch", "baseline"]


def contributors() -> list[ProvisioningContributor]:
    """Every installed plugin that takes part in provisioning.

    Recognised by `provision_resource` alone -- not a full `isinstance`
    check against :class:`ProvisioningContributor`, since every method on
    that Protocol is individually optional and `isinstance` would demand
    all of them at once. See the Protocol's own docstring.

    A ``provision_resource`` claimed by more than one plugin is a
    discovery-time error: both are dropped, logged, and neither takes part,
    the same "failure is contained" treatment a broken plugin already gets.
    """
    found = [p for p in discover() if hasattr(p, "provision_resource")]
    by_resource: dict[str, list[Plugin]] = {}
    for plugin in found:
        by_resource.setdefault(plugin.provision_resource, []).append(plugin)

    accepted: list[ProvisioningContributor] = []
    for resource, plugins in by_resource.items():
        if len(plugins) > 1:
            log.error(
                "provision_resource {!r} claimed by {} -- skipping all of them",
                resource,
                ", ".join(repr(p.name) for p in plugins),
            )
            continue
        accepted.append(plugins[0])
    return accepted


def open_repo(path: Path, remote: Remote, settings: ProvisionSettings) -> StagedRepo:
    """Open a local working tree at `path`, ``git init``ing it if it's new.

    Low-level: doesn't know about profiles or `settings.profiles_dir`.
    Shared by `create_profile` (an operator's own authoring remotes) and
    :mod:`spiriconfig_provision.apply` (a device's persistent applied
    clones) -- both are just "a local git working tree at some path."
    """
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        run(
            Command(argv=[settings.git_bin, "init", "-q"], cwd=path),
            timeout=settings.command_timeout,
            log=log,
        ).check()
    return StagedRepo(path=path, remote=remote)


@dataclass(frozen=True, slots=True)
class Profile:
    """One of an operator's named profiles for one remote. A patch profile
    and a baseline profile never share state, even when an operator gives
    them the same `name` -- that's a naming convenience for pairing them
    in a UI, not a data relationship."""

    name: str
    repo: StagedRepo


def _profile_dir(name: str, remote: Remote, settings: ProvisionSettings) -> Path:
    return settings.profiles_dir / remote / name


def list_profiles(remote: Remote, settings: ProvisionSettings) -> list[Profile]:
    """Every `remote` profile under `settings.profiles_dir`, sorted by name.

    A subdirectory with no git working tree inside it isn't a profile
    (yet) and is silently skipped.
    """
    root = settings.profiles_dir / remote
    if not root.is_dir():
        return []
    profiles: list[Profile] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / ".git").exists():
            profiles.append(Profile(name=entry.name, repo=StagedRepo(path=entry, remote=remote)))
    return profiles


def create_profile(name: str, remote: Remote, settings: ProvisionSettings) -> Profile:
    """Create a new `remote` profile named `name` under
    `settings.profiles_dir`, `git init`ing its working tree.

    Raises `ValueError` for a name that isn't a safe, plain directory name,
    or one already in use for this `remote`.
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"{name!r} is not a valid profile name")
    path = _profile_dir(name, remote, settings)
    if path.exists():
        raise ValueError(f"a {remote} profile named {name!r} already exists")
    staged = open_repo(path, remote, settings)
    return Profile(name=name, repo=staged)


def open_profile(name: str, remote: Remote, settings: ProvisionSettings) -> Profile:
    """The `remote` profile named `name`. Raises `KeyError` if there isn't one."""
    path = _profile_dir(name, remote, settings)
    if not (path / ".git").exists():
        raise KeyError(name)
    return Profile(name=name, repo=StagedRepo(path=path, remote=remote))


def _changed_paths(repo: StagedRepo, settings: ProvisionSettings) -> list[str]:
    """Every changed path in `repo`, relative to its root, one per file."""
    result = run(
        Command(
            argv=[settings.git_bin, "status", "--porcelain", "--untracked-files=all"],
            cwd=repo.path,
        ),
        timeout=settings.command_timeout,
        log=log,
    ).check()
    return [line[3:] for line in result.stdout.splitlines() if line]


def pending_changes(
    repo: StagedRepo,
    settings: ProvisionSettings,
    contributors_: Iterable[ProvisioningContributor] | None = None,
) -> list[Change]:
    """Describe every pending change in `repo`, via whichever contributor
    owns each changed path's top-level directory.

    A path nobody claims -- no contributor's `provision_resource` matches
    its first segment, or the owning contributor's `provision_describe`
    returns `None` -- falls back to the bare path rather than being hidden.

    A `Change` already in the list is not appended again. Every other
    resource type so far is one committed path per logical change, so
    this never mattered before -- but a bundle under `apps/images/` (see
    `spiriconfig_appstore.images`) touches many blob paths for one real
    event, and a contributor describing that whole subtree as a single
    `Change` (the same "one Change for a tree with no natural per-entry
    verb" move `apps/stores.toml` already makes) would otherwise show up
    once per blob instead of once. `Change` is a frozen dataclass, so
    equal content compares equal regardless of which path produced it.
    """
    by_resource = {c.provision_resource: c for c in (contributors_ or contributors())}
    changes: list[Change] = []
    for path in _changed_paths(repo, settings):
        top = path.split("/", 1)[0]
        owner = by_resource.get(top)
        change = owner.provision_describe(repo, path) if owner is not None else None
        change = change or Change(verb="?", summary=path, detail=path)
        if change not in changes:
            changes.append(change)
    return changes


__all__ = [
    "Profile",
    "Remote",
    "contributors",
    "create_profile",
    "list_profiles",
    "open_profile",
    "open_repo",
    "pending_changes",
]
