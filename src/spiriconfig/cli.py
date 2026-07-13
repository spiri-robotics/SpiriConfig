"""The ``spiriconfig`` command.

The root CLI owns almost nothing. It configures logging, discovers plugins, and
mounts each plugin's Typer app as a subcommand, so ``spiriconfig docker up foo``
is the docker plugin's own code. The only thing the core adds is ``serve``, which
starts the web UI, and ``plugins``, which tells you what got loaded.
"""

from __future__ import annotations

import typer

from spiriconfig import logging
from spiriconfig.config import settings
from spiriconfig.plugins import Plugin, discover

app = typer.Typer(
    name="spiriconfig",
    help="Plugin-based configuration and container management.",
    no_args_is_help=True,
)


def _mount(plugins: list[Plugin]) -> None:
    """Mount each plugin's Typer app as ``spiriconfig <name>``."""
    for plugin in plugins:
        sub = plugin.cli()
        if sub is None:
            continue
        app.add_typer(sub, name=plugin.name, help=plugin.description or None)


@app.command()
def serve() -> None:
    """Start the web UI."""
    from spiriconfig import web

    config = settings()
    web.serve(config)


@app.command("plugins")
def list_plugins() -> None:
    """List the installed plugins."""
    plugins = discover()
    if not plugins:
        typer.echo("No plugins installed.")
        return
    width = max(len(p.name) for p in plugins)
    for plugin in plugins:
        faces = []
        if plugin.cli() is not None:
            faces.append("cli")
        if plugin.has_page:
            faces.append("web")
        typer.echo(f"{plugin.name:<{width}}  {','.join(faces):<8}  {plugin.description}")


# Logging is configured, and plugins mounted, at import time: Typer needs every
# subcommand registered before it can route `spiriconfig docker ...` or list them
# in `--help`, and discovery logs as it goes, so the sinks have to exist first.
# Discovery failures are logged and skipped inside discover(), so one broken
# plugin cannot stop the CLI from starting.
logging.configure(settings())
_mount(discover())


if __name__ == "__main__":
    app()
