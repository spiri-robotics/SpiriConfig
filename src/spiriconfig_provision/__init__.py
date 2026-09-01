"""The provisioning plugin: building a signed git repo that resets
passwords, delivers apps, or certifies a device's exact state.

This plugin owns the three things no resource-owning plugin has a home for
-- ``trust.d/``, the review -> commit-and-sign -> export spine, and the
commit history log -- and coordinates everything else. It does not know how
a ``users/add/<name>/`` entry is shaped or what running ``useradd`` looks
like; that belongs to whichever plugin already owns that resource, and is
reached through :mod:`spiriconfig_provision.contract`.

See :doc:`/provisioning` for the full design. Built so far: named
profiles, one per remote -- patch and baseline are separate namespaces,
not a bundled choice -- staging on either,
Review & Sign (commits, signed with whatever ``git config user.signingkey``
already names), Export/Pull, and the device-side apply loop (``spiriconfig
provision apply``, ``off``/``trusted`` tiers). Not yet: ``trust.d/`` and the
``signed`` tier that needs it, the commit history log, and the manual
override / "apply an unmanaged repo" flow.
"""

from __future__ import annotations

import typer

from spiriconfig.plugins import Plugin

from spiriconfig_provision.cli import app as cli_app


class ProvisionPlugin(Plugin):
    """Build a provisioning repo, coordinating other plugins' resource types."""

    name = "provision"
    title = "Provisioning"
    description = "Build a repo that resets passwords, delivers apps, or certifies state."
    icon = "usb"

    # Shown, not gated -- the page still routes and the CLI still works with
    # advanced mode off (see Plugin.advanced). This is a developer's tool
    # right now, not an operator's: no trust.d/, so nothing staged here can
    # reach a `signed`-tier device without also handing it your `trusted`
    # tier. Off by default until docs/provisioning.md's `signed` tier is
    # real.
    advanced = True

    def cli(self) -> typer.Typer:
        return cli_app

    def page(self) -> None:
        # Imported lazily, like the other plugins: `spiriconfig provision status`
        # should not pay to import a web framework to print a status line.
        from spiriconfig_provision import web

        web.page()


__all__ = ["ProvisionPlugin"]
