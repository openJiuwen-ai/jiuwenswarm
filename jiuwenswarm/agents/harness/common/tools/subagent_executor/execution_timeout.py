# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Small, instance-local guard for OfficeAce's synchronous TaskTool children.

Single-round invokes use the child's existing completion_timeout (normally 1800s).
An approval interrupt ends that active invoke; its resume gets a fresh budget.
The guard also covers single-round invokes made by SessionSpawnExecutor through
this factory; task-loop scheduling and stream retain their existing timeouts.
Cancellation is cooperative: bounded waiting does not guarantee that OS processes
or remote tools have stopped. A child cancelled here cannot be reused.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

_PARENT_MARKER = "_officeace_subagent_timeout_wiring"
_CHILD_SESSION = "_officeace_subagent_timeout_session"
_CANCEL_GRACE_SECONDS = 1.0
_PENDING_CLEANUP: set[asyncio.Task[Any]] = set()


async def _cancel_and_observe(task: asyncio.Task[Any], session_id: str) -> str:
    """Cancel only this invoke and retain it until cleanup has actually ended."""
    def finished(done: asyncio.Task[Any]) -> None:
        _PENDING_CLEANUP.discard(done)
        if not done.cancelled():
            # Retrieve a late exception without allowing a late success to escape.
            done.exception()

    _PENDING_CLEANUP.add(task)
    task.add_done_callback(finished)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_CANCEL_GRACE_SECONDS)
    if done:
        return "complete"
    logger.warning(
        "Subagent coroutine cleanup still pending after cancellation: session=%s",
        session_id,
    )
    return "pending"


def _guard_child(child: Any, session_id: str) -> None:
    bound_session = getattr(child, _CHILD_SESSION, None)
    if bound_session is not None:
        if bound_session != session_id:
            raise RuntimeError("subagent instance cannot be reused across sessions")
        return
    original_invoke = child.invoke
    active = False
    unavailable = False

    @functools.wraps(original_invoke)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, unavailable
        if unavailable:
            raise RuntimeError("subagent unavailable after failed or cancelled execution")
        if active:
            raise RuntimeError("subagent already has an active invocation")
        active = True
        try:
            config = getattr(child, "deep_config", None)
            if getattr(config, "enable_task_loop", False):
                # Do not redefine SDK per-round scheduling in this bounded fix.
                result = await original_invoke(*args, **kwargs)
            else:
                timeout = float(getattr(config, "completion_timeout", 1800.0))
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError("subagent completion_timeout must be finite and positive")
                task = asyncio.create_task(original_invoke(*args, **kwargs))
                try:
                    done, _ = await asyncio.wait({task}, timeout=timeout)
                except asyncio.CancelledError:
                    unavailable = True
                    await _cancel_and_observe(task, session_id)
                    raise
                if not done:
                    unavailable = True
                    cleanup = await _cancel_and_observe(task, session_id)
                    raise RuntimeError(
                        f"completion_timeout: subagent session={session_id}, "
                        f"limit={timeout:g}s, coroutine_cleanup={cleanup}"
                    )
                # A TimeoutError raised inside the child is not our deadline.
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    unavailable = True
                    raise
            if isinstance(result, dict) and result.get("result_type") != "interrupt":
                if result.get("error") or result.get("result_type") == "error":
                    unavailable = True
                    reason = (
                        result.get("error") or result.get("message")
                        or result.get("output") or "subagent_error"
                    )
                    # SDK TaskTool catches this before it can mark the child successful.
                    raise RuntimeError(f"subagent_failed: {reason}")
            return result
        finally:
            active = False

    child.invoke = invoke
    setattr(child, _CHILD_SESSION, session_id)


def install_subagent_timeout_wiring(agent: Any) -> bool:
    """Wrap only this parent's factory, independently of authorization switches."""
    if agent is None or not callable(getattr(agent, "create_subagent", None)):
        return False
    if getattr(agent, _PARENT_MARKER, False):
        return True
    original_create = agent.create_subagent

    @functools.wraps(original_create)
    def create(subagent_type: str, subsession_id: str, *args: Any, **kwargs: Any) -> Any:
        child = original_create(subagent_type, subsession_id, *args, **kwargs)
        _guard_child(child, subsession_id)
        return child

    agent.create_subagent = create
    setattr(agent, _PARENT_MARKER, True)
    return True
