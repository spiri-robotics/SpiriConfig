# The docker plugin

Manages the docker compose projects in one directory.

## The model

A **stack** is a subdirectory of `SPIRICONFIG_DOCKER_COMPOSE_DIR` that contains a
compose file. That is the whole model.

```text
/srv/compose/
├── whoami/
│   └── compose.yaml
├── nextcloud/
│   ├── compose.yaml
│   └── .env
└── notes.txt          <- ignored: not a directory
```

Any of `compose.yaml`, `compose.yml`, `docker-compose.yaml`, or
`docker-compose.yml` works, with the same precedence docker compose itself uses.
A directory with no compose file is ignored, not an error.

There is no "install", "register", or "enable" step, because there is no state
for us to keep in sync with the disk. Adding a service is creating a directory;
removing one is deleting it. `mkdir` is a supported workflow.

## Running and not running

There is no concept of "enabled" beyond what docker already tracks. A stack is
either up or it is not, and `docker compose` is the authority on which:

- **running** -- every container is up
- **partial** -- neither all up nor all dead: one crashed, one is restarting, or
  the stack is still coming up
- **stopped** -- containers exist, and all of them are dead
- **down** -- no containers exist yet

:::{note}
`partial` covers "in flux", not just "broken". `docker compose up -d` returns as
soon as it has *started* the containers, which is a moment before the daemon
reports them as running -- so a status check immediately after an up can honestly
see `partial`. Ask again shortly.
:::

## Commands

Each of these maps to one `docker compose` invocation. Add `--show` to any of
them to print that invocation instead of running it.

```console
$ spiriconfig docker list                 # every project, and its status
$ spiriconfig docker up whoami          # docker compose up -d
$ spiriconfig docker down whoami        # docker compose down
$ spiriconfig docker restart whoami     # docker compose restart
$ spiriconfig docker pull whoami        # docker compose pull
$ spiriconfig docker logs whoami -f     # docker compose logs --tail=200 --follow
$ spiriconfig docker ps whoami          # docker compose ps
$ spiriconfig docker config whoami      # print the path to the compose file
$ spiriconfig docker exec whoami whoami   # docker compose exec whoami /bin/sh
$ spiriconfig docker attach whoami whoami # docker compose attach whoami
```

`config` prints a path rather than opening an editor, so that your own tools can
do the part they are better at:

```console
$ $EDITOR "$(spiriconfig docker config whoami)"
```

## Getting inside a container

Two buttons, both [advanced](advanced.md)-only, both opening a real terminal in
the browser. They are not the same thing, and the difference is worth knowing
before you press one.

**Exec** starts a *new* process next to the app — a shell, by default. You can
exit it and nothing has happened to the container. It defaults to `/bin/sh`
rather than `bash` because `sh` is the shell a container actually has: Alpine
images ship busybox and no bash at all. The command is a text box, not a shell
button, because `docker compose exec` runs anything the image has:

```console
$ spiriconfig docker exec grafana grafana                  # a shell
$ spiriconfig docker exec grafana grafana -- ls -la /etc   # or anything else
```

Put `--` before a command with options of its own, or they will be read as ours.

**Attach** connects you to the process the container already *is* — pid 1, the
app itself. It is how you reach a REPL an app serves on its stdin, and how you
see output that never goes near the logs. Your keystrokes go to the app, so what
Ctrl-C does is between you and that process. We pass no `--sig-proxy` and no
`--detach-keys`: it is the plain command, and it behaves exactly as it does when
you type it.

Both only offer services that are **running**, since both need a live process.

(the-orphan)=

### The orphan, and what we do about it

:::{note}
**`docker compose exec` does not clean up after itself, and it never has.**

When the client that started an exec goes away, the process it started inside the
container keeps running. Forever — there is no timeout, and nothing reaps it
short of the container stopping. The exec'd process is not the client's child; it
belongs to the container, and the daemon does not tear an exec down when the API
connection drops. The docker API can *start* an exec and *inspect* an exec, and
offers no way at all to *kill* one.

This is [moby/moby#9098][9098], open since **2014**, along with [#29700][29700]
(the orphan, reparented to PPID 0) and [#35703][35703] (the request for a
`docker exec kill` that would make all of this unnecessary).

Nobody meets it at a terminal, because at a terminal you type `exit`, and a shell
that exits is clean. **A browser tab has no `exit`** — closing it *is* the
hangup. So the rare accident becomes the ordinary path, and without help every
visit to the Exec button would leave a shell running inside your container.

So the web session runs the exec wrapped in a tiny supervisor that records its pid
inside the container, and kills it when you close the dialog or the tab. The
command line the page *shows* you is the plain one — the one the CLI runs, and the
one worth copying — because that line should be about your container, not about
our workaround.

It is worth knowing that the CLI does **not** do this. `spiriconfig docker exec`
is exactly `docker compose exec`, orphan and all: if you kill your terminal
instead of typing `exit`, you will leave a shell behind, precisely as you would
running docker by hand.
:::

[9098]: https://github.com/moby/moby/issues/9098
[29700]: https://github.com/moby/moby/issues/29700
[35703]: https://github.com/moby/moby/issues/35703

## Editing a compose file

The web UI can edit a stack's compose file. Two things about how it saves:

**Your file is text, and stays text.** We never parse the YAML and write it back
out, so your comments, ordering, and formatting survive a save untouched.

**A file that compose would reject is never written.** On save we check that it
parses as YAML, then ask `docker compose config` whether it is actually valid. If
either check fails, the original file is restored and you get the error. A bad
save cannot leave you with a stack that will not start.

This is the one place SpiriConfig does something a plain editor does not, and it
is still not a lock-in: the file on disk is an ordinary compose file, and `vim`
remains a completely valid way to edit it.

## Using podman

The plugin shells out to whatever `SPIRICONFIG_DOCKER_DOCKER_BIN` names, and only
uses `compose` subcommands:

```console
$ export SPIRICONFIG_DOCKER_DOCKER_BIN=podman
```
