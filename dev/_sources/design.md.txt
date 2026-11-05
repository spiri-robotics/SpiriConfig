# Design notes

Why the code looks the way it does. Read this before changing the core.

## Progressive enhancement is the whole point

Most management UIs are a trap. You click a button, something happens, and now
the state of your machine lives in the tool's database. To understand your own
system you have to understand the tool. To leave, you have to reverse-engineer
what it did. The tool has stopped being a convenience and started being a
dependency.

SpiriConfig is built so that cannot happen. The rule:

> Anything SpiriConfig can do, the user must be able to do without it.

Every design decision below follows from that one, and any change that erodes it
is a bug, however convenient it seems.

## Consequences

### Traditional devops workflows continue to work

You can manage a drone with ansible, terraform, whatever sysadmin tools make sense for
managing your cluster of drones and sensor platforms.

### We shell out, on purpose

Everything SpiriConfig does to the system is done by running the command a human
would have run. We do not use `docker-py`; we run `docker compose`.

This looks primitive, and it is deliberate. A subprocess call is something we can
*show the user*, and they can copy it, run it, and understand it. A library call
is invisible and unreproducible. `str(Command)` renders a copy-pasteable shell
line -- properly quoted, `cd` included -- and there is a test that pipes it
through a real shell to prove it is not decorative.

### Output goes to a terminal, because the command expects one

The UI streams command output into xterm.js, and the command runs attached to a
**pseudo-terminal** rather than a pipe. Both halves of that are necessary, and
the first one is the surprising one.

Docker asks whether its output is going to a terminal, and *changes what it says*
based on the answer. On a pipe it emits a flat transcript. On a tty it draws
progress bars, redraws layer-download status in place, and colours each service
differently. So piping the output does not give you a plain version of the same
information — it gives you a program that concluded you were not worth talking to
properly. `docker compose pull` is the clearest case: through a pipe it is a wall
of "Pulling"; on a tty it is a live picture of what is happening.

Since the entire project is built on *running the command a human would have run*,
running it in a way that makes it behave differently than it would for that human
is a bug in the premise. {func}`~spiriconfig.commands.stream_pty` gives it a
terminal.

That decision forces the second: the bytes coming back are raw, carriage returns
and escape sequences and all, and they only mean anything to something that can
interpret them. We do not interpret them. Writing that interpreter is writing a
terminal emulator, and one already exists — NiceGUI ships xterm.js, so there is
no CDN, no vendored blob, and nothing to fetch on a machine with no internet.

The bytes are also never decoded on our side. A multi-byte character split across
a read boundary would be mangled; the browser reassembles the stream.

### The output dialog waits for the human, not the command

{func}`~spiriconfig.commands.stream_pty` finishing is not the end of the
interaction — the person still has to *read* the thing. So the dialog returns
when it is dismissed, not when the command exits.

Every action does
`await _run_in_dialog(...)` and then `refresh()`, and refresh clears the
container the dialog was created inside — deleting it. `up` and `pull` stream for
long enough that nobody noticed. `logs` returns instantly, so the modal appeared
and vanished before it could be read, and after a refresh there was nothing there
at all.

The fix is to not hand control back to the code that clears things until the user
is done looking. And then to `delete()` the dialog, because a closed dialog is
still an element on the page, and a session spent starting and stopping things
would otherwise accrete a pile of invisible modals still holding their output.

### Refresh must not schedule itself inside the thing it clears

The other half of the same bug, and the one that actually blanked the page.

Every action refreshes the list afterwards, and `refresh()` schedules `render()`
on a `ui.timer`. A NiceGUI event handler runs with the *clicked element's* slot
active -- and the button that was clicked lives in a card, inside the container.
So the timer became a child of the container. `render()`'s first act is to clear
that container, which deleted the timer whose callback was running, cancelling it
half-done: cleared, and never repopulated. Close a dialog, and the page behind it
was empty.

The first render works only by accident, because `page()` calls `refresh()` from
the page slot, outside the container.

The timer is now pinned to `container.parent_slot`. Any code that reaches for
`ui.timer` from inside a handler should ask the same question first: which slot
is this actually landing in, and is something about to delete it?

### Building a command is separate from running it

Every function that acts on a stack *returns* a `Command`; it does not execute
one. That separation is what makes three things possible at once:

- the UI can show the exact command before and while it runs
- `--show` can print it instead of running it
- tests can assert on the command line with no docker daemon in sight

Most of the docker plugin's test suite runs on a machine with no docker
installed, because the thing worth testing is *which command we built*.

### There is no state that is ours

No database, no registry, no lockfile, no `enabled/` directory. A stack exists
because a directory with a compose file exists. A stack is running because docker
says it is running. We do not track anything, so we cannot drift out of sync with
reality, and a user who edits the filesystem behind our back is not doing anything
wrong -- they are using the system exactly as intended.

