"""``spiriconfig provision`` -- the CLI face of the provisioning plugin.

Staging a change has no CLI verb of its own: a ``users/add/<name>/`` entry
is a file, and writing one by hand is exactly what "you must be able to do
without us" already means (see :doc:`/provisioning`, "Writing the files is
plain I/O"). Everything else here has a verb because it *is* an action,
not a file: ``list``/``create`` manage profiles; ``status`` reads a
profile's remote; ``commit`` is Review & Sign's non-visual half;
``pull``/``origin``/``export`` are the git-remote plumbing a profile
needs; ``apply`` is the device side, fetching and reconciling a remote
it's told to trust.

``patch`` and ``baseline`` are two separate profile namespaces, not a
choice within one profile -- every command below takes REMOTE before
PROFILE for that reason, the same order as `create`.
"""

from __future__ import annotations

import typer

from spiriconfig.commands import CommandError

from spiriconfig_provision import apply as apply_
from spiriconfig_provision import repo, review, sync
from spiriconfig_provision.apply import ApplyError
from spiriconfig_provision.config import ProvisionSettings, provision_settings
from spiriconfig_provision.contract import StagedRepo
from spiriconfig_provision.repo import Remote

app = typer.Typer(
    name="provision",
    help="Build a provisioning profile: stage, review and sign, export, and apply it.",
)


def _remote(value: str) -> Remote:
    if value not in ("patch", "baseline"):
        raise typer.BadParameter("must be 'patch' or 'baseline'", param_hint="remote")
    return value  # type: ignore[return-value]


def _resolve(remote: str, profile: str, settings: ProvisionSettings) -> StagedRepo:
    kind = _remote(remote)
    try:
        found = repo.open_profile(profile, kind, settings)
    except KeyError as exc:
        typer.echo(f"No such {kind} profile: {profile!r}", err=True)
        raise typer.Exit(1) from exc
    return found.repo


def _origin_line(staged_repo: StagedRepo, settings: ProvisionSettings) -> str:
    url = sync.origin_url(staged_repo, settings)
    if url is None:
        return "origin: not configured"
    result = sync.status(staged_repo, settings)
    if result is None:
        return f"origin: {url} (never fetched -- run 'pull' to check)"
    if result.clean:
        return f"origin: {url} (up to date)"
    return f"origin: {url} ({result.ahead} ahead, {result.behind} behind)"


def _report(staged_repo: StagedRepo, settings: ProvisionSettings) -> None:
    typer.echo(f"  path: {staged_repo.path}")
    typer.echo(f"  {_origin_line(staged_repo, settings)}")
    changes = repo.pending_changes(staged_repo, settings)
    if not changes:
        typer.echo("  Nothing staged.")
        return
    for change in changes:
        typer.echo(f"  [{change.verb}] {change.detail}")


@app.command("list")
def list_profiles() -> None:
    """List every profile in both namespaces, with each remote's `origin` status."""
    settings = provision_settings()
    for kind in ("patch", "baseline"):
        profiles = repo.list_profiles(kind, settings)
        typer.echo(f"{kind}:")
        if not profiles:
            typer.echo("  (none)")
            continue
        for profile in profiles:
            typer.echo(f"  {profile.name} -- {_origin_line(profile.repo, settings)}")


@app.command()
def create(
    remote: str = typer.Argument(..., help="Which namespace: 'patch' or 'baseline'."),
    name: str = typer.Argument(..., help="Profile name -- becomes a directory name."),
) -> None:
    """Create a new PATCH or BASELINE profile named NAME."""
    kind = _remote(remote)
    settings = provision_settings()
    try:
        profile = repo.create_profile(name, kind, settings)
    except ValueError as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Created {kind} profile {profile.name!r}.")


@app.command()
def status(
    remote: str = typer.Argument(..., help="Which namespace: 'patch' or 'baseline'."),
    profile: str = typer.Argument(None, help="Profile name. Omit to report on every profile."),
) -> None:
    """Print a REMOTE profile's `origin` status and what's pending in it."""
    kind = _remote(remote)
    settings = provision_settings()
    profiles = repo.list_profiles(kind, settings)
    if profile is not None:
        profiles = [p for p in profiles if p.name == profile]
        if not profiles:
            typer.echo(f"No such {kind} profile: {profile!r}", err=True)
            raise typer.Exit(1)
    if not profiles:
        typer.echo("No profiles yet -- see 'spiriconfig provision create'.")
        return
    for found in profiles:
        typer.echo(found.name)
        _report(found.repo, settings)


