# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Small, instance-local guard for OfficeAce's synchronous TaskTool children.

Single-round invokes use the child's existing completion_timeout (normally 1800s).
An approval interrupt ends that active invoke; its resume gets a fresh budget.
The guard also covers SessionSpawnExecutor invokes and debug stream consumers.
Single-round streams share the same deadline and instance state as invokes;
task-loop scheduling retains its existing timeouts.
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
# Admission threshold, not eviction: already-running calls may still add cleanup
# tasks. Never drop live tasks just to make the registry appear bounded.
_MAX_PENDING_CLEANUP = 64
_PENDING_CLEANUP: set[asyncio.Task[Any]] = set()


def raise_for_subagent_error(result: Any) -> None:
    """Preserve explicit failures without copying full output into errors/logs."""
    if isinstance(result, dict) and result.get("result_type") != "interrupt":
        if result.get("error") or result.get("result_type") == "error":
            reason = result.get("error") or result.get("message") or "subagent_error"
            raise RuntimeError(f"subagent_failed: {reason}")


def raise_for_subagent_stream_error(chunk: Any) -> None:
    """Check terminal agent errors, leaving recoverable tool-result chunks alone."""
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
    original_stream = getattr(child, "stream", None)
    active = False
    unavailable = False

    async def run(call: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal active, unavailable
        if unavailable:
            raise RuntimeError("subagent unavailable after failed or cancelled execution")
        if active:
            raise RuntimeError("subagent already has an active invocation")
        if sum(not task.done() for task in _PENDING_CLEANUP) >= _MAX_PENDING_CLEANUP:
            logger.warning("Subagent admission paused by pending cleanup: session=%s", session_id)
            raise RuntimeError("subagent_cleanup_backlog: wait for pending cleanup before retrying")
        active = True
        try:
            config = getattr(child, "deep_config", None)
            if getattr(config, "enable_task_loop", False):
                # Do not redefine SDK per-round scheduling in this bounded fix.
                result = await call(*args, **kwargs)
            else:
                timeout = float(getattr(config, "completion_timeout", 1800.0))
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError("subagent completion_timeout must be finite and positive")
                task = asyncio.create_task(call(*args, **kwargs))
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
                result = task.result()
            # SDK TaskTool catches this before it can mark the child successful.
            raise_for_subagent_error(result)
            return result
        except (Exception, asyncio.CancelledError):
            # Both invoke paths must retire failed instances without changing the cause.
            unavailable = True
            raise
        finally:
            active = False

    @functools.wraps(original_invoke)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        return await run(original_invoke, *args, **kwargs)

    @functools.wraps(original_stream)
    async def stream(*args: Any, **kwargs: Any):
        nonlocal unavailable
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        ready = asyncio.Event()

        async def produce() -> None:
            # Keep iteration and aclose in one task: SDK/rail ContextVar tokens
            # must be reset in the context that created them.
            iterator = original_stream(*args, **kwargs)
            try:
                async for chunk in iterator:
                    if unavailable:
                        return  # Drop late chunks even when cancellation was swallowed.
                    raise_for_subagent_stream_error(chunk)
                    await queue.put(chunk)
                    ready.set()
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()

        running = asyncio.create_task(run(produce))
        running.add_done_callback(lambda _: ready.set())
        try:
            while True:
                ready.clear()
                if running.done():
                    running.result()  # Propagate failures before buffered output.
                    if queue.empty():
                        return
                if not queue.empty() and not unavailable:
                    yield queue.get_nowait()
                else:
                    await ready.wait()
        finally:
            if not running.done():
                unavailable = True
                await _cancel_and_observe(running, session_id)
            elif not running.cancelled():
                running.exception()

    child.invoke = invoke
    if callable(original_stream):
        child.stream = stream
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
