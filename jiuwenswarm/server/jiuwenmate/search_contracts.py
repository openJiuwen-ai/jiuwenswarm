"""Group-search result values for the current core checkout.

The designated core exports ``GroupMessage`` and ``GroupMessageQuery``.
Cross-group search also returns a hit and a result. Prefer those names from
core when they are public; otherwise define the same fields here so Agent
Runtime can still load.
"""

from typing import Annotated, Literal

from pydantic import Field

from openjiuwen.harness.persistent_agent import GroupMessage
from openjiuwen.harness.persistent_agent.schema.protocol import (
    Identifier,
    InvocationRef,
    ProtocolModel,
    Reason,
)

try:
    from openjiuwen.harness.persistent_agent import (  # type: ignore[attr-defined]
        GroupMessageSearchHit,
        GroupMessageSearchResult,
    )
except ImportError:
    class GroupMessageSearchHit(ProtocolModel):
        """One authorized group message located by a search."""

        group_id: Identifier
        group_name: Annotated[str, Field(min_length=1, max_length=512)]
        message_ref: Identifier
        message: GroupMessage

    class GroupMessageSearchResult(InvocationRef):
        """Search outcome. An empty result is complete; a failure is not absence."""

        status: Literal["found", "empty", "incomplete", "unavailable", "failed", "forbidden"]
        hits: Annotated[tuple[GroupMessageSearchHit, ...], Field(max_length=500)] = ()
        next_cursor: Identifier | None = None
        query_complete: bool = Field(default=False, strict=True)
        reason: Reason | None = None