@app.command()
def commit(
    remote: str = typer.Argument(..., help="Which namespace: 'patch' or 'baseline'."),
    profile: str = typer.Argument(..., help="Profile name."),
    message: str = typer.Option(
        None, "--message", "-m", help="Commit message. Defaults to a summary of what's pending."
    ),
) -> None:
    """Stage and commit everything pending in REMOTE profile PROFILE.

    Signed with whatever ``git config user.signingkey`` already names --
    this never manages a key of its own; see the Review & Sign section of
    :doc:`/provisioning`.
    """
    settings = provision_settings()
    staged = _resolve(remote, profile, settings)

    changes = repo.pending_changes(staged, settings)
    if not changes:
        typer.echo("Nothing staged.")
        return
    text = message or review.suggested_message(changes)

    try:
        result = review.commit(staged, text, settings)
    except CommandError as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Committed {result.commit} to {remote}/{profile}.")
    typer.echo(f"Signed with {result.signing_key}." if result.signed else "Not signed -- no user.signingkey configured.")


@app.command()
def origin(
    remote: str = typer.Argument(..., help="Which namespace: 'patch' or 'baseline'."),
    profile: str = typer.Argument(..., help="Profile name."),
    url: str = typer.Argument(
        None, help="Set origin to this URL or path. Omit to print the current one."
    ),
) -> None:
    """Get or set a REMOTE profile's `origin` -- what `pull` and a plain `export` push to."""
    settings = provision_settings()
    staged = _resolve(remote, profile, settings)
    if url is None:
        typer.echo(sync.origin_url(staged, settings) or "No origin configured.")
        return
    sync.set_origin(staged, url, settings)
    typer.echo(f"origin set to {url}")


@app.command()
def pull(
    remote: str = typer.Argument(..., help="Which namespace: 'patch' or 'baseline'."),
    profile: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Fast-forward a REMOTE profile from its configured `origin`."""
    settings = provision_settings()
    staged = _resolve(remote, profile, settings)
    if sync.origin_url(staged, settings) is None:
        typer.echo("No origin configured -- see 'spiriconfig provision origin'.", err=True)
        raise typer.Exit(1)
    try:
        sync.pull(staged, settings)
    except CommandError as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Pulled.")


@app.command()
def export(
    remote: str = typer.Argument(..., help="Which namespace: 'patch' or 'baseline'."),
    profile: str = typer.Argument(..., help="Profile name."),
    destination: str = typer.Argument(
        ..., help="Where to send HEAD -- a filesystem path (a drive) or a URL."
    ),
) -> None:
    """Push a REMOTE profile's `HEAD` to DESTINATION -- a drive path or a
    URL, same operation either way."""
    settings = provision_settings()
    staged = _resolve(remote, profile, settings)
    try:
        sync.export_to(staged, destination, settings)
    except (CommandError, ValueError) as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Exported {remote}/{profile} to {destination}")


@app.command()
def apply(
    remote: str = typer.Argument(..., help="Which shape to expect: 'patch' or 'baseline'."),
    source: str = typer.Argument(
        ..., help="Where to fetch from -- anything 'git fetch' accepts: a path, file://, https://, ssh://."
    ),
    ref: str = typer.Option("HEAD", help="The ref to fetch, e.g. a branch or tag."),
    yes: bool = typer.Option(
        False, "--yes", help="Actually run the steps. Without this, only the plan is shown."
    ),
) -> None:
    """Fetch SOURCE, verify it, and reconcile this device to what it declares.

    Device-side, and unrelated to any authoring profile above: it fetches
    into this device's own persistent applied clone
    (`ProvisionSettings.apply_patch_dir`/`apply_baseline_dir`), not any
    profile's working tree. Without ``--yes``, fetches and checks SOURCE
    and prints exactly what would run -- nothing is applied to the live
    system until you say so a second time.
    """
    kind = _remote(remote)

    settings = provision_settings()
    try:
        computed = apply_.plan(kind, source, settings, ref=ref)
    except ApplyError as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Fetched {computed.fetched} for the {kind} remote.")
    if not computed.steps:
        typer.echo("Nothing to apply -- the live system already matches.")
        return
    for step in computed.steps:
        typer.echo(f"  $ {step.command}")

    if not yes:
        typer.echo(f"\n{len(computed.steps)} step(s) above would run. Pass --yes to apply them.")
        return

    try:
        apply_.execute(computed, settings)
    except CommandError as exc:
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Applied.")


__all__ = ["app"]
