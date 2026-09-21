"""A ``{widget-table}`` directive that renders the docker plugin's widgets.

The table used to be hand-written prose in ``docker.md``, kept in sync with
:data:`spiriconfig_docker.widgets.REGISTRY` by nothing but a human remembering
to update both. This reads the registry directly, so a widget added to one and
forgotten in the other is a docs build failure, not a stale page.
"""

from __future__ import annotations

import re

from docutils import nodes
from sphinx.application import Sphinx
from sphinx.util.docutils import SphinxDirective

from spiriconfig_docker import settings, widgets

#: A `` `code` `` span inside a docstring summary, so the rendered cell gets a
#: literal node instead of a literal pair of backticks.
_CODE_SPAN = re.compile(r"`([^`]+)`")


def widget_rows() -> list[tuple[str, str, str]]:
    """One ``(name, renders, needs options)`` row per registered widget.

    Raises if a widget's builder has no docstring, rather than rendering a blank
    row: a widget with nothing to say about itself is a docs bug, and the build
    is where that should be caught, not a page a reader notices is thin.
    """
    rows = []
    for name, widget in widgets.REGISTRY.items():
        doc = (widget.build.__doc__ or "").strip()
        if not doc:
            raise ValueError(
                f"widget {name!r} ({widget.build.__qualname__}) has no docstring "
                "-- add one describing what it renders, it doubles as the docs table row"
            )
        summary = doc.splitlines()[0].strip()

        if name in settings.CHOICE_WIDGETS:
            needs_options = "Yes"
        elif name in settings.DISCOVERED_WIDGETS:
            needs_options = "Optional"
        else:
            needs_options = ""

        rows.append((name, summary, needs_options))
    return rows


def _inline(text: str) -> list[nodes.Node]:
    """`` `code` `` spans as literal nodes; everything else as plain text."""
    parts: list[nodes.Node] = []
    pos = 0
    for match in _CODE_SPAN.finditer(text):
        if match.start() > pos:
            parts.append(nodes.Text(text[pos : match.start()]))
        parts.append(nodes.literal(text=match.group(1)))
        pos = match.end()
    if pos < len(text):
        parts.append(nodes.Text(text[pos:]))
    return parts


def _cell(*content: nodes.Node) -> nodes.entry:
    entry = nodes.entry()
    paragraph = nodes.paragraph()
    paragraph.extend(content)
    entry += paragraph
    return entry


class WidgetTable(SphinxDirective):
    """Usage, on a line of its own in a Markdown page::

        ```{widget-table}
        ```
    """

    has_content = False
    required_arguments = 0
    optional_arguments = 0

    def run(self) -> list[nodes.Node]:
        header = ("widget:", "Renders", "Needs options:")
        rows = widget_rows()

        table = nodes.table()
        tgroup = nodes.tgroup(cols=len(header))
        table += tgroup
        for _ in header:
            tgroup += nodes.colspec(colwidth=1)

        thead = nodes.thead()
        tgroup += thead
        header_row = nodes.row()
        thead += header_row
        for title in header:
            header_row += _cell(nodes.Text(title))

        tbody = nodes.tbody()
        tgroup += tbody
        for name, summary, needs_options in rows:
            row = nodes.row()
            tbody += row
            row += _cell(nodes.literal(text=name))
            row += _cell(*_inline(summary))
            row += _cell(nodes.Text(needs_options))

        return [table]


def setup(app: Sphinx) -> dict[str, bool]:
    app.add_directive("widget-table", WidgetTable)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
