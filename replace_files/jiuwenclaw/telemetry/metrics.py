# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Metric instrument definitions for JiuWenClaw telemetry."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.resources import Resource

from jiuwenclaw.telemetry.attributes import JIUWENCLAW_CLAW_ID

_meter = metrics.get_meter("jiuwenclaw")
_session_active_observer: Callable[[], int] | None = None

# Global Resource reference for metric attributes
_resource: Resource | None = None


def set_resource(resource: Resource | None) -> None:
    """设置 Resource 引用，用于从 Resource 获取 claw_id."""
    global _resource
    _resource = resource


def _with_resource_labels(attrs: dict) -> dict:
    """将 claw.id 注入到 metric attributes 中（从 Resource 获取）."""
    result = dict(attrs)
    if _resource is not None:
        # 从 Resource.attributes 获取 claw_id
        claw_id = _resource.attributes.get(JIUWENCLAW_CLAW_ID)
        if claw_id is not None:
            result[JIUWENCLAW_CLAW_ID] = claw_id
    return result


def set_session_active_observer(observer: Callable[[], int] | None) -> None:
    """Register the current active-session observer used by the session gauge."""
    global _session_active_observer
    _session_active_observer = observer


def _observe_session_active(_options: CallbackOptions) -> Iterable[Observation]:
    observer = _session_active_observer
    if observer is None:
        return []

    try:
        active_sessions = max(int(observer()), 0)
    except Exception:
        return []

    # Add claw.id to attributes
    attrs = {}
    if _resource is not None:
        claw_id = _resource.attributes.get(JIUWENCLAW_CLAW_ID)
        if claw_id is not None:
            attrs[JIUWENCLAW_CLAW_ID] = claw_id

    return [Observation(active_sessions, attributes=attrs)]

# --- Histograms ---
request_duration = _meter.create_histogram(
    name="jiuwenclaw.request.duration",
    unit="s",
    description="End-to-end request processing duration",
)

agent_duration = _meter.create_histogram(
    name="jiuwenclaw.agent.duration",
    unit="s",
    description="Agent invoke duration",
)

llm_duration = _meter.create_histogram(
    name="gen_ai.client.operation.duration",
    unit="s",
    description="GenAI LLM call duration",
)

tool_duration = _meter.create_histogram(
    name="gen_ai.tool.duration",
    unit="s",
    description="Tool execution duration",
)

# --- Counters ---
request_count = _meter.create_counter(
    name="jiuwenclaw.request.count",
    unit="{request}",
    description="Total request count",
)

request_error_count = _meter.create_counter(
    name="jiuwenclaw.request.error.count",
    unit="{request}",
    description="Failed request count",
)

llm_call_count = _meter.create_counter(
    name="gen_ai.client.operation.count",
    unit="{call}",
    description="LLM call count",
)

token_usage = _meter.create_counter(
    name="gen_ai.client.token.usage",
    unit="{token}",
    description="Token usage by type (input/output/cache)",
)

tool_call_count = _meter.create_counter(
    name="gen_ai.tool.call.count",
    unit="{call}",
    description="Tool call count",
)

tool_error_count = _meter.create_counter(
    name="gen_ai.tool.error.count",
    unit="{call}",
    description="Tool call error count",
)

# --- Session metrics ---
session_active = _meter.create_observable_gauge(
    name="jiuwenclaw.session.active",
    callbacks=[_observe_session_active],
    unit="{session}",
    description="Current active session count",
)

session_created_count = _meter.create_counter(
    name="jiuwenclaw.session.created.count",
    unit="{session}",
    description="Total created session count",
)

session_state_count = _meter.create_counter(
    name="jiuwenclaw.session.state",
    unit="{transition}",
    description="Session state transition count",
)

session_stuck_count = _meter.create_counter(
    name="jiuwenclaw.session.stuck",
    unit="{occurrence}",
    description="Session stuck occurrence count",
)

session_stuck_age = _meter.create_histogram(
    name="jiuwenclaw.session.stuck_age_ms",
    unit="ms",
    description="Duration a session has been stuck",
)

# --- Queue metrics ---
_queue_depth_observer: Callable[[], list[Observation]] | None = None


