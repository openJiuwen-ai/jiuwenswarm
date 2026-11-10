# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Observe final provider input through existing Core callbacks, without copying it into events."""
from __future__ import annotations

from contextvars import ContextVar
import logging
import os
import time
from typing import Any
import weakref

from openjiuwen.core.foundation.llm.call_scope import (
    get_current_llm_call_id,
    is_llm_observation_suppressed,
)
from openjiuwen.core.session.stream import OutputSchema

from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session import llm_audit

logger = logging.getLogger(__name__)
_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar("swarm_llm_audit_scope", default=None)
_INSTALLED: weakref.WeakSet = weakref.WeakSet()


def bind_audit_scope(*, session_id: str, request_id: str, member_name: str = "", role: str = "",
                     channel_id: str = "web", mode: str = "agent") -> None:
    """Bind only small identities in the actual model-call task, including persistent agents."""
    _SCOPE.set({"session_id": session_id, "request_id": request_id,
                "member_name": member_name, "role": role, "channel_id": channel_id, "mode": mode, "calls": {}})


def _preview(messages: Any) -> str:
    parts, remaining = [], 2048
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        if not isinstance(content, str):
            continue
        part = content[:remaining]
        parts.append(part)
        remaining -= len(part)
        if remaining <= 0:
            break
    return "\n".join(parts)[:2048]


def _target() -> dict[str, Any] | None:
    scope = _SCOPE.get()
    if scope is None or is_llm_observation_suppressed():
        return None
    from openjiuwen.agent_teams.context import get_session_id
    from jiuwenswarm.agents.harness.agent_observability import _subagent_from_session_id

    sid = get_session_id() or ""
    sub = _subagent_from_session_id(sid)
    if sub:
        return {**scope, "session_id": sub[0], "sub_session_id": sid, "subagent_type": sub[1]}
    return scope


async def _emit(state: dict[str, Any], kind: str, **payload: Any) -> None:
    target, record = state["target"], state["record"]
    payload = {"audit_schema": "llm-call.v1", "call_id": record["call_id"], "member_name": target.get("member_name"),
               "role": target.get("role"), **payload}
    if target.get("sub_session_id"):
        from jiuwenswarm.agents.harness.agent_observability import _append_subagent_llm_record

        await run_history_io(
            _append_subagent_llm_record, parent_id=target["session_id"],
            subagent_type=target["subagent_type"],
            ctx=(target["request_id"], target["channel_id"], target["mode"]), event_type="chat." + kind, extra=payload,
        )
    elif state.get("session") is not None:
        await state["session"].write_stream(OutputSchema(type=kind, index=0, payload=payload))


async def _write(state: dict[str, Any]) -> None:
    await run_history_io(llm_audit.write_call, state["target"]["session_id"], dict(state["record"]))


async def _on_input(*args: Any, session: Any = None, **kwargs: Any) -> None:
    target, identity = _target(), get_current_llm_call_id()
    if target is None or not identity:
        return
    scope = _SCOPE.get()
    if identity in scope["calls"]:
        return
    import psutil

    record = {"schema_version": "llm-call.v1", "call_id": identity, "started_ns": time.time_ns(), "status": "running",
              "request_id": target["request_id"], "model_name": str(kwargs.get("model") or ""),
              "member_name": target.get("member_name", ""), "role": target.get("role", ""),
              "sub_session_id": target.get("sub_session_id"), "subagent_type": target.get("subagent_type"),
              "worker_pid": os.getpid(), "worker_created": psutil.Process().create_time()}
    state = {"target": target, "record": record, "session": session}
    scope["calls"][identity] = state
    preview = _preview(kwargs.get("messages"))
    try:
        await _emit(state, "llm_call_start", prompt=preview, model_name=record["model_name"],
                    llm_start_ts=record["started_ns"] / 1e9)
        await _write(state)
        record["input"] = await run_history_io(
            llm_audit.save_input, target["session_id"], kwargs.get("messages") or [], kwargs.get("tools") or [],
        )
        await _write(state)
        await _emit(state, "llm_call_input", input_ref=record["input"])
    except Exception:
        record["capture_error"] = "Model input could not be saved"
        logger.warning("LLM audit input unavailable: session=%s call=%s", target["session_id"], identity, exc_info=True)


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage_metadata") or response.get("usage")
    dump = getattr(usage, "model_dump", None)
    value = dump(mode="json") if callable(dump) else usage
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if key in {"input_tokens", "output_tokens", "total_tokens", "cache_tokens",
                       "prompt_tokens", "completion_tokens"} and isinstance(number, (int, float))}


async def _finish(state: dict[str, Any], *, response: Any = None, error: str | None = None) -> None:
    record = state["record"]
    record.update(status="failed" if error else "completed", finished_ns=time.time_ns())
    record["latency_ms"] = (record["finished_ns"] - record["started_ns"]) / 1e6
    if error:
        record["error"] = error[:2048]
    else:
        record.pop("error", None)
        record["usage"] = _usage(response)
    try:
        if response is not None:
            try:
                record["response"] = await run_history_io(llm_audit.save_response, state["target"]["session_id"], response)
            except Exception:
                record["capture_error"] = "Model response could not be saved"
                logger.warning("LLM response artifact unavailable: call=%s", record["call_id"], exc_info=True)
        await _write(state)
        await _emit(state, "llm_call_end", status=record["status"], error=record.get("error"),
                    input_ref=record.get("input"), response_ref=record.get("response"),
                    usage=record.get("usage"), latency_ms=record["latency_ms"])
    except Exception:
        logger.warning("LLM audit completion unavailable: call=%s", record["call_id"], exc_info=True)


async def _on_output(*args: Any, **kwargs: Any) -> None:
    scope, identity = _SCOPE.get(), get_current_llm_call_id()
    if scope is None or identity not in scope["calls"]:
        return
    state = scope["calls"][identity]
    response = kwargs.get("response") or kwargs.get("result")
    await _finish(state, response=response)
    scope["calls"].pop(identity, None)


async def _on_error(*args: Any, **kwargs: Any) -> None:
    scope, identity = _SCOPE.get(), get_current_llm_call_id()
    if scope is None or identity not in scope["calls"]:
        return
    # A router may recover from a provider error. Retain this small state until
    # actual completion (which replaces the failure) or the enclosing rail ends.
    error = kwargs.get("error") or kwargs.get("error_msg") or "Model call failed"
    await _finish(scope["calls"][identity], error=str(error)[:2048])


async def finish_pending_calls(error: str = "Model call interrupted") -> None:
    scope = _SCOPE.get()
    if scope is None:
        return
    for identity, state in list(scope["calls"].items()):
        if state["record"].get("status") == "running":
            await _finish(state, error=error)
        scope["calls"].pop(identity, None)


def install_llm_audit_observer() -> None:
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback.events import LLMCallEvents

    framework = Runner.callback_framework
    if framework in _INSTALLED:
        return
    for event, callback in (
        (LLMCallEvents.LLM_INVOKE_INPUT, _on_input),
        (LLMCallEvents.LLM_STREAM_INPUT, _on_input),
        (LLMCallEvents.LLM_INVOKE_OUTPUT, _on_output),
        (LLMCallEvents.LLM_STREAM_COMPLETED, _on_output),
        (LLMCallEvents.LLM_CALL_ERROR, _on_error),
    ):
        framework.register_sync(event, callback, namespace="swarm-llm-call-audit")
    _INSTALLED.add(framework)
