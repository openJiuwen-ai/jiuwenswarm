"""The toolkit's own tool representation: four facts and a callable.

Relocated dependency (§25.5, cut ①): the toolkit used to build openjiuwen's
``Tool``/``ToolCard``/``LocalFunction`` directly, which welded the whole library
layer to one host framework for the sake of three constructors. These local
classes carry the same four facts every host needs -- name, description, input
schema, properties -- plus the callable; the bridge module beside the clouddoc
package translates them into openjiuwen objects for jiuwenswarm's harness, and
any other host (the MCP server, a future embedding) reads them directly.

Attribute-compatible with the previous shapes on purpose: ``tool.card.name``,
``tool.func`` and friends keep working, so nothing downstream notices the swap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCard:
    id: str
    name: str
    description: str
    input_params: dict[str, Any]
    parallel_safe: bool = True
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class LocalFunction:
    card: ToolCard
    func: Callable[..., Any]

    @property
    def _func(self) -> Callable[..., Any]:  # the older accessor some callers probe
        return self.func


# The annotation alias: a toolkit tool IS a LocalFunction here.
Tool = LocalFunction