def set_queue_depth_observer(observer: Callable[[], list[Observation]] | None) -> None:
    """Register the queue depth observer used by the queue gauge."""
    global _queue_depth_observer
    _queue_depth_observer = observer


def _observe_queue_depth(_options: CallbackOptions) -> Iterable[Observation]:
    observer = _queue_depth_observer
    if observer is None:
        return []
    try:
        observations = observer()
        # Wrap each Observation with claw.id
        wrapped = []
        for obs in observations:
            attrs = dict(obs.attributes or {})
            if _resource is not None:
                claw_id = _resource.attributes.get(JIUWENCLAW_CLAW_ID)
                if claw_id is not None:
                    attrs[JIUWENCLAW_CLAW_ID] = claw_id
            wrapped.append(Observation(obs.value, attributes=attrs))
        return wrapped
    except Exception:
        return []


queue_depth = _meter.create_observable_gauge(
    name="jiuwenclaw.queue.depth",
    callbacks=[_observe_queue_depth],
    unit="{message}",
    description="Current message count in queue",
)

queue_enqueued = _meter.create_counter(
    name="jiuwenclaw.queue.enqueued",
    unit="{message}",
    description="Total enqueued message count",
)

queue_dequeued = _meter.create_counter(
    name="jiuwenclaw.queue.dequeued",
    unit="{message}",
    description="Total dequeued message count",
)

queue_wait_duration = _meter.create_histogram(
    name="jiuwenclaw.queue.wait_duration",
    unit="ms",
    description="Message wait duration in queue",
)

message_processed = _meter.create_counter(
    name="jiuwenclaw.message.processed",
    unit="{message}",
    description="Processed message count by status",
)

# --- Recorder helpers with resource labels injection ---


def record_agent_duration(duration: float, attrs: dict) -> None:
    agent_duration.record(duration, _with_resource_labels(attrs))


def record_request_duration(duration: float, attrs: dict) -> None:
    request_duration.record(duration, _with_resource_labels(attrs))


def record_llm_duration(duration: float, attrs: dict) -> None:
    llm_duration.record(duration, _with_resource_labels(attrs))


def record_tool_duration(duration: float, attrs: dict) -> None:
    tool_duration.record(duration, _with_resource_labels(attrs))


def record_queue_wait_duration(duration_ms: float, attrs: dict) -> None:
    queue_wait_duration.record(duration_ms, _with_resource_labels(attrs))


def record_session_stuck_age(age_ms: float, attrs: dict) -> None:
    session_stuck_age.record(age_ms, _with_resource_labels(attrs))


def add_request_count(value: int, attrs: dict) -> None:
    request_count.add(value, _with_resource_labels(attrs))


def add_request_error_count(value: int, attrs: dict) -> None:
    request_error_count.add(value, _with_resource_labels(attrs))


def add_llm_call_count(value: int, attrs: dict) -> None:
    llm_call_count.add(value, _with_resource_labels(attrs))


def add_token_usage(value: int, attrs: dict) -> None:
    token_usage.add(value, _with_resource_labels(attrs))


def add_tool_call_count(value: int, attrs: dict) -> None:
    tool_call_count.add(value, _with_resource_labels(attrs))


def add_tool_error_count(value: int, attrs: dict) -> None:
    tool_error_count.add(value, _with_resource_labels(attrs))


def add_session_created_count(value: int) -> None:
    session_created_count.add(value, _with_resource_labels({}))


def add_session_state_count(value: int, attrs: dict) -> None:
    session_state_count.add(value, _with_resource_labels(attrs))


def add_session_stuck_count(value: int, attrs: dict) -> None:
    session_stuck_count.add(value, _with_resource_labels(attrs))


def add_queue_enqueued(value: int, attrs: dict) -> None:
    queue_enqueued.add(value, _with_resource_labels(attrs))


def add_queue_dequeued(value: int, attrs: dict) -> None:
    queue_dequeued.add(value, _with_resource_labels(attrs))


def add_message_processed(value: int, attrs: dict) -> None:
    message_processed.add(value, _with_resource_labels(attrs))
