"""``CommitPort``: Execution event logs and candidate outputs.

Critical events (``tool.intent``, ``execution.paused``, ``execution.final``) return
only after they are stored, and a storage failure is raised so core stops the
Execution. Ordinary events are handed to ``CommitQueue`` and written in the
background; a failed ordinary write leaves a gap in the log and never delays a
later critical event. A final also stores, in the same transaction, every
candidate output it references that the queue has not written yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone

from jiuwenswarm.server.runtime.persistent_agents.execution_models import PersistentContractError
from jiuwenswarm.server.runtime.persistent_agents.execution_repository import ExecutionRepository
from jiuwenswarm.server.runtime.persistent_agents.models import PersistentAgentError, require_identifier
from openjiuwen.harness.persistent_agent import AgentEvent, EventType, ExecutionContext

logger = logging.getLogger(__name__)

CRITICAL_EVENTS = frozenset({EventType.TOOL_INTENT, EventType.EXECUTION_PAUSED, EventType.EXECUTION_FINAL})
_TERMINAL_EVENTS = frozenset({EventType.EXECUTION_PAUSED, EventType.EXECUTION_FINAL})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CommitQueue:
    """Background writer of ordinary events, started and closed with the AgentServer.

    Candidate outputs stay in memory until their Execution records a final or
    paused event, or ``forget`` is called, so a final can still store them when
    their queued write has not run or has failed.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        maxsize: int = 10_000,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._maxsize = maxsize
        self._clock = clock
        self._queue: asyncio.Queue[AgentEvent] | None = None
        self._task: asyncio.Task[None] | None = None
        self._outputs: dict[tuple[str, str], dict[str, AgentEvent]] = {}

    @property
    def running(self) -> bool:
        return self._queue is not None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._queue = asyncio.Queue(self._maxsize)
        self._task = asyncio.create_task(self._run(self._queue), name="jiuwenmate-commit-queue")

    async def aclose(self) -> None:
        """Stop accepting events, write everything already queued, then stop the writer."""
        if self._task is None:
            return
        queue, task = self._queue, self._task
        self._queue = None
        await queue.join()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def drain(self) -> None:
        """Wait until every event queued so far has been written or given up."""
        if self._queue is not None:
            await self._queue.join()

    def submit(self, event: AgentEvent) -> None:
        if self._queue is None:
            raise PersistentAgentError("commit queue is not running", code="QUEUE_CLOSED")
        if event.event_type == EventType.OUTPUT_PROPOSED:
            key = (event.namespace_id, event.execution_id)
            self._outputs.setdefault(key, {})[event.payload.output_id] = event
        self._queue.put_nowait(event)

    def pending_outputs(self, final: AgentEvent) -> list[AgentEvent]:
        """Candidates of this Execution that the final references, in reference order."""
        held = self._outputs.get((final.namespace_id, final.execution_id), {})
        return [held[output_id] for output_id in final.payload.output_refs if output_id in held]

    def forget(self, namespace_id: str, execution_id: str) -> None:
        self._outputs.pop((namespace_id, execution_id), None)

    async def _run(self, queue: asyncio.Queue[AgentEvent]) -> None:
        while True:
            event = await queue.get()
            try:
                await asyncio.to_thread(self._repository.record_events, [event], now=self._clock())
            except Exception as exc:
                logger.warning(
                    "[JiuwenMate] ordinary event not saved: execution=%s seq=%s type=%s error=%s",
                    event.execution_id,
                    event.local_seq,
                    event.event_type,
                    exc,
                )
            finally:
                queue.task_done()


class HostCommitPort:
    """agent-core ``CommitPort`` backed by ``ExecutionRepository``."""

    def __init__(
        self,
        repository: ExecutionRepository,
        queue: CommitQueue,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._clock = clock

    async def record(self, execution: ExecutionContext, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise PersistentContractError("record() requires an AgentEvent")
        event.assert_matches(execution)
        if event.event_type not in CRITICAL_EVENTS:
            self._queue.submit(event)
            return
        batch = [event]
        if event.event_type == EventType.EXECUTION_FINAL:
            batch = [*self._queue.pending_outputs(event), event]
        await asyncio.to_thread(self._repository.record_events, batch, now=self._clock())
        if event.event_type in _TERMINAL_EVENTS:
            self._queue.forget(event.namespace_id, event.execution_id)

    async def read_execution_log(self, execution: ExecutionContext, execution_id: str) -> tuple[AgentEvent, ...]:
        require_identifier("execution_id", execution_id)
        return await asyncio.to_thread(
            self._repository.read_execution_log,
            execution.namespace_id,
            execution.session_id,
            execution.request_id,
            execution_id,
        )
