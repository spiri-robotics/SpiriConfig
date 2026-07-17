"""The ``spiriconfig`` command.

The root CLI owns almost nothing. It configures logging, discovers plugins, and
mounts each plugin's Typer app as a subcommand, so ``spiriconfig docker up foo``
is the docker plugin's own code. The only thing the core adds is ``serve``, which
starts the web UI, and ``plugins``, which tells you what got loaded.
"""

from __future__ import annotations

import getpass
import secrets
from pathlib import Path
from typing import Annotated

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


def _write_file(path: Path, content: str) -> None:
    """Create parent directories and write a file, for the install's two files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@app.command()
def install(
    source: Annotated[
        str,
        typer.Argument(
            help="What to install: a PyPI spec, a git+ URL, or '.' with --editable."
        ),
    ] = "spiriconfig",
    compose_dir: Annotated[
        Path | None,
        typer.Option(help="Where the docker plugin looks for apps. [system: /srv/compose]"),
    ] = None,
    auth: Annotated[
        str, typer.Option(help="Login gate: 'pam' (the default) or 'none'.")
    ] = "pam",
    host: Annotated[str, typer.Option(help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8080,
    auth_group: Annotated[
        str, typer.Option(help="Group whose members may log in, when run as root.")
    ] = "wheel",
    editable: Annotated[
        bool, typer.Option("--editable", "-e", help="Install the source in editable mode.")
    ] = False,
    show: Annotated[
        bool, typer.Option("--show", help="Print everything install would do, and stop."),
    ] = False,
) -> None:
    """Install SpiriConfig as a systemd service on this machine.

    As root it becomes a system service that runs as root -- the only install
    where the PAM login is multi-user and the users plugin can manage accounts. As
    a normal user it becomes a `systemctl --user` service running as you, so the
    login only ever authenticates your own account.

    `--show` prints the exact `uv tool install`, the unit file, the env file, and
    the `systemctl` commands, so you can do the whole thing by hand instead.
    """
    from spiriconfig import service

    scope = service.Scope.detect()
    config = service.ServiceConfig(
        compose_dir=compose_dir
        or (Path("/srv/compose") if scope.system else Path.home() / "compose"),
        storage_secret=secrets.token_urlsafe(32),
        auth=auth,
        auth_group=auth_group,
        host=host,
        port=port,
    )
    try:
        service.check_exposure(config)
    except service.ServiceError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    exec_path = service.executable_path()
    unit = service.render_unit_file(scope, exec_path)
    env_file = service.render_env_file(config)

    tool = service.install_tool_command(source, editable=editable)
    steps = [tool, service.daemon_reload_command(scope), service.enable_command(scope)]
    if not scope.system:
        steps.append(service.linger_command(getpass.getuser()))

    if show:
        typer.echo(f"# {scope.name} install\n")
        typer.echo(str(tool))
        typer.echo(f"\n# write {scope.env_path}\n{env_file}")
        typer.echo(f"# write {scope.unit_path}\n{unit}")
        for step in steps[1:]:
            typer.echo(str(step))
        return

    from spiriconfig.commands import run

    run(tool, timeout=None).check()
    _write_file(scope.env_path, env_file)
    _write_file(scope.unit_path, unit)
    for step in steps[1:]:
        run(step).check()
    typer.echo(f"Installed and started {service.SERVICE_NAME} ({scope.name} service).")


@app.command()
def update(
    reinstall: Annotated[
        bool,
        typer.Option("--reinstall", help="Force a refetch -- needed for a git branch."),
    ] = False,
    show: Annotated[
        bool, typer.Option("--show", help="Print the upgrade and restart, and stop."),
    ] = False,
) -> None:
    """Update SpiriConfig in place, then restart the service.

    The restart is handed to systemd to do a moment later rather than run inline,
    because this very process is what gets restarted -- an inline restart would cut
    the command off before it could report back.
    """
    from spiriconfig import service

    scope = service.Scope.detect()
    upgrade = service.upgrade_tool_command(reinstall=reinstall)
    restart = service.restart_command(scope)

    if show:
        typer.echo(str(upgrade))
        typer.echo(str(restart))
        return

    from spiriconfig.commands import run

    run(upgrade, timeout=None).check()
    run(restart).check()
    typer.echo("Update applied; the service is restarting.")


# Logging is configured, and plugins mounted, at import time: Typer needs every
# subcommand registered before it can route `spiriconfig docker ...` or list them
# in `--help`, and discovery logs as it goes, so the sinks have to exist first.
# Discovery failures are logged and skipped inside discover(), so one broken
# plugin cannot stop the CLI from starting.
logging.configure(settings())
_mount(discover())


if __name__ == "__main__":
    app()
