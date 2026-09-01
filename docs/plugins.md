# Writing a plugin

Everything a user would call a feature lives in a plugin. The core only discovers
plugins, gives them somewhere to put a CLI and a page, and runs commands for
them. The bundled docker plugin is loaded by exactly the machinery described
here -- there is no privileged built-in path, so if it works for docker it works
for yours.

## A plugin in full

```python
# src/spiriconfig_tailscale/__init__.py
import typer
from nicegui import ui

from spiriconfig.commands import Command, run
from spiriconfig.plugins import Plugin

cli_app = typer.Typer(help="Manage tailscale.")


def status_command() -> Command:
    """Build the command. Do not run it -- see the design notes."""
    return Command(argv=["tailscale", "status"])


@cli_app.command()
def status() -> None:
    """Show tailscale status."""
    typer.echo(run(status_command()).stdout)


class TailscalePlugin(Plugin):
    name = "tailscale"
    title = "Tailscale"
    description = "Show and manage the tailscale connection."
    icon = "vpn_lock"

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        ui.label("Tailscale").classes("text-2xl font-bold")
        ui.code(run(status_command()).stdout)
```

Register it:

```toml
[project.entry-points."spiriconfig.plugins"]
tailscale = "spiriconfig_tailscale:TailscalePlugin"
```

Install it, and it is there:

```console
$ pip install -e .
$ spiriconfig plugins
docker     cli,web   Start, stop, and edit docker compose projects.
tailscale  cli,web   Show and manage the tailscale connection.

$ spiriconfig tailscale status
```

Installing the distribution is what makes a plugin available; uninstalling it is
what removes it. There is no plugin registry to edit.

## The interface

