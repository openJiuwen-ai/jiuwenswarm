# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution ownership between an SDK producer and an independent consumer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any
import logging


logger = logging.getLogger(__name__)


class ExecutionDriver:
    """Own one bounded producer, its output lease and cancellation terminal state.

    Producer completion is signalled outside the queue: cancellation can never
    deadlock trying to put an EOF into a full queue after the consumer leaves.
    Consumers may attach projection work to the same queue, but its lifetime is
    still owned here, rather than by an SDK adapter or a WebSocket connection.
    """

    def __init__(
        self, produce: Callable[[], AsyncIterator[Any]], *, capacity: int
    ) -> None:
        if capacity <= 0:
            raise ValueError("execution queue must be bounded")
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=capacity)
        self.done = asyncio.Event()
        self.cancellation: asyncio.CancelledError | None = None
        self._produce = produce
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("execution producer already started")
        self._task = asyncio.create_task(self._run(), name="adapter-execution-producer")

    async def _run(self) -> None:
        stream = None
        try:
            stream = self._produce()
            async for chunk in stream:
                await self.queue.put(("chunk", chunk))
        except asyncio.CancelledError as exc:
            self.cancellation = exc
            raise
        except Exception as exc:
            logger.exception("adapter stream producer failed")
            try:
                await self.queue.put(("error", exc))
            except asyncio.CancelledError as cancel_exc:
                self.cancellation = cancel_exc
                raise
        finally:
            try:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except asyncio.CancelledError as exc:
                        self.cancellation = exc
                        raise
                    except Exception:
                        logger.debug("adapter producer close failed", exc_info=True)
            finally:
                self.done.set()

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        cancellation = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.done():
                    break
                cancellation = exc
            except Exception:
                logger.debug("adapter producer cleanup failed", exc_info=True)
                break
        if cancellation is not None:
            raise cancellation

    def raise_if_cancelled(self) -> None:
        if self.cancellation is not None:
            raise self.cancellation
