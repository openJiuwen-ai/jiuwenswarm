# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Capture a subagent's stream into the active run's debug dump.

Both subagent dispatch sites — the SDK's builtin ``TaskTool`` and jiuwenswarm's
custom ``AgentTool`` — historically call ``await subagent.invoke(...)`` and keep
only the final output string. The subagent's internal reasoning / tool calls /
token usage never reach the parent run's :class:`DebugTraceLogger`, so a
``/debug`` dump shows a multi-minute gap where the subagent worked invisibly.

This module wraps that dispatch: when a debug run is active (and subagent
capture is enabled) the subagent is driven through ``stream()`` instead, every
chunk is fed to the active logger under a ``source=subagent:...`` label, and the
chunks are reduced back into an invoke-style result via the SDK's own
``DeepAgent._result_from_stream_chunk``. When no debug run is active, behaviour
is identical to a plain ``invoke`` (zero regression for normal runs).

Only ever reads the ContextVar (never set/reset) — the adapter owns the binding
lifetime, see ``interface_deep.JiuWenSwarmDeepAdapter``.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


async def invoke_subagent_with_trace(
    subagent: Any,
    *,
    inputs: dict,
    session: Any,
    source_label: str,
) -> dict:
    """Run *subagent*, capturing its stream into the active run's debug dump.

    Args:
        subagent: A ``DeepAgent`` instance (has ``invoke`` / ``stream``).
        inputs: The inputs dict (``{"query": ..., "conversation_id": ...}``).
        session: The parent session, forwarded to the subagent.
        source_label: Dump source tag, e.g. ``subagent:builtin:explore_agent``.

    Returns:
        An invoke-style result dict (``{"output": ..., "result_type": ...}``),
        matching what ``subagent.invoke`` would have returned.
    """
    # Safety net for a subagent that reached here without going through the
    # create_subagent hook (a host-built instance, say). Idempotent, so the
    # normal path — where the hook already attached the rail — pays nothing.
    from jiuwenswarm.agents.harness.agent_observability import (
        attach_subagent_observability,
    )

    attach_subagent_observability(subagent)

    from jiuwenswarm.server.runtime.debug_trace.context import (
        get_debug_trace_logger,
        get_debug_trace_logger_for_session,
    )

    dbg = get_debug_trace_logger()
    if dbg is None:
        # Same task-boundary gap as TaskTool: this helper can be reached from the
        # custom AgentTool path inside the DeepAgent's supervisor task, where the
        # per-request ContextVar isn't visible. Fall back to a session lookup.
        sid = session.get_session_id() if hasattr(session, "get_session_id") else None
        if sid:
            dbg = get_debug_trace_logger_for_session(sid)
    if dbg is None or not dbg.captures_subagent_flow():
        # No debug run, or capture disabled — original path, unchanged.
        return await subagent.invoke(inputs, session=session)

    output_parts: list[str] = []
    result: dict | None = None
    dbg.begin_subagent(source=source_label, prompt=inputs.get("query", "") or "")
    try:
        # Drain the subagent's stream from an ISOLATED session (session=None ->
        # ReActAgent.stream auto-creates one from inputs.conversation_id). Passing
        # parent_session here would make BOTH the parent run and this helper drain
        # parent_session.stream_iterator() — ReActAgent._inner_stream routes chunks
        # through a shared per-session stream queue, so two drainers split the
        # subagent's tokens round-robin between source=main and source=subagent.
        # The isolated session is drained only by this loop, so tagging is clean
        # and nothing leaks into the parent (TUI) stream.
        async for chunk in subagent.stream(inputs, session=None):
            dbg.feed_subagent(source=source_label, chunk=chunk)
            reduced = _reduce_stream_chunk(chunk, output_parts)
            if reduced is not None:
                result = reduced
    finally:
        dbg.end_subagent(source=source_label)

    if result is None:
        result = {"output": "".join(output_parts), "result_type": "answer"}
    return result


def _reduce_stream_chunk(chunk: Any, output_parts: list[str]) -> dict | None:
    """Reduce a stream chunk to an invoke-style result dict (or ``None``).

    Mirrors the SDK's ``DeepAgent._result_from_stream_chunk`` reduction so the
    result matches what ``subagent.invoke`` would have returned: accumulate
    ``llm_output`` content and finalise on an ``answer`` chunk. Inlined (rather
    than calling the SDK staticmethod) to avoid protected-member access.
    """
    chunk_type = getattr(chunk, "type", None)
    payload = getattr(chunk, "payload", None)
    if isinstance(chunk, dict):
        chunk_type = chunk.get("type", chunk_type)
        payload = chunk.get("payload", payload)
    if not isinstance(payload, dict):
        return None
    result: dict | None = None
    if chunk_type == "llm_output":
        content = payload.get("content")
        if isinstance(content, str):
            output_parts.append(content)
    elif chunk_type == "answer":
        result = dict(payload)
        result.setdefault("result_type", "answer")
        if "output" not in result:
            content = result.get("content")
            if isinstance(content, str):
                output_parts.append(content)
                result["output"] = content
            else:
                result = None
    return result


__all__ = ["invoke_subagent_with_trace"]
