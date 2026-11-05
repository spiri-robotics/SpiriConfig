# Installing SpiriConfig

Every other service SpiriConfig manages lives in a container. SpiriConfig itself
cannot: the thing that starts your containers on boot has to live on the host,
start on boot itself, and update in place. So it installs itself as a systemd
service.

Like everything else here, the installer works by building the commands you could
have run by hand -- a `uv tool install`, a unit file, `systemctl enable` -- and it
will show you every one of them before it runs anything. If SpiriConfig were not
here, you would set the machine up with exactly these steps; the installer just
types them for you.

`spiriconfig install` uses [uv](https://docs.astral.sh/uv/) to place the program,
so uv needs to be on the machine first.

## Install it

From a machine that already has `spiriconfig` on its path (a checkout, or a
throwaway `uvx spiriconfig`), run:

```console
$ spiriconfig install
```

That installs the latest release from PyPI, writes a systemd unit and an
environment file, and enables and starts the service. When it finishes, the web
UI is running and will come back on every boot.

To see precisely what it will do without touching anything, add `--show`. It
prints the `uv tool install`, the full unit file, the environment file with its
path, and the `systemctl` commands -- everything, in order, ready to copy:

```console
$ spiriconfig install --show
```

## Two scopes: root or a single user

The installer looks at who you are and picks one of two installs. You do not
choose; the choice *is* the security model (see [Authentication](configuration.md#authentication)).

- **As root**, it installs a system service under `/etc/systemd/system` that runs
  as root. Root can verify any account's password, so this is the only install
  where the PAM login is genuinely multi-user and the [users plugin](plugins.md)
  can manage accounts.
- **As a normal user**, it installs a `systemctl --user` service under your
  `~/.config` that runs as you. PAM can then only authenticate your one account --
  a single-operator box. It also runs `loginctl enable-linger` for you, without
  which a user service stops the moment you log out, which on a headless drone
  means it never really runs.

Either way, the login gate defaults to PAM. A single-operator box still gets a
password in front of it; it simply only ever accepts that one account.

## What it writes

Two files, both plain text you can read and edit:

| | System (root) | User |
| --- | --- | --- |
| Unit | `/etc/systemd/system/spiriconfig.service` | `~/.config/systemd/user/spiriconfig.service` |
| Environment | `/etc/spiriconfig/config.env` | `~/.config/spiriconfig/config.env` |

The environment file is one `SPIRICONFIG_*` variable per line -- the same settings
documented under [Configuration](configuration.md). To change how the installed
service runs, edit that file and restart:

```console
$ systemctl restart spiriconfig          # or: systemctl --user restart spiriconfig
```

## Options

The install questions all have flags, with sensible defaults:

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Address to bind. Loopback by default; a real address needs a login. |
| `--port` | `8080` | Port to bind. |
| `--auth` | `pam` | Login gate: `pam` or `none`. |
| `--auth-group` | `wheel` | Group whose members may log in, when run as root. |
| `--compose-dir` | `/srv/compose` (root), `~/spiri-apps` (user) | Where the docker plugin looks for apps. |
| `--editable`, `-e` | off | Install the source in editable mode (for developing the installer). |

The first positional argument is what to install, and it is anything uv accepts:

```console
$ spiriconfig install                                  # latest release
$ spiriconfig install 'spiriconfig==0.1.0'             # a pinned version
$ spiriconfig install 'git+https://git.spirirobotics.com/Spiri/SpiriConfig@main'
$ spiriconfig install -e .                             # a working checkout
```

:::{note}
The installer refuses one combination outright: `--auth none` on a non-loopback
address. That would leave the machine reachable off-box with no login and full
control, and an install is durable -- set up that way, it stays that way. Bind to
loopback, or keep the login (the default). This is the same danger `spiriconfig
serve` only *warns* about, escalated to a refusal because a service is permanent.
:::

## Updating

```console
$ spiriconfig update
```

This upgrades the program via `uv tool upgrade` and restarts the service. The
restart is handed to systemd to perform a moment later rather than run inline,
because the process being restarted is the very one running the command -- an
inline restart would cut `update` off before it could tell you how it went.

`uv tool upgrade` re-resolves versions, which is what you want for a release
install. A git-branch install is a different case: a moved branch is the same
"version" to uv, so pass `--reinstall` to force a refetch of the new commit:

```console
$ spiriconfig update --reinstall
```

`--show` works here too, printing the upgrade and restart commands without running
them.

## Managing the service afterward

Nothing about the installed service is special -- it is an ordinary systemd unit.
Use `systemctl` (add `--user` for a user install) as you would for anything else:

```console
$ systemctl status spiriconfig
$ systemctl restart spiriconfig
$ systemctl disable --now spiriconfig    # stop it and remove it from boot
```

To remove SpiriConfig entirely, disable the service, delete the unit and
environment files listed above, and run `uv tool uninstall spiriconfig`.