This is why `--show` is safe on a production box and why `mkdir` is a supported
way to add a service.

### The project name is the directory name

We pass `-p <directory-name>` to compose, which is what compose would have
defaulted to anyway. If we picked our own project name, SpiriConfig would be
managing a *different set of containers* than the user gets from running
`docker compose up` in that same directory, and they would never find ours. This
one line is what keeps `spiriconfig docker up x` and a bare `docker compose up -d`
interchangeable.

### Compose files are text, not parsed YAML

Saving an edited compose file writes the user's text verbatim. We never round-trip
it through a YAML parser, because that would silently eat their comments and
reformat their file. We *validate* by asking `docker compose config` -- the same
authority that will have to accept the file later -- and restore the original if
it says no.

### Advanced mode hides, it does not forbid

The web UI serves two audiences from one machine, so [advanced mode](advanced.md)
filters what a page renders. It is tempting to reach for it as a permission system
-- "regular users cannot edit compose files" -- and that would be a lie twice over.

The first lie is the usual one: the CLI still does everything, so a hidden button
is not a removed capability.

The second is more immediate. **The toggle is self-service.** It is in the sidebar,
it is labelled, and anyone can click it. A "regular user" who wants the Edit button
turns advanced mode on and gets it, in the browser, without a shell. And below
that, hiding an element is only a statement about what the page draws -- the
websocket underneath does not care what the browser chose to render.

Nothing is withheld from anyone, so there is nothing here to lean on.

Advanced mode is a preference, like a dark-mode switch. Nothing about it is a
boundary, and it is not trying to be.

### PAM is a login gate, not a permission model

