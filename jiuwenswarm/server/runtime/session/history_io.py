"""Async boundary for synchronous history persistence and queue backpressure."""
from __future__ import annotations

import asyncio
from typing import Any, Callable


async def run_history_io(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Drain an accepted write before propagating cancellation to its caller.

    Cancelling a to_thread await cannot stop the running function. Waiting for
    it preserves the ordering between the record and subsequent cleanup writes.
    """
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


def stream_chunk_writes_history(chunk: Any) -> bool:
    kind = getattr(chunk, "type", None)
    if kind is None and isinstance(chunk, dict):
        kind = chunk.get("type")
    return getattr(kind, "value", kind) in (
        "subagent_updated", "subagent_message", "subagent_activity",
    )


async def run_stream_parser(
    parser: Callable[..., Any], chunk: Any, *, projection_owner: Any = None,
    projection_parent_session_id: str | None = None, **kwargs: Any,
) -> Any:
    """Parse on the loop, then explicitly commit neutral subagent events.

    A parser is a pure SDK adapter. The consumer owns projection and commits;
    ordinary tokens never dispatch a thread, and silent transcripts still pass
    through the accepted-write cancellation barrier.
    """
    parsed = parser(chunk, **kwargs)
    from jiuwenswarm.server.runtime.agent_adapter.subagent_projection import project_stream_event

    return await project_stream_event(
        parsed, owner=projection_owner, parent_session_id=projection_parent_session_id,
    )
