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

This is not fussiness, it is a bug we shipped. Every action does
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

The second is more immediate. **The toggle is self-service.** It is in the header,
it is labelled, and anyone can click it. A "regular user" who wants the Edit button
turns advanced mode on and gets it, in the browser, without a shell. And below
that, hiding an element is only a statement about what the page draws -- the
websocket underneath does not care what the browser chose to render.

Nothing is withheld from anyone, so there is nothing here to lean on.

Advanced mode is a preference, like a dark-mode switch. Nothing about it is a
boundary, and it is not trying to be.

### Permissions, if they ever happen, belong to the OS

Not planned. But the intended design, so that nobody invents a worse one:

**Log the user in with PAM, then fork to that unix user.**

Authorisation is then whatever the kernel says it is -- file modes, group
membership, whether they are in the `docker` group. No role model, no ACL table,
nothing of ours to get wrong or to keep in sync.

This is the same argument as [there is no state that is ours](#there-is-no-state-that-is-ours),
pointed at permissions. We drive the machine by running the commands a human would
run, so a person should be able to do precisely what their shell account could do.
An application-level permission model would be a second, weaker source of truth in
front of a capability the OS already governs, and the first time the two disagreed
the OS would win and our UI would be the one lying.

It also means the answer to "can this person restart telemetry?" is not a question
about SpiriConfig at all. It is a question about a unix account, which the sysadmin
already knows how to answer, with tools that already exist. Which is the whole
point of the project.

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

**Anything clever with the docker socket.** If a thing cannot be expressed as a
command line, it does not belong in a plugin -- because it could not be shown to
the user, and they could not do it themselves.
