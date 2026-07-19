"""A test that is here no matter which examples were scaffolded.

It keeps `uv run pytest` green (and CI happy) even for a project generated with no
examples selected, and locks down the one thing every generated project has: a
version string the release script can bump.
"""

from __future__ import annotations

import re

import esc_config


def test_has_a_semver_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", esc_config.__version__)
