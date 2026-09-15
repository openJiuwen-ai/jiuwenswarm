# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-command stdio control routing; execution and approvals stay in Runtime."""

from __future__ import annotations

import asyncio
import builtins
import uuid
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.duplex_input import DuplexLineReader
from jiuwenswarm.channels.process_cli.duplex_protocol import decode_control
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.protocol import RunStatus, RuntimeErrorInfo
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.interaction import InteractionAnswerInput

_INTERACTIONS = frozenset(
    {"chat.ask_user_question", "plan.approval_required", "harness.activate_interaction"}
)
_ACK_EVENTS = frozenset(
    {"runtime.accepted", "chat.answer_accepted", "harness.activate_resume_ack"}
)
_MAX_PENDING = 16


class DuplexControlError(ValueError):
    """Invalid routing or an unsupported interaction, without input echo."""

    def __init__(self, message: str, *, code: str = "INVALID_CONTROL") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _StreamItem:
    operation_id: str
    event: RuntimeEvent | None = None
    error: Exception | None = None
    done: bool = False


class DuplexController:
    """Route answers to observed cards within exactly one owned Session.

    Original and answer streams can both remain open: Runtime decides which
    one owns subsequent output. Their EOFs/acks are not separate command results.
    """

    def __init__(self, reader: DuplexLineReader, writer: OneShotWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.failure: RuntimeErrorInfo | None = None
        self.failure_status = RunStatus.FAILED
        self.failure_exit_code = 2
        self._owner: asyncio.Task[Any] | None = None
        self._input_task: asyncio.Task[None] | None = None
        self._client: InProcessRuntimeClient | None = None
        self._request: AgentRequest | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._queue: asyncio.Queue[_StreamItem] = asyncio.Queue(maxsize=32)
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._stopping_input = False
        self._stopping_streams = False
        self._input_closed = False

    def start(self) -> None:
        self._owner = asyncio.current_task()
        self._input_task = asyncio.create_task(
            self._read_controls(), name="one-shot-controls"
        )

    def _terminate(self, *, code: str, message: str, cancelled: bool = False) -> None:
        if self._stopping_input or self.failure is not None:
            return
        self.failure = RuntimeErrorInfo(code=code, message=message)
        if cancelled:
            self.failure_status = RunStatus.CANCELLED
            self.failure_exit_code = 130
        if self._owner is not None:
            self._owner.cancel()

    async def _read_controls(self) -> None:
        try:
            while not self._stopping_input:
                line = await self.reader.read_line()
                if self._stopping_input:
                    return
                if line is None:
                    self._input_closed = True
                    self._check_answer_available()
                    return
                control = decode_control(line)
                if control.request_id != self.writer.request_id:
                    raise DuplexControlError("Control request does not match this run.")
                if (
                    control.session_id is not None
                    and control.session_id != self.writer.session_id
                ):
                    raise DuplexControlError("Control Session does not match this run.")
                if control.kind == "cancel":
                    self._terminate(
                        code="CANCELLED",
                        message="Command cancelled by its caller.",
                        cancelled=True,
                    )
                    return
                payload = self._pending.pop(control.interaction_id or "", None)
                if payload is None:
                    raise DuplexControlError(
                        "Interaction is not pending.", code="INTERACTION_NOT_PENDING"
                    )
                if self._request is None or self._client is None:
                    raise DuplexControlError("Execution has not started.")
                answer = self._answer_input(payload, control.answers)
                self._add_stream(
                    answer.request_id, self._client.stream_interaction_answer(answer)
                )
        except Exception as error:  # noqa: BLE001 - control input must stop owned execution
            code = getattr(error, "code", "INVALID_CONTROL")
            self._terminate(
                code=code,
                message="Control input was rejected; no answer was delivered.",
            )

    def _check_answer_available(self) -> None:
        if self._input_closed and self._pending:
            self._terminate(
                code="INPUT_CLOSED",
                message="Control input closed while an interaction requires an answer.",
                cancelled=True,
            )

    def _answer_input(
        self, payload: dict[str, Any], answers: tuple[dict[str, Any], ...]
    ) -> InteractionAnswerInput:
        request = self._request
        if request is None:
            raise DuplexControlError("Execution has not started.")
        params = request.params or {}
        return InteractionAnswerInput(
            request_id=f"answer_{uuid.uuid4().hex}",
            channel_id=request.channel_id,
            session_id=request.session_id or "",
            interaction_id=str(
                payload.get("request_id") or payload.get("interaction_id") or ""
            ),
            answers=answers,
            source=str(payload.get("source") or ""),
            approval_schema=str(payload.get("approval_schema") or ""),
            evolution_meta=payload.get("evolution_meta"),
            mode=str(params.get("mode") or ""),
            work_mode=str(params.get("work_mode") or ""),
            project_dir=str(params.get("project_dir") or ""),
            cwd=str(params.get("cwd") or ""),
            trusted_dirs=tuple(params.get("trusted_dirs") or ()),
        )

    def _add_stream(
        self, operation_id: str, stream: AsyncIterator[RuntimeEvent]
    ) -> None:
        self._streams[operation_id] = asyncio.create_task(
            self._pump(operation_id, stream),
            name="one-shot-runtime-stream",
        )

    async def _pump(
        self, operation_id: str, stream: AsyncIterator[RuntimeEvent]
    ) -> None:
        error = None
        try:
            async for event in stream:
                if not self._stopping_streams:
                    await self._queue.put(_StreamItem(operation_id, event=event))
        except Exception as caught:  # noqa: BLE001 - propagate through the consumer queue
            error = caught
        except builtins.SystemExit:
            error = RuntimeError("Runtime stream exited unexpectedly.")
        finally:
            close = getattr(stream, "aclose", None)
            try:
                if close is not None:
                    await close()
            except (Exception, builtins.SystemExit) as caught:  # noqa: BLE001 - stream-close boundary
                if error is None:
                    error = (
                        RuntimeError("Runtime stream exited during close.")
                        if isinstance(caught, builtins.SystemExit)
                        else caught
                    )
            if not self._stopping_streams:
                await self._queue.put(_StreamItem(operation_id, error=error, done=True))
            elif error is not None:
                raise error

    def _register_interaction(self, event: RuntimeEvent) -> tuple[str, dict[str, Any]]:
        payload = deepcopy(event.payload or {})
        locator = payload.get("request_id") or payload.get("interaction_id")
        if (
            event.event_type == "harness.activate_interaction"
            or not isinstance(locator, str)
            or not locator
        ):
            raise DuplexControlError(
                "This interaction is not supported by the typed Runtime API.",
                code="INTERACTION_UNSUPPORTED",
            )
        if len(self._pending) >= _MAX_PENDING:
            raise DuplexControlError("Too many pending interactions.")
        token = uuid.uuid4().hex
        self._pending[token] = payload
        return token, payload

    def _notice(self, event_type: str, **payload: Any) -> None:
        self.writer.write_event(
            RuntimeEvent.control(
                request_id=self.writer.request_id,
                channel_id="process_cli",
                session_id=self.writer.session_id,
                payload={"event_type": event_type, **payload},
            )
        )

    async def consume(
        self,
        stream: AsyncIterator[RuntimeEvent],
        *,
        client: InProcessRuntimeClient,
        request: AgentRequest,
        observe: Callable[[RuntimeEvent], None],
    ) -> bool:
        self._client = client
        self._request = request
        self._notice("run.started")
        self._add_stream(request.request_id, stream)
        completed = False
        had_interaction = False
        while self._streams or self._pending:
            item = await self._queue.get()
            if item.done:
                task = self._streams.pop(item.operation_id)
                await task
                if item.error is not None:
                    raise item.error
                continue
            event = item.event
            if event is None:
                continue
            if not event.ok or event.event_type in {"chat.error", "runtime.error"}:
                observe(event)
                return False
            if event.event_type in _INTERACTIONS:
                completed = False
                had_interaction = True
                token, payload = self._register_interaction(event)
                observe(event)
                self._notice(
                    "interaction.requested", interaction_id=token, interaction=payload
                )
                self._check_answer_available()
            else:
                observe(event)
                if event.event_type not in _ACK_EVENTS and event.ok:
                    root_terminal = (
                        not had_interaction
                        and item.operation_id == request.request_id
                        and event.is_complete
                    )
                    # UI segment flushes and stream EOF are not execution
                    # outcomes. Runtime distinguishes suspended flushes from
                    # completed (possibly text-free) runs without text guesses.
                    final = event.event_type == "chat.final" and not self._pending
                    explicit = (
                        event.runtime_completion == "completed" and not self._pending
                    )
                    completion_observed = final or root_terminal or explicit
                    if event.runtime_completion != "suspended" and completion_observed:
                        completed = True
        return completed

    async def stop_input(self) -> None:
        self._stopping_input = True
        # The command consumer has stopped. Runtime cancellation may close a
        # producer before close_streams runs; its finally must not block while
        # enqueuing a terminal item into an undrained, full output queue.
        self._stopping_streams = True
        # Unblock producers already waiting in put(). Keep consuming Runtime
        # output while its public cancellation API terminates the execution,
        # but no longer enqueue observations for the stopped command consumer.
        while not self._queue.empty():
            self._queue.get_nowait()
        await self.reader.close()
        if self._input_task is not None:
            self._input_task.cancel()
            await asyncio.gather(self._input_task, return_exceptions=True)

    async def close_streams(self) -> None:
        self._stopping_streams = True
        tasks = list(self._streams.values())
        self._streams.clear()
        self._pending.clear()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result
