# SpiriConfig

Plugin-based configuration and container management, built around one rule:

> **Anything SpiriConfig can do, you must be able to do without it.**

Press **Up** on a stack in the web UI and SpiriConfig runs this, showing you the
line as it goes, with a button to copy it:

```console
$ cd /srv/compose/whoami && docker compose -p whoami -f compose.yaml up -d
```

No database, no registry, no bespoke on-disk format. If SpiriConfig vanished
tomorrow, everything it manages would keep working -- and you would already know
the commands to manage it.

## Quickstart

From a fresh checkout, with nothing to configure and nothing touched outside this
directory:

```console
$ uv sync
$ ./scripts/test-data.sh                        # a compose dir + an example app store
$ uv run spiriconfig appstore sync
$ uv run spiriconfig appstore install whoami
$ uv run spiriconfig docker up whoami
$ curl localhost:8080
Hostname: 2d5bcd6f2629
GET / HTTP/1.1
```

`test_data/` is gitignored and disposable, and the default settings point at it --
so trying SpiriConfig out cannot start managing the containers on your real
machine. On a real machine, point it somewhere real:

```console
$ export SPIRICONFIG_DOCKER_COMPOSE_DIR=/srv/compose
$ spiriconfig serve            # web UI on http://localhost:8080
```

Adding a service is making a directory. No CLI required -- that is the point:

```console
$ mkdir -p /srv/compose/whoami
$ $EDITOR /srv/compose/whoami/compose.yaml
```

From the shell:

```console
$ spiriconfig docker list
whoami   running
nextcloud  stopped

$ spiriconfig docker up whoami
$ spiriconfig docker logs whoami -f
```

Not sure what a command will do? Ask, without running it:

```console
$ spiriconfig docker up whoami --show
cd /srv/compose/whoami && docker compose -p whoami -f compose.yaml up -d
```

Because the compose project name is the directory name, these are the same
containers you get from running compose yourself -- SpiriConfig and a plain shell
can manage the same stack on the same afternoon without confusing each other.

## Configuration

Environment variables only. See [the docs](docs/configuration.md) for the full
list; the one you need is:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_DOCKER_COMPOSE_DIR` | `test_data/compose` | One subdirectory per compose project. Set it to `/srv/compose` on a real machine. |

The defaults are relative on purpose: running out of a checkout should not start
managing the containers on your actual box. See [configuration](docs/configuration.md).

## Plugins

The docker plugin is the only one that ships, and it is not special: it is
discovered through the `spiriconfig.plugins` entry point group exactly as yours
would be.

```toml
[project.entry-points."spiriconfig.plugins"]
tailscale = "spiriconfig_tailscale:TailscalePlugin"
```

Install the package and it appears in the CLI and the web UI. See
[docs/plugins.md](docs/plugins.md) for the interface and the rules, and
[docs/design.md](docs/design.md) for why the rules exist.

## Development

```console
$ uv sync
$ uv run pytest                                   # 92 tests
$ uv run sphinx-build -b html docs docs/_build    # docs
```

Tests that need a docker daemon are skipped when there is not one, so the suite
passes on a laptop with no docker and still means something on a machine that has
it. Most of them never need one anyway: the plugin *builds* commands and the tests
assert on the command line, which is the actual contract.
