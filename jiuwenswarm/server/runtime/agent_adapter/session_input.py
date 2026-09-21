# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDK adaptation for supplemental input, without starting another chat turn."""

import asyncio
import time
from typing import Any
from uuid import uuid4

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.schema.interaction import InputDispatchMode

from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.runtime.session_input import SessionInputTargetError, resolve_session_input_mode


class QueuedSessionInput(str):
    """A string accepted by the SDK queue, retaining identity until it is joined.

    ON_USER_MESSAGE receives these exact values before the SDK joins the batch.
    Equal message text must never be used to identify a submitted request.
    """

    def __new__(cls, text: str, request_id: str):
        value = super().__new__(cls, text)
        value.request_id = request_id
        value.display_content = text
        value.boundary_ready = asyncio.Event()
        value.boundary_error = None
        return value


def enqueue_bound_session_input(instance, target_round, request, sdk_request) -> QueuedSessionInput:
    """Check and enqueue synchronously: the SDK send_input idle fallback is forbidden.

    Use the same public queue API as DeepAgent.send_input's active STEER
    branch. No await may separate the target check from enqueueing, including
    SDK send/control lock acquisition, which could otherwise start fresh work.
    Permission admission still belongs to the adapter's existing transaction.
    """
    runtime = get_current_runtime()
    expected_id = request.params.get("expected_execution_id")
    execution = runtime.get_session_execution(expected_id) if runtime and expected_id else None
    if expected_id and execution is None:
        raise SessionInputTargetError(
            "the targeted execution has ended or changed; supplemental input was not sent"
        )
    if execution is not None:
        if (
            execution.session_id != request.session_id
            or execution.state is not SessionExecutionState.RUNNING
            or execution.cancellation_requested
        ):
            raise SessionInputTargetError(
                "the targeted execution has ended or changed; supplemental input was not sent"
            )
    if (
        instance.active_round is not target_round
        or target_round is None
        or not instance.has_output_stream()
    ):
        raise SessionInputTargetError(
            "the targeted execution has ended or changed; supplemental input was not sent"
        )
    controller = instance.loop_controller
    handler = controller.event_handler if controller is not None else None
    if getattr(handler, "interaction_queues", None) is None:
        raise RuntimeError("active execution has no steering queue; supplemental input was not sent")
    entry = QueuedSessionInput(str(sdk_request.inputs["query"]), request.request_id)
    entry.display_content = str(request.params.get("content") or request.params.get("query") or entry)
    controller.enqueue_steer(entry)
    return entry


class SessionInputDeliveryUnknown(RuntimeError):
    """The SDK returned across a closing boundary; do not retry automatically."""

    code = "SESSION_INPUT_DELIVERY_UNKNOWN"


def sdk_input_mode(params: Any) -> InputDispatchMode | None:
    mode = resolve_session_input_mode(params)
    return InputDispatchMode(mode.value) if mode is not None else None


class SessionInputGuard(AgentRail):
    """Guard admission and reconnect the SDK queue when an interaction resumes.

    This uses public callbacks only; it neither drains the steering queue nor
    drives the loop. A rejected input has not been submitted to the SDK.
    """

    def __init__(self, owner: Any):
        super().__init__()
        self.owner = owner
        self.accepting = False
        self._model_allows_steer = False
        self._active_tools = 0
        self._session = None

    async def publish_input_received(self, entry: QueuedSessionInput) -> None:
        """Insert the user boundary into the SAME queue as model output.

        The next model call waits for this marker, even if it drained the input
        while write_stream was backpressured. ACK and output transport timing
        therefore cannot move the user bubble across already emitted text.
        """
        try:
            await self._session.write_stream(OutputSchema(
                type="session_input_received", index=0,
                payload={"input_request_id": entry.request_id, "content": entry.display_content,
                         "timestamp": time.time() * 1000},
            ))
        except (Exception, asyncio.CancelledError) as exc:
            entry.boundary_error = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SessionInputDeliveryUnknown(
                "supplemental delivery is unknown; could not publish the input boundary"
            ) from exc
        finally:
            entry.boundary_ready.set()

    async def on_user_message(self, ctx):
        if ctx.inputs.source == "steering":
            # Keep the mutable batch itself: other rails may remove/reorder parts.
            ctx.extra["session_input_parts"] = ctx.inputs.parts

    async def before_invoke(self, ctx):
        # DeepAgent's InteractiveInput path bypasses the task-loop executor,
        # which normally passes this queue to ReAct. Bind before its first
        # steering drain, leaving consumption and context ordering to the SDK.
        if ctx.agent is not self.owner.react_agent:
            return
        if not isinstance(ctx.inputs.query, InteractiveInput) or ctx.steering_queue is not None:
            return
        handler = self.owner.event_handler
        queues = getattr(handler, "interaction_queues", None)
        if queues is not None:
            ctx.bind_steering_queue(queues.steering)

    async def before_model_call(self, ctx):
        self._session = ctx.session
        parts = ctx.extra.pop("session_input_parts", [])
        entries = [part for part in parts if isinstance(part, QueuedSessionInput)]
        for entry in entries:
            await entry.boundary_ready.wait()
            if entry.boundary_error is not None:
                raise RuntimeError("supplemental input boundary was not published") from entry.boundary_error
        if entries or "session_output_phase" not in ctx.extra:
            phase_id = uuid4().hex
            ctx.extra["session_output_phase"] = phase_id
            await ctx.session.write_stream(OutputSchema(
                type="session_output_phase", index=0,
                payload={"output_phase_id": phase_id,
                         "applied_input_ids": [entry.request_id for entry in entries]},
            ))
        limit = getattr(ctx.agent.config, "max_iterations", 0)
        iteration = getattr(ctx.inputs, "react_iteration", 0)
        self._model_allows_steer = bool(limit and 0 < iteration < limit)
        self.accepting = self._model_allows_steer
        self._active_tools = 0

    async def after_model_call(self, ctx):
        if not getattr(getattr(ctx.inputs, "response", None), "tool_calls", None):
            self.accepting = False

    async def before_tool_call(self, ctx):
        self._active_tools += 1
        self.accepting = self._model_allows_steer

    async def after_tool_call(self, ctx):
        # Close when the last tool settles, but keep admission open for other
        # running tools. A subsequent serial tool also reopens its own window.
        self._active_tools = max(0, self._active_tools - 1)
        self.accepting = self._model_allows_steer and self._active_tools > 0

    async def on_model_exception(self, ctx):
        self.accepting = False

    async def on_tool_exception(self, ctx):
        self.accepting = False

    async def after_invoke(self, ctx):
        self.accepting = False
