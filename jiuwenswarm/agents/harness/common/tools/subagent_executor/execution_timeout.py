# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bound host-side delegation without replacing SDK tool or session handling.

The child's completion_timeout bounds one delegated call, including debug
stream draining and task-loop children. Normal parent interaction loops and the
persistent spawn_agent/wait_agent runtime keep their own scheduling policies.
Cancellation is cooperative: remote work or an OS process may outlive a Python
task. Pending cleanup stays observed and applies backpressure to new calls.

Failure retirement is instance-scoped and permanent. Retries need a fresh child;
configuration-based factory paths (e.g. SubAgentConfig) create one per new delegation.
Integrations registering a prebuilt/shared DeepAgent must replace the failed
instance themselves; the guard does not reset its unavailable state. Healthy
or interrupted children may still resume in the same session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_STATE_ATTRIBUTE = "_jiuwenswarm_delegation_state"
_CANCEL_GRACE_SECONDS = 1.0
# Admission threshold, not an eviction limit: in-flight failures may add more.
_MAX_PENDING_CLEANUP = 64
_PENDING_CLEANUP: set[asyncio.Task[Any]] = set()
# Fixed text, so it stays safe to surface; without it the parent has to guess and redoes the work.
_MAX_ITERATIONS_REASON = (
    "max_iterations_reached: the subagent ran out of iterations before finishing. "
    "Files it already wrote remain in the workspace; inspect them before retrying or redoing the work."
)
# SQLAlchemy wraps driver errors as "(sqlite3.OperationalError) ..." and inlines the
# failing statement — that prefix is how a checkpoint-save failure reaches this boundary.
_PERSISTENCE_DB_PREFIX = "(sqlite3."
# 业务执行与进度保存不是同一事务：checkpoint 在子代理每步执行前后落盘，存储层故障
# 不代表业务失败。错误消息可能内嵌大对象参数（生产日志见 51 万字符 blob），进结果
# 文本前必须截断。
_MAX_REASON_CHARS = 400
_CHECKPOINT_DEGRADED_NOTE = (
    "\n\n[checkpoint_degraded] The subagent's progress checkpoint failed to save "
    "before delivery ({reason}). The text above is what the child produced before the "
    "interruption and may not be the final delivery. Verify the files it already wrote "
    "in the workspace before retrying; do not redo the whole task blindly."
)
_PROGRESS_UNKNOWN_REASON = (
    "subagent_progress_unknown: the subagent's checkpoint save failed, so its progress "
    "record is unreliable; business may have completed and files may already exist in "
    "the workspace. Verify the workspace before retrying; do not redo the whole task "
    "blindly. persist_error: {reason}"
)


@dataclass
class _CallState:
    session_id: str
    active: bool = False
    unavailable: bool = False


class _CompletionDeadline(Exception):
    """Distinguish our deadline from a TimeoutError raised inside the child."""


def raise_for_subagent_error(result: Any) -> None:
    """Keep explicit failure reasons; never copy full output into an error.

    When called by TaskTool, raising here intentionally leaves succeeded=False.
    With KV-cache affinity enabled, SDK cleanup then evicts a failed sticky
    child's cache instead of offloading it for reuse. Successful/interrupt
    results retain the SDK's existing cache policy.
    """
    if isinstance(result, dict) and result.get("result_type") != "interrupt":
        if result.get("error") or result.get("result_type") == "error":
            reason = result.get("error") or result.get("message")
            if not reason and result.get("finish_reason") == "max_iterations":
                reason = _MAX_ITERATIONS_REASON
            raise RuntimeError(f"subagent_failed: {reason or 'subagent_error'}")


def ensure_subagent_available(child: Any) -> None:
    """Prevent a cancelled stream from publishing late chunks into debug output."""
    state = getattr(child, _STATE_ATTRIBUTE, None)
    if state is not None and state.unavailable:
        raise RuntimeError("subagent unavailable after failed or cancelled execution")


def raise_for_subagent_stream_error(chunk: Any) -> None:
    """Reject terminal agent errors without rejecting recoverable tool errors."""
    kind = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
    payload = chunk.get("payload") if isinstance(chunk, dict) else getattr(chunk, "payload", None)
    if kind == "error":
        error = dict(payload) if isinstance(payload, dict) else {}
        error["result_type"] = "error"
        if isinstance(payload, str):
            error["message"] = payload
        raise_for_subagent_error(error)
    elif kind == "answer":
        raise_for_subagent_error(payload)
    elif kind is None:
        raise_for_subagent_error(chunk)


def _observe_cleanup(task: asyncio.Task[Any]) -> None:
    _PENDING_CLEANUP.add(task)

    def finished(done: asyncio.Task[Any]) -> None:
        _PENDING_CLEANUP.discard(done)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.warning("Subagent cleanup ended with %s", type(error).__name__)

    task.add_done_callback(finished)


