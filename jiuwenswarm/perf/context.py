# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar
from typing import Any, Literal, TypeAlias

logger = logging.getLogger(__name__)

DeepResearchReportType: TypeAlias = Literal["professional", "brief"]

_request_context: ContextVar[dict[str, Any] | None] = ContextVar("perf_request_context", default=None)
_request_wall_start: ContextVar[float | None] = ContextVar("perf_request_wall_start", default=None)
_current_llm_call_id: ContextVar[str | None] = ContextVar("perf_current_llm_call_id", default=None)
_current_llm_call_start: ContextVar[float | None] = ContextVar("perf_current_llm_call_start", default=None)
_tool_starts: ContextVar[dict[str, tuple[str, float]] | None] = ContextVar(
    "perf_tool_starts",
    default=None,
)
_inherited_task_id: ContextVar[str | None] = ContextVar("perf_inherited_task_id", default=None)
_react_iteration: ContextVar[int] = ContextVar("perf_react_iteration", default=0)

# DeepAgent runs LLM/tool hooks on a long-lived supervisor/round Task that does
# NOT inherit the stream-handler ContextVar. Session registry bridges that gap.
_REGISTRY_LOCK = threading.Lock()
_SESSION_ACTIVE: dict[str, dict[str, Any]] = {}


def normalize_session_key(session_id: str | None) -> str:
    """Map subagent session ids back to the parent session key."""
    sid = (session_id or "").strip() or "default"
    marker = "_sub_"
    if marker in sid:
        parent = sid.split(marker, 1)[0].strip()
        return parent or sid
    return sid


def set_request_context(
    *,
    session_id: str,
    request_id: str,
    channel_id: str,
    mode: str,
    trace_id: str | None = None,
    service_id: str | None = None,
    agent_id: str | None = None,
    requested_report_type: DeepResearchReportType | None = None,
    begin_perf_collection: bool = True,
) -> dict[str, Any]:
    started_at = time.time()
    ctx: dict[str, Any] = {
        "session_id": session_id,
        "request_id": request_id,
        "channel_id": channel_id,
        "mode": mode,
        "trace_id": trace_id,
        "started_at": started_at,
        "service_id": service_id,
        "agent_id": agent_id,
        "requested_report_type": requested_report_type,
    }
    _request_context.set(ctx)
    _request_wall_start.set(started_at)
    with _REGISTRY_LOCK:
        _SESSION_ACTIVE[normalize_session_key(session_id)] = ctx
    if begin_perf_collection:
        from jiuwenswarm.perf.collector import get_perf_collector

        get_perf_collector().begin_request(
            session_id=session_id,
            request_id=request_id,
            channel_id=channel_id,
            mode=mode,
            trace_id=trace_id,
            started_at=started_at,
            service_id=service_id,
            agent_id=agent_id,
        )
    return ctx


def clear_request_context(
    *,
    session_id: str | None = None,
    request_id: str | None = None,
) -> None:
    _request_context.set(None)
    _request_wall_start.set(None)
    _current_llm_call_id.set(None)
    _current_llm_call_start.set(None)
    _tool_starts.set(None)
    _inherited_task_id.set(None)
    _react_iteration.set(0)

    rid = (request_id or "").strip()
    with _REGISTRY_LOCK:
        if rid:
            for key, value in list(_SESSION_ACTIVE.items()):
                if str(value.get("request_id") or "").strip() == rid:
                    _SESSION_ACTIVE.pop(key, None)
        elif session_id is not None:
            _SESSION_ACTIVE.pop(normalize_session_key(session_id), None)


def reset_react_iteration() -> None:
    _react_iteration.set(0)


def increment_react_iteration() -> int:
    iteration = int(_react_iteration.get() or 0) + 1
    _react_iteration.set(iteration)
    return iteration


def get_react_iteration() -> int:
    return int(_react_iteration.get() or 0)


def set_inherited_task_id(task_id: str | None) -> None:
    _inherited_task_id.set(task_id)


