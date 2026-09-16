# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execute one machine request through the shared Runtime, then release it."""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.machine_result import RunSummary
from jiuwenswarm.channels.process_cli.machine_signals import (
    command_signals,
    defer_command_signals,
)
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunInput,
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent

if TYPE_CHECKING:
    from jiuwenswarm.channels.process_cli.duplex_control import DuplexController

logger = logging.getLogger(__name__)
CHANNEL_ID = "process_cli"
SHUTDOWN_TIMEOUT_SECONDS = 5.0
_INTERACTION_EVENTS = frozenset(
    {"chat.ask_user_question", "plan.approval_required", "harness.activate_interaction"}
)


class MachineRunError(RuntimeError):
    """A stable failure of this noninteractive execution boundary."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _exception_info(error: Exception) -> RuntimeErrorInfo:
    code = getattr(error, "code", "RUNTIME_FAILED")
    code = getattr(code, "value", code)
    if not isinstance(code, str) or not code.strip():
        code = "RUNTIME_FAILED"
    # Unknown dependency exceptions may contain credentials or full requests.
    message = (
        str(error)
        if isinstance(error, MachineRunError)
        else "Runtime operation failed."
    )
    return RuntimeErrorInfo(
        code=code,
        message=message,
        retryable=getattr(error, "retryable", False) is True,
    )


def _cancel_request(
    request: AgentRequest, *, session_scope: bool = False
) -> AgentRequest:
    params = request.params or {}
    return AgentRequest(
        request_id=request.request_id,
        channel_id=CHANNEL_ID,
        session_id=request.session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        is_stream=False,
        timestamp=time.time(),
        params={
            "intent": "cancel",
            "target_request_id": "" if session_scope else request.request_id,
            "mode": params.get("mode"),
            "work_mode": params.get("work_mode"),
            "project_dir": params.get("project_dir"),
        },
    )


def _workspace_params(run_input: OneShotRunInput, *, resumed: bool) -> dict[str, Any]:
    workspace = run_input.workspace
    if resumed:
        # Omission means inheritance, not overwriting persisted bindings with
        # the caller's current directory. Runtime owns the merge semantics.
        if workspace is None:
            return {}
        params: dict[str, Any] = {}
        if workspace.cwd is not None:
            params["cwd"] = workspace.cwd
        if workspace.project_dir is not None:
            params["project_dir"] = workspace.project_dir
        if workspace.trusted_dirs:
            params["trusted_dirs"] = list(workspace.trusted_dirs)
        return params
    cwd = workspace.cwd if workspace is not None else None
    project_dir = workspace.project_dir if workspace is not None else None
    cwd = cwd or os.getcwd()
    project_dir = project_dir or cwd
    trusted_dirs = workspace.trusted_dirs if workspace is not None else ()
    return {
        "cwd": cwd,
        "project_dir": project_dir,
        "trusted_dirs": list(trusted_dirs) or [project_dir],
    }


class _MachineRun:
    """Own only the process's public client, request and stream handles."""

    def __init__(
        self,
        run_input: OneShotRunInput,
        writer: OneShotWriter,
        *,
        control: DuplexController | None = None,
    ) -> None:
        self.run_input = run_input
        self.writer = writer
        self.client: InProcessRuntimeClient | None = None
        self.request: AgentRequest | None = None
        self.stream: AsyncIterator[RuntimeEvent] | None = None
        self.summary = RunSummary()
        self.error: RuntimeErrorInfo | None = None
        self.status = RunStatus.COMPLETED
        self.exit_code = 0
        self.cleanup_errors: list[str] = []
        self.control = control

    def fail(
        self,
        error: RuntimeErrorInfo,
        *,
        status: RunStatus = RunStatus.FAILED,
        exit_code: int = 1,
    ) -> None:
        if self.error is not None:
            return
        self.error = error
        self.status = status
        self.exit_code = exit_code

    async def execute(
        self, client_factory: Callable[[], InProcessRuntimeClient]
    ) -> None:
        self.client = client_factory()
        await self.client.start()
        descriptor = None
        if self.run_input.session_id is not None:
            descriptor = await self.client.describe_session(
                session_id=self.run_input.session_id
            )
            if descriptor is None:
                raise MachineRunError("Session not found.", code="SESSION_NOT_FOUND")
            # Validate the existing root even if the caller supplies a new mode.
            self.client.resolve_mode_capability(descriptor.mode)
        mode_name = self.run_input.mode or (
            descriptor.mode if descriptor else "agent.code.normal"
        )
        mode = self.client.resolve_mode_capability(mode_name)
        if self.run_input.agent is not None:
            self.client.validate_agent_definition(
                self.run_input.agent.to_dict(), mode=mode.mode
            )
        session_id = await self.client.create_or_resume_session(
            channel_id=CHANNEL_ID,
            session_id=self.run_input.session_id,
        )
        self.writer.session_id = session_id
        params = _workspace_params(self.run_input, resumed=descriptor is not None)
        params.update(
            {
                "query": self.run_input.input,
                "content": self.run_input.input,
                "mode": mode.mode,
                "work_mode": mode.work_mode,
                "supports_user_interaction": self.control is not None,
            }
        )
        self.request = AgentRequest(
            request_id=self.writer.request_id,
            channel_id=CHANNEL_ID,
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            timestamp=time.time(),
            params=params,
        )
        if self.run_input.agent is None:
            self.stream = self.client.stream(self.request)
        else:
            self.stream = self.client.stream_agent(
                self.request, self.run_input.agent.to_dict()
            )
        if self.control is not None:
            completed = await self.control.consume(
                self.stream,
                client=self.client,
                request=self.request,
                observe=self.observe,
            )
        else:
            completed = await self.consume_noninteractive()
        if self.summary.error is not None:
            self.fail(self.summary.error)
        elif not completed:
            raise MachineRunError(
                "Runtime stream ended without a completion event.",
                code="INCOMPLETE_RUN",
            )

    def observe(self, event: RuntimeEvent) -> None:
        self.writer.write_event(event)
        self.summary.observe(event)
        if self.summary.error is not None:
            self.fail(self.summary.error)

    async def consume_noninteractive(self) -> bool:
        completed = False
        stream = self.stream
        if stream is None:
            return False
        async for event in stream:
            self.observe(event)
            if self.summary.error is not None:
                return False
            if event.ok and (event.is_complete or event.event_type == "chat.final"):
                completed = True
            if event.event_type in _INTERACTION_EVENTS:
                raise MachineRunError(
                    "This command cannot answer interactions; no approval was granted.",
                    code="INTERACTION_REQUIRED",
                )
        return completed

    async def cleanup_step(
        self, name: str, operation: Callable[[], Awaitable[Any]]
    ) -> None:
        try:
            # Stay in the consuming task: async generator context tokens cannot
            # safely be reset by wait_for's separate task.
            async with asyncio.timeout(SHUTDOWN_TIMEOUT_SECONDS):
                await operation()
        except (Exception, asyncio.CancelledError, builtins.SystemExit) as error:
            self.cleanup_errors.append(name)
            logger.warning("one-shot %s failed (%s)", name, type(error).__name__)
            if isinstance(error, asyncio.CancelledError) and self.error is None:
                self.fail(
                    RuntimeErrorInfo(code="CANCELLED", message="Command interrupted."),
                    status=RunStatus.CANCELLED,
                    exit_code=130,
                )

    async def cleanup(self) -> None:
        if self.control is not None:
            await self.cleanup_step("control_input", self.control.stop_input)
        client = self.client
        request = self.request
        session_id = self.writer.session_id
        if client is None:
            self._record_cleanup_errors()
            return
        if self.error is not None and request is not None:
            await self.cleanup_step(
                "cancel",
                lambda: client.cancel(
                    _cancel_request(request, session_scope=self.control is not None)
                ),
            )
        if self.control is not None:
            await self.cleanup_step("control_streams", self.control.close_streams)
        close_stream = getattr(self.stream, "aclose", None)
        if close_stream is not None:
            await self.cleanup_step("stream_close", close_stream)
        if session_id is not None:
            await self.cleanup_step(
                "cleanup_session",
                lambda: client.cleanup_session(
                    channel_id=CHANNEL_ID,
                    session_id=session_id,
                ),
            )
        await self.cleanup_step("runtime_close", client.close)
        self._record_cleanup_errors()

    def _record_cleanup_errors(self) -> None:
        if self.cleanup_errors:
            error = self.error or RuntimeErrorInfo(
                code="SHUTDOWN_FAILED", message="Runtime cleanup failed."
            )
            if self.error is None:
                self.fail(error)
            self.error = replace(
                error,
                details={
                    **dict(error.details),
                    "cleanup_errors": tuple(self.cleanup_errors),
                },
            )

    def result(self) -> OneShotRunResult:
        return OneShotRunResult(
            sequence=self.writer.sequence,
            request_id=self.writer.request_id,
            session_id=self.writer.session_id,
            status=self.status,
            exit_code=self.exit_code,
            output=self.summary.output,
            usage=self.summary.usage,
            error=self.error,
        )


