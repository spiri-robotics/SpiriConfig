# Notes: background jobs

Scratch. Not documentation. A proposal, nothing here is built yet.

## The complaint

A colleague expected to close the fullscreen output modal and have the command keep
running in the background. Today that can't work: the modal *is* the job.
`_run_in_dialog` (written twice, `spiriconfig_docker/web.py` and
`spiriconfig_appstore/web.py`) owns the `async for chunk in stream_pty(...)` loop,
and the caller blocks on `await dialog` before refreshing (design.md, "The output
dialog waits for the human"). Closing it early would either kill the output or
orphan it with nowhere to look at it again.

The constraint that makes it tricky: plugins must get the same UX as the core, and
container plugins live in an iframe, so they can't share a NiceGUI dialog. Whatever
we pick has to cross that boundary without asking a Go/Rust plugin to reimplement
our UI.

## Proposal: a job is a tmux session

Split *the job* from *a view of the job*, and let tmux hold the job.

    tmux new-session -d -s spiriconfig-<plugin>-<slug> -x <cols> -y <rows> '<command>'
    tmux set-option -t spiriconfig-<plugin>-<slug> remain-on-exit on

| question | answer | from |
|---|---|---|
| what jobs exist? | `tmux ls -F ...`, filter `spiriconfig-*` | tmux |
| show me one | `stream_pty(tmux attach -t <name>)` | tmux |
| did it finish / how? | `#{pane_dead}` / `#{pane_dead_status}` | tmux |
| stop it | `tmux kill-session -t <name>` | tmux |

Why tmux and not an in-process registry:

- **No state of ours.** The job list is `tmux ls`. Nothing to keep in sync.
- **Shells out to what a human would run**, and every step is a `Command`, so
  `--show` and no-daemon tests keep working.
- **CLI parity, literally.** Over ssh, `tmux attach -t spiriconfig-docker-up-whoami`
  shows the same job the web UI does.
- **Survives things.** A SpiriConfig restart, a browser reload, a second tab or
  device: all see the same jobs, with scrollback replayed on attach.
- **The existing terminal widget works unchanged.** Attach is just another pty;
  raw bytes still go straight to xterm.js. Interactive jobs (`exec`) keep working
  because attach is interactive.

## Shell UX

- A **jobs indicator in the header**, always present (count badge, never appears/
  disappears or moves -- UI button policy). Tap: list of jobs. Tap a job: the same
  fullscreen view as today.
- The view has two **fixed** buttons: **Hide** (detach the view; job keeps running)
  and **Stop** (kill the session). Both always there, never swapped for a
  contextual Close/Stop depending on state.
- Finished jobs stay in the list (with exit status) until dismissed. Dismiss =
  `kill-session`.
- Confirmation dialogs ("Stop it?", "Adopt it?") stay ordinary modals. They're
  questions, not jobs.

## Plugin contract

**In-process plugins.** One core helper replaces both `_run_in_dialog` copies:

    jobs.start(title, commands, on_exit=refresh)

Less code for plugins than today. The view lives in the *shell's* slot, not the
plugin's refreshable container, so "refresh deletes my dialog" (design.md) goes away
by construction. "Wait for the human, then refresh" becomes "refresh when the job
exits". Multiple commands (appstore runs lists) become one session running them in
sequence, stopping on first failure, the same as today.

**Container plugins.**

1. Have `tmux` in the image; start long jobs as `spiriconfig-*` sessions.
   The core discovers them with `docker exec <container> tmux ls` and attaches with
   `docker exec -it <container> tmux attach -t <name>`, so they show up in the same
   tray.
2. To pop the view open, call one function from `shell.js`, e.g.
   `spiriconfig.openJob("flash")`. It `postMessage`s the parent; the *shell* opens
   its own fullscreen view over the whole app, not inside the iframe.
   The shell must only honour session names from the container that owns the
   sending iframe.

This is another piece of the public contract, so it falls under the
`spiriconfig.plugin.api` versioning question in NOTES-out-of-process-plugins.md.

## Refinements from discussion

**Status: not sold yet.** Modals may still be the right tool; this is an advanced
feature and the extra UX (tray, Hide vs Stop, dismissing finished jobs) has a real
cost on a glove-operated screen. Before building it, find out which complaint we're
answering: "stuck watching a 5-minute pull" (cheap fix: let Close detach while the
process keeps running, toast on exit, docker's own state is the tracker) vs "modals
feel heavy" (styling). Also check what a dropped websocket / closed tab does to a
half-finished `up` today -- `stream_pty` closes the pty master in its `finally`.

**Open the modal explicitly, don't auto-detect sessions.** If SpiriConfig popped a
modal on every new session it couldn't tell *which browser* started it: every open
tab (and sessions started over ssh) would get one. `spiriconfig.openJob(name)` from
`shell.js` comes from the iframe in the tab that clicked, so only that tab opens it.

**Plugins must still run standalone.** Same stance as deep links ("include the
script tag if you want them"): `openJob` is optional. Framed, it postMessages the
shell, which renders the standard closable modal. Unframed, `shell.js` sees no
parent and it does nothing (or the plugin shows a status line); the job still runs,
and `docker exec -it <plugin> tmux attach -t <name>` is the fallback. SpiriConfig
enhances, the plain command is always underneath -- parity rule applied to plugins.
A plugin can embed its own xterm.js if it wants a nice standalone terminal.

**Discovery via a socket directory, not polling.** Each job's tmux server uses its
own socket on a shared mount: `tmux -S /run/spiriconfig/jobs/<plugin>/<name>.sock`.
A job exists because its socket exists -- the same "directory is the source of
truth" as stacks. Listing is `ls`; inotify makes new jobs instant.

- Per-plugin subdirectory, so a plugin can't spoof another's jobs. The mount becomes
  part of the plugin contract.
- **Stale sockets**: a crashed/restarted container leaves its socket behind.
  Liveness check with `tmux -S <sock> has-session` and sweep dead ones, on change
  rather than a timer.
- **Attach still goes through `docker exec`.** tmux refuses a client whose protocol
  version differs from the server's, and by default rejects clients with a different
  uid (container root vs SpiriConfig's service user). So the folder is for
  *discovery*; attach is `docker exec -it <container> tmux -S <sock> attach`.
- Cheap detection doesn't fix the which-tab problem above: the folder feeds the
  always-on jobs list, `openJob` still decides when a modal pops.

## Costs / open questions

- **tmux becomes a dependency** on the host and in plugin images. Small, offline-
  friendly, but a dependency. A machine without it: run in the foreground as today,
  or refuse? Leaning: degrade to today's blocking dialog and log.
- **Pane size.** Multiple viewers shrink the pane to the smallest. Pin it with
  `window-size manual` and `TERMINAL_ROWS`/`TERMINAL_COLUMNS`.
- **Accumulation.** Dead sessions pile up until dismissed. GC dead ones older than
  N hours? Or only on dismiss?
- **Naming collisions.** Two `up whoami` at once. Append a short suffix, or refuse
  the second because one is already running? Refusing is probably the more honest
  answer for docker actions.
- **tmux server ownership.** Which user's tmux server? SpiriConfig runs as a service
  user; a human ssh-ing in as themselves won't see it without `-S <socket>`. Pick a
  fixed socket path (`tmux -S /run/spiriconfig/tmux.sock`) and document it, so the
  parity command is copy-pasteable.
- **Refresh on exit** for jobs started in a previous server life: nobody holds the
  `on_exit` callback. Probably fine -- pages already derive state from `docker ps`
  on render; poll the job list while the tray has anything running.

## Rejected (for now)

**In-process job registry** (ring buffer per job, same tray and view). Simpler, but
it's state we own, it's lost on restart, the CLI can't see it, and container plugins
would need an HTTP API to join in. Only worth it if a tmux dependency is a
dealbreaker.

## First slice

Core `spiriconfig.jobs` + the header tray + the docker plugin switched over. No
container plugin support, no appstore. Enough to put in front of the colleague.
