"""App stores, and the git commands that act on them.

An *app store* is a git repository with one top-level directory per app, each
containing a compose file. An *app* is one of those directories. There is no
manifest, no index, and no registry -- the repository layout is the format, so a
store can be read with ``ls`` and published with ``git push``.

Installing an app **symlinks** it into the compose directory::

    /srv/compose/whoami -> /var/lib/spiriconfig/stores/spiri-apps/whoami

That single decision is what makes the rest of this module small, because it
means SpiriConfig records nothing about the install. The symlink *is* the record:

* Where did this app come from? -- ``readlink``
* Have I changed it? -- ``git status``
* What did I change? -- ``git diff``
* What did the store change? -- ``git diff HEAD @{upstream}``
* Update it -- ``git merge``
* Uninstall it -- ``rm`` the symlink, which deletes nothing real

Every one of those is a git question with a git answer, on a working tree the
user can ``cd`` into. We are not tracking installs; git is, and it was going to
do a better job of it than we would.

As everywhere else in SpiriConfig, functions that act on a store *return* a
:class:`~spiriconfig.commands.Command` rather than running it, so the UI can show
the user the git line before it runs and tests can assert on it without a network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from spiriconfig.commands import Command, CommandError, Result, run
from spiriconfig_docker.stacks import find_compose_file

from spiriconfig_appstore.config import AppStoreSettings

#: The compose key a store maintainer can set to name a version themselves.
#:
#: Versioning a bag of containers is not a thing anyone can do well, so we do not
#: ask them to. This is optional, and when it is absent we fall back to the date
#: of the last commit that touched the app -- which is always available, always
#: monotonic, and requires no discipline from the maintainer.
#:
#: Nothing depends on it. See :meth:`App.version`.
VERSION_KEY = "x-spiri-config-version"

#: The upstream ref, as git spells it: the branch our checkout tracks.
#:
#: Using ``@{upstream}`` rather than hardcoding ``origin/main`` means a store on
#: ``master``, or on a branch a user checked out themselves, just works.
UPSTREAM = "@{upstream}"

#: The start of a git conflict marker. See :meth:`Store.unresolved`.
CONFLICT_MARKER = "<<<<<<< "

log = logger.bind(plugin="appstore")


class StoreError(Exception):
    """Something is wrong with a store, or with the request made of it."""


def slug_for(url: str) -> str:
    """Derive a directory name from a git URL.

    ``https://github.com/spiri/spiri-apps.git`` -> ``spiri-apps``. Purely
    cosmetic -- it names the clone directory and the store in the UI.
    """
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.removesuffix(".git") or "store"


def remote_url(settings: AppStoreSettings, path: Path) -> str | None:
    """Read a checkout's origin URL, or None if it has none we can read.

    This is what makes disk the source of truth: a store is a clone, and a clone
    remembers where it came from, so we ask *it* rather than keeping a list of our
    own. ``git remote get-url origin`` is the same question ``readlink`` answers
    for an installed app -- "where did this come from?" asked of the filesystem.

    None for a directory with no ``origin`` (someone's local-only repo, say): still
    a perfectly good store to install from, just one with no upstream to show or
    pull. The caller decides what to display for it.
    """
    command = Command(argv=[settings.git_bin, "-C", str(path), "remote", "get-url", "origin"])
    try:
        result = run(command, timeout=settings.command_timeout, log=log)
    except CommandError as exc:
        log.warning("could not read origin for {}: {}", path, exc)
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


@dataclass(frozen=True, slots=True)
class App:
    """One app in a store: a directory with a compose file in it."""

    store: Store
    name: str
    """The directory name in the store, and the default name once installed."""

    path: Path
    """The app's directory inside the store's checkout."""

    compose_file: Path

    def version(self) -> str:
        """A human-readable label for what is currently checked out.

        Two sources, in order: the store maintainer's :data:`VERSION_KEY` if they
        set one, otherwise the date of the last commit to touch this app.

        This is *only* a label. Nothing decides whether to update based on it,
        because git can answer that question exactly -- see
        :meth:`Store.apps_with_updates`. Which is the escape from the usual
        trap: we never have to define what "version 2" of a set of containers
        means, or trust a maintainer to bump a number they forgot about.
        """
        declared = self._declared_version()
        if declared is not None:
            return declared
        return self._last_commit_date() or "unknown"

    def _declared_version(self) -> str | None:
        """Read :data:`VERSION_KEY` out of the compose file, if it is there.

        Reading YAML is fine; we never write it back. A malformed compose file is
        the user's problem to see in the editor, not a reason to fail a listing,
        so anything unparseable just means "no declared version".
        """
        try:
            document = yaml.safe_load(self.compose_file.read_text())
        except (OSError, yaml.YAMLError) as exc:
            log.warning("could not read a version from {}: {}", self.compose_file, exc)
            return None
        if not isinstance(document, dict):
            return None
        declared = document.get(VERSION_KEY)
        return str(declared) if declared is not None else None

    def log_command(self) -> Command:
        """``git log`` for the last commit touching this app. ``%cs`` is a date."""
        return self.store._git("log", "-1", "--format=%cs", "--", self.name)

    def _last_commit_date(self) -> str | None:
        result = self.store._capture(self.log_command())
        if result is None or not result.ok:
            return None
        return result.stdout.strip() or None

    # -- what has changed, and who changed it ---------------------------------
    #
    # Two different questions, and keeping them apart is what makes an update
    # safe. "Did I change this?" is HEAD vs the working tree. "Did the store
    # change this?" is HEAD vs upstream. An update is only risky where both are
    # true, and that is precisely where we hand the user a diff.

    def local_diff(self) -> Command:
        """What the user has changed in this app, against the store's version."""
        return self.store._git("diff", "HEAD", "--", self.name)

    def upstream_diff(self) -> Command:
        """What the store has changed in this app since we last pulled."""
        return self.store._git("diff", "HEAD", UPSTREAM, "--", self.name)

    def changed_locally(self) -> list[str]:
        """The app's *tracked* files that the user has edited.

        Tracked files only, and that is deliberate. A compose file with a
        relative bind mount (``./config:/config``) makes docker create data
        directories inside the store's checkout, which show up as untracked
        files. Those are the user's data, not an edit to the app, and counting
        them as a modification would tell every user their apps were "modified"
        the moment they started one.
        """
        return self.store._changed("HEAD", "--", self.name)

    def changed_upstream(self) -> list[str]:
        """The app's files the store has changed since the version we have."""
        return self.store._changed("HEAD", UPSTREAM, "--", self.name)

    def is_modified(self) -> bool:
        return bool(self.changed_locally())

    def has_update(self) -> bool:
        return bool(self.changed_upstream())

    # -- installation ---------------------------------------------------------

    def link_path(self, compose_dir: Path, name: str | None = None) -> Path:
        """Where this app's symlink would live in the compose directory."""
        return compose_dir / (name or self.name)

    def install_command(self, compose_dir: Path, name: str | None = None) -> Command:
        """The ``ln -s`` that installs this app.

        Rendered as a command rather than done with :func:`os.symlink` for the
        usual reason: it is the line the user could have typed, and ``--show``
        has to be able to print it.
        """
        return Command(
            argv=["ln", "-s", str(self.path), str(self.link_path(compose_dir, name))]
        )


@dataclass(frozen=True, slots=True)
class Store:
    """One cloned app store."""

    slug: str
    url: str
    path: Path
    """The clone's working tree."""

    settings: AppStoreSettings

    # -- command construction -------------------------------------------------

    def _git(self, *args: str) -> Command:
        return Command(argv=[self.settings.git_bin, *args], cwd=self.path)

    def _capture(self, command: Command) -> Result | None:
        """Run a read-only git command, returning None if git could not run.

        A missing git binary, or a store directory that is not a repository,
        should degrade a page to "I cannot tell you about this store" rather than
        crash it -- the same bargain :meth:`Stack.containers` makes with docker.
        """
        try:
            return run(command, timeout=self.settings.command_timeout, log=log)
        except CommandError as exc:
            log.warning("git failed in {}: {}", self.path, exc)
            return None

    def _changed(self, *args: str) -> list[str]:
        """Files that differ, for some ``git diff`` argument list.

        ``--name-only`` rather than ``--quiet``: both answer the question, but
        ``--quiet`` answers it by *exiting 1*, and an exit code of 1 is
        indistinguishable to :func:`~spiriconfig.commands.run` from git falling
        over -- so every routine "has this changed?" check would log itself as a
        command failure. Asking for the names instead keeps a clean exit clean,
        and hands back the file list we want to show the user anyway.
        """
        result = self._capture(self._git("diff", "--name-only", *args))
        if result is None or not result.ok:
            # We could not ask. Report "nothing changed" rather than guessing:
            # the update path re-checks before it acts, so the cost of being
            # wrong here is a missing badge, not a bad merge.
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def clone_command(self) -> Command:
        """Clone the store.

        No ``cwd``: both the source and the target are absolute (see
        :func:`stores`), so this runs correctly from anywhere, and git creates
        the leading directories itself.
        """
        return Command(
            argv=[self.settings.git_bin, "clone", self.url, str(self.path)],
        )

    def fetch_command(self) -> Command:
        """Ask the remote what it has, without touching the working tree."""
        return self._git("fetch", "origin")

    def merge_command(self) -> Command:
        """Merge the fetched upstream into the checkout.

        A merge, not a rebase or a reset: the user's edits to installed apps are
        commits on this branch (see :meth:`commit_local_command`), and a merge is
        the operation that reconciles two lines of change while keeping both. If
        it conflicts, it conflicts in the file, with the markers git always uses,
        and the user resolves it in the editor they already have.
        """
        return self._git("merge", "--no-edit", UPSTREAM)

    def _commit(self, *args: str) -> Command:
        """A ``git commit`` carrying an identity, since the box may not have one.

        Passed with ``-c`` rather than written into the repo's config so that the
        line we print is the whole truth -- a user who copies it gets the same
        commit we would have made, on a machine with no git identity at all.
        """
        return self._git(
            "-c", f"user.name={self.settings.commit_name}",
            "-c", f"user.email={self.settings.commit_email}",
            "commit", *args,
        )

    def commit_local_command(self, message: str) -> Command:
        """Commit whatever the user has edited, before merging over it.

        This is the step that makes an update non-destructive. git will refuse to
        merge across uncommitted changes, and the tempting fixes -- stash, reset,
        checkout -- all end with the user's work somewhere they will not think to
        look. Committing it puts their edit on the branch, so the merge has to
        *reconcile* with it, and the worst case is a conflict they can see rather
        than an edit that silently vanished.

        ``--all`` stages tracked modifications only, which is what we want: the
        data directories docker creates inside the checkout are untracked, and
        committing a user's media library into the app store would be a memorable
        way to fail.
        """
        return self._commit("--all", "--message", message)

    def discard_local_command(self) -> Command:
        """Throw away every local edit in this store. Asked for, never assumed."""
        return self._git("checkout", "--", ".")

    def remove_command(self) -> Command:
        """Delete this store's checkout: ``rm -rf`` the clone.

        The counterpart to :meth:`clone_command`, and safe to offer for exactly the
        reason ``adopt`` is: we made this directory with ``git clone``, so removing
        it puts the machine back where it was before we added the store. Nothing
        else here deletes a directory -- an app uninstall only unlinks -- and this
        one is allowed to precisely because it is undoing our own ``clone`` and
        touching nothing the user created.

        Apps installed from the store become dangling symlinks, which the docker
        plugin and :func:`spiriconfig_appstore.installs.installed` already tolerate.
        The caller is expected to warn about them first; git will not, because as
        far as it is concerned this is just a directory.
        """
        return Command(argv=["rm", "-rf", str(self.path)])

    def status_command(self) -> Command:
        return self._git("status", "--short")

    # -- finishing a conflicted merge -----------------------------------------
    #
    # A conflict is not a failure state, it is a question, and the user is the
    # only one who can answer it. What would be a failure is leaving them stranded
    # in it -- a half-merged repo they did not ask for, cannot see, and have no
    # button to get out of. So both answers are always available: finish it, or
    # pretend it never happened.

    def resolve_plan(self) -> list[Command]:
        """Conclude a conflicted merge: stage the fixed files, commit the merge.

        Guarded by :meth:`unresolved`, and that guard is not decoration. It is
        tempting to write this as ``git commit --all``, which reads like "commit
        whatever is there" -- and it is a trap. ``-a`` stages *unmerged* files
        too, which git treats as "the user has resolved these", so a file still
        full of ``<<<<<<<`` markers gets committed into the store as though it
        were a considered decision. The user gets told the update finished, and
        the broken file is now the app's history.

        So: refuse while any marker remains, and stage explicitly rather than
        with ``-a``. Git will not check this for us -- it has no idea what a
        conflict marker means once the index says the file is resolved.
        """
        remaining = self.unresolved()
        if remaining:
            raise StoreError(
                "these files still have conflict markers in them:\n"
                + "\n".join(f"  {self.path / name}" for name in remaining)
                + "\nEdit them, keep the version you want, and delete the "
                "<<<<<<< ======= >>>>>>> lines."
            )

        conflicted = self.conflicts()
        if not conflicted:
            # Everything is already staged; just land the merge commit.
            return [self._commit("--no-edit")]
        return [
            self._git("add", "--", *conflicted),
            self._commit("--no-edit"),
        ]

    def unresolved(self) -> list[str]:
        """Conflicted files that still contain conflict markers.

        A text search, because a conflict marker is a text fact. We are not
        parsing the YAML -- a half-merged compose file is usually not valid YAML
        at all, which is the point.
        """
        remaining = []
        for name in self.conflicts():
            try:
                text = (self.path / name).read_text(errors="replace")
            except OSError as exc:
                log.warning("could not read {} to check for markers: {}", name, exc)
                continue
            if CONFLICT_MARKER in text:
                remaining.append(name)
        return remaining

    def abort_command(self) -> Command:
        """Undo the merge entirely, back to how things were before the update.

        The user's own edits survive this: they were committed *before* the merge
        began, precisely so that aborting could be a safe thing to offer.
        """
        return self._git("merge", "--abort")

    @property
    def in_merge(self) -> bool:
        """Whether a merge is in progress and waiting on the user."""
        return (self.path / ".git" / "MERGE_HEAD").exists()

    def conflicts(self) -> list[str]:
        """Files with unresolved conflict markers in them."""
        result = self._capture(
            self._git("diff", "--name-only", "--diff-filter=U")
        )
        if result is None or not result.ok:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    # -- state ----------------------------------------------------------------

    @property
    def is_cloned(self) -> bool:
        return (self.path / ".git").exists()

    def is_dirty(self) -> bool:
        """Whether any tracked file in the store has been edited."""
        return bool(self._changed("HEAD"))

    def apps(self) -> list[App]:
        """Every app in the store: top-level directories with a compose file.

        The same rule :mod:`spiriconfig_docker.stacks` uses to find a stack, and
        that is not a coincidence -- a store is just a compose directory that
        happens to live in git. Which is why ``git clone`` + ``cp -r`` is a
        perfectly good way to use one without SpiriConfig at all.
        """
        if not self.is_cloned:
            return []

        apps = []
        for child in sorted(self.path.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            compose_file = find_compose_file(child)
            if compose_file is None:
                log.debug("skipping {}: no compose file", child)
                continue
            apps.append(
                App(store=self, name=child.name, path=child, compose_file=compose_file)
            )
        return apps

    def app(self, name: str) -> App:
        """Return the named app, or raise :class:`StoreError`.

        Matched against what is on disk, so a caller cannot escape the store by
        asking for something like ``../../etc``.
        """
        for app in self.apps():
            if app.name == name:
                return app
        known = ", ".join(a.name for a in self.apps()) or "none"
        raise StoreError(f"no such app: {name!r} in {self.slug} (available: {known})")


def update_plan(store: Store, *, discard_local: bool = False) -> list[Command]:
    """The commands that bring ``store`` up to date, in order.

    A list rather than one command, because an update is genuinely three steps and
    the middle one is conditional -- and the user should watch all three go past
    rather than have us hide the interesting one.

    Note what this does *not* do: update a single app. The symlinks all point into
    one working tree, which is at one commit, so "update whoami but not
    nextcloud" is not a state this repository can be in. That sounds like a
    limitation and is closer to a feature: pulling the store only rewrites files
    on disk. Nothing is restarted, nothing is pulled from a registry, and no
    running container changes until someone runs ``docker compose up`` on that
    stack. Per-app control still exists -- it just lives at up-time, where the
    user was going to make that decision anyway.

    Raises if a previous update is still waiting on a conflict. Merging on top of
    an unfinished merge is how a confusing situation becomes an unrecoverable one.
    """
    if store.in_merge:
        raise StoreError(
            f"{store.slug} has an unfinished update with conflicts in: "
            f"{', '.join(store.conflicts()) or 'unknown files'}.\n"
            f"Edit those files to remove the <<<<<<< markers, then run "
            f"`spiriconfig appstore resolve`, or run "
            f"`spiriconfig appstore resolve --abort` to undo the update."
        )

    plan = [store.fetch_command()]

    # Checked before fetching, but fetch does not touch the working tree, so the
    # answer cannot go stale between here and the merge.
    if store.is_dirty():
        if discard_local:
            plan.append(store.discard_local_command())
        else:
            plan.append(store.commit_local_command("Local changes, before update"))

    plan.append(store.merge_command())
    return plan


def _absolute(url: str) -> str:
    """Make a *local path* store absolute, and leave a real URL alone.

    Both defaults in this package are relative (``test_data/...``), so that a
    checkout never reaches for ``/srv/compose``. That is good for developers and
    a trap for everything downstream: a relative path is only meaningful against
    a working directory, and the two things we do with a store -- clone it, and
    symlink into it -- each resolve paths against a *different* directory than
    the one the user was standing in.

    The symlink is the one that bites. ``ln -s test_data/stores/x/whoami
    test_data/compose/whoami`` does not make a link to the store; it makes a link
    that resolves against ``test_data/compose/``, which is nowhere, and the app
    dangles. Absolute paths are the fix, and they also make every command we
    print correct to paste from any directory.

    A URL (``https://``, ``git@host:path``) is left exactly as given -- it is not
    a path, and is not ours to rewrite.
    """
    if "://" in url or url.startswith("git@"):
        return url
    return str(Path(url).expanduser().resolve())


def stores(settings: AppStoreSettings) -> list[Store]:
    """Every store: the clones on disk, plus any not-yet-cloned seeds.

    Disk is the source of truth. A store *is* a git checkout under
    :attr:`~spiriconfig_appstore.config.AppStoreSettings.store_dir`, so the live
    list is whatever is actually cloned there -- which is what lets the UI add one
    (clone it) and remove one (delete it) without SpiriConfig keeping a list of its
    own. Each clone's URL is read back off the clone itself; see :func:`remote_url`.

    :attr:`~spiriconfig_appstore.config.AppStoreSettings.stores` is a *seed* list,
    not the authority. Any seed whose slug is not already cloned is included as a
    not-yet-cloned store, so ``sync`` (or the Clone button) can bring it down -- and
    the moment it is cloned, disk discovery takes over and the seed entry is
    deduplicated away. A store the user cloned themselves and never listed is a
    first-class store; a seed the user removed stays gone until they clone it again.

    Paths are resolved here, once, so that nothing downstream has to remember to.
    """
    root = settings.store_dir.expanduser().resolve()

    found: dict[str, Store] = {}
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not (child / ".git").exists():
                continue
            found[child.name] = Store(
                slug=child.name,
                url=remote_url(settings, child) or "",
                path=child,
                settings=settings,
            )

    for url in settings.stores:
        slug = slug_for(url)
        if slug in found:
            continue
        found[slug] = Store(
            slug=slug,
            url=_absolute(url),
            path=root / slug,
            settings=settings,
        )

    # Insertion order: the cloned stores first (in sorted directory order, from the
    # scan above), then any not-yet-cloned seeds in the order they are listed. Stable
    # without re-sorting the two groups together, which would let a seed jump above a
    # cloned store just because of its name.
    return list(found.values())


def store_for_url(settings: AppStoreSettings, url: str) -> Store:
    """Build the :class:`Store` that adding ``url`` would create.

    Not necessarily one of :func:`stores` -- this is how a *new* store is named and
    placed before it exists, so the caller can run its :meth:`Store.clone_command`.
    Once cloned, :func:`stores` rediscovers it from disk like any other.
    """
    root = settings.store_dir.expanduser().resolve()
    slug = slug_for(url)
    return Store(slug=slug, url=_absolute(url), path=root / slug, settings=settings)


def get_store(settings: AppStoreSettings, slug: str) -> Store:
    """Return the named store, or raise :class:`StoreError`."""
    for store in stores(settings):
        if store.slug == slug:
            return store
    known = ", ".join(s.slug for s in stores(settings)) or "none"
    raise StoreError(f"no such store: {slug!r} (configured: {known})")


def find_app(settings: AppStoreSettings, name: str) -> App:
    """Find an app by ``name`` or ``store/name`` across every cloned store."""
    if "/" in name:
        slug, _, app_name = name.partition("/")
        return get_store(settings, slug).app(app_name)

    matches = [app for store in stores(settings) for app in store.apps()
               if app.name == name]
    if not matches:
        raise StoreError(f"no such app: {name!r}")
    if len(matches) > 1:
        found = ", ".join(f"{a.store.slug}/{a.name}" for a in matches)
        raise StoreError(f"{name!r} is in more than one store; say which: {found}")
    return matches[0]


__all__ = [
    "App",
    "Store",
    "StoreError",
    "VERSION_KEY",
    "find_app",
    "get_store",
    "remote_url",
    "slug_for",
    "store_for_url",
    "stores",
    "update_plan",
]
