# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deep adapter integration hooks for request_summaries.jsonl."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.perf.collector import get_perf_collector
from jiuwenswarm.perf.config import get_perf_summary_config
from jiuwenswarm.perf.context import (
    DeepResearchReportType,
    clear_request_context,
    mark_first_answer_latency,
    mark_first_byte_latency,
    set_request_context,
)
from jiuwenswarm.perf.guard import run_perf_safe

_COMPONENT = "JiuWenSwarmDeepAdapter"

# Align with history.jsonl assistant surface: first tool/reasoning/answer event.
_FIRST_BYTE_EVENT_TYPES = frozenset(
    {"chat.delta", "chat.final", "chat.tool_call", "chat.reasoning"}
)
# Answer-token only (excludes tool_call / reasoning).
_FIRST_ANSWER_EVENT_TYPES = frozenset({"chat.delta", "chat.final"})


def snapshot_perf_summary_usage(request_id: str | None) -> dict[str, int] | None:
    """Read request-scoped token totals before the perf accumulator is finalized."""
    rid = (request_id or "").strip()
    if not rid:
        return None
    acc = get_perf_collector().get_accumulator(rid)
    if acc is None:
        return None
    input_tokens = max(0, int(getattr(acc, "input_tokens", 0) or 0))
    output_tokens = max(0, int(getattr(acc, "output_tokens", 0) or 0))
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    total_tokens = max(0, int(getattr(acc, "total_tokens", 0) or 0))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or input_tokens + output_tokens,
        "cache_tokens": max(0, int(getattr(acc, "cache_read_tokens", 0) or 0)),
    }


def record_deepresearch_sdk_token_usage(
    request_id: str | None,
    usage_id: str,
    usage: dict[str, Any],
) -> None:
    """Best-effort accounting for one terminal DeepResearch SDK usage snapshot."""
    rid = (request_id or "").strip()
    if not rid:
        return

    def _record() -> None:
        get_perf_collector().record_external_token_usage(
            rid,
            source="deepresearch_sdk",
            usage_id=usage_id,
            input_tokens=int(usage["input_tokens"]),
            output_tokens=int(usage["output_tokens"]),
            total_tokens=int(usage["total_tokens"]),
        )

    run_perf_safe(
        _COMPONENT,
        f"external token usage request_id={rid} source=deepresearch_sdk",
        _record,
    )


def merge_perf_summary_usage_fallback(
    stream_usage: dict[str, Any],
    perf_usage: dict[str, int] | None,
) -> bool:
    """Recover a dropped llm_usage frame without double-counting normal streams."""
    if not perf_usage:
        return False
    stream_total = max(
        int(stream_usage.get("total_tokens", 0) or 0),
        int(stream_usage.get("input_tokens", 0) or 0)
        + int(stream_usage.get("output_tokens", 0) or 0),
    )
    perf_total = int(perf_usage.get("total_tokens", 0) or 0)
    if perf_total <= stream_total:
        stream_usage["cache_tokens"] = max(
            int(stream_usage.get("cache_tokens", 0) or 0),
            int(perf_usage.get("cache_tokens", 0) or 0),
        )
        return False
    for key in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
        stream_usage[key] = int(perf_usage.get(key, 0) or 0)
    return True


def set_perf_summary_context(
    rail: Any | None = None,
    *,
    channel_id: str = "",
    session_id: str = "",
    request_id: str = "",
    mode: str = "agent.plan",
    service_id: str | None = None,
    agent_id: str | None = None,
    requested_report_type: DeepResearchReportType | None = None,
    deepresearch_followup: dict[str, Any] | None = None,
) -> None:
    """Bind perf request context at parent-agent request entry.

    Always binds the execution request context, even when performance summaries
    are disabled. When collection is enabled, it also begins the collector
    request and binds a RequestSummaryRail so interaction-round hooks can resolve
    request_id without relying on ContextVar inheritance.
    """

    def _setup() -> None:
        perf_enabled = get_perf_summary_config().enabled
        ctx = set_request_context(
            session_id=session_id,
            request_id=request_id,
            channel_id=channel_id,
            mode=mode,
            service_id=service_id,
            agent_id=agent_id,
            requested_report_type=requested_report_type,
            deepresearch_followup=deepresearch_followup,
            begin_perf_collection=perf_enabled,
        )
        if not perf_enabled:
            return
        bind = getattr(rail, "bind_request_context", None)
        if callable(bind):
            bind(ctx)

    run_perf_safe(
        _COMPONENT,
        "perf summary context setup",
        _setup,
    )


