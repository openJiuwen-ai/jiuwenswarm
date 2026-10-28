"""``CommitPort``: Execution event logs and candidate outputs.

Critical events (``tool.intent``, ``execution.paused``, ``execution.final``) return
only after they are stored, and a storage failure is raised so core stops the
Execution. Ordinary events are handed to ``CommitQueue`` and written in the
background; a failed ordinary write leaves a gap in the log and never delays a
later critical event. A final or paused event also stores, in the same
transaction, every candidate output it references that the queue has not written
yet. A file candidate is stored only after ``WorkspaceOutputFiles`` verifies its
request-authorized source in the Agent's shared ``output/`` directory; one that
cannot be verified is held for the ending, which verifies it again and is not
recorded when that fails. An optional
observer sees each accepted event as it is recorded, before an ordinary event is
written; it serves live display only and cannot affect the Execution.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone

from jiuwenswarm.server.jiuwenmate.execution_models import PersistentContractError
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.output_files import FILE_SHARE_MODES, WorkspaceOutputFiles
from openjiuwen.harness.persistent_agent import AgentEvent, EventType, ExecutionContext
from openjiuwen.harness.persistent_agent.schema.port_results import ExecutionLink

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
        self.hold(event)
        self._queue.put_nowait(event)

    def hold(self, event: AgentEvent) -> None:
        """Keep a candidate for its Execution's ending without writing it now."""
        if event.event_type == EventType.OUTPUT_PROPOSED:
            key = (event.namespace_id, event.execution_id)
            self._outputs.setdefault(key, {})[event.payload.output_id] = event

    def pending_outputs(self, ending: AgentEvent) -> list[AgentEvent]:
        """Candidates of this Execution that a final or paused event references, in reference order."""
        held = self._outputs.get((ending.namespace_id, ending.execution_id), {})
        return [held[output_id] for output_id in ending.payload.output_refs if output_id in held]

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
        observer: Callable[[AgentEvent], None] | None = None,
        files: WorkspaceOutputFiles | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._clock = clock
        self._observer = observer
        self._files = files

    async def record(self, execution: ExecutionContext, event: AgentEvent) -> None:
        if not isinstance(event, AgentEvent):
            raise PersistentContractError("record() requires an AgentEvent")
        event.assert_matches(execution)
        if event.event_type not in CRITICAL_EVENTS:
            if await self._verified_or_held(execution, event):
                self._queue.submit(event)
            self._observe(event)
            return
        batch = [event]
        if event.event_type in _TERMINAL_EVENTS:
            batch = [*self._queue.pending_outputs(event), event]
            for output in batch[:-1]:
                await asyncio.to_thread(self._verify_file, execution, output)
        await asyncio.to_thread(self._repository.record_events, batch, now=self._clock())
        if event.event_type in _TERMINAL_EVENTS:
            self._queue.forget(event.namespace_id, event.execution_id)
        self._observe(event)

    async def _verified_or_held(self, execution: ExecutionContext, event: AgentEvent) -> bool:
        """Whether an ordinary event may be queued; an invalid file waits for the ending."""
        if event.event_type != EventType.OUTPUT_PROPOSED or event.payload.share_mode not in FILE_SHARE_MODES:
            return True
        try:
            await asyncio.to_thread(self._verify_file, execution, event)
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] shared file not verified yet: execution=%s output=%s error=%s",
                event.execution_id,
                event.payload.output_id,
                exc,
            )
            self._queue.hold(event)
            return False
        return True

    def _verify_file(self, execution: ExecutionContext, event: AgentEvent) -> None:
        if event.payload.share_mode not in FILE_SHARE_MODES:
            return
        if self._files is None:
            raise PersistentAgentError("shared files cannot be verified by this host", code="OUTPUT_FILE_UNAVAILABLE")
        self._files.verify(execution, event.payload)

    def _observe(self, event: AgentEvent) -> None:
        if self._observer is None:
            return
        try:
            self._observer(event)
        except Exception as exc:
            logger.warning(
                "[JiuwenMate] event observer failed: execution=%s seq=%s error=%s",
                event.execution_id,
                event.local_seq,
                exc,
            )

    async def read_execution_log(self, execution: ExecutionContext, execution_id: str) -> tuple[AgentEvent, ...]:
        require_identifier("execution_id", execution_id)
        return await asyncio.to_thread(
            self._repository.read_execution_log,
            execution.namespace_id,
            execution.session_id,
            execution.request_id,
            execution_id,
        )

    async def read_execution_link(self, execution: ExecutionContext, execution_id: str) -> ExecutionLink:
        """The predecessor saved before dispatch; a missing link is an error, not an empty chain."""
        require_identifier("execution_id", execution_id)
        row = await asyncio.to_thread(
            self._repository.execution_link,
            execution.namespace_id,
            execution.request_id,
            execution_id,
        )
        if row is None or row["session_id"] != execution.session_id:
            raise PersistentAgentError("execution link not found", code="NOT_FOUND")
        return ExecutionLink.model_validate(row)
