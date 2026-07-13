"""SpiriConfig: plugin-based configuration and container management.

The core is deliberately small. It discovers plugins, gives them a CLI to hang
subcommands off and a web UI to render pages into, and runs commands on their
behalf. Everything a user would actually call a feature lives in a plugin.

The rule the whole project is built around: **anything the web UI can do, the
user must be able to do without it.** Concretely, that means we drive the
system by running the same commands a human would run -- see
:mod:`spiriconfig.commands`.
"""

from __future__ import annotations

__version__ = "0.1.0"
