"""Provisioning plugin settings, read from the environment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

#: How much a fetched repo is trusted before `apply` reconciles a device to
#: it -- see NOTES-usb-provisioning.md's "Premise" and "Two-tier trust".
Tier = Literal["off", "signed", "trusted"]


class ProvisionSettings(BaseSettings):
    """Settings for the provisioning plugin, prefixed ``SPIRICONFIG_PROVISION_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_PROVISION_",
        env_file=".env",
        extra="ignore",
    )

    tier: Tier = "off"
    """``SPIRICONFIG_PROVISION_TIER``. ``"off"``: `apply` always refuses.
    ``"trusted"``: applied after only the fast-forward check (dev kits
    only). ``"signed"``: would additionally require every commit be
    verified against a key in `trust.d/` -- refused for now, since
    `trust.d/` doesn't exist as a resource type yet (see
    :doc:`/provisioning`'s scope note). Root-owned on purpose: not
    reachable from the web UI or from a drive itself, the same reasoning
    as ``SPIRICONFIG_AUTH`` -- a drive-reachable toggle for "should this
    device trust unsigned drives" would itself be the privilege
    escalation.
    """

    apply_patch_dir: Path = Path("var/provision-apply/patch")
    """This device's persistent local clone of the *patch* remote it
    applies from. ``SPIRICONFIG_PROVISION_APPLY_PATCH_DIR``. Distinct from
    any profile's own ``patch/`` under `profiles_dir`, which is an
    operator's own authoring working tree; this is the copy a device
    fetches into and reconciles itself against. See
    NOTES-usb-provisioning.md's "Mechanism: a git remote".
    """

    apply_baseline_dir: Path = Path("var/provision-apply/baseline")
    """Same as `apply_patch_dir`, for the baseline remote.
    ``SPIRICONFIG_PROVISION_APPLY_BASELINE_DIR``."""

    fetch_timeout: float = 120.0
    """Seconds before `git fetch` is considered hung.
    ``SPIRICONFIG_PROVISION_FETCH_TIMEOUT``. Longer than `command_timeout`
    on purpose -- a fetch may cross a real network, everything else here
    is local disk and process I/O."""

    profiles_dir: Path = Path("var/provision")
    """Parent directory an operator's named profiles live under.
    ``SPIRICONFIG_PROVISION_PROFILES_DIR``. Two entirely separate
    namespaces live under it, ``patch/`` and ``baseline/`` -- each
    immediate subdirectory of one of those is a profile's own working
    tree, its own git history (e.g. ``patch/shop-floor-fleet``,
    ``baseline/shop-floor-fleet``). A patch profile and a baseline profile
    sharing a name are unrelated data -- the shared name is only ever a
    naming convenience for an operator pairing them in a UI, never a
    relationship this module tracks (see "Two remotes" in
    ``NOTES-usb-provisioning.md``). See
    `spiriconfig_provision.repo.list_profiles`/`create_profile`.
    """

    git_bin: str = "git"

    command_timeout: float = 30.0
    """Seconds before a provisioning command is considered hung.
    ``SPIRICONFIG_PROVISION_COMMAND_TIMEOUT``."""


def provision_settings() -> ProvisionSettings:
    """Load provisioning plugin settings from the environment."""
    return ProvisionSettings()