def _persistence_failure_reason(exc: BaseException) -> str | None:
    """Return bounded storage-error text when *exc* is a checkpoint-save failure.

    沿异常链识别 sqlite3 错误（驱动直抛或 SQLAlchemy "(sqlite3.OperationalError)"
    包装）。只有存储层故障才进入降级分支：工具与业务异常本就不会以 sqlite3 错误
    的形态穿出子代理，误判面收窄到基础设施层。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    prefix = _PERSISTENCE_DB_PREFIX.lower()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current)
        if isinstance(current, sqlite3.Error) or prefix in text.lower():
            return text[:_MAX_REASON_CHARS]
        current = current.__cause__ or current.__context__
    return None


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _recover_last_assistant_text(child: Any, session_id: str) -> str | None:
    """Best-effort read of the child's final answer from its in-memory context.

    对话消息只存在 context_engine 的进程内存（不落 checkpointer），进度保存失败发生在
    最终回答生成之后时，回答仍可读。只认最后一条无 tool_calls 的 assistant 消息
    （ReAct 最终回答形态）：最后一条 assistant 还挂着 tool_calls 说明业务中断在半途，
    不能把中间叙述当作交付结果。
    """
    try:
        # DeepAgent 暴露公开 property react_agent；其余形态回落私有属性名兜底。
        react = getattr(child, "react_agent", None) or getattr(child, "_react_agent", None)
        engine = getattr(react, "context_engine", None)
        if engine is None:
            engine = getattr(child, "context_engine", None)
        get_context = getattr(engine, "get_context", None)
        if not callable(get_context):
            return None
        context = get_context(session_id=session_id)
        if context is None:
            return None
        for message in reversed(list(context.get_messages())):
            if isinstance(message, dict):
                role = message.get("role")
                tool_calls = message.get("tool_calls")
                content = message.get("content")
            else:
                role = getattr(message, "role", None)
                tool_calls = getattr(message, "tool_calls", None)
                content = getattr(message, "content", None)
            if role != "assistant":
                continue
            if tool_calls:
                return None
            text = _assistant_text(content)
            return text if text and text.strip() else None
        return None
    except Exception as exc:  # noqa: BLE001 — 回收是尽力而为，任何读取问题都不得掩盖原异常
        logger.debug("Subagent answer recovery skipped: %s", type(exc).__name__)
        return None


async def _cancel_and_observe(child: Any, task: asyncio.Task[Any], session_id: str) -> str:
    # Capture this child's controller before cancellation lets SDK finally blocks
    # clear it. Use public instance APIs; never enumerate another session's tasks.
    controller = getattr(child, "loop_controller", None)
    abort = getattr(child, "abort", None)
    stop = getattr(controller, "stop", None)

    pending = set()
    if not task.done():
        task.cancel()
        pending.add(task)
    # Start both independently: an uncooperative stream must not prevent the
    # child's scheduler from receiving its stop request.
    if callable(abort):
        pending.add(asyncio.create_task(abort()))
    if callable(stop):
        pending.add(asyncio.create_task(stop()))
    for cleanup in pending:
        _observe_cleanup(cleanup)
    if not pending:
        return "complete"
    _, unfinished = await asyncio.wait(pending, timeout=_CANCEL_GRACE_SECONDS)
    if not unfinished:
        return "complete"
    logger.warning("Subagent cleanup still pending: session=%s", session_id)
    return "pending"


async def run_subagent_with_timeout(
    child: Any,
    *,
    session_id: str,
    call: Callable[[], Awaitable[Any]],
) -> Any:
    """Run one dispatch, preserving success/interrupt results and failure causes.

    A successful or interrupted child may resume with a fresh time budget.
    Failed instances cannot be reused, even when their cancellation is delayed.

    存储层（checkpoint 保存）失败按阶段降级：能从子代理内存上下文确认最终回答时，
    返回带 ``checkpoint_degraded`` 标注的回答（实例仍永久退役）；确认不了则抛
    ``subagent_progress_unknown``，保留「业务可能已完成」的不确定性，由上层核对
    工作区后自行决定是否重试。
    """
    state = getattr(child, _STATE_ATTRIBUTE, None)
    if state is None:
        state = _CallState(session_id=session_id)
        setattr(child, _STATE_ATTRIBUTE, state)
    if state.session_id != session_id:
        raise RuntimeError("subagent instance cannot be reused across sessions")
    if state.unavailable:
        raise RuntimeError("subagent unavailable after failed or cancelled execution")
    if state.active:
        raise RuntimeError("subagent already has an active invocation")
    if sum(not task.done() for task in _PENDING_CLEANUP) >= _MAX_PENDING_CLEANUP:
        raise RuntimeError("subagent_cleanup_backlog: wait for pending cleanup before retrying")

    config = getattr(child, "deep_config", None)
    timeout = float(getattr(config, "completion_timeout", 1800.0))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("subagent completion_timeout must be finite and positive")
    state.active = True

    async def execute() -> Any:
        return await call()

    task = asyncio.create_task(execute())
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise _CompletionDeadline
        result = task.result()
        raise_for_subagent_error(result)
        return result
    except (Exception, asyncio.CancelledError) as exc:
        state.unavailable = True
        deadline = isinstance(exc, _CompletionDeadline)
        # 先回收再取消：回收是纯内存读，且任何内部失败都会被吞掉，不得影响取消时序。
        degraded = None if deadline else _persistence_failure_reason(exc)
        recovered = None if degraded is None else _recover_last_assistant_text(child, session_id)
        cleanup = await _cancel_and_observe(child, task, session_id)
        if deadline:
            raise RuntimeError(
                f"completion_timeout: subagent session={session_id}, "
                f"limit={timeout:g}s, coroutine_cleanup={cleanup}"
            ) from None
        if degraded is not None:
            # 存储层故障 ≠ 业务失败：能确认已生成最终回答就随降级标注一并交付，
            # 确认不了就保留「结果未知」，绝不能当成业务没执行引导上层整段重做。
            if recovered:
                logger.warning(
                    "Subagent checkpoint save failed; delivering recovered answer: "
                    "session=%s, error=%s",
                    session_id, degraded,
                )
                return {
                    "result_type": "answer",
                    "output": recovered + _CHECKPOINT_DEGRADED_NOTE.format(reason=degraded),
                }
            raise RuntimeError(_PROGRESS_UNKNOWN_REASON.format(reason=degraded)) from exc
        raise
    finally:
        state.active = False
