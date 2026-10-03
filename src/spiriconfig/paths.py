"""Where SpiriConfig keeps things on disk, unless told otherwise.

One answer per question, shared by the plugins' settings defaults and by
``spiriconfig install``, so that ``uvx spiriconfig serve`` and an installed
service run as the same user look at the same apps. Which answer depends only on
who is running:

* **root** gets the machine-wide conventional paths -- ``/srv/compose`` and
  ``/var/lib/spiriconfig``.
* **anyone else** gets paths in their own home, following the XDG base directory
  spec where it has an opinion.

A checkout is not special here: ``./scripts/test-data.sh`` points it at
``test_data/`` through the checkout's ``.env``, the same way any deployment
overrides a default.
"""

from __future__ import annotations

import os
from pathlib import Path


def system() -> bool:
    """True when running as root: a machine-wide install rather than a per-user one."""
    return os.geteuid() == 0


def _user_data_home() -> Path:
    """``$XDG_DATA_HOME`` or ``~/.local/share``."""
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def compose_dir() -> Path:
    """One subdirectory per compose project; installed apps are symlinks in here.

    Somewhere a person will look and edit by hand, which is why the user default is
    a plain directory in their home and not tucked under ``~/.local``.
    """
    return Path("/srv/compose") if system() else Path.home() / "spiri-apps"


def store_dir() -> Path:
    """Where app store clones live.

    Data, not cache: a user's edits to an installed app are commits in here.
    """
    if system():
        return Path("/var/lib/spiriconfig/stores")
    return _user_data_home() / "spiriconfig" / "stores"