async def run_machine(
    run_input: OneShotRunInput,
    writer: OneShotWriter,
    *,
    client_factory: Callable[[], InProcessRuntimeClient] = InProcessRuntimeClient,
    control: DuplexController | None = None,
) -> OneShotRunResult:
    """Return the sole outcome only after cancelling/releasing owned resources.

    The caller writes it after asyncio's own shutdown, not while Runtime is
    still active. A broken pipe is a failed run whose cleanup still happens.
    """
    run = _MachineRun(run_input, writer, control=control)
    deadline = asyncio.timeout(run_input.timeout_seconds)
    try:
        if control is not None:
            control.start()
        async with deadline:
            await run.execute(client_factory)
    except TimeoutError as error:
        if deadline.expired():
            run.fail(
                RuntimeErrorInfo(code="TIMEOUT", message="Command timed out."),
                status=RunStatus.TIMED_OUT,
                exit_code=124,
            )
        else:
            run.fail(_exception_info(error))
    except asyncio.CancelledError:
        if control is not None and control.failure is not None:
            run.fail(
                control.failure,
                status=control.failure_status,
                exit_code=control.failure_exit_code,
            )
        else:
            run.fail(
                RuntimeErrorInfo(code="CANCELLED", message="Command interrupted."),
                status=RunStatus.CANCELLED,
                exit_code=130,
            )
    except OSError as error:
        code = "OUTPUT_CLOSED" if writer.broken else "RUNTIME_FAILED"
        run.fail(RuntimeErrorInfo(code=code, message="Command I/O failed."))
        logger.warning("one-shot I/O failed (%s)", type(error).__name__)
    except builtins.SystemExit:
        run.fail(
            RuntimeErrorInfo(
                code="RUNTIME_FAILED", message="Runtime exited unexpectedly."
            )
        )
    except Exception as error:  # noqa: BLE001 - machine execution boundary
        run.fail(_exception_info(error))
        logger.warning("one-shot execution failed (%s)", type(error).__name__)
    finally:
        defer_command_signals()
        await run.cleanup()
    return run.result()


async def run_with_signals(
    run_input: OneShotRunInput,
    writer: OneShotWriter,
    *,
    control: DuplexController | None = None,
) -> OneShotRunResult:
    """Route OS termination to the same bounded cancellation/cleanup path."""
    with command_signals() as signals:
        return await signals.run(run_machine(run_input, writer, control=control))
