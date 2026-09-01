# Provisioning: the plugin contribution interface

Provisioning lets an operator build a signed git repo -- on a USB drive, or an
ordinary remote -- that resets passwords, delivers apps, or certifies a
device's exact state. The design rationale and the trust model live in
`NOTES-usb-provisioning.md` at the repo root while this is still being built;
read that first if the *why* here is unclear. The on-disk repo layout itself
-- the directory tree, file formats, and which plugin owns which folder -- is
{doc}`provision-bundle-spec`. This document is narrower and more durable than
either: it is the interface a plugin implements to take part.

**Scope.** This covers what a plugin implements: staging changes in the web
UI, describing them, and reconciling a live system to what a checked-out
remote declares. It does *not* cover who drives that last part -- fetching a
remote, verifying signatures, the fast-forward check, deciding it's safe to
call `provision_apply` at all, a boot-time trigger, or writing a log back to
a drive. That orchestration is a separate, later design, and nothing here
assumes an answer to it; this document only fixes what a plugin hands to it.

## Provisioning is a plugin, coordinating other plugins

`spiriconfig_provision` is an ordinary plugin, package and sidebar entry and
all, the same shape as `spiriconfig_system`. It owns three things no other
plugin has a home for: `trust.d/`, the review -> commit-and-sign -> export
spine, and the commit history log.

Everything else -- what a `users/add/<name>/` entry looks like, what form
staging one shows, how to describe a changed `apps/state/nextcloud/env` in
plain English -- belongs to the plugin that already owns that resource.
`spiriconfig_provision` does not parse or generate a `users/` entry any more
than the Docker page parses a `network/` one. It coordinates: it puts each
contributing plugin's block on the page, and it collects what they report
about pending changes. That is the entire job, and the reason a network
plugin arriving later only has to *implement* this interface -- nothing here
gets taught a new resource type.

## Why a separate contract, not new `Plugin` methods

`spiriconfig.plugins.Plugin` (see {doc}`plugins`) stays exactly as small as it
is today. Provisioning is a concept only plugins that opt in need to know
about -- `spiriconfig_tailscale` has no reason to import anything about
staging blocks or commit segments, and a smaller core is one less thing for
every plugin author to read past. So the contract below lives in
`spiriconfig_provision.contract`, a small module with no heavy dependencies,
and a plugin that wants to contribute imports from it. `spiriconfig_provision`
finds contributors the same way the CLI finds plugins at all -- by asking, not
by a registry it keeps in sync:

```python
from spiriconfig.plugins import discover

contributors = [p for p in discover() if hasattr(p, "provision_resource")]
```

