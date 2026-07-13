"""``spiriconfig appstore`` -- the CLI face of the app store.

Every command here is a thin wrapper over a ``git``, ``ln``, ``rm``, or ``cp``
invocation, and ``--show`` prints it instead of running it. Which means the
answer to "what did the app store actually do to my machine?" is always a short
list of commands the user could have typed, and an app store that they could
walk away from by never running it again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from spiriconfig.commands import Command, run, stream
from spiriconfig_docker.config import docker_settings

from spiriconfig_appstore.config import appstore_settings
from spiriconfig_appstore.installs import (
    Install,
    install_command,
    installed,
    uninstall as find_install,
)
from spiriconfig_appstore.stores import (
    App,
    StoreError,
    find_app,
    stores,
    update_plan,
)

log = logger.bind(plugin="appstore")

app = typer.Typer(
    name="appstore",
    help="Install apps from a git-hosted app store.",
    no_args_is_help=True,
)

ShowOption = Annotated[
    bool,
    typer.Option("--show", help="Print the command instead of running it."),
]
AppArg = Annotated[
    str,
    typer.Argument(help="App name, or store/name if two stores both have it."),
]


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _compose_dir() -> Path:
    """The compose directory, absolute.

    The default is relative (``test_data/compose``), and a symlink into a store
    must be made with absolute paths or it dangles -- see
    :func:`spiriconfig_appstore.stores._absolute`. Resolving once, here, keeps
    that from being something every caller has to remember.
    """
    return docker_settings().compose_dir.expanduser().resolve()


def _app(name: str) -> App:
    try:
        return find_app(appstore_settings(), name)
    except StoreError as exc:
        raise _fail(str(exc)) from exc


def _install(name: str) -> Install:
    settings = appstore_settings()
    try:
        return find_install(settings, _compose_dir(), name)
    except StoreError as exc:
        raise _fail(str(exc)) from exc


def _execute(command: Command, *, show: bool) -> None:
    """Run a command, or print it, then exit non-zero if it failed."""
    if show:
        typer.echo(str(command))
        return
    result = run(command, timeout=appstore_settings().command_timeout, log=log)
    if result.stdout:
        typer.echo(result.stdout.rstrip())
    if result.stderr:
        typer.echo(result.stderr.rstrip(), err=True)
    if not result.ok:
        raise typer.Exit(result.returncode)


def _execute_streaming(commands: list[Command], *, show: bool) -> None:
    """Run commands in order, echoing output as it arrives, stopping on failure.

    Stopping on failure matters more here than in the docker plugin: these are
    sequences (fetch, commit, merge), and running the merge after a failed fetch
    would report success for an update that never happened.
    """
    if show:
        for command in commands:
            typer.echo(str(command))
        return

    async def pump() -> None:
        for command in commands:
            failed = False
            async for line in stream(command, log=log):
                typer.echo(line)
                if line.startswith("[command exited with code "):
                    failed = True
            if failed:
                raise typer.Exit(1)

    asyncio.run(pump())


# -- stores -------------------------------------------------------------------


@app.command("stores")
def list_stores() -> None:
    """List configured app stores and whether they have been cloned."""
    configured = stores(appstore_settings())
    if not configured:
        typer.echo(
            "No app stores configured. Set SPIRICONFIG_APPSTORE_STORES to a JSON "
            'list of git URLs, e.g. \'["https://github.com/spiri/spiri-apps"]\'.'
        )
        return
    width = max(len(s.slug) for s in configured)
    for store in configured:
        if not store.is_cloned:
            state = "not cloned (run: appstore sync)"
        elif store.in_merge:
            state = "UPDATE STOPPED ON A CONFLICT (run: appstore resolve)"
        elif store.is_dirty():
            state = "cloned, with local edits"
        else:
            state = "cloned"
        typer.echo(f"{store.slug:<{width}}  {state}\n{'':<{width}}  {store.url}")


@app.command()
def sync(show: ShowOption = False) -> None:
    """Clone any missing stores and fetch the rest. Changes no installed app.

    Safe to run whenever: fetching only updates git's idea of what the remote
    has. Installed apps keep the exact files they had until `update` merges.
    """
    settings = appstore_settings()
    configured = stores(settings)
    if not configured:
        typer.echo("No app stores configured.")
        return

    commands = []
    for store in configured:
        if store.is_cloned:
            commands.append(store.fetch_command())
        else:
            if not show:
                store.path.parent.mkdir(parents=True, exist_ok=True)
            commands.append(store.clone_command())
    _execute_streaming(commands, show=show)


@app.command()
def update(
    discard_local: Annotated[
        bool,
        typer.Option(
            "--discard-local",
            help="Throw away your edits instead of merging them. Cannot be undone.",
        ),
    ] = False,
    show: ShowOption = False,
) -> None:
    """Pull every store, merging your edits with the store's changes.

    Your edits are committed to the store's git repo first, so the merge has to
    reconcile with them rather than overwrite them. A conflict leaves the usual
    git markers in the file -- which are not valid YAML, so docker compose (and
    SpiriConfig's editor) will refuse to accept the file until you resolve it.

    This rewrites files on disk. It does not restart anything: run
    `spiriconfig docker up <app>` when you want an app to actually pick up its
    new definition.
    """
    settings = appstore_settings()
    cloned = [s for s in stores(settings) if s.is_cloned]
    if not cloned:
        typer.echo("No stores cloned yet. Run: spiriconfig appstore sync")
        return

    try:
        commands = [
            command
            for store in cloned
            for command in update_plan(store, discard_local=discard_local)
        ]
    except StoreError as exc:
        raise _fail(str(exc)) from exc

    try:
        _execute_streaming(commands, show=show)
    except typer.Exit:
        # The most likely reason an update stops is a conflict, and git's own
        # message ("fix conflicts and then commit the result") is advice for
        # someone standing in a shell inside a repo they know about. Point at the
        # actual files, and at the commands that exist here.
        conflicted = [s for s in cloned if s.in_merge]
        if not conflicted:
            raise
        typer.echo()
        typer.secho("Update stopped: your edits conflict with the store's.", bold=True)
        for store in conflicted:
            for path in store.conflicts():
                typer.echo(f"  {store.path / path}")
        typer.echo(
            "\nEdit those files and delete the <<<<<<< ======= >>>>>>> markers, "
            "keeping\nwhat you want. Nothing can start until you do -- the markers "
            "are not valid\nYAML, so docker compose will refuse the file.\n\n"
            "  spiriconfig appstore resolve          when you are done\n"
            "  spiriconfig appstore resolve --abort  to undo the update instead"
        )
        raise


@app.command()
def resolve(
    abort: Annotated[
        bool,
        typer.Option("--abort", help="Undo the update instead of finishing it."),
    ] = False,
    show: ShowOption = False,
) -> None:
    """Finish (or abandon) an update that stopped on a conflict.

    Run this after editing the conflicted files to remove the <<<<<<< markers.
    If any are still there, this refuses and tells you which files -- running it
    too early cannot commit a broken app.

    `--abort` puts everything back the way it was before the update. Your own
    edits survive either way: they were committed before the merge started.
    """
    settings = appstore_settings()
    merging = [s for s in stores(settings) if s.is_cloned and s.in_merge]
    if not merging:
        typer.echo("No update is waiting on a conflict.")
        return

    try:
        commands = [
            command
            for store in merging
            for command in (
                [store.abort_command()] if abort else store.resolve_plan()
            )
        ]
    except StoreError as exc:
        raise _fail(str(exc)) from exc

    _execute_streaming(commands, show=show)
    if not show:
        typer.echo(
            "Update undone." if abort
            else "Update finished. Run `spiriconfig docker up <app>` to apply it."
        )


# -- apps ---------------------------------------------------------------------


@app.command("list")
def list_apps() -> None:
    """List every app in every store, and what is installed."""
    settings = appstore_settings()
    compose_dir = _compose_dir()

    cloned = [s for s in stores(settings) if s.is_cloned]
    if not cloned:
        typer.echo("No stores cloned yet. Run: spiriconfig appstore sync")
        return

    links = {i.app_name: i for i in installed(settings, compose_dir)}

    for store in cloned:
        apps = store.apps()
        typer.secho(f"{store.slug}", bold=True)
        if not apps:
            typer.echo("  (no apps)")
            continue
        width = max(len(a.name) for a in apps)
        for entry in apps:
            notes = []
            install = links.get(entry.name)
            if install is not None and install.store.slug == store.slug:
                notes.append(
                    "installed" if install.name == entry.name
                    else f"installed as {install.name}"
                )
                if entry.is_modified():
                    notes.append("locally edited")
            if entry.has_update():
                notes.append("update available")
            suffix = f"  [{', '.join(notes)}]" if notes else ""
            typer.echo(f"  {entry.name:<{width}}  {entry.version()}{suffix}")


@app.command("installed")
def list_installed() -> None:
    """List installed apps and where each one points."""
    settings = appstore_settings()
    links = installed(settings, _compose_dir())
    if not links:
        typer.echo("No apps installed from a store.")
        return
    width = max(len(i.name) for i in links)
    for item in links:
        typer.echo(f"{item.name:<{width}}  -> {item.target}")


@app.command()
def install(
    name: AppArg,
    as_: Annotated[
        str | None,
        typer.Option("--as", help="Install under a different name."),
    ] = None,
    show: ShowOption = False,
) -> None:
    """Install an app: symlink it from the store into the compose directory.

    This does not start it. The app shows up as a stack, and you start it the
    same way you start any other: `spiriconfig docker up <name>`.
    """
    entry = _app(name)
    compose_dir = _compose_dir()
    try:
        command = install_command(entry, compose_dir, as_)
    except StoreError as exc:
        raise _fail(str(exc)) from exc

    _execute(command, show=show)
    if not show:
        installed_as = as_ or entry.name
        typer.echo(f"Installed {entry.store.slug}/{entry.name} as {installed_as}.")
        typer.echo(f"Start it with: spiriconfig docker up {installed_as}")


@app.command()
def uninstall(
    name: Annotated[str, typer.Argument(help="Installed name, as it appears in the compose directory.")],
    show: ShowOption = False,
) -> None:
    """Remove an installed app's symlink.

    Removes a link and nothing else. The app's files stay in the store, and this
    does not stop it -- run `spiriconfig docker down <name>` first if it is
    running, or its containers will outlive the thing that defined them.
    """
    item = _install(name)
    _execute(item.uninstall_command(), show=show)
    if not show:
        typer.echo(f"Uninstalled {name} (the store's copy is untouched).")


@app.command()
def adopt(
    name: Annotated[str, typer.Argument(help="Installed name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do not ask for confirmation."),
    ] = False,
    show: ShowOption = False,
) -> None:
    """Turn an installed app into an ordinary compose project you own.

    Replaces the symlink with a real copy. The app stops being connected to its
    store: it will never update again, and nothing here will touch it. Use it
    when you want to take an app somewhere the store is not going to follow.

    This cannot be undone, so it asks first. `--yes` skips the question, for
    scripts; `--show` prints the commands without running them, and never asks.
    """
    item = _install(name)
    commands = item.adopt_commands()

    if show:
        for command in commands:
            _execute(command, show=True)
        return

    # The only prompt in this CLI, and it earns its place: every other command
    # here can be undone by running another one. This one leaves a directory that
    # SpiriConfig will subsequently refuse to touch, so the way back out is a
    # shell and an `rm -rf`. Say so before, not after.
    if not yes:
        typer.echo(f"Adopt {name} from {item.store.slug}?\n")
        for command in commands:
            typer.echo(f"    {command}")
        typer.echo(
            f"\n{name} stops tracking the store for good: no more updates, and "
            f"nothing here\nwill touch it again. There is no undo -- afterwards "
            f"`spiriconfig appstore uninstall`\nwill refuse it too, because the "
            f"directory will be a real one we did not create."
        )
        typer.confirm("\nAdopt it?", abort=True)

    for command in commands:
        _execute(command, show=False)
    if not show:
        typer.echo(f"Adopted {name}: it is now a plain compose project at {item.link}.")


@app.command()
def diff(name: AppArg, show: ShowOption = False) -> None:
    """Show what you changed, and what the store changed, for one app.

    The two halves of an update, separately, before you run one. "Yours" is
    against the version you have checked out; "store" is against what was
    fetched, so run `appstore sync` first if you want it to be current.
    """
    entry = _app(name)

    if show:
        typer.echo(str(entry.local_diff()))
        typer.echo(str(entry.upstream_diff()))
        return

    for title, command in (
        ("Your changes", entry.local_diff()),
        ("Store changes since your version", entry.upstream_diff()),
    ):
        result = run(command, timeout=appstore_settings().command_timeout, log=log)
        typer.secho(f"--- {title} ---", bold=True)
        typer.echo(result.stdout.rstrip() if result.stdout.strip() else "(none)")
        typer.echo()
