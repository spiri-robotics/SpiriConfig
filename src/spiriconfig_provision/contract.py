"""The provisioning plugin contribution interface.

A plugin that owns a resource type (``spiriconfig_users`` for ``users/``,
``spiriconfig_appstore`` for ``apps/``, and so on) implements
:class:`ProvisioningContributor` on its :class:`~spiriconfig.plugins.Plugin`
subclass to take part in provisioning. ``spiriconfig_provision`` never parses
or generates a resource type's files itself -- it renders what a contributor
gives it, and reads what git already knows.

See :doc:`/provisioning` for the full design and rationale; this module is
just the interface it describes, kept small and dependency-light on purpose
so a plugin that does not care about provisioning never has to import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol

from spiriconfig.commands import Command


@dataclass(frozen=True, slots=True)
class StagedRepo:
    """The open repo a provisioning block reads from and writes into."""

    path: Path
    """Working tree root. A contributor writes under
    ``path / provision_resource / ...`` and reads whatever it needs to from
    the same tree -- nothing else is passed in, because nothing else exists:
    the working tree *is* the state, same as everywhere in this codebase."""

    remote: Literal["patch", "baseline"]
    """Which remote this call is for. For the render hooks it is redundant
    with which one was called -- carried here purely so a contributor whose
    two hooks share code doesn't have to infer it from the call site. For
    :meth:`ProvisioningContributor.provision_apply` it is not redundant at
    all: it is the only thing that says whether to expect ``add/``/``remove/``
    or ``state/`` on disk."""


@dataclass(frozen=True, slots=True)
class Change:
    """One contributor's description of one changed path."""

    verb: str
    """Short, plugin-chosen label -- typically ``"add"``, ``"remove"``, or
    ``"state"``, matching the directory the path was under, but not
    enforced: a store registration under ``apps/stores.toml`` is a change
    with no natural verb among those three, and can say whatever reads best.
    Used only for grouping and choosing an icon; never parsed."""

    summary: str
    """A short phrase for the suggested commit message -- e.g.
    ``"install whoami"``, ``"remove alice"``. Lower case, no trailing
    punctuation: ``spiriconfig_provision`` joins these with commas to build
    a subject line, it does not reword them."""

    detail: str
    """A full sentence for the Review & Sign list -- e.g. ``"Installs
    whoami from spiri-apps"``, ``"Removes account alice"``. Stands alone;
    the reader has not necessarily seen ``summary``."""


@dataclass(frozen=True, slots=True)
class ApplyStep:
    """One command in a :meth:`ProvisioningContributor.provision_apply` plan."""

    command: Command

    input: str | None = None
    """Passed straight through as :func:`~spiriconfig.commands.run`'s own
    ``input=``, for a command that reads from stdin -- ``chpasswd -e`` is
    the case this exists for: the hash sitting in a committed
    ``password.hash`` file has to reach it somehow, and ``Command.argv`` is
    rendered into a copy-pasteable shell line, so it can never hold a
    secret. Same discipline :func:`spiriconfig_users.users.set_password`
    already follows for a typed password; this is that same shape, carried
    across the interface."""


