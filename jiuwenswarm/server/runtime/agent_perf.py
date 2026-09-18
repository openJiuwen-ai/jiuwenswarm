# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified AgentPerf phase logs: ``phase=`` + ``request_id=`` + ``elapsed_ms=``.

Session 队列会 ``create_task`` 到长期 processor 上跑，ContextVar 不会从提交方带过去，
因此时间戳按 request_id 落一份进程内表，新 Task 里用 :func:`restore` 接上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_request_id: ContextVar[str] = ContextVar("agent_perf_request_id", default="")
_t_request: ContextVar[float] = ContextVar("agent_perf_t_request", default=0.0)
_t_agent_ready: ContextVar[float] = ContextVar("agent_perf_t_agent_ready", default=0.0)
_t_invoke: ContextVar[float] = ContextVar("agent_perf_t_invoke", default=0.0)
_t_runner: ContextVar[float] = ContextVar("agent_perf_t_runner", default=0.0)

_TIMINGS: dict[str, dict[str, float]] = {}


def _now() -> float:
    return time.perf_counter()


def _ms_since(t0: float) -> float:
    if t0 <= 0.0:
        return -1.0
    return (_now() - t0) * 1000.0


def _slot(request_id: str) -> dict[str, float]:
    slot = _TIMINGS.get(request_id)
    if slot is None:
        slot = {}
        if request_id:
            _TIMINGS[request_id] = slot
    return slot


def current_request_id() -> str:
    return _request_id.get() or ""


def bind_request(request_id: str | None) -> None:
    """Record request arrival (idempotent for request clock)."""
    rid = str(request_id or "").strip()
    _request_id.set(rid)
    slot = _slot(rid)
    t0 = slot.get("request") or 0.0
    if t0 <= 0.0:
        t0 = _now()
        slot["request"] = t0
    _t_request.set(t0)


def mark_agent_ready(request_id: str | None = None) -> None:
    """Call immediately after get_agent returns."""
    rid = str(request_id or _request_id.get() or "").strip()
    if rid and not _request_id.get():
        bind_request(rid)
    t0 = _now()
    _t_agent_ready.set(t0)
    if rid:
        _slot(rid)["agent_ready"] = t0


def restore(request_id: str | None) -> None:
    """Re-bind clocks in a new asyncio Task (session queue worker)."""
    rid = str(request_id or "").strip()
    _request_id.set(rid)
    slot = _TIMINGS.get(rid) or {}
    _t_request.set(float(slot.get("request") or 0.0))
    _t_agent_ready.set(float(slot.get("agent_ready") or 0.0))
    _t_invoke.set(float(slot.get("invoke") or 0.0))
    _t_runner.set(float(slot.get("runner") or 0.0))


def clear(request_id: str | None) -> None:
    rid = str(request_id or "").strip()
    if rid:
        _TIMINGS.pop(rid, None)


def reset_for_tests() -> None:
    """Clear process-wide timing state (unit tests only)."""
    _TIMINGS.clear()
    _request_id.set("")
    _t_request.set(0.0)
    _t_agent_ready.set(0.0)
    _t_invoke.set(0.0)
    _t_runner.set(0.0)


def elapsed_from_request() -> float:
    return _ms_since(_t_request.get())


def elapsed_from_agent_ready() -> float:
    t0 = _t_agent_ready.get()
    if t0 <= 0.0:
        return elapsed_from_request()
    return _ms_since(t0)


def elapsed_from_invoke() -> float:
    t0 = _t_invoke.get()
    if t0 <= 0.0:
        return elapsed_from_request()
    return _ms_since(t0)


def elapsed_from_runner() -> float:
    t0 = _t_runner.get()
    if t0 <= 0.0:
        return elapsed_from_invoke()
    return _ms_since(t0)


def _store_mark(name: str) -> float:
    t0 = _now()
    rid = _request_id.get()
    if name == "invoke":
        _t_invoke.set(t0)
    elif name == "runner":
        _t_runner.set(t0)
    if rid:
        _slot(rid)[name] = t0
    return t0


