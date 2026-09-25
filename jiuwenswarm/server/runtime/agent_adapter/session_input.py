# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDK adaptation for supplemental input, without starting another chat turn."""

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, AgentRail
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
    UserMessage,
)
from openjiuwen.harness.schema.interaction import InputDispatchMode

from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY, SESSION_MESSAGE_ORIGIN
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.runtime.session_input import SessionInputTargetError, resolve_session_input_mode
from jiuwenswarm.server.runtime.agent_adapter.session_message_input import cross_session_model_messages


_INPUT_BOUNDARY_TIMEOUT_SECONDS = 5.0


class QueuedSessionInput(str):
    """A string accepted by the SDK queue, retaining identity until it is joined.

    ON_USER_MESSAGE receives these exact values before the SDK joins the batch.
    Equal message text must never be used to identify a submitted request.
    """

    def __new__(cls, text: str, request_id: str):
        value = super().__new__(cls, text)
        value.request_id = request_id
        value.display_content = text
        value.cross_session = None
        value.message_route = None
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
    cross_session = request.params.get(SESSION_MESSAGE_INTERNAL_KEY)
    if isinstance(cross_session, dict):
        entry.cross_session = {**cross_session, "content": entry.display_content}
        entry.message_route = {
            "session_id": request.session_id,
            "request_id": request.request_id,
            "user_id": request.user_id,
            "chain_id": cross_session.get("chain_id", ""),
            "parent_message_id": cross_session.get("message_id", ""),
            "hop_count": cross_session.get("hop_count", 0),
        }
    controller.enqueue_steer(entry)
    return entry


class SessionInputDeliveryUnknown(RuntimeError):
    """The SDK returned across a closing boundary; do not retry automatically."""

    code = "SESSION_INPUT_DELIVERY_UNKNOWN"


def sdk_input_mode(params: Any) -> InputDispatchMode | None:
    mode = resolve_session_input_mode(params)
    return InputDispatchMode(mode.value) if mode is not None else None


class SessionInputBoundaryGuard(AgentRail):
    """Remove failed publications before memory or other input observers run."""

    priority = 1000

    async def on_user_message(self, ctx):
        admitted = []
        for part in ctx.inputs.parts:
            if isinstance(part, QueuedSessionInput):
                await part.boundary_ready.wait()
                if part.boundary_error is not None:
                    continue
            admitted.append(part)
        ctx.inputs.parts[:] = admitted


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
        self._generation_boundary_pending = False
        self._session = None
        self.boundary_guard = SessionInputBoundaryGuard()

    def callback_priority(self, event: AgentCallbackEvent) -> int:
        # Admit the final batch after other rails have removed/reordered inputs.
        if event is AgentCallbackEvent.ON_USER_MESSAGE:
            return -1000
        return super().callback_priority(event)

    async def publish_input_received(self, entry: QueuedSessionInput) -> None:
        """Insert the user boundary into the SAME queue as model output.

        Admission waits for this marker before adding the input to model history.
        Failed or cancelled publication discards that input without failing the
        original task. Bound backpressure so it cannot stall the target forever.
        """
        try:
            provenance = {}
            if entry.cross_session:
                provenance = {
                    "message_origin": SESSION_MESSAGE_ORIGIN,
                    "session_message_id": entry.cross_session["message_id"],
                    "cross_session": entry.cross_session,
                }
            await asyncio.wait_for(
                self._session.write_stream(OutputSchema(
                    type="session_input_received", index=0,
                    payload={"input_request_id": entry.request_id, "content": entry.display_content,
                             "timestamp": time.time() * 1000, **provenance},
                )),
                timeout=_INPUT_BOUNDARY_TIMEOUT_SECONDS,
            )
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
        parts = ctx.inputs.parts
        if ctx.inputs.source == "steering":
            ctx.extra["session_input_parts"] = list(parts)

        run_context = ctx.extra.get("run_context")
        extra = (
            run_context.get("extra", {})
            if isinstance(run_context, Mapping)
            else getattr(run_context, "extra", {})
        )
        cross_session = extra.get(SESSION_MESSAGE_INTERNAL_KEY) if isinstance(extra, Mapping) else None
        if ctx.inputs.source == "query" and isinstance(cross_session, dict) and parts:
            await ctx.context.add_messages(cross_session_model_messages("\n".join(parts), cross_session))
            parts.clear()
        elif any(isinstance(part, QueuedSessionInput) and part.cross_session for part in parts):
            # Preserve batch order, including interleaved human and Agent input.
            messages = []
            for part in parts:
                if isinstance(part, QueuedSessionInput) and part.cross_session:
                    messages.extend(cross_session_model_messages(part, part.cross_session))
                else:
                    messages.append(UserMessage(
                        content=f"[STEERING] {part}",
                        metadata={
                            OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
                            OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: ctx.inputs.source,
                        },
                    ))
            await ctx.context.add_messages(messages)
            parts.clear()

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
        if entries:
            # Tool Tasks inherit the consumed Agent input's message chain,
            # rather than resetting its hop count to that of the original turn.
            # A mixed batch uses the deepest chain conservatively.
            routes = [entry.message_route for entry in entries if entry.message_route]
            ctx.extra["session_input_message_route"] = (
                max(routes, key=lambda route: route["hop_count"]) if routes else None
            )
        if entries or "session_output_phase" not in ctx.extra:
            phase_id = uuid4().hex
            ctx.extra["session_output_phase"] = phase_id
            await ctx.session.write_stream(OutputSchema(
                type="session_output_phase", index=0,
                payload={"output_phase_id": phase_id,
                         "applied_input_ids": [entry.request_id for entry in entries]},
            ))
        limit = getattr(ctx.agent.config, "max_iterations", None)
        iteration = getattr(ctx.inputs, "react_iteration", 0)
        # Unconfigured (None) means the inner loop is unbounded, so steer
        # stays open. A configured cap keeps the original in-window check.
        if limit is None:
            self._model_allows_steer = True
        else:
            self._model_allows_steer = bool(limit and 0 < iteration < limit)
        self.accepting = self._model_allows_steer
        self._active_tools = 0

    async def before_steering_drain(self, ctx):
        """Mark the next visible model output as a steered generation."""
        if int(getattr(ctx.inputs, "pending", 0) or 0) > 0:
            self._generation_boundary_pending = True

    def consume_generation_boundary(self) -> bool:
        pending = self._generation_boundary_pending
        self._generation_boundary_pending = False
        return pending

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
