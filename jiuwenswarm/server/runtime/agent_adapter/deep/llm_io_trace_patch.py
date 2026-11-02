"""Model.invoke/stream 的 LLM IO trace monkeypatch。

职责：
- 维护 llm_trace / latency ContextVar
- 幂等打上 invoke/stream 包装，写 IO trace 并打 stage=3 pre_llm

进程级副作用；adapter 只在 create_instance 时调用一次。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, List, Tuple

from openjiuwen.core.foundation.llm import Model

from jiuwenswarm.server.runtime.agent_adapter.deep.observability_helpers import (
    _extract_iteration_from_obj,
)
from jiuwenswarm.server.runtime.agent_adapter.llm_io_trace import (
    begin_llm_trace_event,
    end_llm_trace_event,
    log_invoke_input,
    log_invoke_output,
    log_reasoning_delta,
    log_stream_input,
    log_stream_output,
)

logger = logging.getLogger(__name__)

_LLM_TRACE_SESSION_ID: ContextVar[str] = ContextVar(
    "llm_trace_session_id",
    default="",
)
_LLM_TRACE_REQUEST_ID: ContextVar[str] = ContextVar(
    "llm_trace_request_id",
    default="",
)
_LLM_TRACE_ITERATION: ContextVar[int | None] = ContextVar(
    "llm_trace_iteration",
    default=None,
)
_LLM_TRACE_MODEL_NAME: ContextVar[str] = ContextVar(
    "llm_trace_model_name",
    default="",
)
# First Model.invoke/stream of this request → stage=3 pre_llm (jiuwenswarm-only).
_LATENCY_PRE_LLM_MARKED: ContextVar[bool] = ContextVar(
    "latency_pre_llm_marked",
    default=False,
)

_REASONING_TRACE_LOG_BATCH = 5

_LLM_IO_TRACE_PATCH_APPLIED = False

def _log_latency_pre_llm_once(request_id: str | None) -> None:
    """Mark ③ endpoint once per request, right before provider call."""
    if _LATENCY_PRE_LLM_MARKED.get():
        return
    _LATENCY_PRE_LLM_MARKED.set(True)
    logger.info(
        "[latency] stage=3 name=pre_llm request_id=%s",
        (request_id or "").strip() or "-",
    )

def _apply_llm_io_trace_patch() -> None:
    """Monkey Patch Model.invoke/stream 添加 LLM IO trace 日志."""
    global _LLM_IO_TRACE_PATCH_APPLIED
    if _LLM_IO_TRACE_PATCH_APPLIED:
        return

    try:
        original_invoke = Model.invoke
        original_stream = Model.stream

        async def _traced_invoke(
            self: Model,
            messages: List[Any],
            tools: List[Any] | None = None,
            model: str | None = None,
            **kwargs: Any,
        ) -> Any:
            model_name = model or getattr(self.model_config, "model_name", "") or ""
            trace_sid = _LLM_TRACE_SESSION_ID.get()
            trace_rid = _LLM_TRACE_REQUEST_ID.get()
            trace_iter = _LLM_TRACE_ITERATION.get()
            resolved_iter = (
                _extract_iteration_from_obj(kwargs) if trace_iter is None else trace_iter
            )
            _ev_token = begin_llm_trace_event()
            try:
                try:
                    log_invoke_input(
                        session_id=trace_sid,
                        request_id=trace_rid,
                        iteration=resolved_iter,
                        model_name=model_name,
                        messages=messages,
                        tools=tools,
                        max_tokens=kwargs.get("max_tokens"),
                        temperature=kwargs.get("temperature"),
                        top_p=kwargs.get("top_p"),
                        stop=kwargs.get("stop"),
                        timeout=kwargs.get("timeout"),
                    )
                except Exception:
                    logger.debug("[llm_trace] log_invoke_input failed", exc_info=True)
                _log_latency_pre_llm_once(trace_rid)
                result = await original_invoke(
                    self, messages, tools=tools, model=model, **kwargs
                )
                try:
                    log_invoke_output(
                        session_id=trace_sid,
                        request_id=trace_rid,
                        iteration=resolved_iter,
                        model_name=model_name,
                        assistant_msg=result,
                    )
                except Exception:
                    logger.debug("[llm_trace] log_invoke_output failed", exc_info=True)
                return result
            finally:
                end_llm_trace_event(_ev_token)

        async def _traced_stream(
            self: Model,
            messages: List[Any],
            tools: List[Any] | None = None,
            model: str | None = None,
            **kwargs: Any,
        ) -> Any:
            model_name = model or getattr(self.model_config, "model_name", "") or ""
            trace_sid = _LLM_TRACE_SESSION_ID.get()
            trace_rid = _LLM_TRACE_REQUEST_ID.get()
            trace_iter = _LLM_TRACE_ITERATION.get()
            resolved_iter = (
                _extract_iteration_from_obj(kwargs) if trace_iter is None else trace_iter
            )
            _ev_token = begin_llm_trace_event()
            try:
                try:
                    log_stream_input(
                        session_id=trace_sid,
                        request_id=trace_rid,
                        iteration=resolved_iter,
                        model_name=model_name,
                        messages=messages,
                        tools=tools,
                        max_tokens=kwargs.get("max_tokens"),
                        temperature=kwargs.get("temperature"),
                        top_p=kwargs.get("top_p"),
                        stop=kwargs.get("stop"),
                        timeout=kwargs.get("timeout"),
                    )
                except Exception:
                    logger.debug("[llm_trace] log_stream_input failed", exc_info=True)
                _log_latency_pre_llm_once(trace_rid)
                accumulated: Any = None
                reasoning_seq = 0
                reasoning_trace_pending: List[Tuple[int, str]] = []

                def emit_reasoning_trace_batch() -> None:
                    if not reasoning_trace_pending:
                        return
                    try:
                        log_reasoning_delta(
                            session_id=trace_sid,
                            request_id=trace_rid,
                            iteration=resolved_iter,
                            model_name=model_name,
                            reasoning_seq=reasoning_trace_pending[0][0],
                            fragment="".join(t[1] for t in reasoning_trace_pending),
                        )
                    except Exception:
                        logger.debug("[llm_trace] log_reasoning_delta failed", exc_info=True)
                    reasoning_trace_pending.clear()

                async for chunk in original_stream(
                    self, messages, tools=tools, model=model, **kwargs
                ):
                    if accumulated is None:
                        accumulated = chunk
                    else:
                        try:
                            accumulated = accumulated + chunk
                        except Exception:
                            accumulated = chunk

                    reasoning_content = (
                        getattr(chunk, "reasoning_content", None)
                        or (
                            chunk.get("reasoning_content")
                            if isinstance(chunk, dict)
                            else None
                        )
                        or (
                            (chunk.payload.get("reasoning_content") or chunk.payload.get("reasoning"))
                            if isinstance(getattr(chunk, "payload", None), dict)
                            else None
                        )
                    )
                    if reasoning_content:
                        reasoning_trace_pending.append(
                            (reasoning_seq, str(reasoning_content))
                        )
                        if len(reasoning_trace_pending) >= _REASONING_TRACE_LOG_BATCH:
                            emit_reasoning_trace_batch()
                        reasoning_seq += 1

                    yield chunk

                emit_reasoning_trace_batch()

                if accumulated:
                    try:
                        log_stream_output(
                            session_id=trace_sid,
                            request_id=trace_rid,
                            iteration=resolved_iter,
                            model_name=model_name,
                            assistant_msg=accumulated,
                        )
                    except Exception:
                        logger.debug("[llm_trace] log_stream_output failed", exc_info=True)
            except Exception:
                emit_reasoning_trace_batch()
                raise
            finally:
                end_llm_trace_event(_ev_token)

        Model.invoke = _traced_invoke
        Model.stream = _traced_stream
        _LLM_IO_TRACE_PATCH_APPLIED = True
        logger.info("[JiuWenSwarmDeepAdapter] LLM IO trace patch applied")
    except Exception:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] Failed to apply LLM IO trace patch", exc_info=True
        )
