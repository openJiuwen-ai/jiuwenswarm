# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""LLM request/reasoning/response tracing for debugging.

Tracing runs only when ``jiuwenswarm.common.utils`` logger is at **DEBUG** (e.g. ``LOG_LEVEL=DEBUG``).
Lines use ``logger.debug``. Payloads may contain secrets.

Line length:
  By default each line is chunked to JIUWENSWARM_LLM_IO_TRACE_MAX_PART (8192) bytes;
  long bodies use ``body_part=i/total``.

Request body (jiuwenswarm layer):
  ``event=stream_request`` / ``invoke_request`` logs a JSON object from
  :func:`build_jiuwenswarm_llm_request_envelope` — same ``messages`` and ``tools`` as
  ``Model.stream`` / ``Model.invoke``, plus ``model``, ``max_tokens``, ``stream``, etc.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextvars import ContextVar, Token
from typing import Any, Mapping

from jiuwenswarm.common.utils import logger


# 单次 LLM 调用的 event_id，由 _apply_llm_io_trace_patch 的 invoke/stream wrapper 在进入时
# 设置、退出时重置；同一次 LLM 调用内 invoke_request/output、stream_request/output、
# reasoning_delta、chat.final 等多条 trace 行共享同一个 event_id，便于在 full.log 中
# 关联同一次 LLM 调用的完整流程。
_LLM_TRACE_EVENT_ID: ContextVar[str] = ContextVar(
    "llm_trace_event_id",
    default="",
)

# 工具调用 trace 专用 event_id。与 LLM trace 解耦，避免一次 LLM 调用内多次工具
# 调用串成同一个 event_id。tool_call_request 与对应的 tool_call_output 共享同一
# event_id，便于在 full.log 中通过 event_id grep 出完整的一次工具调用。
_TOOL_TRACE_EVENT_ID: ContextVar[str] = ContextVar(
    "tool_trace_event_id",
    default="",
)


def begin_llm_trace_event() -> Token:
    """生成一个新的 event_id 并写入 ContextVar，返回 Token 供 finally 重置。"""
    return _LLM_TRACE_EVENT_ID.set(uuid.uuid4().hex)


def end_llm_trace_event(token: Token) -> None:
    """重置 event_id ContextVar。"""
    _LLM_TRACE_EVENT_ID.reset(token)


def begin_tool_trace_event() -> Token:
    """生成一个新的工具调用 event_id，返回 Token 供 finally 重置。"""
    return _TOOL_TRACE_EVENT_ID.set(uuid.uuid4().hex)


def end_tool_trace_event(token: Token) -> None:
    """重置工具调用 event_id ContextVar。"""
    _TOOL_TRACE_EVENT_ID.reset(token)


def _current_event_id() -> str:
    return _LLM_TRACE_EVENT_ID.get()


def _current_tool_event_id() -> str:
    return _TOOL_TRACE_EVENT_ID.get()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def _llm_trace_active() -> bool:
    """Emit trace when DEBUG is on for jiuwenswarm logger."""
    return logger.isEnabledFor(logging.DEBUG)


