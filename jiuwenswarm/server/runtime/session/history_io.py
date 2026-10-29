"""Async boundary for synchronous history persistence and queue backpressure."""
from __future__ import annotations

import asyncio
from typing import Any, Callable


async def run_history_io(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Drain an accepted write before propagating cancellation to its caller.

    Cancelling a to_thread await cannot stop the running function. Waiting for
    it preserves the ordering between the record and subsequent cleanup writes.
    """
    from jiuwenswarm.server.runtime.session.session_history import append_history_record

    if fn is append_history_record and not args:
        from jiuwenswarm.server.runtime.session.history_events import try_append_platform_history
        from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError

        handled, receipt = await try_append_platform_history(kwargs)
        if handled:
            if receipt is not None and receipt.status == "rejected":
                raise SessionHistoryError("APPEND_REJECTED")
            return None
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


async def run_stream_parser(parser: Callable[..., Any], chunk: Any, **kwargs: Any) -> Any:
    """Only persistence-bearing chunks need a thread; tokens stay on the loop."""
    if stream_chunk_writes_history(chunk):
        return await run_history_io(parser, chunk, **kwargs)
    return parser(chunk, **kwargs)
