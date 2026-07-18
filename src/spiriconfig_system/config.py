"""System plugin settings, read from the environment."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Tool(BaseModel):
    """A program the overview checks for, and the command that proves it is there.

    ``probe`` is the argv run to establish both presence and version -- a tool
    that is installed answers it (its version, on stdout), and one that is not
    fails to launch. It is a real command line, shown on the page in advanced
    mode, so it doubles as "here is how you would check this yourself".
    """

    name: str
    """What to call the tool on the page, e.g. ``docker compose``."""

    probe: list[str]
    """The argv that prints the tool's version, e.g. ``["git", "--version"]``."""


#: The tools SpiriConfig itself leans on. ``docker`` and ``docker compose`` are
#: how every managed service is started; ``git`` is how the app store fetches and
#: updates its repos. A deployment that needs more (netplan, a modem tool) adds
#: them with the env override rather than editing this list.
_DEFAULT_TOOLS = [
    Tool(name="docker", probe=["docker", "--version"]),
    Tool(name="docker compose", probe=["docker", "compose", "version"]),
    Tool(name="git", probe=["git", "--version"]),
]


class SystemSettings(BaseSettings):
    """Settings for the system plugin, prefixed ``SPIRICONFIG_SYSTEM_``."""

    model_config = SettingsConfigDict(
        env_prefix="SPIRICONFIG_SYSTEM_",
        env_file=".env",
        extra="ignore",
    )

    required_tools: list[Tool] = _DEFAULT_TOOLS
    """The programs whose presence the overview reports.

    ``SPIRICONFIG_SYSTEM_REQUIRED_TOOLS``, as a JSON array of ``{"name", "probe"}``
    objects -- pydantic-settings parses the complex value from the environment.
    Override it to check for whatever a particular image also depends on::

        SPIRICONFIG_SYSTEM_REQUIRED_TOOLS='[{"name":"netplan","probe":["netplan","--version"]}]'
    """

    command_timeout: float = 10.0
    """Seconds before a version probe is considered hung. ``SPIRICONFIG_SYSTEM_COMMAND_TIMEOUT``.

    A version check should be near-instant; this only exists so a tool that hangs
    on ``--version`` (a broken wrapper script, say) turns into a "could not check"
    rather than freezing the page's refresh.
    """


def system_settings() -> SystemSettings:
    """Load system plugin settings from the environment."""
    return SystemSettings()