def _serialize_one(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg
    dump = getattr(msg, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
        except Exception:
            return {"type": type(msg).__name__, "repr": repr(msg)}
    return {"type": type(msg).__name__, "repr": repr(msg)}


def format_messages_for_trace(messages: list[Any]) -> str:
    serialized = [_serialize_one(m) for m in messages]
    return json.dumps(serialized, ensure_ascii=False, default=str)


def _serialize_tool_definition(t: Any) -> Any:
    """Serialize ToolInfo / dict for request tracing."""
    if isinstance(t, dict):
        return t
    dump = getattr(t, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
        except Exception:
            return {
                "name": getattr(t, "name", None),
                "description": getattr(t, "description", None),
                "parameters": getattr(t, "parameters", None),
                "type": type(t).__name__,
            }
    return {
        "name": getattr(t, "name", None),
        "description": getattr(t, "description", None),
        "parameters": getattr(t, "parameters", None),
        "type": type(t).__name__,
    }


def build_jiuwenswarm_llm_request_envelope(
    *,
    messages: list[Any],
    tools: list[Any] | None,
    model: str,
    max_tokens: int | None,
    stream: bool,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: str | None = None,
    timeout: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured request matching the jiuwenswarm → openjiuwen ``Model`` call surface."""
    envelope: dict[str, Any] = {
        "jiuwenswarm_llm_request": True,
        "stream": stream,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stop": stop,
        "timeout": timeout,
        "messages": [_serialize_one(m) for m in messages],
        "tools": [_serialize_tool_definition(t) for t in (tools or [])],
    }
    if extra:
        envelope["extra"] = dict(extra)
    return envelope


def format_jiuwenswarm_llm_request_envelope_json(
    *,
    messages: list[Any],
    tools: list[Any] | None,
    model: str,
    max_tokens: int | None,
    stream: bool,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: str | None = None,
    timeout: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    env = build_jiuwenswarm_llm_request_envelope(
        messages=messages,
        tools=tools,
        model=model,
        max_tokens=max_tokens,
        stream=stream,
        temperature=temperature,
        top_p=top_p,
        stop=stop,
        timeout=timeout,
        extra=extra,
    )
    return json.dumps(env, ensure_ascii=False, default=str)


def _serialize_tool_calls(tool_calls: list[Any]) -> list[Any]:
    out: list[Any] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            out.append(tc)
            continue
        dump = getattr(tc, "model_dump", None)
        if callable(dump):
            try:
                out.append(dump(exclude_none=True))
            except TypeError:
                out.append(dump())
            except Exception:
                out.append({"type": type(tc).__name__, "repr": repr(tc)})
        else:
            out.append({"type": type(tc).__name__, "repr": repr(tc)})
    return out


def format_llm_assistant_for_trace(obj: Any) -> str:
    payload = {
        "role": getattr(obj, "role", None),
        "content": getattr(obj, "content", None) or "",
        "reasoning_content": getattr(obj, "reasoning_content", None),
        "tool_calls": _serialize_tool_calls(getattr(obj, "tool_calls", None) or []),
        "finish_reason": getattr(obj, "finish_reason", None),
        "usage_metadata": getattr(obj, "usage_metadata", None),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _resolve_trace_ids(session_id: str, request_id: str) -> tuple[str, str]:
    """从 session 注册表获取最新 request_id。

    supervisor task 的 ContextVar 在 task 创建时冻结，后续请求的
    request_id 无法通过 ContextVar 传播，session 注册表弥补这一缺口。
    """
    if session_id:
        try:
            from jiuwenswarm.perf.context import get_request_context

            ctx = get_request_context(session_id=session_id)
            if ctx is not None:
                rid = str(ctx.get("request_id") or "")
                if rid:
                    return session_id, rid
        except Exception:
            logger.debug(
                "[llm_trace] session registry fallback failed",
                exc_info=True,
            )
    return session_id, request_id


def _trace_header(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    event: str,
    event_id: str = "",
) -> str:
    session_id, request_id = _resolve_trace_ids(session_id, request_id)
    it = "" if iteration is None else str(iteration)
    eid = event_id or _current_event_id()
    event_id_str = f"{eid!r}" if eid else ""
    return (
        f"[LLM_IO_TRACE] event={event} event_id={event_id_str} session_id={session_id!r} "
        f"request_id={request_id!r} iteration={it} model_name={model_name!r}"
    )


def _log_body_parts(header: str, body: str) -> None:
    max_part = max(512, _env_int("JIUWENSWARM_LLM_IO_TRACE_MAX_PART", 8192))
    if len(body) <= max_part:
        logger.debug("%s body=%s", header, body)
        return
    total = (len(body) + max_part - 1) // max_part
    for i in range(total):
        chunk = body[(i * max_part):(i + 1) * max_part]
        logger.debug("%s body_part=%s/%s body=%s", header, i + 1, total, chunk)


def _log_llm_request_envelope(
    *,
    event: str,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    envelope: dict[str, Any],
) -> None:
    header = _trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        event=event,
    )
    body = json.dumps(envelope, ensure_ascii=False, default=str)
    _log_body_parts(header, body)


def log_stream_input(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    messages: list[Any],
    tools: list[Any] | None,
    max_tokens: int | None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: str | None = None,
    timeout: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    if not _llm_trace_active():
        return
    envelope = build_jiuwenswarm_llm_request_envelope(
        messages=messages,
        tools=tools,
        model=model_name,
        max_tokens=max_tokens,
        stream=True,
        temperature=temperature,
        top_p=top_p,
        stop=stop,
        timeout=timeout,
        extra=extra,
    )
    _log_llm_request_envelope(
        event="stream_request",
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        envelope=envelope,
    )


def log_invoke_input(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    messages: list[Any],
    tools: list[Any] | None,
    max_tokens: int | None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: str | None = None,
    timeout: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    if not _llm_trace_active():
        return
    envelope = build_jiuwenswarm_llm_request_envelope(
        messages=messages,
        tools=tools,
        model=model_name,
        max_tokens=max_tokens,
        stream=False,
        temperature=temperature,
        top_p=top_p,
        stop=stop,
        timeout=timeout,
        extra=extra,
    )
    _log_llm_request_envelope(
        event="invoke_request",
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        envelope=envelope,
    )


def log_reasoning_delta(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    reasoning_seq: int,
    fragment: str,
) -> None:
    if not _llm_trace_active():
        return
    header = _trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        event="reasoning_delta",
    )
    _log_body_parts(f"{header} reasoning_seq={reasoning_seq}", fragment)


def log_stream_output(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    assistant_msg: Any,
) -> None:
    if not _llm_trace_active():
        return
    header = _trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        event="stream_output",
    )
    body = format_llm_assistant_for_trace(assistant_msg)
    _log_body_parts(header, body)


def log_invoke_output(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    assistant_msg: Any,
) -> None:
    if not _llm_trace_active():
        return
    header = _trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        event="invoke_output",
    )
    body = format_llm_assistant_for_trace(assistant_msg)
    _log_body_parts(header, body)


def log_chat_final(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
) -> None:
    """Record the user-facing chat.final boundary without logging final content."""
    if not _llm_trace_active():
        return
    header = _trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        model_name=model_name,
        event="chat.final",
    )
    _log_body_parts(header, "")


def _tool_trace_header(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    agent: str,
    tool_name: str,
    tool_call_id: str,
    event: str,
) -> str:
    """工具调用 trace 行头部，复用 LLM_IO_TRACE 前缀以便统一日志解析。"""
    session_id, request_id = _resolve_trace_ids(session_id, request_id)
    it = "" if iteration is None else str(iteration)
    eid = _current_tool_event_id()
    event_id_str = f"{eid!r}" if eid else ""
    return (
        f"[LLM_IO_TRACE] event={event} event_id={event_id_str} session_id={session_id!r} "
        f"request_id={request_id!r} iteration={it} agent={agent!r} "
        f"tool_name={tool_name!r} tool_call_id={tool_call_id!r}"
    )


def log_tool_call_input(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    agent: str,
    tool_name: str,
    tool_call_id: str,
    args: Mapping[str, Any] | None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """记录一次工具调用的入参。仅在 DEBUG 级别有效。"""
    if not _llm_trace_active():
        return
    header = _tool_trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        agent=agent,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        event="tool_call_request",
    )
    payload: dict[str, Any] = {"args": _serialize_one(dict(args or {}))}
    if extra:
        payload["extra"] = _serialize_one(dict(extra))
    body = json.dumps(payload, ensure_ascii=False, default=str)
    _log_body_parts(header, body)


def log_tool_call_output(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    agent: str,
    tool_name: str,
    tool_call_id: str,
    status: str,
    duration_ms: float,
    result: Any = None,
    error: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """记录一次工具调用的出参。``status`` 取值：``ok`` / ``error`` / ``skipped`` / ``interrupted``。"""
    if not _llm_trace_active():
        return
    header = _tool_trace_header(
        session_id=session_id,
        request_id=request_id,
        iteration=iteration,
        agent=agent,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        event="tool_call_output",
    )
    payload: dict[str, Any] = {
        "status": status,
        "duration_ms": round(float(duration_ms), 3),
    }
    if result is not None:
        payload["result"] = _serialize_one(result)
    if error is not None:
        payload["error"] = str(error)
    if extra:
        payload["extra"] = _serialize_one(dict(extra))
    body = json.dumps(payload, ensure_ascii=False, default=str)
    _log_body_parts(header, body)