Not `isinstance(p, ProvisioningContributor)`, even though the Protocol
below is written to look like it should work that way: every method on it
is individually optional (see the Protocol's own docstring), and
`isinstance` against a Protocol demands every member at once. Checking for
`provision_resource` -- the one thing every contributor actually sets --
is what discovery really means here; `ProvisioningContributor` itself is
for type-checking a contributor's own class, not for finding one.

A plugin that never imports `spiriconfig_provision` is simply absent from
that list. Nothing breaks, nothing has to be disabled -- the same "failure is
contained" property {doc}`plugins` already promises for a broken plugin
applies here to a plugin that just doesn't care about this feature.

## The contract

```python
# spiriconfig_provision/contract.py
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
    """Which remote this render call is for. Redundant with which hook was
    called (see below) -- carried here purely so a contributor whose two
    hooks share rendering code doesn't have to infer it from the call site."""


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
    punctuation: `spiriconfig_provision` joins these with commas to build a
    subject line, it does not reword them."""

    detail: str
    """A full sentence for the Review & Sign list -- e.g. ``"Installs whoami
    from spiri-apps"``, ``"Removes account alice"``. Stands alone; the
    reader has not necessarily seen `summary`."""


@dataclass(frozen=True, slots=True)
class ApplyStep:
    """One command in a `provision_apply` plan."""

    command: Command

    input: str | None = None
    """Passed straight through as `run`'s own `input=`, for a command that
    reads from stdin -- `chpasswd -e` is the case this exists for: the
    hash sitting in a committed `password.hash` file has to reach it
    somehow, and `Command.argv` is rendered into a copy-pasteable shell
    line, so it can never hold a secret. Same discipline
    `spiriconfig_users.set_password` already follows for a typed password;
    this is that same shape, carried across the interface."""


class ProvisioningContributor(Protocol):
    """Interface a `Plugin` also implements to take part in provisioning.
    For static type-checking only -- see the discovery snippet above for
    how a contributor is actually recognised at runtime.

    Only `provision_resource` is truly required; every method below is
    individually optional -- see each one's docstring for what its
    absence means -- because `spiriconfig_provision` checks with
    `hasattr` before calling any of them, the same tolerant style
    `Plugin.cli`/`Plugin.page` already use. That per-method optionality
    is exactly why this Protocol is **not** `@runtime_checkable`: an
    `isinstance` check against it would require every member below to be
    present, defeating the point."""

    provision_resource: ClassVar[str]
    """Top-level repo directory this plugin owns -- e.g. ``"users"``,
    ``"apps"``. Exactly one plugin may claim a given name; a name claimed
    twice is a discovery-time error, logged and both skipped, the same as
    any other broken plugin."""

    def provision_patch_block(self, repo: StagedRepo) -> None:
        """Render the add/remove staging block for `repo`, inside the
        *patch* remote's editor on the Provisioning page.

        Called with NiceGUI's `ui.*` available, same as `Plugin.page`. This
        method *is* the entire write path: it writes files directly under
        ``repo.path / provision_resource / "add"`` or ``.../"remove"``
        itself, the moment the operator stages something -- there is no
        callback into `spiriconfig_provision` to hand a change to.
        Additive only: touches only the entries the operator adds or
        removes this session, never resyncs the directory wholesale.

        Omit this method entirely for a resource type with no everyday
        add/remove flow -- `spiriconfig_provision` skips a contributor that
        doesn't define it, the same as a `Plugin` that never overrides
        `page`."""

    def provision_baseline_block(self, repo: StagedRepo) -> None:
        """Render the exact-set editor for `repo`, inside the *baseline*
        remote's editor.

        Writes under ``repo.path / provision_resource / "state"``. Unlike
        `provision_patch_block`, this one *does* resync that subtree
        wholesale on every save -- deleting entries no longer in the
        declared list, not just adding new ones -- because `state/` is not
        relative to anything: it is an absolute declaration, and the
        directory on disk must always equal exactly what the list shows,
        never a diff against what used to be there.

        Optional, independently of `provision_patch_block`: a resource type
        with no sane notion of "the exact set" -- one-shot scripts, say --
        simply doesn't define it, and `spiriconfig_provision` leaves it out
        of the baseline editor entirely rather than showing an empty
        section."""

    def provision_describe(self, repo: StagedRepo, path: str) -> Change | None:
        """Describe one changed path under `provision_resource`, relative
        to `repo.path` -- e.g. ``"users/add/operator/password.hash"``.

        Called once per changed path `git status` reports under this
        plugin's directory, to build the Review & Sign list and the
        suggested commit message. May read `repo.path / path` (or sibling
        files) to build a better description -- the settings dialog reading
        back an app's declared fields to summarise what changed is the
        expected use -- but must not shell out or touch anything outside
        `repo.path`: this describes a path, it does not investigate a live
        system.

        Return `None` for a path this plugin doesn't recognise.
        `spiriconfig_provision` falls back to the bare path for a change
        nobody claims, rather than hiding it -- an unrecognised file in a
        signed commit is exactly the kind of thing Review & Sign exists to
        surface, not swallow."""

    def provision_apply(self, repo: StagedRepo) -> list[ApplyStep]:
        """Return the steps that reconcile the live system to what
        `repo`'s `provision_resource` directory declares.

        Called with a remote already fetched, verified, and fast-forwarded
        -- by the time this runs, whether to trust and apply `repo` at all
        is a decided question, not this method's to re-ask. It only reads
        `repo.path / provision_resource` (and the live system, the same way
        `Plugin.page` already does -- `getent passwd`, `docker ps`, whatever
        this plugin normally reads) and returns steps rather than running
        them, same as every other action in this codebase: the caller
        decides how to run each one (`run(step.command, input=step.input)`),
        in the order returned, and stops the whole apply on the first
        failure, the same as a human working down a list by hand would.

        `repo.remote` tells this method what shape to expect on disk --
        `add/`/`remove/` for `"patch"`, `state/` for `"baseline"` -- so one
        method handles both; there's no separate hook per verb. An empty
        list means nothing under this plugin's directory needs changing,
        which is the common case and not an error."""
```

## Writing the files is plain I/O, not a `Command`

Rule 1 in {doc}`plugins` is "do the work by running the command a human would
run." That rule is about *acting on the live system* -- starting a container,
writing to `/etc/passwd`. Placing a file inside a git working tree that
nobody has applied yet is authoring, the same category as hand-writing a
`compose.yaml`, and `provision_patch_block`/`provision_baseline_block`
implementations use plain `Path.write_bytes`/`write_text` accordingly. There
is nothing to shell out to: the file *is* the declaration, the same
"literal name on disk is correct" stance the repo layout itself is built on.

## Applying a repo *is* rule 1, in full

`provision_apply` is the mirror image of the previous section: here, rule 1
applies exactly as written, because this method acts on the live system.
Its body ends up taking one of two shapes, depending on whether this
resource type has a real destination directory to point at:

- **File-shaped types** (`trust.d/`, `network/`) have one: a real directory
  of drop-in files. The whole method is one `ApplyStep` -- `rsync -a` for
  `add/`, `rsync -a --delete` for `state/`, `rm` of the named files for
  `remove/` -- and its `input` is `None`, since none of those read stdin.
- **Everything else** (`users/`, `apps/`) doesn't -- accounts live in
  `/etc/passwd`, installs are symlinks that depend on a store checkout
  existing first. The method reads the live system the way its `page()`
  already would (`getent passwd`, `installed()`'s symlink scan), diffs that
  against what `repo.path / provision_resource` declares, and returns one
  `ApplyStep` per entry that differs: `useradd` or `userdel`,
  clone-store-then-`ln -s` or `rm` the symlink -- and, for a staged password,
  a `chpasswd -e` step whose `input` is `f"{name}:{hash}\n"` built from the
  committed `password.hash` file, never from an argument on the command
  line. This is exactly why `ApplyStep` carries `input` separately from
  `Command`: `str(Command)` is rendered as a copy-pasteable shell line, and a
  password hash is precisely the thing that must never end up in one.

See `NOTES-usb-provisioning.md`'s "add/remove/state contract" section for
the reasoning behind that split; it predates this interface and this is
just where it landed.

**Ordering across resource types is the orchestrator's fixed sequence, not
this contract's.** Nothing in this design lets one resource type declare a
dependency on another -- there's no manifest to hold one -- so
`spiriconfig_provision` calls every contributor's `provision_apply` in a
plain, hardcoded order (trust.d, then everything else) and that has been
enough so far. If a real ordering dependency ever turns up, it gets solved
then, not speculatively here.

## A worked example

`spiriconfig_users` is the first real contributor, not a hypothetical one --
its patch-side wiring (`provision_resource`, and the four methods, each a
one-line delegation) is in `src/spiriconfig_users/__init__.py`, and the
substance -- staging, describing, applying -- is in
`src/spiriconfig_users/provision.py`, with tests in
`tests/test_users_provision.py` and `tests/test_provision_web.py`. Not
reproduced here: it would drift the moment either side changed, and the
files themselves are short. Two things worth knowing before reading them:

- **`remove/<name>` is a bare empty file, never a directory.** git tracks
  nothing about an empty directory, so an empty-*directory* marker would
  silently vanish the instant it's the only thing in a commit -- a real bug
  the first pass at `_stage_remove` had, caught by `git status` reporting
  nothing staged for a removal that very much existed on disk. Every
  `remove/<name>` in `NOTES-usb-provisioning.md`'s layout, across every
  resource type, is a marker file for exactly this reason.
- **A credential gets (re)set for every declared entry, not just newly
  created ones.** `provision_apply` can't tell whether a live password
  already matches a declared hash without reading `/etc/shadow`, so both
  the `add/` and `state/` branches call `chpasswd -e` unconditionally for
  everything they touch -- re-setting an already-correct hash is harmless,
  skipping a changed one silently isn't. Baseline missed this on a first
  pass (it only set the password for names it also had to `useradd`) before
  a test wrote the "already-certified account" case out loud and caught it.

Both `provision_patch_block` and `provision_baseline_block` exist end to
end for `users/` today. One more thing the baseline side made concrete:
its editor can't show what a save will change on any particular device,
for the same reason "Applying a repo" above gives -- it doesn't know any
device's actual state, only the declaration. It shows the declared list,
full stop, not a diff.

## Profiles: an operator's own authoring repos

An operator's own working trees -- as opposed to a device's applied
clones, see "The device-side apply loop" below -- are organised into
named *profiles*, one per remote: a patch profile and a baseline profile
are entirely separate namespaces (`ProvisionSettings.profiles_dir /
"patch" / <name>` and `.../ "baseline" / <name>`), never one profile
bundling both. An operator can give a patch profile and a baseline
profile the same name (`shop-floor-fleet`) to pair them by convention,
but nothing in `spiriconfig_provision.repo` tracks that pairing as data --
`list_profiles(remote, settings)` and `create_profile(name, remote,
settings)` both take `remote` as which namespace, and a
`spiriconfig_provision.repo.Profile` is just a name plus one
`StagedRepo`, never two.

This went through two earlier shapes before landing here. First, a single
mode per repo (`git config provision.mode`), which was wrong: a real
fleet profile plausibly wants both a patch remote for everyday changes
and a baseline remote for its compliance story. Then, one profile
bundling an optional `patch/` and an optional `baseline/` working tree
under a shared directory -- closer, and matched an early design mockup,
but still coupled two independent authoring efforts (different content,
different review, different export schedule) behind one shared name and
one shared "profile" concept. Splitting them into two flat, independently
named lists removed that coupling without losing anything: pairing by
name is still available to an operator who wants it, it's just a naming
convention now, not a data relationship this package has to keep
consistent.

On the Provisioning page, this shows up as two independent sections, each
with its own profile picker -- a patch profiles picker with contributors'
staging blocks, a pending-changes count, Review & Sign, and Export;
separately, a baseline profiles picker with the exact-set editor and the
same actions. Switching the selected patch profile never touches which
baseline profile is selected, and vice versa -- there is no shared
selection to keep in sync, because there is no shared profile underneath
either picker.

## Review & Sign, Pull, and Export

`spiriconfig_provision.review.commit` is Review & Sign's substance: stage
everything pending (`git add -A`) and commit it, signed with whatever
`git config user.signingkey` already names -- never a key this module
picks or manages itself, the same "shell out to what a human would run"
discipline as everywhere else in this codebase. No key configured means
an honestly unsigned commit, not a refusal: whether that's good enough is
what `ProvisionSettings.tier` decides at apply time, not this function's
to gate.

`spiriconfig_provision.sync` is the git-remote plumbing a profile's
remote needs: `origin_url`/`set_origin`, `status` (ahead/behind against
`origin`, read fresh, never fetched implicitly), `pull` (fast-forward
only -- a real merge is a person's call, not this function's), and
`export_to`, which sends `HEAD` to a drive path or a URL -- the same
operation either way, a drive destination just needs to be a *bare* repo
first (`_ensure_bare`), so pushing into it is never mistaken for editing
a checked-out working tree somewhere else.

## Not yet an operator's tool

`ProvisionPlugin.advanced = True`, so its sidebar entry is hidden with
advanced mode off -- the same treatment the terminal plugin gets, and for
the same reason: not a permission (the route and the CLI both still work
regardless, per {doc}`plugins`'s "it is not a permission system"), a
display filter for a page that would currently mislead an ordinary
operator. Staging, reviewing, signing, and exporting all work end to end
now, but there's still no `trust.d/`, so nothing exported here can reach
a `signed`-tier device without also handing it the weaker `trusted` tier.
Promoting it to shown-by-default is a one-line change, made once
`trust.d/` and the `signed` tier exist.

## `trust.d/` dogfoods the same contract

`trust.d/` has no owning plugin -- it is a property of the tool itself, not a
system resource -- so `spiriconfig_provision` implements
`ProvisioningContributor` on itself for it, internally, the same interface
every other contributor uses: staging block, description, and
`provision_apply` alike (one `ApplyStep` running `rsync -a`/`rsync -a
--delete`/`rm`, per the file-shaped case above). There is deliberately no
second, privileged code path for any of it; if the interface were awkward
for its own author to use, that would be a defect in the interface, not a
reason to special-case around it.

## The device-side apply loop

`spiriconfig provision apply <remote> <source>` is the first caller of
`provision_apply` -- `spiriconfig_provision.apply`, CLI only, matching
step 1 of `NOTES-usb-provisioning.md`'s phasing. It does exactly what
"Applying a repo" above assumes has already happened by the time a
contributor's `provision_apply` runs:

1. Opens (`git init`ing if new) a persistent local clone at
   `ProvisionSettings.apply_patch_dir`/`apply_baseline_dir` -- a different
   tree from any profile's `patch/`/`baseline/`, which are an operator's
   own authoring working trees (see "Profiles," above). This is the copy
   a device fetches into and applies from.
2. `git fetch <source> <ref>` into it. The transport is never special --
   `source` is anything `git fetch` itself accepts.
3. Fast-forward check against a `refs/spiriconfig/last-applied` ref kept in
   that same clone (a git ref rather than a state file, so advancing it is
   atomic and it can never name a commit the object store doesn't have).
   Refuses outright if the fetched commit isn't a descendant of it.
4. Tier-gates on `ProvisionSettings.tier`: `off` refuses before even
   fetching; `trusted` proceeds straight through; `signed` refuses too, for
   now -- verifying a commit means checking it against `trust.d/`, which
   doesn't exist as a resource type yet. This is the one piece of "Applying
   a repo" this document doesn't yet deliver on.
5. Checks the fetched commit out into the clone's working tree, calls every
   contributor's `provision_apply` against it, and returns the collected
   `ApplyStep`s as a `spiriconfig_provision.apply.ApplyPlan` -- nothing
   run yet.

Running that plan is a separate step, `spiriconfig_provision.apply.execute`,
kept apart from `plan()` on purpose: the CLI shows every step's command line
and asks for `--yes` before calling it, the same "read before you run it"
discipline every other command in this project follows. `last-applied` only
advances once every step has succeeded, so a plan that fails partway leaves
the remote at its old baseline rather than marking a half-applied commit as
done.

## What this document does not cover

- **Verifying a commit's signature, and the `signed` tier.** Needs
  `trust.d/` to check against, and `trust.d/` doesn't exist as a resource
  type yet -- `spiriconfig_provision.apply.plan` refuses outright at that
  tier rather than pretending to check anything.
- **Any boot-time trigger, or a USB drive specifically.** The apply loop
  above is reachable by CLI only, one remote at a time, on demand -- a
  systemd unit calling it once per configured remote in baseline-then-patch
  order at boot is a separate, later piece (step 7 of the phasing in
  `NOTES-usb-provisioning.md`).
- **Where `StagedRepo` comes from, on the authoring side.** Profile
  management, which git remote is "open" for staging, and the repo picker
  are `spiriconfig_provision`'s own page code, not part of the contract a
  contributor implements against.
- **The Review & Sign and Export dialogs' own layout.** This document
  specifies what a contributor hands them (a `Change` per path, blocks to
  render), not how they're drawn.
