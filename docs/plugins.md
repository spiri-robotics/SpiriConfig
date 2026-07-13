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
`description`. Then provide either or both of:

`cli()`
: A `typer.Typer` app, mounted at `spiriconfig <name> ...`.

`page()`
: Called inside a NiceGUI page route at `/<name>`. Use `ui.*` freely.

Both are optional. A plugin with only a `cli()` is fine and gets no nav entry. A
plugin with only a `page()` is *technically* fine and is almost always a mistake
-- see below.

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
