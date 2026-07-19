"""Entry point: ``python -m esc_config`` and the ``esc-config`` script.

This is what the container runs. Each example is a module with a ``run()``
function; this file just picks which one to run. In ``compose.yaml`` every
service names its example explicitly (``command: ["esc-config", "worker"]``),
so a project with several examples runs the right one in each container.

    esc-config            # the only example, or the first if there are several
    esc-config nicegui    # a named example
"""

from __future__ import annotations

import importlib
import sys

#: The examples this project was scaffolded with, in order. Names map to the
#: module whose ``run()`` starts them. Add an example to copier.yml and it appears
#: here; the list is empty only if you scaffolded with no examples selected.
EXAMPLES: dict[str, str] = {
    "nicegui": "esc_config.nicegui_app",
}


def main() -> None:
    if not EXAMPLES:
        sys.exit("no examples were scaffolded; nothing to run")

    name = sys.argv[1] if len(sys.argv) > 1 else next(iter(EXAMPLES))
    module = EXAMPLES.get(name)
    if module is None:
        sys.exit(f"unknown example {name!r}; choose one of: {', '.join(EXAMPLES)}")

    # Imported lazily so running one example does not import another's dependencies.
    importlib.import_module(module).run()


if __name__ == "__main__":
    main()
