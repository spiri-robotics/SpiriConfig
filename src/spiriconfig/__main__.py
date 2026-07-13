"""Allow ``python -m spiriconfig`` as well as the ``spiriconfig`` script."""

from __future__ import annotations

from spiriconfig.cli import app

if __name__ == "__main__":
    app()
