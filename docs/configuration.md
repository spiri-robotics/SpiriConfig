# Configuration

SpiriConfig is configured entirely through environment variables. There is no
config file to learn and none for us to rewrite behind your back, which means a
systemd unit, a shell, a `.env` file, or a container runtime can all configure
it the same way.

If a `.env` file exists in the working directory it is read, but real environment
variables always win. Flags to `spiriconfig serve` win over both:

```console
$ spiriconfig serve                         # $SPIRICONFIG_HOST:$SPIRICONFIG_PORT
$ spiriconfig serve 127.0.0.1:8338          # HOST:PORT
$ spiriconfig serve 0.0.0.0                 # HOST, configured port
$ spiriconfig serve :8338                   # PORT, configured host ([::1]:8338 for IPv6)
$ spiriconfig serve --no-login-required     # SPIRICONFIG_AUTH=none
```

## Core

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_HOST` | `127.0.0.1` | Address the web UI binds to. Loopback by default; set `0.0.0.0` to expose it on the network. |
| `SPIRICONFIG_PORT` | `8337` | Port the web UI binds to. |
| `SPIRICONFIG_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `SPIRICONFIG_LOG_FILE` | *(none)* | Also log to this file, rotated at 10 MB. |
| `SPIRICONFIG_ADVANCED` | `false` | Default for [advanced mode](advanced.md), for someone who has not chosen. |
| `SPIRICONFIG_STORAGE_SECRET` | *(generated)* | Signs the cookie per-person settings are keyed on. Set it, or those settings reset on every restart. |
| `SPIRICONFIG_AUTH` | `pam` | `none` or `pam`. `pam` puts a login in front of every page. See [Authentication](#authentication). |
| `SPIRICONFIG_AUTH_SERVICE` | `login` | PAM service (a file under `/etc/pam.d/`) to authenticate against. |
| `SPIRICONFIG_AUTH_GROUP` | `wheel` | Group whose members may log in, *when SpiriConfig runs as root*. `sudo` on Debian. |
| `SPIRICONFIG_TLS` | `auto` | `auto` or `off`. `auto` serves a self-signed cert when exposed off loopback; `off` stays plain HTTP. See [Transport (TLS)](#transport-tls). |
| `SPIRICONFIG_TLS_CERT` | *(none)* | Path to a TLS cert to serve instead of a generated one. Set with `SPIRICONFIG_TLS_KEY`. |
| `SPIRICONFIG_TLS_KEY` | *(none)* | Path to the private key for `SPIRICONFIG_TLS_CERT`. |

## Authentication

By default every page requires a login, checked against the host's PAM stack — the
same accounts that can `ssh` in or `sudo`, no separate user list of ours. Set
`SPIRICONFIG_AUTH=none` to drop that gate, which only makes sense on loopback in a
checkout with no other users on the box; `spiriconfig serve` warns if you bind a
non-loopback address and turn it off.

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

## Transport (TLS)

With `auth=pam` the login sends a real host password, so it must not travel over
plain HTTP. These are appliances with no reverse proxy in front, so the TLS lives
here: bind a non-loopback address and, by default (`SPIRICONFIG_TLS=auto`),
SpiriConfig generates a **self-signed** cert and serves HTTPS. The cert is written
once to the service's state directory (`/var/lib/spiriconfig/tls` as a system
service, `~/.local/state/spiriconfig/tls` as a user one) and reused across
restarts, so the browser warning is a one-time approval, not a per-boot one. A
loopback checkout stays on plain HTTP, where a cert warning would be pure friction.

Be clear about what self-signed buys. It encrypts the wire, so a **passive**
eavesdropper on the LAN cannot read the password or the session cookie. It does
**not** stop an **active** man-in-the-middle: the browser warns, the operator
clicks through, and an attacker presenting *their own* self-signed cert is clicked
through just the same. Self-signed is encryption, not identity.

For a deployment that must resist an active attacker, give SpiriConfig a cert the
browser can *validate* — a per-device cert from a CA your fleet trusts — with
`SPIRICONFIG_TLS_CERT` and `SPIRICONFIG_TLS_KEY`. That is the only path where a
substitute cert is rejected instead of clicked through, and the only one where
`Strict-Transport-Security` is sent (HSTS on a self-signed origin would forbid the
very click-through the operator needs and lock them out).

Set `SPIRICONFIG_TLS=off` only when something in front already terminates TLS —
a reverse proxy — so SpiriConfig should not encrypt a second time. On a
non-loopback bind with `pam` and `tls=off`, `spiriconfig serve` warns that
passwords are going out in the clear.

## Docker plugin

Every plugin namespaces its settings under its own prefix, so plugin config never
collides.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_DOCKER_COMPOSE_DIR` | `/srv/compose` as root, `~/spiri-apps` otherwise | Directory holding one subdirectory per compose project. Installed apps are symlinks in here. |
| `SPIRICONFIG_DOCKER_DOCKER_BIN` | `docker` | The docker executable. Set to `podman` to use podman. |
| `SPIRICONFIG_DOCKER_COMMAND_TIMEOUT` | `300` | Seconds before a captured command is considered hung. |

## App store plugin

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPIRICONFIG_APPSTORE_STORES` | `[]` | JSON list of git URLs, or local paths, of [app stores](appstore.md) to offer before any is added. |
| `SPIRICONFIG_APPSTORE_STORE_DIR` | `/var/lib/spiriconfig/stores` as root, `~/.local/share/spiriconfig/stores` otherwise | Where store clones live. Not a cache: your edits to installed apps are commits in here. |
| `SPIRICONFIG_APPSTORE_GIT_BIN` | `git` | The git executable. |
| `SPIRICONFIG_APPSTORE_COMMAND_TIMEOUT` | `300` | Seconds before a git command is considered hung. |

## Where things live by default

The path defaults depend only on who runs SpiriConfig, and are the same ones
`spiriconfig install` writes into the service's environment file -- so
`uvx spiriconfig serve` and the installed service, run as the same user, see the
same apps:

| | root | anyone else |
| --- | --- | --- |
| Installed apps (`SPIRICONFIG_DOCKER_COMPOSE_DIR`) | `/srv/compose` | `~/spiri-apps` |
| Store clones (`SPIRICONFIG_APPSTORE_STORE_DIR`) | `/var/lib/spiriconfig/stores` | `$XDG_DATA_HOME/spiriconfig/stores` (`~/.local/share/...`) |

The compose directory is created by the first app install if it does not exist.

**A checkout overrides them.** Running out of a checkout should not start
managing the containers on the developer's actual machine, so
`./scripts/test-data.sh` builds a disposable `test_data/` tree with an example app
store in it, and appends lines to the checkout's (gitignored) `.env` pointing all
three app settings at it.

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

`spiriconfig install` writes a unit like the one below and enables it for you --
see [Installing SpiriConfig](install.md). This section shows the unit itself,
because nothing about SpiriConfig is special here: it is a normal program that
reads its environment, so you can also just write the unit by hand.

```ini
[Unit]
Description=SpiriConfig
After=docker.service
Wants=docker.service

[Service]
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