def set_active_task_id(
    task_id: str | None,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """Bind the current main-agent todo id onto the active request context.

    Stored on the shared request-context dict (ContextVar + session registry +
    rail binding all reference the same object when set at request entry), so
    interaction-round LLM/tool hooks can attribute without TaskExecutionRail.
    """
    tid = (task_id or "").strip() or None
    ctx = _request_context.get()
    if ctx is not None:
        ctx["active_task_id"] = tid

    rid = (request_id or "").strip()
    with _REGISTRY_LOCK:
        if rid:
            for value in _SESSION_ACTIVE.values():
                if str(value.get("request_id") or "").strip() == rid:
                    value["active_task_id"] = tid
            return
        if session_id is not None:
            entry = _SESSION_ACTIVE.get(normalize_session_key(session_id))
            if entry is not None:
                entry["active_task_id"] = tid


def resolve_task_id(
    *,
    request_ctx: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str | None:
    """Resolve task_id for llm/tool attribution.

    Order: TaskExecutionRail ContextVar (if enabled) → inherited ContextVar →
    active_task_id on the request context (main-agent todo binding).
    """
    try:
        from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
            get_current_task_id,
        )

        task_id = get_current_task_id()
    except Exception:
        task_id = None
    if task_id:
        return str(task_id).strip() or None

    inherited = _inherited_task_id.get()
    if inherited:
        return str(inherited).strip() or None

    ctx = request_ctx if request_ctx is not None else get_request_context(session_id=session_id)
    if ctx is None:
        return None
    return str(ctx.get("active_task_id") or "").strip() or None


def get_request_context(*, session_id: str | None = None) -> dict[str, Any] | None:
    """Return active request context (task-local ContextVar, else session registry)."""
    ctx = _request_context.get()
    if ctx is not None:
        return ctx
    if session_id is None:
        return None
    with _REGISTRY_LOCK:
        return _SESSION_ACTIVE.get(normalize_session_key(session_id))


def extract_session_id_from_callback(ctx: Any) -> str | None:
    """Best-effort session id from AgentCallbackContext / agent inputs."""
    if ctx is None:
        return None
    session = getattr(ctx, "session", None)
    if session is not None:
        getter = getattr(session, "get_session_id", None)
        if callable(getter):
            try:
                sid = str(getter() or "").strip()
                if sid:
                    return sid
            except Exception as exc:
                logger.debug(
                    "[perf] session.get_session_id() failed, falling back: %s",
                    exc,
                    exc_info=True,
                )
        sid = str(getattr(session, "session_id", "") or "").strip()
        if sid:
            return sid

    inputs = getattr(ctx, "inputs", None)
    if inputs is not None:
        for attr in ("conversation_id", "session_id", "parent_session_id"):
            value = getattr(inputs, attr, None)
            if value is None and isinstance(inputs, dict):
                value = inputs.get(attr)
            sid = str(value or "").strip()
            if sid:
                return sid
    return None


def get_request_wall_start(*, session_id: str | None = None) -> float | None:
    wall = _request_wall_start.get()
    if wall is not None:
        return wall
    ctx = get_request_context(session_id=session_id)
    if ctx is None:
        return None
    started = ctx.get("started_at")
    return float(started) if isinstance(started, (int, float)) else None


def mark_first_byte_latency() -> None:
    wall_start = _request_wall_start.get()
    ctx = _request_context.get()
    if wall_start is None or ctx is None:
        return
    request_id = str(ctx.get("request_id") or "")
    latency_ms = max(0.0, (time.time() - wall_start) * 1000)
    from jiuwenswarm.perf.collector import get_perf_collector

    get_perf_collector().mark_first_byte_latency(request_id, latency_ms)


def mark_first_answer_latency() -> None:
    wall_start = _request_wall_start.get()
    ctx = _request_context.get()
    if wall_start is None or ctx is None:
        return
    request_id = str(ctx.get("request_id") or "")
    latency_ms = max(0.0, (time.time() - wall_start) * 1000)
    from jiuwenswarm.perf.collector import get_perf_collector

    get_perf_collector().mark_first_answer_latency(request_id, latency_ms)


def set_current_llm_call(call_id: str, start_monotonic: float) -> None:
    _current_llm_call_id.set(call_id)
    _current_llm_call_start.set(start_monotonic)


def get_current_llm_call() -> tuple[str | None, float | None]:
    return _current_llm_call_id.get(), _current_llm_call_start.get()


def clear_current_llm_call() -> None:
    _current_llm_call_id.set(None)
    _current_llm_call_start.set(None)


def set_tool_start(tool_call_id: str, tool_name: str, start_monotonic: float) -> None:
    # Copy-on-write: asyncio.create_task copies ContextVar *values* by reference;
    # mutating a shared dict would race parent/child tool timings.
    key = (tool_call_id or "").strip() or f"__anon__{tool_name}_{start_monotonic}"
    starts = dict(_tool_starts.get() or {})
    starts[key] = (tool_name, start_monotonic)
    _tool_starts.set(starts)


def pop_tool_start(tool_call_id: str, tool_name: str = "") -> tuple[str, float] | None:
    key = (tool_call_id or "").strip()
    starts = _tool_starts.get()
    if not starts:
        return None
    # Copy-on-write before mutate (see set_tool_start).
    starts = dict(starts)
    if key and key in starts:
        tool_name_stored, start_monotonic = starts.pop(key)
        _tool_starts.set(starts or None)
        return tool_name_stored, start_monotonic
    if len(starts) == 1:
        only_key = next(iter(starts))
        tool_name_stored, start_monotonic = starts.pop(only_key)
        _tool_starts.set(None)
        return tool_name_stored, start_monotonic
    anon_for_name = [
        candidate_key
        for candidate_key, value in list(starts.items())
        if candidate_key.startswith("__anon__") and tool_name and value[0] == tool_name
    ]
    if len(anon_for_name) == 1:
        candidate_key = anon_for_name[0]
        value = starts.pop(candidate_key)
        _tool_starts.set(starts or None)
        return value
    return None
