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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_STATE_ATTRIBUTE = "_jiuwenswarm_delegation_state"
_CANCEL_GRACE_SECONDS = 1.0
# Admission threshold, not an eviction limit: in-flight failures may add more.
_MAX_PENDING_CLEANUP = 64
_PENDING_CLEANUP: set[asyncio.Task[Any]] = set()


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
            reason = result.get("error") or result.get("message") or "subagent_error"
            raise RuntimeError(f"subagent_failed: {reason}")


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
        cleanup = await _cancel_and_observe(child, task, session_id)
        if isinstance(exc, _CompletionDeadline):
            raise RuntimeError(
                f"completion_timeout: subagent session={session_id}, "
                f"limit={timeout:g}s, coroutine_cleanup={cleanup}"
            ) from None
        raise
    finally:
        state.active = False
