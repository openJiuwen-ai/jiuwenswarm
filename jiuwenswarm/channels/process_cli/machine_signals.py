# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Signal ownership for one machine invocation, including input and shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Coroutine, Iterator
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, TypeVar

from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
)
from jiuwenswarm.channels.process_cli.protocol.query import OneShotQueryResult

_T = TypeVar("_T")
_Result = TypeVar("_Result", OneShotRunResult, OneShotQueryResult)
_ACTIVE: ContextVar[CommandSignals | None] = ContextVar("machine_signals", default=None)


class CommandSignals:
    """Cancel execution once; defer subsequent signals until cleanup finishes.

    A signal during synchronous input/import raises KeyboardInterrupt. During
    async execution it cancels the owning task, not an arbitrary Runtime task.
    Cleanup stays in that task for async-generator context-token ownership.
    Repeated graceful signals never escalate to abandoning Runtime.close().
    """

    def __init__(self) -> None:
        self.interrupted = False
        self.cleaning = False
        self._settled = False
        self._task: asyncio.Task[Any] | None = None

    def interrupt(self, _signum: int, _frame: Any) -> None:
        """The signal callback owns no Runtime or transport implementation."""
        if self._settled or self.interrupted:
            return
        self.interrupted = True
        if self.cleaning:
            return
        if self._task is not None and not self._task.done():
            self._task.cancel()
        else:
            raise KeyboardInterrupt

    async def run(self, operation: Coroutine[Any, Any, _T]) -> _T:
        """Bind early enough to cover the first JSONL line and Runtime imports."""
        previous = self._task
        self._task = asyncio.current_task()
        try:
            return await operation
        finally:
            self.cleaning = True
            self._task = previous

    def finalize(self, result: _Result) -> _Result:
        """Freeze the outcome before publication; keep the first failure."""
        self._settled = True
        if self.interrupted and result.error is None:
            return replace(
                result,
                status=RunStatus.CANCELLED,
                exit_code=130,
                error=RuntimeErrorInfo(
                    code="CANCELLED", message="Command interrupted."
                ),
            )
        return result


@contextlib.contextmanager
def command_signals() -> Iterator[CommandSignals]:
    """Install only for the machine entry; restore every previous handler."""
    existing = _ACTIVE.get()
    if existing is not None:
        yield existing
        return
    owner = CommandSignals()
    previous: dict[int, Any] = {}
    token = _ACTIVE.set(owner)
    try:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, owner.interrupt)
        yield owner
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _ACTIVE.reset(token)


def defer_command_signals() -> None:
    """Keep signal cancellation from interrupting cooperative resource release."""
    owner = _ACTIVE.get()
    if owner is not None:
        owner.cleaning = True
