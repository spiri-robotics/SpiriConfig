#!/usr/bin/env python3
"""Bump the version in pyproject.toml and compose.yaml together, then tag it.

A release is a single number that has to appear in three places and agree with
itself: the Python package version, the published image tag, and the
``x-spiri-config-version`` SpiriConfig reads. This script is the one writer of all
three, so they never drift.

    uv run scripts/release.py patch          # 0.1.0 -> 0.1.1
    uv run scripts/release.py minor          # 0.1.1 -> 0.2.0
    uv run scripts/release.py major          # 0.2.0 -> 1.0.0
    uv run scripts/release.py 1.4.0          # set it explicitly
    uv run scripts/release.py patch --no-git  # edit the files, skip commit/tag

The files are edited with targeted regex substitutions rather than by parsing and
re-emitting YAML, so comments, ordering, and formatting survive untouched.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
COMPOSE = ROOT / "compose.yaml"


def current_version() -> tuple[int, int, int]:
    match = re.search(r'(?m)^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', PYPROJECT.read_text())
    if not match:
        sys.exit("could not find a semver version in pyproject.toml")
    return int(match[1]), int(match[2]), int(match[3])


def next_version(current: tuple[int, int, int], bump: str) -> str:
    major, minor, patch = current
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if re.fullmatch(r"\d+\.\d+\.\d+", bump):
        return bump
    sys.exit(f"expected major/minor/patch or an X.Y.Z version, got: {bump}")


def replace(
    path: Path, pattern: str, repl: str, what: str, *, count: int = 1, required: bool = True
) -> None:
    text = path.read_text()
    new_text, n = re.subn(pattern, repl, text, count=count)
    if n == 0 and required:
        sys.exit(f"could not find {what} in {path.name}")
    path.write_text(new_text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", help="major, minor, patch, or an explicit X.Y.Z")
    parser.add_argument("--no-git", action="store_true", help="edit files but do not commit or tag")
    args = parser.parse_args()

    version = next_version(current_version(), args.bump)

    replace(
        PYPROJECT,
        r'(?m)^(version\s*=\s*")\d+\.\d+\.\d+(")',
        rf"\g<1>{version}\g<2>",
        "version",
    )
    replace(
        COMPOSE,
        r'(?m)^(x-spiri-config-version:\s*")[^"]*(")',
        rf"\g<1>{version}\g<2>",
        "x-spiri-config-version",
    )
    # Every service runs the same image, so update them all. A project scaffolded
    # with no examples has no image line at all -- that is fine, not an error.
    replace(
        COMPOSE,
        r"(?m)^(\s*image:\s*ghcr\.io/[^:\s]+:)\S+",
        rf"\g<1>{version}",
        "image tag",
        count=0,
        required=False,
    )

    # Keep uv.lock's record of the project version in step. Harmless if uv is not
    # installed on the machine running this -- the file edits above are what matter.
    subprocess.run(["uv", "lock"], cwd=ROOT, check=False)

    print(f"bumped to {version}")
    if args.no_git:
        return

    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"release: {version}"], cwd=ROOT, check=True)
    subprocess.run(["git", "tag", f"v{version}"], cwd=ROOT, check=True)
    print(f"tagged v{version} -- push with: git push && git push origin v{version}")


if __name__ == "__main__":
    main()