def mark_request_stage(request_id: str | None, stage: str) -> None:
    """Record a stage timestamp for the monitoring summary log (#6745).

    First mark per stage wins; no-op when no accumulator exists for the
    request (perf collection disabled). Safe to call from log-only sites.
    """
    rid = (request_id or "").strip()
    if not rid:
        return

    def _mark() -> None:
        get_perf_collector().mark_stage(rid, stage)

    run_perf_safe(
        _COMPONENT,
        f"perf summary stage mark request_id={rid} stage={stage}",
        _mark,
    )


def resolve_stream_request_id() -> str:
    """Resolve the request id for model-client-stream-level hooks (#6745).

    The model client stream may run on long-lived runtime tasks that do NOT
    inherit the stream-task ContextVars, so resolution falls back to contexts
    that the runtime propagates itself:

    1. perf request ContextVar (direct inheritance, the common case);
    2. openjiuwen trace session id → perf session registry (the runtime
       re-binds its own session context on its worker tasks — the same channel
       the ``[MODEL_HTTP]`` log lines use);
    3. deep-adapter LLM trace request-id ContextVar.
    """
    from jiuwenswarm.perf.context import get_request_context

    rid = str((get_request_context() or {}).get("request_id") or "").strip()
    if rid:
        return rid
    try:
        from openjiuwen.core.common.logging import get_session_id

        sid = str(get_session_id() or "").strip()
        if sid and sid not in ("default", "default_trace_id"):
            rid = str(
                (get_request_context(session_id=sid) or {}).get("request_id") or ""
            ).strip()
    except Exception:
        rid = ""
    if rid:
        return rid
    try:
        from jiuwenswarm.server.runtime.agent_adapter.deep.llm_io_trace_patch import (
            _LLM_TRACE_REQUEST_ID,
        )

        rid = (_LLM_TRACE_REQUEST_ID.get() or "").strip()
    except Exception:
        rid = ""
    return rid


def record_llm_stream_timing(
    *,
    ttft_ms: float | None,
    total_ms: float,
    chunk_count: int,
    status: str,
) -> None:
    """Record LLM HTTP stream timing from llm_sse_patch (last call wins)."""
    rid = resolve_stream_request_id()
    if not rid:
        return

    def _record() -> None:
        get_perf_collector().record_llm_stream_timing(
            rid,
            ttft_ms=ttft_ms,
            total_ms=total_ms,
            chunk_count=chunk_count,
            status=status,
        )

    run_perf_safe(
        _COMPONENT,
        f"llm stream timing request_id={rid}",
        _record,
    )


def record_stream_metric(
    request_id: str | None,
    metric: str,
    *,
    duration_ms: float | None = None,
    name: str | None = None,
) -> None:
    """Record one stream-level metric for the monitoring summary log.

    Metrics: "thinking" | "msg_gen" | "gap" (duration_ms), "chat_error",
    "ask_emitted" (no payload), "skill" (name = skill display name).
    """
    rid = (request_id or "").strip()
    if not rid:
        return
    collector = get_perf_collector()
    if metric == "thinking":
        if duration_ms is None:
            return

        def _action() -> None:
            collector.record_thinking_segment(rid, duration_ms)
    elif metric == "msg_gen":
        if duration_ms is None:
            return

        def _action() -> None:
            collector.record_message_gen(rid, duration_ms)
    elif metric == "gap":
        if duration_ms is None:
            return

        def _action() -> None:
            collector.record_stream_gap(rid, duration_ms)
    elif metric == "chat_error":

        def _action() -> None:
            collector.record_chat_error(rid)
    elif metric == "ask_emitted":

        def _action() -> None:
            collector.mark_ask_emitted(rid)
    elif metric == "skill":
        if not (name or "").strip():
            return

        def _action() -> None:
            collector.record_skill_usage(rid, name or "")
    else:
        return
    run_perf_safe(
        _COMPONENT,
        f"stream metric request_id={rid} metric={metric}",
        _action,
    )