async def event_loop_lag_ms() -> float:
    """Yield once; high value means the loop was busy before we were scheduled again."""
    t0 = _now()
    await asyncio.sleep(0)
    return (_now() - t0) * 1000.0


def _fmt_field(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key}={str(value).lower()}"
    if isinstance(value, float):
        return f"{key}={value:.1f}"
    return f"{key}={value}"


def log_phase(phase: str, elapsed_ms: float, **fields: Any) -> None:
    """兼容旧 phase= 格式；压测汇总也能从行内抠 ``*_ms``。"""
    parts = [
        f"phase={phase}",
        f"request_id={_request_id.get() or '-'}",
        f"elapsed_ms={elapsed_ms:.1f}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(_fmt_field(key, value))
    logger.info("[AgentPerf] %s", " ".join(parts))


def log_event(event: str, *, request_id: str | None = None, **fields: Any) -> None:
    """文档约定格式：``[AgentPerf] <事件名>: request_id=... <key>_ms=...``。"""
    rid = str(request_id or _request_id.get() or "-").strip() or "-"
    parts = [f"{event}:", f"request_id={rid}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(_fmt_field(key, value))
    logger.info("[AgentPerf] %s", " ".join(parts))


def approx_json_bytes(obj: Any) -> int:
    """Best-effort UTF-8 size of a JSON dump (for spotting huge LLM bodies)."""
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return -1


async def approx_json_bytes_async(obj: Any) -> int:
    """同步估算 body 大小（本移植不做 to_thread）。"""
    return approx_json_bytes(obj)


async def log_invoke_start() -> None:
    """get_agent 已返回后、真正进入 adapter 业务时调用。"""
    elapsed = elapsed_from_agent_ready()
    _store_mark("invoke")
    lag = await event_loop_lag_ms()
    log_phase(
        "invoke_start",
        elapsed,
        since_request_ms=elapsed_from_request(),
        event_loop_lag_ms=lag,
    )


def log_runtime_config_ms(elapsed_ms: float) -> None:
    log_phase("runtime_config_ms", elapsed_ms)


async def log_runner_start() -> None:
    """Immediately before Runner.run_agent / run_agent_streaming."""
    elapsed = elapsed_from_invoke()
    _store_mark("runner")
    lag = await event_loop_lag_ms()
    log_phase(
        "runner_start",
        elapsed,
        since_request_ms=elapsed_from_request(),
        event_loop_lag_ms=lag,
    )


async def log_llm_http_start(*, body_bytes: int | None = None) -> float:
    """Right before the HTTP stream/invoke is awaited. Returns http start clock."""
    pre_llm = elapsed_from_runner()
    lag = await event_loop_lag_ms()
    log_phase("pre_llm_ms", pre_llm, body_bytes=body_bytes)
    log_event(
        "llm_http_start",
        pre_llm_ms=pre_llm,
        body_bytes=body_bytes,
        event_loop_lag_ms=lag,
        since_request_ms=elapsed_from_request(),
    )
    return _now()


def log_llm_first_token_ms(t_http: float) -> None:
    ttft = _ms_since(t_http)
    log_event("llm_first_token", ttft_ms=ttft)


def log_llm_http_done(t_http: float, *, chunk_count: int | None = None) -> None:
    latency = _ms_since(t_http)
    log_event("llm_http_done", latency_ms=latency, chunk_count=chunk_count)


async def iter_llm_stream(
    chunks: AsyncIterator[_T],
    *,
    body_bytes: int | None = None,
) -> AsyncIterator[_T]:
    """Wrap an LLM stream: llm_http_start → first_token → http_done."""
    t_http = await log_llm_http_start(body_bytes=body_bytes)
    first = True
    count = 0
    try:
        async for chunk in chunks:
            count += 1
            if first:
                first = False
                log_llm_first_token_ms(t_http)
            yield chunk
    finally:
        log_llm_http_done(t_http, chunk_count=count)
