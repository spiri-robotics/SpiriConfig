# Spec: long-running jobs

Draft. Nothing here is built yet. Replaces the earlier "a job is a tmux session"
proposal; that and the other alternatives are summarised under *Considered* at the
end.

## The complaint

A colleague expected to close the fullscreen output modal and have the command keep
running in the background. Today that can't work: the modal *is* the job.
`_run_in_dialog` (written twice, `spiriconfig_docker/web.py` and
`spiriconfig_appstore/web.py`) owns the `async for chunk in stream_pty(...)` loop,
and the caller blocks on `await dialog` before refreshing (design.md, "The output
dialog waits for the human"). Closing it early either kills the output or orphans
it with nowhere to look at it again.

## What a job is

A **job** is a non-interactive command whose output a person wants to watch, and
which must outlive whoever is watching it. Concretely, a job:

- **finishes.** It has an exit code. Something meant to run forever is a service
  (a container, a systemd unit), not a job.
- **runs on a terminal, of a fixed size.** It gets a real pty, so tools see a TTY and
  draw colour and progress bars (checked: `docker pull` emits its cursor-up/erase-line
  redraws). The size is fixed at 80x40 and never changes: no resize events.
- **is an ANSI byte stream out, a byte stream in, and an exit code.** That is the
  whole protocol: `output.log` (a file), `stdin.fifo` (a FIFO), `status_code` (a
  file). Viewing means replaying `output.log` from the start into a vt100 (xterm.js);
  there's no screen state to sync. Typing means writing bytes to `stdin.fifo`. They
  go through the pty's line discipline exactly as keystrokes would, so they're
  echoed into the log, and ctrl-c becomes SIGINT.
- **can be cancelled.** ctrl-c down `stdin.fifo`; SIGTERM as the fallback.
- **outlives its viewers and SpiriConfig.** Closing the modal, a dropped
  websocket, a second browser, or a SpiriConfig restart don't affect it. A
  reboot ends a running job, but its directory and log survive, and it shows as
  "died".

Not jobs:

- **Interactive sessions** (the terminal plugin, `docker exec -it`) stay on
  `stream_pty`/`PtySession` as today. A job can answer a `[y/N]` prompt, but it never
  resizes, and nothing ends when the viewer leaves, which is wrong for a shell.
- **Quick queries** whose output we parse (`docker ps`, `git status`) stay on `run()`.

Proposed: everything that goes through `_run_in_dialog` today becomes a job, fast or
not. One code path, and there's no guessing in advance which `up` is slow because
of a pull.

## Job folders

Each **owner** gets its own job folder, and jobs live inside it. An owner is whoever
starts the job:

- an **in-process plugin** (`docker`, `appstore`, ...) by its plugin name;
- an **app**, i.e. a container plugin, by its app name (compose project directory).

```
/var/lib/spiriconfig/jobs/                  root install
$XDG_STATE_HOME/spiriconfig/jobs/           user install (default ~/.local/state; paths.py decides, as for compose_dir)
  docker/
    20261005T154130-up-whoami/
  appstore/
    20261005T154502-clone-spiri/
  netplan/                                  an app's folder, mounted into that app only
    20261005T160011-apply/
```

An app gets its folder by mounting it in its own compose file, at a fixed path
inside the container:

```yaml
    volumes:
      - /var/lib/spiriconfig/jobs/netplan:/var/lib/spiriconfig/jobs
```

The app only sees its own jobs, and the folder has the same name as everything else
about the app. This keeps things organised but isn't a security boundary: apps are
trusted code and can mount whatever they like (container-plugins.md).

**Persistent, so logs survive a reboot.** Nothing else needs to move for that:

- A job that was running at reboot has no `status_code`, and nobody holds its
  `lock`, so the state table already reads it as "died". No cleanup at boot.
- `pid` is stale after a reboot. It is only ever used while `lock` is held, so
  `kill` checks the lock first.
- `stdin.fifo` can live on disk. A FIFO is just an inode there; its data never
  touches the disk.
- `script -f` flushes but doesn't fsync, so a power cut can lose the last few
  lines. Fine for a log.

`$XDG_STATE_HOME` is the XDG directory meant for exactly this ("logs, history").
For a user install, apps mount the same path, and docker creates a missing mount
folder as root. A non-root SpiriConfig then can't dismiss that app's jobs. Accepted.

## A job directory

`<job folder>/<UTC timestamp>-<slug>/`. Sorting by name is sorting by start time,
and two `up whoami` jobs never collide.

| file | written by | contents |
|---|---|---|
| `command` | start | the command line, copy-pasteable |
| `size` | start | `80x40`; a viewer replays at the size the job ran at, even if the default changes later |
| `lock` | wrapper | `flock`ed for the life of the job; the kernel releases it if the wrapper dies |
| `pid` | the command's shell | pid (= process group id) of the command, inside `script`'s pty session |
| `output.log` | `script -f` | raw pty bytes (stdout and stderr, already merged by the pty), flushed as written. Starts with a `Script started ... [COMMAND=...]` line and ends with `Script done ... [COMMAND_EXIT_CODE=...]`. These stay in the log on purpose: they make it a self-describing log file |
| `stdin.fifo` | start (`mkfifo`) | input. Any number of writers, one after another; the wrapper holds it open read-write, so a writer leaving never sends EOF |
| `status_code` | wrapper | exit code, written when the command ends (via a temp file + `mv`, so a reader never sees it empty) |
| `cancelled` | cancel/kill | empty marker, written before the ctrl-c or signal |

State is derived from those files, never stored:

| `lock` held | `status_code` | `cancelled` | state |
|---|---|---|---|
| yes | - | - | running |
| yes | - | yes | cancelling |
| no | yes | no | exited (`status_code` = code) |
| no | yes | yes | cancelled |
| no | no | - | died: the wrapper was killed, or the container went away mid-run |

## The wrapper

One POSIX sh script, `spiriconfig-job`, part of the public contract. Needs `sh`,
`setsid`, `flock`, `mkfifo`, and util-linux `script`. Prototyped and tested
2026-10-05: exit codes; a `[y/N]` prompt answered through `stdin.fifo`, with two
separate writers in a row; cancel by ctrl-c (stops immediately, 130); `kill` for a
command that traps INT (143); crash (state "died"); duplicate refused; `tput`
inside reports 80x40; `docker pull` progress bars recorded intact.

```sh
#!/bin/sh
# spiriconfig-job start DIR COMMAND...   run COMMAND detached, as job DIR
# spiriconfig-job cancel DIR             ctrl-c it; it is recorded as cancelled
# spiriconfig-job kill DIR               SIGTERM it, for a command that ignores ctrl-c
set -eu
cols=80 rows=40

case $1 in
start)
	d=$2; shift 2
	mkdir "$d"                       # fails if the job already exists
	printf '%s\n' "$*" > "$d/command"
	echo "${cols}x${rows}" > "$d/size"
	mkfifo "$d/stdin.fifo"
	# setsid -f, not `&`: a non-interactive shell starts `&` jobs with SIGINT
	# ignored, and the command would inherit that and shrug off ctrl-c.
	setsid -f sh -c '
		d=$1 cols=$2 rows=$3; shift 3
		exec 9>"$d/lock"; flock 9      # held while running; the kernel drops it if we die
		exec 8<>"$d/stdin.fifo"        # read-write: never blocks, never sees EOF when a writer leaves
		set +e
		COLUMNS=$cols LINES=$rows script -qfec "stty cols $cols rows $rows; echo \$\$ > $d/pid; $*" "$d/output.log" <&8 >/dev/null
		echo $? > "$d/status_code.tmp" && mv "$d/status_code.tmp" "$d/status_code"
	' job "$d" "$cols" "$rows" "$@" </dev/null >/dev/null 2>&1
	;;
cancel)
	d=$2
	touch "$d/cancelled"
	printf '\003' > "$d/stdin.fifo"  # as a human would; works from outside a container's pid namespace
	;;
kill)
	d=$2
	touch "$d/cancelled"
	kill -TERM -- "-$(cat "$d/pid")"   # pid is only valid in the job's own pid namespace
	;;
*)
	echo "usage: $0 start DIR COMMAND... | cancel DIR | kill DIR" >&2; exit 2
	;;
esac
```

Notes from the prototype:

- **`setsid -f`, never `&`.** A non-interactive shell starts background jobs with
  SIGINT ignored; the command inherits it and ctrl-c does nothing.
- **`pid` has to come from *inside* `script`.** `script` puts the command in a new
  session on its pty, so signalling the wrapper's group misses it: the command then
  only dies when the pty closes, about 2s later, and `script` reports exit 0.
- **Holding `stdin.fifo` open read-write** is what makes it reusable. Opened
  read-only, the first writer to finish would send EOF and close the command's stdin
  for good.
- **Cancel is ctrl-c, not a pid.** It goes through the pty like a keystroke, so it
  works from the host into an app's job without `docker exec` (a FIFO on a bind
  mount is the same FIFO on both sides), and the command gets the signal it expects
  from a human. `kill` remains for commands that ignore ctrl-c, and only works in
  the job's own pid namespace.
