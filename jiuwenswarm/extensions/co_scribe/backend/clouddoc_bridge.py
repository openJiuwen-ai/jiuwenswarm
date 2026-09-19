"""Translate the clouddoc toolkit's local tool cards into openjiuwen objects.

This is deliberately **outside** the toolkit package (``backend/toolkit``): that
package must stay host-framework-free (§25.5, cut ①), and this bridge is
jiuwenswarm's own adapter -- it stays behind when the toolkit is extracted into
the library. It moved into the co-scribe plugin with the rest of the feature,
which changes who owns it, not which side of that line it sits on.
"""

from __future__ import annotations

from typing import Any


def to_openjiuwen(tools: list[Any]) -> list[Any]:
    """Wrap each local tool as an openjiuwen ``LocalFunction`` with a real card."""
    from openjiuwen.core.foundation.tool.base import ToolCard
    from openjiuwen.core.foundation.tool.function.function import LocalFunction

    out: list[Any] = []
    for t in tools:
        c = t.card
        out.append(LocalFunction(
            card=ToolCard(
                id=c.id,
                name=c.name,
                description=c.description,
                input_params=c.input_params,
                parallel_safe=c.parallel_safe,
                properties=dict(c.properties),
            ),
            func=t.func,
        ))
    return out