class ProvisioningContributor(Protocol):
    """Interface a :class:`~spiriconfig.plugins.Plugin` also implements to
    take part in provisioning. For static type-checking only -- see
    :func:`spiriconfig_provision.repo.contributors` for how a contributor
    is actually recognised at runtime.

    Only `provision_resource` is truly required; every method below is
    individually optional -- see each one's docstring for what its absence
    means -- because ``spiriconfig_provision`` checks with ``hasattr``
    before calling any of them, the same tolerant style
    ``Plugin.cli``/``Plugin.page`` already use. That per-method optionality
    is exactly why this Protocol is **not** ``@runtime_checkable``: an
    ``isinstance`` check against it would require every member below to be
    present, defeating the point. Discovery instead checks for
    `provision_resource` alone, the one thing every contributor sets.
    """

    provision_resource: ClassVar[str]
    """Top-level repo directory this plugin owns -- e.g. ``"users"``,
    ``"apps"``. Exactly one plugin may claim a given name; a name claimed
    twice is a discovery-time error, logged and both skipped, the same as
    any other broken plugin."""

    def provision_patch_block(self, repo: StagedRepo) -> None:
        """Render the add/remove staging block for `repo`, inside the
        *patch* remote's editor on the Provisioning page.

        Called with NiceGUI's ``ui.*`` available, same as
        :meth:`~spiriconfig.plugins.Plugin.page`. This method *is* the
        entire write path: it writes files directly under
        ``repo.path / provision_resource / "add"`` or ``.../"remove"``
        itself, the moment the operator stages something -- there is no
        callback into ``spiriconfig_provision`` to hand a change to.
        Additive only: touches only the entries the operator adds or
        removes this session, never resyncs the directory wholesale.

        Omit this method entirely for a resource type with no everyday
        add/remove flow -- ``spiriconfig_provision`` skips a contributor
        that doesn't define it, the same as a `Plugin` that never overrides
        `page`.
        """

    def provision_baseline_block(self, repo: StagedRepo) -> None:
        """Render the exact-set editor for `repo`, inside the *baseline*
        remote's editor.

        Writes under ``repo.path / provision_resource / "state"``. Unlike
        :meth:`provision_patch_block`, this one *does* resync that subtree
        wholesale on every save -- deleting entries no longer in the
        declared list, not just adding new ones -- because ``state/`` is
        not relative to anything: it is an absolute declaration, and the
        directory on disk must always equal exactly what the list shows,
        never a diff against what used to be there.

        Optional, independently of :meth:`provision_patch_block`: a
        resource type with no sane notion of "the exact set" -- one-shot
        scripts, say -- simply doesn't define it, and
        ``spiriconfig_provision`` leaves it out of the baseline editor
        entirely rather than showing an empty section.
        """

    def provision_describe(self, repo: StagedRepo, path: str) -> Change | None:
        """Describe one changed path under `provision_resource`, relative
        to `repo.path` -- e.g. ``"users/add/operator/password.hash"``.

        Called once per changed path ``git status`` reports under this
        plugin's directory, to build the Review & Sign list and the
        suggested commit message. May read ``repo.path / path`` (or
        sibling files) to build a better description -- the settings
        dialog reading back an app's declared fields to summarise what
        changed is the expected use -- but must not shell out or touch
        anything outside `repo.path`: this describes a path, it does not
        investigate a live system.

        Return `None` for a path this plugin doesn't recognise.
        ``spiriconfig_provision`` falls back to the bare path for a change
        nobody claims, rather than hiding it -- an unrecognised file in a
        signed commit is exactly the kind of thing Review & Sign exists to
        surface, not swallow.
        """

    def provision_apply(self, repo: StagedRepo) -> list[ApplyStep]:
        """Return the steps that reconcile the live system to what
        `repo`'s `provision_resource` directory declares.

        Called with a remote already fetched, verified, and
        fast-forwarded -- by the time this runs, whether to trust and
        apply `repo` at all is a decided question, not this method's to
        re-ask. It only reads ``repo.path / provision_resource`` (and the
        live system, the same way
        :meth:`~spiriconfig.plugins.Plugin.page` already does --
        ``getent passwd``, ``docker ps``, whatever this plugin normally
        reads) and returns steps rather than running them, same as every
        other action in this codebase: the caller decides how to run each
        one (``run(step.command, input=step.input)``), in the order
        returned, and stops the whole apply on the first failure, the same
        as a human working down a list by hand would.

        `repo.remote` tells this method what shape to expect on disk --
        ``add/``/``remove/`` for ``"patch"``, ``state/`` for
        ``"baseline"`` -- so one method handles both; there's no separate
        hook per verb. An empty list means nothing under this plugin's
        directory needs changing, which is the common case and not an
        error.
        """


__all__ = ["ApplyStep", "Change", "ProvisioningContributor", "StagedRepo"]
