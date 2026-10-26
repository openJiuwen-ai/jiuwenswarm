# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patch: prevent ReAct from hanging forever on stuck tool tasks."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional
from weakref import WeakKeyDictionary

logger = logging.getLogger("jiuwenswarm.openjiuwen_streaming_tool_patch")

_STREAMING_TOOL_WAIT_TIMEOUT_PATCHED = False

# Bound to the StreamingToolExecutor that owns the current tool task, so HITL
# waits inside tools can pause that executor's wait_all budget.
_current_streaming_tool_executor: ContextVar[Optional[Any]] = ContextVar(
    "current_streaming_tool_executor",
    default=None,
)

# Default active-execution budget for StreamingToolExecutor.wait_all().
# HITL/interrupt waits should call pause_streaming_tool_wait_timeout() so they
# do not consume this budget.
# Set STREAMING_TOOL_WAIT_TIMEOUT_S=0 (or negative) to disable and keep upstream behavior.
# Prefer agent-core DEFAULT_WAIT_ALL_TIMEOUT_S (180) when openjiuwen already ships timeout.
_DEFAULT_STREAMING_TOOL_WAIT_TIMEOUT_S = 180.0


@dataclass
class _WaitTimeoutClock:
    """Pauseable deadline clock for wait_all (HITL time excluded)."""

    pause_depth: int = 0
    paused_total: float = 0.0
    _pause_started: float | None = None
    _unpaused: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._unpaused.set()

    def pause(self) -> None:
        if self.pause_depth == 0:
            self._pause_started = time.monotonic()
            self._unpaused.clear()
        self.pause_depth += 1

    def resume(self) -> None:
        if self.pause_depth <= 0:
            return
        self.pause_depth -= 1
        if self.pause_depth == 0:
            if self._pause_started is not None:
                self.paused_total += time.monotonic() - self._pause_started
                self._pause_started = None
            self._unpaused.set()

    async def wait_unpaused(self, timeout: float) -> bool:
        """Wait until not paused. Returns False on timeout."""
        try:
            await asyncio.wait_for(self._unpaused.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# Clocks keyed by executor instance (G.CLS.11: no protected attrs on client class).
_executor_wait_clocks: WeakKeyDictionary[Any, _WaitTimeoutClock] = WeakKeyDictionary()
# Fallback when the executor type cannot be weakly referenced.
_executor_wait_clocks_strong: dict[int, _WaitTimeoutClock] = {}


def _resolve_streaming_tool_wait_timeout_s(
    *,
    default: float = _DEFAULT_STREAMING_TOOL_WAIT_TIMEOUT_S,
) -> Optional[float]:
    """Return wait_all timeout seconds, or None to disable."""
    raw = os.environ.get("STREAMING_TOOL_WAIT_TIMEOUT_S", str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return None
    return value


def _executor_wait_clock(executor: Any) -> _WaitTimeoutClock:
    clock = _executor_wait_clocks.get(executor)
    if isinstance(clock, _WaitTimeoutClock):
        return clock
    clock = _executor_wait_clocks_strong.get(id(executor))
    if isinstance(clock, _WaitTimeoutClock):
        return clock
    clock = _WaitTimeoutClock()
    try:
        _executor_wait_clocks[executor] = clock
    except TypeError:
        _executor_wait_clocks_strong[id(executor)] = clock
    return clock


def pause_streaming_tool_wait_timeout() -> None:
    """Pause the active StreamingToolExecutor wait_all budget (HITL / interrupt)."""
    executor = _current_streaming_tool_executor.get()
    if executor is None:
        return
    _executor_wait_clock(executor).pause()


def resume_streaming_tool_wait_timeout() -> None:
    """Resume a previously paused wait_all budget."""
    executor = _current_streaming_tool_executor.get()
    if executor is None:
        return
    _executor_wait_clock(executor).resume()


@asynccontextmanager
async def streaming_tool_wait_timeout_paused():
    """Context manager: exclude enclosed await time from wait_all timeout."""
    pause_streaming_tool_wait_timeout()
    try:
        yield
    finally:
        resume_streaming_tool_wait_timeout()


def _remap_wait_all_timeout_results(results: list, timeout: float) -> list:
    remapped = []
    for tool_call, value in results:
        name = getattr(tool_call, "name", "?")
        if isinstance(value, asyncio.CancelledError):
            remapped.append(
                (
                    tool_call,
                    TimeoutError(f"Tool timed out after {timeout}s: {name}"),
                )
            )
        else:
            remapped.append((tool_call, value))
    return remapped


async def _wait_all_with_pauseable_timeout(
    executor: Any,
    orig_wait_all,
    timeout: float,
) -> list:
    """Run orig wait_all; pause stretches the deadline (HITL excluded)."""
    clock = _executor_wait_clock(executor)
    wait_task = asyncio.create_task(orig_wait_all(executor))
    deadline = time.monotonic() + float(timeout)
    try:
        while True:
            if wait_task.done():
                return wait_task.result()
            if clock.pause_depth > 0:
                # Don't hang forever if wait_all finishes while still marked paused.
                while clock.pause_depth > 0 and not wait_task.done():
                    await clock.wait_unpaused(timeout=0.5)
                continue
            remaining = deadline + clock.paused_total - time.monotonic()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait({wait_task}, timeout=min(remaining, 1.0))
            if wait_task in done:
                return wait_task.result()

        logger.warning(
            "StreamingToolExecutor wait_all timed out after %.1fs "
            "(paused_total=%.1fs); cancelling pending tools",
            float(timeout),
            clock.paused_total,
        )
        executor.cancel_all()
        grace = min(5.0, float(timeout))
        try:
            results = await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace)
        except asyncio.TimeoutError:
            logger.warning(
                "StreamingToolExecutor tool task(s) still running "
                "after cancel grace=%.1fs; draining via wait_all",
                grace,
            )
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except (asyncio.CancelledError, Exception):
                    pass
            results = await orig_wait_all(executor)
        return _remap_wait_all_timeout_results(results, float(timeout))
    finally:
        if not wait_task.done():
            wait_task.cancel()
            try:
                await wait_task
            except (asyncio.CancelledError, Exception):
                pass


def apply_streaming_tool_wait_timeout_patch() -> None:
    """Prevent ReAct from hanging forever on stuck tool tasks.

    Prefer native agent-core ``StreamingToolExecutor.wait_all(timeout=...)``.
    Only monkey-patch when the installed openjiuwen build has no timeout.

    Env ``STREAMING_TOOL_WAIT_TIMEOUT_S`` overrides the default (180s) when
    the shim is active; ``<=0`` disables.

    HITL / interrupt waits should use ``streaming_tool_wait_timeout_paused()``
    so user-approval time is not counted against the budget.
    """
    global _STREAMING_TOOL_WAIT_TIMEOUT_PATCHED
    if _STREAMING_TOOL_WAIT_TIMEOUT_PATCHED:
        return
    try:
        from openjiuwen.core.operator import streaming_tool_executor as ste  # type: ignore
    except ImportError:
        return

    _orig_wait_all = ste.StreamingToolExecutor.wait_all
    _orig_init = ste.StreamingToolExecutor.__init__

    # Native agent-core already implements timeout — do not wrap again.
    if "timeout" in getattr(_orig_wait_all, "__code__", type("_", (), {"co_varnames": ()})).co_varnames:
        native_default = getattr(ste, "DEFAULT_WAIT_ALL_TIMEOUT_S", None)
        _STREAMING_TOOL_WAIT_TIMEOUT_PATCHED = True
        logger.info(
            "StreamingToolExecutor.wait_all timeout already native "
            "(DEFAULT_WAIT_ALL_TIMEOUT_S=%s); skip shim",
            native_default,
        )
        return

    def _patched_init(self, executor_fn, *args, **kwargs):
        async def _bound_executor_fn(tool_call):
            token = _current_streaming_tool_executor.set(self)
            try:
                return await executor_fn(tool_call)
            finally:
                _current_streaming_tool_executor.reset(token)

        _orig_init(self, _bound_executor_fn, *args, **kwargs)
        _executor_wait_clock(self)

    async def _patched_wait_all(self, timeout=...):
        # Ellipsis => resolve from env (default 180s). Explicit None => no timeout.
        # Uses only public cancel_all / wait_all; remaps CancelledError
        # from cancel-on-timeout to TimeoutError for the ReAct loop.
        # Interrupt/HITL pauses stretch the deadline via _WaitTimeoutClock.
        if timeout is ...:
            timeout = _resolve_streaming_tool_wait_timeout_s()
        if timeout is None:
            return await _orig_wait_all(self)
        return await _wait_all_with_pauseable_timeout(self, _orig_wait_all, float(timeout))

    ste.StreamingToolExecutor.__init__ = _patched_init  # type: ignore[method-assign]
    ste.StreamingToolExecutor.wait_all = _patched_wait_all  # type: ignore[method-assign]
    _STREAMING_TOOL_WAIT_TIMEOUT_PATCHED = True
    logger.info(
        "StreamingToolExecutor.wait_all timeout patch applied "
        "(STREAMING_TOOL_WAIT_TIMEOUT_S default=%s; HITL pause supported)",
        _DEFAULT_STREAMING_TOOL_WAIT_TIMEOUT_S,
    )
