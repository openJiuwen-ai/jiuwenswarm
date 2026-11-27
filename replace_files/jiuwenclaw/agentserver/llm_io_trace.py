# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""LLM request/reasoning/response tracing for debugging.

Tracing runs only when ``jiuwenclaw.utils`` logger is at **DEBUG** (e.g. ``LOG_LEVEL=DEBUG``).
Lines use ``logger.debug``. Payloads may contain secrets.

Line length:
  By default each line is chunked to JIUWENCLAW_LLM_IO_TRACE_MAX_PART (8192) bytes;
  long bodies use ``body_part=i/total``.

Request body (jiuwenclaw layer):
  ``event=stream_request`` / ``invoke_request`` logs a JSON object from
  :func:`build_jiuwenclaw_llm_request_envelope` — same ``messages`` and ``tools`` as
  ``Model.stream`` / ``Model.invoke``, plus ``model``, ``max_tokens``, ``stream``, etc.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

from jiuwenclaw.utils import logger


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def _llm_trace_active() -> bool:
    """Emit trace when DEBUG is on for jiuwenclaw logger."""
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
    return {
        "name": getattr(t, "name", None),
        "description": getattr(t, "description", None),
        "parameters": getattr(t, "parameters", None),
        "type": type(t).__name__,
    }


def build_jiuwenclaw_llm_request_envelope(
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
    """Structured request matching the jiuwenclaw → openjiuwen ``Model`` call surface."""
    envelope: dict[str, Any] = {
        "jiuwenclaw_llm_request": True,
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


def format_jiuwenclaw_llm_request_envelope_json(
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
    env = build_jiuwenclaw_llm_request_envelope(
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


def _trace_header(
    *,
    session_id: str,
    request_id: str,
    iteration: int | None,
    model_name: str,
    event: str,
) -> str:
    it = "" if iteration is None else str(iteration)
    return (
        f"[LLM_IO_TRACE] event={event} session_id={session_id!r} "
        f"request_id={request_id!r} iteration={it} model_name={model_name!r}"
    )


def _log_body_parts(header: str, body: str) -> None:
    max_part = max(512, _env_int("JIUWENCLAW_LLM_IO_TRACE_MAX_PART", 8192))
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
    envelope = build_jiuwenclaw_llm_request_envelope(
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
    envelope = build_jiuwenclaw_llm_request_envelope(
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