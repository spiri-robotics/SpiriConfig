# The docker plugin

Manages the docker compose projects in one directory.

## The model

A **stack** is a subdirectory of `SPIRICONFIG_DOCKER_COMPOSE_DIR` that contains a
compose file. That is the whole model.

```text
/srv/compose/
├── telemetry/
│   └── compose.yaml
├── grafana/
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
$ spiriconfig docker up telemetry          # docker compose up -d
$ spiriconfig docker down telemetry        # docker compose down
$ spiriconfig docker restart telemetry     # docker compose restart
$ spiriconfig docker pull telemetry        # docker compose pull
$ spiriconfig docker logs telemetry -f     # docker compose logs --tail=200 --follow
$ spiriconfig docker ps telemetry          # docker compose ps
$ spiriconfig docker config telemetry      # print the path to the compose file
```

`config` prints a path rather than opening an editor, so that your own tools can
do the part they are better at:

```console
$ $EDITOR "$(spiriconfig docker config telemetry)"
```

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