- Not tested yet: whether BusyBox's `script` (Alpine) supports `-e`/`-f`. If it
  doesn't, Alpine images need `util-linux-misc`.

## Watching, typing, cancelling, dismissing

Every operation is a plain command on the job directory. That's the parity story:
`spiriconfig jobs ...` wraps these, and `--show` prints them.

| | command |
|---|---|
| list | `ls <job folder>/*/` |
| watch | `tail -c +1 -f <job>/output.log` (from byte 0, then follow: no gap, no duplication) |
| type | `echo y > <job>/stdin.fifo` |
| state | the table above (`flock -n <job>/lock true`, `test -e`) |
| cancel | `spiriconfig-job cancel <job>`, i.e. `printf '\003' > <job>/stdin.fifo`. Same for host and app jobs |
| kill | `spiriconfig-job kill <job>` (only while `lock` is held: after a reboot `pid` is stale); for an app's job, `docker exec <container> spiriconfig-job kill /var/lib/spiriconfig/jobs/<id>` |
| dismiss | `rm -r <job>` (refused while running) |

The viewer must replay into an xterm with exactly the job's columns and at least its
rows. Progress output redraws with relative cursor-up moves, which only replay
faithfully on the same geometry. To fit a small screen, scale the font; don't
change the size.

## CLI

