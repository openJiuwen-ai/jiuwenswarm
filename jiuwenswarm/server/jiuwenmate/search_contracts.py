"""Group-search result values for the current core checkout.

The designated core exports ``GroupMessage`` and ``GroupMessageQuery``.
Cross-group search also returns a hit and a result. Prefer those names from
core when they are public; otherwise define the same fields here so Agent
Runtime can still load.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from openjiuwen.harness.persistent_agent import GroupMessage
from openjiuwen.harness.persistent_agent.schema.protocol import (
    Identifier,
    InvocationRef,
    ProtocolModel,
    Reason,
)

try:
    from openjiuwen.harness.persistent_agent import (  # type: ignore[attr-defined]
        GroupArtifactSearchHit,
        GroupMessageSearchHit,
        GroupMessageSearchResult,
    )
except ImportError:
    class GroupMessageSearchHit(ProtocolModel):
        """One authorized group message located by a search."""

        group_id: Identifier
        group_name: Annotated[str, Field(min_length=1, max_length=512)]
        platform: Identifier | None = None
        platform_chat_id: Identifier | None = None
        message_ref: Identifier
        message: GroupMessage

    class GroupArtifactSearchHit(ProtocolModel):
        """One final Session file or online document from an authorized group."""

        group_id: Identifier
        group_name: Annotated[str, Field(min_length=1, max_length=512)]
        platform: Identifier | None = None
        platform_chat_id: Identifier | None = None
        artifact_id: Identifier
        artifact_kind: Literal["file", "online_doc"]
        title: Annotated[str, Field(min_length=1, max_length=1024)]
        file_name: Annotated[str, Field(min_length=1, max_length=512)] | None = None
        file_size: int | None = None
        file_digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")] | None = None
        source_session_id: Identifier | None = None
        source_file_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
        url: Annotated[str, Field(max_length=4096)] | None = None
        snippet: Annotated[str, Field(max_length=4096)] | None = None
        created_at: AwareDatetime
        imported_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    class GroupMessageSearchResult(InvocationRef):
        """Search outcome. An empty result is complete; a failure is not absence."""

        status: Literal["found", "empty", "incomplete", "unavailable", "failed", "forbidden"]
        hits: Annotated[tuple[GroupMessageSearchHit, ...], Field(max_length=500)] = ()
        artifacts: Annotated[tuple[GroupArtifactSearchHit, ...], Field(max_length=500)] = ()
        next_cursor: Identifier | None = None
        query_complete: bool = Field(default=False, strict=True)
        reason: Reason | None = None