Subclass {class}`~spiriconfig.plugins.Plugin` and set `name`, `title`, and
`description`. Optionally set `icon` to a [Material icon](https://fonts.google.com/icons)
name, which is what the sidebar shows beside your title; it defaults to a generic
plug. Then provide either or both of:

`cli()`
: A `typer.Typer` app, mounted at `spiriconfig <name> ...`.

`page()`
: Called inside a NiceGUI page route at `/<name>`. The shell gives you the whole
  main area -- everything right of the sidebar is yours, heading included.

Both are optional. A plugin with only a `cli()` is fine and gets no sidebar entry
(it still appears on the index page). A plugin with only a `page()` is
*technically* fine and is almost always a mistake -- see below.

## The rules

**1. Do the work by running the command a human would run.**

Build a {class}`~spiriconfig.commands.Command` and hand it to
{func}`~spiriconfig.commands.run` or {func}`~spiriconfig.commands.stream`. Do not
reach for a Python API when a command line exists -- not `docker-py`, not
`requests` against a local socket. The point is not that subprocesses are elegant;
it is that a command is something the user can read, copy, and run without us. A
Python API call is not.

**2. Build commands separately from running them.**

Note that `status_command()` above returns a `Command` rather than running one.
This is what lets the UI show the user what it is about to do, lets `--show`
print it, and lets your tests assert on the exact command line without the
underlying tool installed anywhere. The docker plugin tests do exactly this, and
most of them need no docker daemon at all.

**3. Never make the web UI the only way to do something.**

If your page has a button, there must be a way to do that thing from a shell --
ideally your `cli()`, but "run this documented command" counts too. A feature that
only exists behind a mouse click is a feature the user cannot script, cannot
automate, and cannot fix at 3am over a broken SSH connection.

**4. Put developer-facing clutter behind advanced mode -- and nothing else.**

```python
from spiriconfig import advanced

ui.button("Up", on_click=...)          # everyone
with advanced.only():
    ui.button("Edit", on_click=...)    # developers only
```

[Advanced mode](advanced.md) is a display filter, not a permission -- the CLI
still does everything, whatever the toggle says. If you are hiding a button
because someone *should not be allowed* to press it, you want authorisation, and
that is a unix account, not this.

**5. Log through loguru, bound to your plugin.**

```python
from loguru import logger
log = logger.bind(plugin="tailscale")

run(status_command(), log=log)
```

Do not configure sinks; the core does that.

**6. Take your settings from the environment.**

Namespace them under your own prefix, so plugins cannot collide:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class TailscaleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPIRICONFIG_TAILSCALE_")
    tailscale_bin: str = "tailscale"
```

## Failure is contained

A plugin that fails to import, blows up when constructed, or is not actually a
`Plugin` is logged and skipped. It does not take down the CLI or the web UI --
the rest of the app loads, and you get a loud reason why yours is missing. A page
that raises while rendering is caught and shown as an error on its own page.

You still have to write a working plugin. But a half-written one will not lock
you out of the machine while you do.

(out-of-process-plugins)=
## Out-of-process plugins

Everything above assumes a plugin shares our Python interpreter. It doesn't
have to: a plugin can also be a container that serves HTTP, written in any
language, with its own dependencies entirely separate from ours. SpiriConfig
discovers it and proxies it into the shell the same way it does everything
else -- by reading docker.

### Declare yourself with labels

No code changes turn a container into a plugin, just labels on an existing
compose service:

```yaml
services:
  ui:
    image: ghcr.io/you/spiriconfig-tailscale
    labels:
      spiriconfig.plugin.name: tailscale
      spiriconfig.plugin.title: Tailscale
      spiriconfig.plugin.icon: vpn_lock
      spiriconfig.plugin.port: "8080"
```

`name` and `port` are required; `title` defaults to `name` and `icon` to a
generic web icon. SpiriConfig finds it with
`docker ps --filter label=spiriconfig.plugin.name` -- the same command you
could run yourself -- and rescans on a loop, so installing, starting, or
stopping the container is all it takes. There is no separate registration
step and nothing of ours to keep in sync.

See `examples/store/whoami/compose.yaml` for a working, minimal one.

### Installed like any other app

A plugin container is a compose app with labels on it, so it is installed,
updated, and removed exactly like any other [app store](appstore.md) entry.
There is no separate plugin install path to learn.

### Reached through us, not a published port

No host port is published. SpiriConfig reaches the container on its own
docker network IP and reverse-proxies it at `/plugin/<name>/...`, same origin
as the shell -- so it inherits the login gate, cookies, and the theme for
free, and nobody allocates or firewalls a port per plugin. Both plain HTTP and
WebSocket traffic are proxied, so a live UI (NiceGUI's socket.io, for
instance) works over it.

The shell frames the plugin at `/app/<name>` in an iframe, listed in the same
sidebar as every in-process plugin. A name whose container isn't running gets
an honest "not available" card instead of a blank frame.

### Working behind a prefix

The proxy sets `X-Forwarded-Prefix: /plugin/<name>` on every request. A
framework that honours it emits correct URLs with no idea it's behind a proxy
-- NiceGUI does, thoroughly, and needs no code changes to port. A plugin
author's most likely first bug is one that doesn't: a hardcoded absolute path
(`/static/…`, `ui.link(target="/routes")`) escapes the prefix, where a
relative one (`routes`, `./routes`) stays inside it.

### One script tag, for deep links

```html
<script src="/plugin-sdk/shell.js"></script>
```

Include it and the shell's address bar tracks your page as the user navigates
inside the iframe, so a deep link, a reload, and the back button all behave.
It works via same-origin `history.replaceState` -- no handshake or
cooperation needed beyond the tag. It's optional: skip it and the plugin still
works, its URL in the address bar just doesn't move.

### The CLI face

A container plugin doesn't register a `spiriconfig <name> ...` subcommand --
its entrypoint is its CLI:

```console
$ docker exec spiriconfig-tailscale tailscale status
```

A command a human could run without SpiriConfig at all, the same bar every
in-process plugin's `cli()` is held to.

### Trust model

A plugin container is trusted code the operator chose to install, not a
sandboxed guest -- it may mount `/var/run/docker.sock`, `/etc`, or anything
else it needs, and the reverse proxy is plumbing, not a security boundary. See
[Plugins are not sandboxed](design.md#a-plugin-can-also-be-a-container-and-is-not-sandboxed)
for the reasoning.