```
spiriconfig jobs list [OWNER]
spiriconfig jobs log JOB [-f]
spiriconfig jobs send JOB TEXT
spiriconfig jobs cancel JOB [--kill]
spiriconfig jobs rm JOB
```

Plugin CLIs keep running in the foreground by default, as a human would expect from
`spiriconfig docker up whoami`. Proposed: a `--background` flag that starts a job
instead and prints its id.

## Web UX

Carried over from the tmux proposal; the job mechanism doesn't change it.

- A **jobs indicator in the header**, always present (count badge, never appears/
  disappears or moves; UI button policy). Tap: list of jobs. Tap a job: the same
  fullscreen view as today.
- The view has two **fixed** buttons: **Hide** (close the view; the job keeps
  running) and **Stop** (cancel: ctrl-c). Both are always there; we never swap in a
  contextual Close/Stop depending on state. Whether Stop escalates to `kill` when
  ctrl-c is ignored, and after how long, is still open.
- **Keystrokes** in the view go to `stdin.fifo`, so `[y/N]` prompts can be answered
  from the web. With several viewers, whoever types goes into the same stream, as
  with two people at one keyboard.
- Finished jobs stay in the list (with state and exit code) until dismissed.
- Confirmation dialogs ("Stop it?") stay ordinary modals. They're questions, not
  jobs.
- Change detection: inotify on the job folders, so new and finished jobs show up
  without a timer. Never schedule that inside a container a refresh clears
  (design.md).

**In-process plugins.** One core helper replaces both `_run_in_dialog` copies:

    jobs.start(owner, title, commands, on_exit=refresh)

The view lives in the shell's slot, not the plugin's refreshable container, so
"refresh deletes my dialog" (design.md) goes away by construction. "Wait for the
human, then refresh" becomes "refresh when the job exits". A list of commands
(appstore) becomes one job running them in sequence, stopping on the first
failure, as today. `on_exit` is only a hint: a job started before a restart has no
callback, and pages already derive their state from `docker ps` on render.

**Apps (container plugins).**

1. Start long work with `spiriconfig-job start /var/lib/spiriconfig/jobs/<id> ...`
   inside the container. SpiriConfig sees it in the app's job folder and shows it in
   the same tray. No `docker exec` is needed to watch, since the log is a file on the
   host.
2. To pop the view open, call `spiriconfig.openJob("<id>")` from `shell.js`. It
   `postMessage`s the parent, and the *shell* opens its own fullscreen view over the
   whole app, not inside the iframe. The shell only honours job ids from the
   job folder of the app that owns the sending iframe.
   - Explicit, not automatic: if SpiriConfig popped a modal for every new job it
     couldn't tell *which browser* started it, and every open tab (and every job
     started over ssh) would get one.
   - Optional: unframed, `shell.js` sees no parent and does nothing. The job still
     runs and `tail -f` is the fallback. SpiriConfig enhances, and the plain command
     is always underneath.

This is new public contract (the wrapper, the mount path, `openJob`), so it falls
under the `spiriconfig.plugin.api` versioning question in
NOTES-out-of-process-plugins.md.

**Decided direction: apps import SpiriConfig as a library.** The plumbing above is
provided by SpiriConfig itself, imported by the app, rather than by each app
re-implementing it. The library sets up the app's routes, and when the app runs
stand-alone (unframed) it shows the app's live jobs itself, instead of the
"`shell.js` does nothing, fall back to `tail -f`" behaviour in point 2. The same
library also fixes session cookies for nested apps (todo.md): a proxied app shares
the shell's origin, so its cookie names/paths have to come from
`X-Forwarded-Prefix`, at any nesting depth, without per-app config. This needs
SpiriConfig split so the app-side part can be imported without the whole shell.

