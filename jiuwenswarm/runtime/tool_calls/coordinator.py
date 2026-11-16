"""Own local executions without competing with the backend admission ledger."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .types import ToolCall, ToolCallAdapter, ToolEvent, ToolExecution, ToolExecutionContext, ToolExecutor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    call: ToolCall
    task: asyncio.Task[None] | None = None
    execution: ToolExecution | None = None
    open: bool = True
    cancel_requested: bool = False
    cleaning: bool = False


class ToolCallCoordinator:
    def __init__(
        self,
        adapter: ToolCallAdapter,
        executor: ToolExecutor,
        context: ToolExecutionContext,
        *,
        timeout_s: float = 120,
        max_calls: int = 4096,
    ) -> None:
        self.adapter, self.executor, self.context = adapter, executor, context
        self.timeout_s, self.max_calls = timeout_s, max_calls
        self.entries: dict[str, _Entry] = {}
        self.retired: set[str] = set()
        self.closed = False

    async def handle(self, event: ToolEvent) -> None:
        if self.closed:
            return
        if event.kind == "settled":
            if event.call_id not in self.retired and len(self.retired) >= self.max_calls:
                raise ValueError("Duplex tool settlement budget exhausted")
            self.retired.add(event.call_id)
            entry = self.entries.get(event.call_id)
            if entry is not None:
                entry.open = False
                if entry.task is not None and not entry.task.done() and not entry.cleaning and not entry.cancel_requested:
                    entry.cancel_requested = True
                    entry.task.cancel()
            return
        if event.kind != "call" or event.call is None:
            return
        call = event.call
        if call.call_id in self.retired:
            return
        previous = self.entries.get(call.call_id)
        if previous is not None:
            if previous.call.signature != call.signature:
                raise ValueError("Backend reused a call identity with different arguments")
            return
        if len(self.entries) >= self.max_calls:
            raise ValueError("Duplex tool call budget exhausted")
        entry = self.entries[call.call_id] = _Entry(call)
        if any(item is not entry and item.task is not None and not item.task.done()
               for item in self.entries.values()):
            entry.open = False
            await self.adapter.cancel_call(call, "failed")
            return
        entry.task = asyncio.create_task(self._run(entry), name=f"duplex-tool:{call.command_id}")

    async def _run(self, entry: _Entry) -> None:
        call = entry.call
        try:
            async with asyncio.timeout(self.timeout_s):
                entry.execution = await self.executor.start(call, self.context)
                result = await entry.execution.wait()
                if not entry.open or self.closed:
                    return
                if not result.success:
                    entry.open = False
                    await self.adapter.cancel_call(call, "failed")
                    return
                accepted = await self.adapter.submit_result(call, result)
                if accepted and entry.open and not self.closed:
                    # Result admission is terminal; continuation has its own lifetime.
                    entry.open = False
                    await self.adapter.continue_after_result(call)
                elif not accepted:
                    entry.open = False
                    await self._cancel_backend(call)
        except asyncio.CancelledError:
            entry.cancel_requested = True
            raise
        except TimeoutError:
            entry.cancel_requested = True
            entry.open = False
            await self._cancel_backend(call)
        except Exception:
            logger.exception("Duplex tool execution failed: %s", call.command_id)
            entry.open = False
            await self._cancel_backend(call)
        finally:
            entry.open = False
            entry.cleaning = True
            if entry.execution is not None:
                try:
                    await entry.execution.cancel("call_finished")
                except Exception:
                    logger.exception("Duplex execution cleanup remains unsettled: %s", call.command_id)

    async def _cancel_backend(self, call: ToolCall) -> None:
        try:
            await self.adapter.cancel_call(call, "failed")
        except Exception:
            logger.warning("Could not cancel backend call %s", call.command_id)

    async def close(self) -> None:
        self.closed = True
        tasks = []
        for entry in self.entries.values():
            entry.open = False
            if entry.task is not None and not entry.task.done():
                if not entry.cancel_requested and not entry.cleaning:
                    entry.cancel_requested = True
                    entry.task.cancel()
                tasks.append(entry.task)
        await asyncio.gather(*tasks, return_exceptions=True)
