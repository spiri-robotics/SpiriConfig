# SpiriConfig

Plugin-based configuration and container management.

SpiriConfig gives you a web UI and a CLI over the services running on a machine.
It is built around one rule:

> **Anything SpiriConfig can do, you must be able to do without it.**

This is not a slogan, it is a design constraint that shows up in the code. When
you press **Up** on a stack in the web UI, SpiriConfig runs:

```console
$ cd /srv/compose/whoami && docker compose -p whoami -f compose.yaml up -d
```

...and shows you that line while it runs, with a button to copy it. There is no
hidden state, no database, and no bespoke on-disk format. If SpiriConfig were
uninstalled tomorrow, everything it manages would keep working, and you would
already know the commands to manage it.

That property is called *progressive enhancement*: the tool is a convenience on
top of a system that is fully usable without it, never a dependency the system
grows into.

## What that buys you

You can manage a stack from SpiriConfig, from a plain shell, or from both on the
same afternoon, and nothing gets confused. The compose project name we pass is
the directory name, so the containers SpiriConfig starts are the *same*
containers you get from running compose yourself in that directory:

```console
$ spiriconfig docker up whoami      # these two do
$ cd /srv/compose/whoami            # exactly the same
$ docker compose up -d                # thing
```

Not sure what a button will do? Ask, without running it:

```console
$ spiriconfig docker up whoami --show
cd /srv/compose/whoami && docker compose -p whoami -f compose.yaml up -d
```

## Getting started

```console
$ uv sync
$ export SPIRICONFIG_DOCKER_COMPOSE_DIR=/srv/compose
$ spiriconfig serve
```

The web UI is at <http://localhost:8080>. To add a service, make a directory with
a compose file in it -- no CLI required, that is the point:

```console
$ mkdir -p /srv/compose/whoami
$ $EDITOR /srv/compose/whoami/compose.yaml
```

It shows up in the UI on the next refresh.

```{toctree}
:maxdepth: 2
:caption: Contents

install
configuration
advanced
docker
appstore
plugins
design
api
```