## Open questions

- **Mount path for user installs.** The compose file hard-codes
  `/var/lib/spiriconfig/jobs/<app>`, but a user install's job folder is under
  `$XDG_STATE_HOME`. Interpolate `${SPIRICONFIG_JOBS_DIR}`? Then a human running
  `docker compose up` by hand has to set it.
- **Retention.** Logs now survive a reboot, so finished jobs pile up until dismissed.
  Keep the last N per owner, or delete anything older than N days, when a new job
  starts (no timer)?
- **Which container to `docker exec` into for `kill`.** Only needed for the fallback
  now that cancel is ctrl-c. Probably whichever container mounts that job folder
  (`docker inspect` `.Mounts`), which needs no new label.
- **stdin never reaches EOF.** That's what makes `stdin.fifo` reusable, but it also
  means a prompt waits for an answer instead of failing on `/dev/null` the way it
  does today. That's probably better (the job shows as running and someone can
  answer it), but a command that reads stdin *to EOF* never finishes. Do we need a
  `spiriconfig jobs send --eof` that closes it, and is that one more file the
  wrapper watches?
- **Passwords typed into a job** are not echoed (the prompt turns echo off), so
  they don't reach `output.log`. Anything else typed is in the log for good.
- **Log size and flash wear.** Progress bars redraw constantly, and on an SD/eMMC
  device every redraw is now a write to persistent storage. Cap the log size per job?
- **Concurrent jobs on one thing.** Two `up whoami` at once don't collide on
  disk, but should the second be refused? Probably yes for docker actions, as a
  per-plugin choice.
- **Shipping the wrapper.** In-process: package data, run by `jobs.start`. Apps:
  copy it into the image, or provide it on a read-only mount alongside the run
  folder?

## Considered

- **`stdout.log` / `stderr.log`.** Rejected for the same terminal reason as the
  FIFOs below. Under a pty the program writes both streams to the same terminal and
  the kernel merges them before we see a byte, so they can't be split after the fact.
  Splitting them means stderr isn't the pty. Then `isatty(2)` is false, and tools
  that draw on stderr (`docker compose` progress, most loggers' colour) fall back to
  plain output, and the order between the two files is lost. `output.log` is what
  the person saw, and `status_code` already says whether it failed. If one tool ever
  needs a clean stderr, that tool can write its own file.

- **`stdout.fifo` / `stderr.fifo`.** Output can't be a FIFO. A FIFO hands each byte
  to exactly one reader, so two viewers would each get half the output. With no
  reader, the writer blocks once the pipe buffer (64 KiB) fills, so an unwatched job
  would stall, which defeats the point. And nobody would see the output from
  before they connected. Splitting stdout from stderr also breaks the terminal:
  separate pipes aren't TTYs, so tools stop drawing colour and progress (`docker
  compose` draws its progress on stderr), and the relative order of the two streams
  is lost. The pty merges them on purpose; a file is the only thing many readers can
  replay from the start. stdin is the opposite case: many writers, one reader, no
  history. A FIFO fits that exactly.

- **tmux sessions** (the earlier proposal). Brings re-attach with screen redraw,
  interactivity, and resize, none of which a job needs. Costs: tmux on the host
  and in every image; client/server protocol must match exactly across host and
  container; uid checks between container root and the service user; a fixed socket
  path for parity. Attach-via-`docker exec` works around the protocol mismatch but
  makes every view a `docker exec`.
- **dtach.** Stable protocol and file-permission access, but it records no exit code
  and keeps no scrollback. Once you add a log file and an exit file, the dtach part
  only provides attach, and we don't need attach for a stream that is a file.
- **A job is a container** (`docker run -dit`, `docker logs`, `docker attach`). No
  new dependency, and exit codes and labels come free. But our commands run on the
  host: docker commands would need a `docker:cli` runner with the socket and
  compose dir mounted at the same path, and non-docker ones a privileged `nsenter`.
  The copy-paste line stops being the command a human would run. Apps would need
  the docker socket to start jobs. Checked on docker 29.8: `docker logs` returns the
  raw pty stream from the start; `docker attach` does not replay.
- **In-process job registry** (ring buffer per job). State we own, lost on restart,
  invisible to the CLI, and apps would need an HTTP API to join in.

## First slice

Core `spiriconfig.jobs` (wrapper + state + viewer) + the header tray + the docker
plugin switched over. No app support, no appstore. Enough to put in front of the
colleague.
