# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Content-free observations of normal TaskTool execution; no scheduling changes."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import monotonic
from typing import Any
from uuid import uuid4

from openjiuwen.core.common.logging import server_logger as logger

from jiuwenswarm.common.stage_timer import StageTimer


@dataclass
class SubagentTrace:
    """One dispatch attempt, inherited by its child tasks but never by siblings."""

    parent_session_id: str
    parent_tool_call_id: str
    subagent_type: str
    dispatch_id: str = field(default_factory=lambda: uuid4().hex)
    child_session_id: str | None = None
    started: float = field(default_factory=monotonic)
    sequence: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    raw_result_type: str | None = None
    invoke_finished: float | None = None
    tools: dict[str, tuple[float, int, Any]] = field(default_factory=dict)

    def emit(self, event: str, **fields: Any) -> None:
        """Logging must not replace a result, exception or cancellation."""
        self.sequence += 1
        try:
            logger.info("[SubagentExecution] %s", json.dumps({
                "event": event, "dispatch_id": self.dispatch_id,
                "sequence": self.sequence, "parent_session_id": self.parent_session_id,
                "parent_tool_call_id": self.parent_tool_call_id,
                "child_session_id": self.child_session_id,
                "subagent_type": self.subagent_type,
                "elapsed_ms": round((monotonic() - self.started) * 1000, 3),
                **fields,
            }, ensure_ascii=True))
        except Exception:  # A broken log sink must not break task execution.
            pass


_TRACE: ContextVar[SubagentTrace | None] = ContextVar("subagent_execution_trace", default=None)


def current_subagent_trace() -> SubagentTrace | None:
    return _TRACE.get()


def _exception_status(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, GeneratorExit):
        return "closed"
    return "error"


@contextmanager
def subagent_creation(subagent_type: str, child_session_id: str):
    """Measure the existing factory and runtime defaults without replacing them."""
    trace = current_subagent_trace()
    timer = StageTimer()
    if trace is not None:
        trace.child_session_id = child_session_id
        trace.emit("creation.start", resolved_subagent_type=subagent_type)
    status, exception_type = "completed", None
    try:
        yield timer
    except BaseException as exc:
        status, exception_type = _exception_status(exc), type(exc).__name__
        raise
    finally:
        if trace is not None:
            trace.emit("creation.end", duration_ms=round(timer.total_ms(), 3),
                       stages=timer.render(), status=status, exception_type=exception_type)


class ModelObservation:
    """A single actual model request (including individual retry attempts)."""

    def __init__(self, trace: SubagentTrace, model: Any, kwargs: dict, streaming: bool):
        self.trace = trace
        self.started = monotonic()
        self.first_chunk_ms = None
        self.usage: dict[str, int] = {}
        trace.model_calls += 1
        config = getattr(model, "model_config", None)
        client = getattr(model, "model_client_config", None)
        messages = kwargs.get("messages")
        self.fields = {
            "model_call": trace.model_calls,
            "model": kwargs.get("model") or getattr(config, "model", None)
            or getattr(config, "model_name", None),
            "provider": getattr(client, "client_provider", None),
            "streaming": streaming,
            "message_count": 1 if isinstance(messages, str) else len(messages or []),
            "tool_count": len(kwargs.get("tools") or []),
        }
        trace.emit("model.start", **self.fields)

    def response(self, response: Any) -> None:
        if self.fields["streaming"] and self.first_chunk_ms is None:
            self.first_chunk_ms = round((monotonic() - self.started) * 1000, 3)
        usage = getattr(response, "usage_metadata", None)
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(usage, name, None)
            if isinstance(value, int):
                self.usage[name] = value

    def finish(self, status: str, exception_type: str | None) -> None:
        self.trace.emit("model.end", **self.fields, status=status, exception_type=exception_type,
                        duration_ms=round((monotonic() - self.started) * 1000, 3),
                        first_chunk_ms=self.first_chunk_ms, usage=self.usage)


@contextmanager
def model_observation(model: Any, kwargs: dict, *, streaming: bool):
    trace = current_subagent_trace()
    observation = ModelObservation(trace, model, kwargs, streaming) if trace else None
    status, exception_type = "completed", None
    try:
        yield observation
    except BaseException as exc:
        status, exception_type = _exception_status(exc), type(exc).__name__
        raise
    finally:
        if observation is not None:
            observation.finish(status, exception_type)


def install_task_tool_logging() -> None:
    """Wrap the SDK entry point, preserving its invoke/resume/cleanup semantics."""
    from openjiuwen.harness.tools.subagent.task_tool import TaskTool
    from openjiuwen.core.session.agent import Session
    from openjiuwen.core.session import InteractiveInput

    if getattr(TaskTool.invoke, "_jws_execution_logging", False):
        return
    original = TaskTool.invoke

    @wraps(original)
    async def invoke(self, inputs, **kwargs):
        session = kwargs.get("session")
        if not isinstance(session, Session):
            return await original(self, inputs, **kwargs)
        trace = SubagentTrace(
            parent_session_id=session.get_session_id(),
            parent_tool_call_id=str(kwargs.get("tool_call_id") or ""),
            subagent_type=str(inputs.get("subagent_type") or "") if isinstance(inputs, dict) else "",
        )
        token = _TRACE.set(trace)
        status, exception_type = "returned", None
        trace.emit("dispatch.start", resume=isinstance(inputs, dict)
                   and isinstance(inputs.get("query"), InteractiveInput))
        try:
            result = await original(self, inputs, **kwargs)
            result_type = trace.raw_result_type
            if isinstance(result, dict) and result.get("result_type") == "interrupt":
                result_type = trace.raw_result_type = "interrupt"
            status = {"answer": "completed", "error": "error", "interrupt": "interrupted"}.get(
                result_type, "returned",
            )
            if getattr(result, "success", None) is False:
                status = "error"
            return result
        except BaseException as exc:
            status, exception_type = _exception_status(exc), type(exc).__name__
            raise
        finally:
            # Core deliberately skips after-tool hooks on cancellation. Close only
            # our observations; resource cancellation/cleanup stays owned by Core.
            for call_id, (started, number, tool_inputs) in trace.tools.items():
                trace.emit(
                    "tool.end", tool_call_id=call_id, tool_call=number,
                    tool_name=getattr(tool_inputs, "tool_name", ""),
                    execution_started=getattr(tool_inputs, "execution_started", False),
                    status=status if status in {"cancelled", "error", "interrupted"} else "unobserved_end",
                    exception_type=exception_type,
                    callback_duration_ms=round((monotonic() - started) * 1000, 3),
                )
            trace.tools.clear()
            trace.emit(
                "dispatch.end", status=status, exception_type=exception_type,
                raw_result_type=trace.raw_result_type,
                duration_ms=round((monotonic() - trace.started) * 1000, 3),
                post_invoke_ms=(round((monotonic() - trace.invoke_finished) * 1000, 3)
                                if trace.invoke_finished is not None else None),
                model_calls=trace.model_calls, tool_calls=trace.tool_calls,
            )
            _TRACE.reset(token)

    invoke._jws_execution_logging = True
    TaskTool.invoke = invoke
