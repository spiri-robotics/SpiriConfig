"""The docker plugin: manage docker compose projects.

Registered under the ``spiriconfig.plugins`` entry point group, so it is loaded
by exactly the same machinery a third-party plugin would be. If you want to
write your own plugin, this package is the worked example.
"""

from __future__ import annotations

import typer

from spiriconfig.plugins import Plugin

from spiriconfig_docker.cli import app as cli_app


class DockerPlugin(Plugin):
    """Manage the docker compose projects in a configured directory."""

    # `name` is the CLI subcommand and the URL, and stays "docker": that is what
    # the thing under it actually is. `title` is what a user is shown, and they
    # do not think of these as compose projects -- they think of them as the apps
    # running on the box. Plural: the page lists all of them.
    name = "docker"
    title = "Apps"
    description = "Start, stop, and edit docker compose projects."
    icon = "apps"

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        # Imported here, not at module scope: every plugin is imported to build
        # the CLI, and a plugin that pulls in the whole web framework to do it
        # makes `spiriconfig docker list` pay for a UI it never renders.
        from spiriconfig_docker import web

        web.page()


__all__ = ["DockerPlugin"]
