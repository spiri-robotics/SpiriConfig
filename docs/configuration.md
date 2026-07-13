# Configuration

SpiriConfig is configured entirely through environment variables. There is no
config file to learn and none for us to rewrite behind your back, which means a
systemd unit, a shell, a `.env` file, or a container runtime can all configure
it the same way.

If a `.env` file exists in the working directory it is read, but real environment
variables always win.

## Core

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_HOST` | `0.0.0.0` | Address the web UI binds to. |
| `SPIRICONFIG_PORT` | `8080` | Port the web UI binds to. |
| `SPIRICONFIG_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `SPIRICONFIG_LOG_FILE` | *(none)* | Also log to this file, rotated at 10 MB. |
| `SPIRICONFIG_ADVANCED` | `false` | Default for [advanced mode](advanced.md), for someone who has not chosen. |
| `SPIRICONFIG_STORAGE_SECRET` | *(generated)* | Signs the cookie per-person settings are keyed on. Set it, or those settings reset on every restart. |

## Docker plugin

Every plugin namespaces its settings under its own prefix, so plugin config never
collides.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_DOCKER_COMPOSE_DIR` | `/srv/compose` | Directory holding one subdirectory per compose project. |
| `SPIRICONFIG_DOCKER_DOCKER_BIN` | `docker` | The docker executable. Set to `podman` to use podman. |
| `SPIRICONFIG_DOCKER_COMMAND_TIMEOUT` | `300` | Seconds before a captured command is considered hung. |

## What gets logged

Commands that *change* something -- `up`, `down`, `restart`, `pull` -- are logged
at INFO, as the exact shell line that ran:

```text
13:34:30 INFO     docker   $ cd /srv/compose/hello && docker compose -p hello -f compose.yaml up -d
```

Read-only queries (statuses, listings) are logged at DEBUG, so the INFO log stays
a clean record of what SpiriConfig actually did to the machine. Set
`SPIRICONFIG_LOG_LEVEL=DEBUG` to see everything.

## Running as a service

Nothing about SpiriConfig is special here; it is a normal program that reads its
environment.

```ini
[Unit]
Description=SpiriConfig
After=docker.service
Wants=docker.service

[Service]
Environment=SPIRICONFIG_DOCKER_COMPOSE_DIR=/srv/compose
Environment=SPIRICONFIG_PORT=8080
ExecStart=/usr/local/bin/spiriconfig serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

:::{note}
SpiriConfig runs `docker` as whatever user it runs as. That user needs access to
the docker socket. Granting docker socket access is equivalent to granting root,
so run SpiriConfig somewhere you would be comfortable running `docker` by hand.
:::