def record_chunk_sent(request_id: str | None) -> None:
    """Count one wire chunk actually sent to the client (summary ``chunks``)."""
    rid = (request_id or "").strip()
    if not rid:
        return

    def _record() -> None:
        get_perf_collector().record_chunk_sent(rid)

    run_perf_safe(
        _COMPONENT,
        f"chunk sent count request_id={rid}",
        _record,
    )


def has_request_in_flight(request_id: str | None) -> bool:
    """True while a rail-tracked model call or tool is running for this request.

    Cross-task safe: the counters live on the shared accumulator (hook tasks
    and the stream task do not share ContextVars).
    """
    rid = (request_id or "").strip()
    if not rid:
        return False
    try:
        acc = get_perf_collector().get_accumulator(rid)
    except Exception:
        return False
    return acc.has_in_flight() if acc is not None else False


def record_stream_context_usage(
    request_id: str | None,
    *,
    limit_tokens: Any,
    occupancy_rate: Any,
) -> None:
    """Record a context.usage snapshot for the monitoring summary log."""
    rid = (request_id or "").strip()
    if not rid:
        return

    def _record() -> None:
        get_perf_collector().record_context_usage(
            rid,
            limit_tokens=limit_tokens,
            occupancy_rate=occupancy_rate,
        )

    run_perf_safe(
        _COMPONENT,
        f"context usage request_id={rid}",
        _record,
    )


def mark_request_first_byte() -> None:
    """Record first history-visible assistant event latency."""
    run_perf_safe(
        _COMPONENT,
        "perf summary first-byte mark",
        mark_first_byte_latency,
    )


def mark_request_first_answer() -> None:
    """Record first answer-token latency (chat.delta / chat.final)."""
    run_perf_safe(
        _COMPONENT,
        "perf summary first-answer mark",
        mark_first_answer_latency,
    )


def maybe_mark_answer_first_byte(payload: Any) -> None:
    """Mark first-byte / first-answer from a streamed chat payload."""
    if not isinstance(payload, dict):
        return
    event_type = payload.get("event_type")
    if event_type in _FIRST_BYTE_EVENT_TYPES:
        mark_request_first_byte()
    if event_type in _FIRST_ANSWER_EVENT_TYPES:
        mark_request_first_answer()


def finalize_perf_summary_request(
    request_id: str | None,
    *,
    status: str = "ok",
) -> None:
    """Finalize and persist request summary; safe to call from finally blocks."""
    rid = (request_id or "").strip()
    if not rid:
        return

    def _finalize() -> None:
        from jiuwenswarm.perf.extract import current_trace_id_hex

        trace_id = current_trace_id_hex()
        acc = get_perf_collector().get_accumulator(rid)
        if acc is not None and trace_id:
            acc.meta = acc.meta.with_trace_id(trace_id)
        get_perf_collector().finalize_request(rid, status=status)

    run_perf_safe(
        _COMPONENT,
        f"perf summary finalize request_id={rid} status={status}",
        _finalize,
    )


def clear_perf_summary_context(
    rail: Any | None = None,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """Clear perf ContextVar + session registry + optional rail binding."""

    def _clear() -> None:
        clear_fn = getattr(rail, "clear_bound_request_context", None)
        if callable(clear_fn):
            clear_fn()
        clear_request_context(session_id=session_id, request_id=request_id)

    run_perf_safe(
        _COMPONENT,
        "perf summary context clear",
        _clear,
    )


def install_perf_hooks(_adapter: Any = None) -> None:
    """Install create_subagent RequestSummaryRail(record_only) attachment.

    Parent DeepAdapter already wires the rail in ``interface_deep`` /
    ``interface_code``. This covers TaskTool / SessionSpawn children via
    ``DeepAgent.create_subagent`` patching.
    """
    from jiuwenswarm.perf.subagent_hooks import apply_create_subagent_perf_patch

    apply_create_subagent_perf_patch()