`SPIRICONFIG_AUTH=pam` gates the UI behind a PAM login (see
{mod}`spiriconfig.auth` and [configuration](configuration.md#authentication)),
authenticating against the host's own accounts with no user list of ours. That is
all it does. It answers one question -- "does the machine trust this person enough
to let them in?" -- and nothing past the door.

So **everyone who gets in is equal.** They share the one process, running as
whatever unix user SpiriConfig runs as, and can do everything the UI can do. PAM
tells us *who* is at the keyboard; it does not, and is not trying to, tell us what
they may touch once inside. There is no authorization layer, and none planned --
this is a login gate, not a permissions system.

If we ever did want per-user permissions, they would belong to the OS, not to us:
file modes, group membership, whether they are in the `docker` group -- no role
model, no ACL table, nothing of ours to get wrong or to keep in sync. That is the
same argument as [there is no state that is ours](#there-is-no-state-that-is-ours),
and it is the only permission design we would entertain. But that is a hypothetical,
not a roadmap. Today the honest statement is the one above: authenticated users are
undifferentiated.

### An installed app is a symlink, and that is the whole app store

An [app store](appstore.md) is a git repo with one directory per app. Installing
one is:

```console
$ ln -s /var/lib/spiriconfig/stores/spiri-apps/whoami /srv/compose/whoami
```

That is not an implementation detail, it is the design. Everything an app store
has to know is a question the symlink and the git repo behind it already answer:

| Question | Answer |
| --- | --- |
| Where did this app come from? | `readlink` |
| Have I changed it? | `git status` |
| What did I change? | `git diff` |
| What did the store change? | `git diff HEAD @{upstream}` |
| Update it | `git merge` |
| Uninstall it | `rm` a symlink -- which deletes nothing real |

Note what is *not* on that list: anything of ours. There is no install manifest,
no version database, no lockfile, no record of which commit an app came from. The
symlink is the record, and git is the state -- and git is a tool the user already
has, already understands, and can drive without us when we are wrong.

It also means the docker plugin needed no changes at all. A symlink to a
directory *is* a directory, so an installed app is just a stack: `spiriconfig
docker up whoami` works on it, and so does `cd /srv/compose/whoami && docker
compose up -d`. The app store and the thing that runs apps do not know about each
other, which is why neither can drift out of sync with the other.

#### Versions are a label, not a mechanism

Nothing decides whether to update based on a version, because git can answer that
question exactly: an app has an update if `git diff HEAD @{upstream} -- <app>/`
says its files changed. That is what "update available" means, and it is precise
even when the store bumps three images, rewrites a healthcheck, and touches a
config file the maintainer forgot to bump a number for.

Which is the escape from a genuinely hard problem. "What version is this bag of
containers?" has no good answer, so we do not need one to be right. The version
string is *cosmetic*: `x-spiri-config-version` if a maintainer set one, and the
date of the last commit touching the app if they did not. A store maintainer who
never thinks about versioning still gets one that moves when the app does.

#### Updates are per-store, and that is on purpose

The symlinks all point into one working tree, which is at one commit, so
"update whoami but not nextcloud" is not a state the repository can be in.

This sounds like a limitation and is closer to a feature. Pulling a store only
rewrites files on disk: nothing is restarted, nothing is pulled from a registry,
and no running container changes until someone runs `docker compose up` on that
stack. Per-app control still exists -- it moved to up-time, which is where the
user was going to make that decision anyway, and where they can see what they are
about to restart.

#### An edit is a commit, so an update is a merge

Editing an installed app edits a file in the store's git working tree. So when
the store moves on, an update is a three-way merge, and the user's changes and
the store's changes are reconciled by the thing that is best in the world at
reconciling them.

Before merging, we `git commit` whatever the user has edited. This is the step
that makes an update non-destructive, and it is worth being explicit about why
the alternatives are worse: `stash`, `reset`, and `checkout` all end with the
user's work somewhere they will not think to look. Committing it puts the edit on
the branch, so the merge has to *reconcile* with it. The worst case becomes a
conflict they can see, rather than an edit that silently vanished -- and it is
what makes "undo the update" safe to offer, because their work survives the abort.

A conflict is left in the file with git's usual markers, and then something nice
happens for free: **conflict markers are not valid YAML.** So `docker compose`
refuses the file, and `Stack.write` -- which validates with `docker compose
config` before saving -- refuses it too. A half-merged app cannot be started and
cannot be saved over. We did not build that safety net; it fell out.

The one place git will *not* save us is finishing the merge. `git commit --all`
stages unmerged files, which git reads as "the user resolved these" -- so it will
cheerfully commit a file still full of `<<<<<<<` markers and report a successful
update. So `resolve` scans for markers itself and refuses while any remain. That
guard is ours because there is no git flag that will do it.

#### Things the symlink design costs

Worth stating plainly, because both are real:

**Relative bind mounts write into the store's checkout.** A compose file with
`./config:/config` resolves that path against the symlink, so docker creates
`config/` inside the git clone. This is why "have I modified this app?" means
*tracked* files only -- otherwise every user would be told their apps were
modified the moment they started one. Store authors should prefer named volumes.

**Editing an installed app edits the store's copy of it.** Two installs of the
same app from the same store are the same files. If you want an app that is
yours, `adopt` it: that replaces the symlink with a real copy, and the app leaves
the store's orbit for good.

#### Two clever ideas that were rejected

*Copy the app in, and record where it came from in a sidecar file.* This is the
obvious design, and it is worse in every particular: it invents a file format,
makes SpiriConfig the keeper of provenance, and requires reimplementing three-way
merge against a base version we would have to store somewhere. The symlink makes
all of that git's problem, and git already solved it.

*Make `/srv/compose` itself the clone, with sparse-checkout picking the installed
apps.* Seductive -- install becomes `git sparse-checkout add whoami` -- and
fatally wrong. It makes the entire compose directory the property of one store,
so the user's own hand-made projects become untracked files in somebody else's
repository, `mkdir` stops being a supported way to add a service, and you can
never have two stores. The user owns that tree. A symlink is a guest in it; a
working tree is a landlord.

### One seam for "whose setting is this?"

Advanced mode is per-person, and SpiriConfig currently has no people -- so
"person" means "browser", via a session cookie. Users are coming, and when they
arrive the setting should follow the human, not the laptop.

Every preference therefore goes through {mod}`spiriconfig.preferences`, and
nothing else touches browser storage. Adding user support is registering a
different store, once; no plugin changes. The module-level store is a deliberate
process-wide global -- the alternative is threading a preferences object through
every plugin's `page()`, which buys nothing and would be the very refactor the
seam exists to avoid.

### Plugins are entry points

Installing a package is what installs a plugin. This is not novel, and that is
the appeal: it is how the rest of the Python ecosystem already works, so there is
no bespoke plugin format, no plugin directory to manage, and nothing for us to
keep in sync.

A broken plugin is logged and skipped, never fatal. The failure mode of a
half-written plugin should be "my plugin is missing and the log says why", not
"I can no longer administer this machine".

## Things we deliberately do not do

**An "enabled" state.** An earlier design had nginx-style
`available/` + `enabled/` symlinks. It was dropped: docker already knows whether
a stack is running, so a second source of truth could only ever disagree with the
first. Up and down is the whole lifecycle.

**Creating or deleting project directories.** The user owns that tree. We read it.

The [app store](appstore.md) does not bend this rule, it is shaped by it. An
install creates a *symlink*, and an uninstall removes one; the app's actual files
live in the store's checkout the whole time, and are still there afterwards. If
installing meant `cp -r` and uninstalling meant `rm -rf`, we would be deleting
directories full of the user's data on their behalf, and this line would have had
to go. It did not, and the design is better for having had to obey it.

**Anything clever with the docker socket.** If a thing cannot be expressed as a
command line, it does not belong in a plugin -- because it could not be shown to
the user, and they could not do it themselves.
