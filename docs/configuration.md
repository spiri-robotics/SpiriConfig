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
| `SPIRICONFIG_HOST` | `127.0.0.1` | Address the web UI binds to. Loopback by default; set `0.0.0.0` to expose it on the network. |
| `SPIRICONFIG_PORT` | `8080` | Port the web UI binds to. |
| `SPIRICONFIG_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `SPIRICONFIG_LOG_FILE` | *(none)* | Also log to this file, rotated at 10 MB. |
| `SPIRICONFIG_ADVANCED` | `false` | Default for [advanced mode](advanced.md), for someone who has not chosen. |
| `SPIRICONFIG_STORAGE_SECRET` | *(generated)* | Signs the cookie per-person settings are keyed on. Set it, or those settings reset on every restart. |
| `SPIRICONFIG_AUTH` | `none` | `none` or `pam`. `pam` puts a login in front of every page. See [Authentication](#authentication). |
| `SPIRICONFIG_AUTH_SERVICE` | `login` | PAM service (a file under `/etc/pam.d/`) to authenticate against. |
| `SPIRICONFIG_AUTH_GROUP` | `wheel` | Group whose members may log in, *when SpiriConfig runs as root*. `sudo` on Debian. |

## Authentication

By default the web UI has no login: on loopback, in a checkout, that is the point.
Set `SPIRICONFIG_AUTH=pam` and every page requires a password, checked against the
host's PAM stack — the same accounts that can `ssh` in or `sudo`, no separate user
list of ours. Turn it on for any deployment the UI can be reached from off-box;
`spiriconfig serve` warns if you bind a non-loopback address and leave it off.

Who can log in depends on whether SpiriConfig runs as root, because only root can
verify another account's password:

- **As root**, it can authenticate any system user, so a group gates who counts as
  an administrator. Membership of `SPIRICONFIG_AUTH_GROUP` (default `wheel`) is the
  gate — set it to `sudo` on Debian, or to whatever group your admins are in.
- **Not as root**, it can only authenticate the one account it runs as. That is the
  only login the page will accept (it prefills the name for you), and the group
  setting does not apply.

`login` is used as the PAM service because it exists on essentially every system.
A deployment that wants its own policy can drop a `/etc/pam.d/spiriconfig` file and
set `SPIRICONFIG_AUTH_SERVICE=spiriconfig`.

Set `SPIRICONFIG_STORAGE_SECRET` when auth is on: it signs the session cookie, and
without a stable one everybody is logged out every time the process restarts.

:::{note}
This is authentication, not authorization. It only decides who may log in. Once
logged in, everyone drives the same process with the same access — anyone you let
in can do anything the UI can. Per-user permissions are not part of the model. See
[design](design.md).
:::

## Docker plugin

Every plugin namespaces its settings under its own prefix, so plugin config never
collides.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_DOCKER_COMPOSE_DIR` | `test_data/compose` | Directory holding one subdirectory per compose project. |
| `SPIRICONFIG_DOCKER_DOCKER_BIN` | `docker` | The docker executable. Set to `podman` to use podman. |
| `SPIRICONFIG_DOCKER_COMMAND_TIMEOUT` | `300` | Seconds before a captured command is considered hung. |

## App store plugin

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_APPSTORE_STORES` | `["test_data/example-store"]` | JSON list of git URLs, or local paths, of [app stores](appstore.md). |
| `SPIRICONFIG_APPSTORE_STORE_DIR` | `test_data/stores` | Where store clones live. Not a cache: your edits to installed apps are commits in here. |
| `SPIRICONFIG_APPSTORE_GIT_BIN` | `git` | The git executable. |
| `SPIRICONFIG_APPSTORE_COMMAND_TIMEOUT` | `300` | Seconds before a git command is considered hung. |

## Why the defaults are relative

`test_data/compose`, not `/srv/compose`. Running SpiriConfig out of a checkout
should not silently start managing the containers on the developer's actual
machine, and a default of `/srv/compose` would do exactly that the first time
someone typed `uv run spiriconfig docker list` to see what it did.

So the defaults point somewhere harmless and local, `./scripts/test-data.sh`
builds that tree with an example app store in it, and the whole thing is
gitignored and disposable.

**A deployment sets absolute paths.** That is what the systemd unit below is for,
and `/srv/compose` and `/var/lib/spiriconfig/stores` are the conventional ones.

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
# Absolute, every one. The defaults are relative for the benefit of a checkout,
# which makes them meaningless to a service whose working directory is not one.
Environment=SPIRICONFIG_DOCKER_COMPOSE_DIR=/srv/compose
Environment=SPIRICONFIG_APPSTORE_STORE_DIR=/var/lib/spiriconfig/stores
Environment=SPIRICONFIG_APPSTORE_STORES=["https://github.com/spiri/spiri-apps"]
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
