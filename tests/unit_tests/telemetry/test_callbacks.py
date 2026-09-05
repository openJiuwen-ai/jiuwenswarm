from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Iterator
import inspect
from types import SimpleNamespace
import threading
import textwrap
import time

import pytest

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from openjiuwen.agent_teams.observability import (
    ObservabilityConfig,
    init_observability,
    shutdown_observability,
)
from openjiuwen.agent_teams.observability.span_context import (
    clear_team_span,
    set_current_agent_span,
    set_team_span,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

from openjiuwen.core.runner.callback import (
    AgentEvents,
    AsyncCallbackFramework,
    LLMCallEvents,
    ToolCallEvents,
)

from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
import jiuwenswarm.telemetry.enrichment.callbacks as callbacks_module
from jiuwenswarm.telemetry.enrichment.callbacks import (
    EnrichmentStateRegistry,
    INPUT_PRIORITY,
    NAMESPACE,
    OUTPUT_PRIORITY,
    MetricCallStateRegistry,
    RichTelemetryCallbacks,
    SkillSessionRegistry,
)
from jiuwenswarm.telemetry.enrichment.tokens import ContextTokenBreakdown
from jiuwenswarm.telemetry.span_registry import SpanRegistryProcessor
from jiuwenswarm.telemetry.metrics import TelemetryMetrics, metrics_channel_id


def test_callback_label_keeps_searching_after_string_conversion_failure() -> None:
    class Unstringable:
        @staticmethod
        def __str__() -> str:
            raise ValueError("cannot stringify")

    assert (
        RichTelemetryCallbacks._callback_label(
            {"provider": Unstringable()},
            {"provider": "fallback"},
            "provider",
        )
        == "fallback"
    )


def test_callback_label_does_not_continue_directly_from_exception_handler() -> None:
    source = textwrap.dedent(
        inspect.getsource(RichTelemetryCallbacks._callback_label)
    )
    tree = ast.parse(source)

    assert not any(
        isinstance(child, ast.Continue)
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        for child in ast.walk(handler)
    )


def test_validate_core_ordering_is_static() -> None:
    descriptor = RichTelemetryCallbacks.__dict__["validate_core_ordering"]

    assert isinstance(descriptor, staticmethod)


def _metric_points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


class _Metrics:
    def add(self, name, value, attributes=None):
        del name, value, attributes


class _RecordingMetrics:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str, int | float, dict]] = []
        self.error = error

    def add(self, name, value, attributes=None):
        if self.error is not None:
            raise self.error
        self.calls.append(("add", name, value, dict(attributes or {})))

    def record(self, name, value, attributes=None):
        if self.error is not None:
            raise self.error
        self.calls.append(("record", name, value, dict(attributes or {})))


class _RealSkillLifecycleTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        error: BaseException | None = None,
        success: bool = True,
    ) -> None:
        super().__init__(ToolCard(name=name, description="Telemetry skill fixture"))
        self._error = error
        self._success = success

    async def invoke(self, inputs, **kwargs):
        del kwargs
        if self._error is not None:
            raise self._error
        if not self._success:
            return ToolOutput(success=False, error="skill load failed")
        return ToolOutput(
            success=True,
            data={
                "skill_directory": f"/skills/{inputs['skill_name']}",
                "skill_content": "body",
            },
        )

    async def stream(self, inputs, **kwargs):
        del inputs, kwargs
        if False:
            yield None


class _FailingFramework(AsyncCallbackFramework):
    def __init__(
        self,
        *,
        fail_register_at: int | None = None,
        fail_unregister_at: int | None = None,
    ) -> None:
        super().__init__()
        self.fail_register_at = fail_register_at
        self.fail_unregister_at = fail_unregister_at
        self.register_count = 0
        self.unregister_count = 0
        self.fail_unregister_callback = None

    async def register(self, event, callback, **kwargs):
        self.register_count += 1
        if self.register_count == self.fail_register_at:
            raise RuntimeError("register failed")
        return await super().register(event, callback, **kwargs)

    async def unregister(self, event, callback):
        self.unregister_count += 1
        if self.unregister_count == self.fail_unregister_at:
            raise RuntimeError("rollback unregister failed")
        if callback is self.fail_unregister_callback:
            self.fail_unregister_callback = None
            raise RuntimeError("unregister failed")
        return await super().unregister(event, callback)


@pytest.fixture
async def telemetry_env() -> AsyncIterator[SimpleNamespace]:
    shutdown_observability()
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "callback-test",
                "service.version": "1.2.3",
                "jiuwenclaw.claw.id": "claw-test",
            }
        )
    )
    registry = SpanRegistryProcessor(max_spans=64, ttl_seconds=60)
    provider.add_span_processor(registry)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    metrics = TelemetryMetrics(meter_provider)
    callbacks = RichTelemetryCallbacks(
        span_registry=registry,
        metrics=metrics,
        config=TelemetryConfig(enabled=True, claw_id="claw-config"),
    )
    init_observability(
        ObservabilityConfig(enabled=True, backend="otlp"),
        tracer_provider_override=provider,
        owns_provider=False,
    )
    await callbacks.register(Runner.callback_framework)
    parent = provider.get_tracer("test").start_span(
        "agent.worker.invoke",
        attributes={"gen_ai.request.model": "core-model"},
    )
    set_current_agent_span(parent)
    set_team_span(parent)
    context_token = otel_context.attach(trace.set_span_in_context(parent))
    try:
        yield SimpleNamespace(
            callbacks=callbacks,
            exporter=exporter,
            framework=Runner.callback_framework,
            meter_provider=meter_provider,
            parent=parent,
            provider=provider,
            reader=reader,
            registry=registry,
        )
    finally:
        otel_context.detach(context_token)
        set_current_agent_span(None)
        clear_team_span()
        await callbacks.unregister(Runner.callback_framework)
        shutdown_observability()
        if parent.is_recording():
            parent.end()
        provider.force_flush()
        meter_provider.shutdown()
        provider.shutdown()


    def record(self, name, value, attributes=None):
        del name, value, attributes


@pytest.fixture
async def unsampled_telemetry_env() -> AsyncIterator[SimpleNamespace]:
    shutdown_observability()
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_OFF)
    registry = SpanRegistryProcessor(max_spans=64, ttl_seconds=60)
    provider.add_span_processor(registry)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    callbacks = RichTelemetryCallbacks(
        span_registry=registry,
        metrics=TelemetryMetrics(meter_provider),
        config=TelemetryConfig(enabled=True),
    )
    init_observability(
        ObservabilityConfig(enabled=True, backend="otlp"),
        tracer_provider_override=provider,
        owns_provider=False,
    )
    await callbacks.register(Runner.callback_framework)
    parent = provider.get_tracer("test").start_span("agent.unsampled.invoke")
    assert not parent.is_recording()
    set_current_agent_span(parent)
    set_team_span(parent)
    context_token = otel_context.attach(trace.set_span_in_context(parent))
    try:
        yield SimpleNamespace(
            callbacks=callbacks,
            exporter=exporter,
            framework=Runner.callback_framework,
            meter_provider=meter_provider,
            parent=parent,
            provider=provider,
            reader=reader,
            registry=registry,
        )
    finally:
        otel_context.detach(context_token)
        set_current_agent_span(None)
        clear_team_span()
        await callbacks.unregister(Runner.callback_framework)
        shutdown_observability()
        parent.end()
        meter_provider.shutdown()
        provider.shutdown()


@pytest.fixture
def request_metric_channel() -> Iterator[None]:
    token = metrics_channel_id.set("web")
    try:
        yield
    finally:
        metrics_channel_id.reset(token)


def _decorate_like_real_model(
    framework: AsyncCallbackFramework,
    invoke,
    *,
    model_config: object,
    model_client_config: object,
):
    extra = {
        "model_config": model_config,
        "model_client_config": model_client_config,
    }
    wrapped = framework.emit_before(
        LLMCallEvents.LLM_INVOKE_INPUT,
        extra_kwargs=extra,
    )(invoke)
    wrapped = framework.transform_io(
        input_event=LLMCallEvents.LLM_INVOKE_INPUT,
        output_event=LLMCallEvents.LLM_INVOKE_OUTPUT,
    )(wrapped)
    return framework.emit_after(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        extra_kwargs=extra,
    )(wrapped)


def _decorate_like_real_agent_stream(
    framework: AsyncCallbackFramework,
    stream,
):
    wrapped = framework.emit_before(AgentEvents.AGENT_STREAM_INPUT)(stream)
    wrapped = framework.transform_io(
        input_event=AgentEvents.AGENT_STREAM_INPUT,
        output_event=AgentEvents.AGENT_STREAM_OUTPUT,
    )(wrapped)
    return framework.emit_after(
        AgentEvents.AGENT_STREAM_OUTPUT,
        item_key="result",
    )(wrapped)


@pytest.mark.asyncio
async def test_registers_complete_real_event_set_with_fixed_priorities() -> None:
    framework = AsyncCallbackFramework()
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=16, ttl_seconds=60),
        metrics=_Metrics(),
        config=TelemetryConfig(),
    )

    await callbacks.register(framework)

    input_events = {
        LLMCallEvents.LLM_INVOKE_INPUT,
        LLMCallEvents.LLM_STREAM_INPUT,
        ToolCallEvents.TOOL_CALL_STARTED,
        AgentEvents.AGENT_INVOKE_INPUT,
        AgentEvents.AGENT_STREAM_INPUT,
    }
    output_events = {
        LLMCallEvents.LLM_STREAM_OUTPUT,
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        LLMCallEvents.LLM_OUTPUT,
        LLMCallEvents.LLM_CALL_ERROR,
        ToolCallEvents.TOOL_CALL_FINISHED,
        ToolCallEvents.TOOL_CALL_ERROR,
        AgentEvents.AGENT_INVOKE_OUTPUT,
        AgentEvents.AGENT_STREAM_OUTPUT,
    }
    assert len(callbacks._registered) == len(input_events | output_events)
    for event in input_events:
        assert framework.list_callbacks(event) == [
            {
                "name": framework.list_callbacks(event)[0]["name"],
                "priority": INPUT_PRIORITY,
                "enabled": True,
                "namespace": NAMESPACE,
                "tags": [],
                "once": False,
                "max_retries": 0,
                "timeout": None,
            }
        ]
    for event in output_events:
        assert framework.list_callbacks(event)[0]["priority"] == OUTPUT_PRIORITY
        assert framework.list_callbacks(event)[0]["namespace"] == NAMESPACE


@pytest.mark.asyncio
async def test_real_llm_callbacks_enrich_the_single_core_span(
    telemetry_env: SimpleNamespace,
) -> None:
    result = SimpleNamespace(
        content="world",
        finish_reason="stop",
        tool_calls=[],
        usage_metadata=SimpleNamespace(
            input_tokens=7,
            output_tokens=3,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=4,
            reasoning_tokens=1,
        ),
    )

    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "hello"}],
        model="enrichment-model",
        temperature=0.2,
    )
    callback_results = await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=result,
    )
    telemetry_env.provider.force_flush()

    spans = [
        span
        for span in telemetry_env.exporter.get_finished_spans()
        if span.name == "llm.call"
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["gen_ai.request.model"] == "enrichment-model"
    assert span.attributes["gen_ai.input.messages.count"] == 1
    assert span.attributes["gen_ai.input.messages.total_length"] == 5
    assert span.attributes["gen_ai.decision.type"] == "answer"
    assert span.attributes["gen_ai.usage.input_tokens"] == 7
    assert span.attributes["gen_ai.usage.cache_creation.input_tokens"] == 4
    assert span.attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert span.attributes["gen_ai.response.finish_reason"] == "stop"
    assert any(event.name == "gen_ai.user.message" for event in span.events)
    assert callback_results[0] is result


@pytest.mark.asyncio
async def test_real_model_decorators_normalize_positional_input_and_llm_output_once(
    telemetry_env: SimpleNamespace,
) -> None:
    messages = [{"role": "user", "content": "positional prompt"}]
    tool_calls = [
        {
            "id": "call-weather",
            "function": {
                "name": "weather",
                "arguments": '{"city":"Paris"}',
            },
        }
    ]
    usage = SimpleNamespace(
        input_tokens=11,
        output_tokens=5,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=2,
        reasoning_tokens=1,
    )
    business_result = SimpleNamespace(
        content="parsed business result",
        reasoning_content="parsed reasoning",
        finish_reason="tool_calls",
        tool_calls=tool_calls,
        usage_metadata=usage,
    )
    state_counts_after_inner_output: list[tuple[int, int]] = []

    async def invoke(received_messages, **kwargs):
        assert received_messages is messages
        assert kwargs["tools"][0]["function"]["name"] == "weather"
        await telemetry_env.framework.trigger(
            LLMCallEvents.LLM_OUTPUT,
            response="observable answer",
            reasoning_content="observable reasoning",
            tool_calls=tool_calls,
            usage=usage,
            model_name="response-model",
            model_provider="OpenAI",
        )
        state_counts_after_inner_output.append(
            (
                telemetry_env.callbacks._span_state.active_count(),
                telemetry_env.callbacks._metric_state.active_count(),
            )
        )
        return business_result

    wrapped = _decorate_like_real_model(
        telemetry_env.framework,
        invoke,
        model_config=SimpleNamespace(
            model="configured-model",
            temperature=0.25,
            top_p=0.75,
        ),
        model_client_config=SimpleNamespace(client_provider="OpenAI"),
    )

    returned = await wrapped(
        messages,
        tools=[{"function": {"name": "weather"}}],
    )
    telemetry_env.provider.force_flush()

    assert returned is business_result
    # AgentCore now treats LLM_OUTPUT as the terminal event for both streaming
    # and non-streaming calls, so the JiuwenClaw state is settled before the
    # core callback closes its owned span.
    assert state_counts_after_inner_output == [(0, 0)]
    spans = [
        span
        for span in telemetry_env.exporter.get_finished_spans()
        if span.name == "llm.call"
    ]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["gen_ai.input.messages.count"] == 1
    assert "positional prompt" in attrs["gen_ai.input.messages"]
    assert attrs["gen_ai.request.model"] == "configured-model"
    assert attrs["gen_ai.request.temperature"] == 0.25
    assert attrs["gen_ai.request.top_p"] == 0.75
    assert attrs["gen_ai.response.model"] == "response-model"
    assert attrs["gen_ai.span.type"] == "model"
    assert attrs["gen_ai.decision.type"] == "tool_call"
    assert attrs["gen_ai.decision.tool_names"] == ("weather",)
    assert "observable answer" in attrs["gen_ai.output.messages"]
    assert "observable reasoning" in attrs["gen_ai.output.messages"]
    assert "call-weather" in attrs["gen_ai.output.messages"]
    assert 'city\\":\\"Paris' in attrs["gen_ai.output.messages"]
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 3
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert attrs["gen_ai.usage.cache_read_tokens"] == 3
    assert attrs["gen_ai.usage.cache_creation_tokens"] == 2
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    operation_counts = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.operation.count",
    )
    assert len(operation_counts) == 1
    assert operation_counts[0].value == 1
    assistant_events = [
        event for event in spans[0].events if event.name == "gen_ai.assistant.message"
    ]
    assert len(assistant_events) == 1
    assert "observable answer" in assistant_events[0].attributes["content"]


@pytest.mark.asyncio
async def test_real_tool_callbacks_enrich_the_single_core_span(
    telemetry_env: SimpleNamespace,
) -> None:
    result = {"answer": "sunny"}

    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="weather",
        tool_id="call-1",
        inputs=(("Paris",), {}),
    )
    callback_results = await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="weather",
        tool_id="call-1",
        inputs=(("Paris",), {}),
        result=result,
    )
    telemetry_env.provider.force_flush()

    spans = [
        span
        for span in telemetry_env.exporter.get_finished_spans()
        if span.name == "tool.weather"
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["gen_ai.tool.name"] == "weather"
    assert span.attributes["gen_ai.tool.call.id"] == "call-1"
    assert span.attributes["gen_ai.span.type"] == "tool"
    assert any(event.name == "tool.arguments" for event in span.events)
    assert any(event.name == "tool.result" for event in span.events)
    assert callback_results[0] == result


@pytest.mark.asyncio
async def test_streaming_tool_finish_without_result_does_not_invent_null_payload(
    telemetry_env: SimpleNamespace,
) -> None:
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="streaming_tool",
        tool_id="stream-card-id",
        inputs=(("input",), {}),
    )

    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="streaming_tool",
        tool_id="stream-card-id",
    )
    telemetry_env.provider.force_flush()

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "tool.streaming_tool"
    ][0]
    assert "gen_ai.tool.result" not in span.attributes
    assert not any(event.name == "tool.result" for event in span.events)
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_llm_metrics_state_and_streaming_ttft_are_finalized(
    telemetry_env: SimpleNamespace,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_STREAM_INPUT,
        messages=[{"role": "user", "content": "stream"}],
        model="stream-model",
        tools=[{"function": {"name": "weather", "description": "forecast"}}],
    )
    assert telemetry_env.callbacks._span_state.active_count() == 1
    assert telemetry_env.callbacks._metric_state.active_count() == 1

    from openjiuwen.agent_teams.observability.span_context import get_current_llm_span

    span = get_current_llm_span()
    assert span is not None
    span.otel_llm_state.start_ns = time.monotonic_ns() - 20_000_000
    chunk = SimpleNamespace(content="first", finish_reason=None)
    results = await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_STREAM_OUTPUT,
        result=chunk,
    )
    final_results = await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_OUTPUT,
        response="done",
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    telemetry_env.provider.force_flush()

    assert results[0] is chunk
    assert final_results[0] == "done"
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    finished = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ]
    assert len(finished) == 1
    assert finished[0].attributes["gen_ai.streaming.first_token"] is True
    ttft_points = _metric_points(
        telemetry_env.reader, "gen_ai.client.token.first_token_duration"
    )
    assert len(ttft_points) == 1
    assert 0.01 <= ttft_points[0].sum <= 1.0
    duration_points = _metric_points(
        telemetry_env.reader, "gen_ai.client.operation.duration"
    )
    count_points = _metric_points(telemetry_env.reader, "gen_ai.client.operation.count")
    assert len(duration_points) == 1
    assert len(count_points) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_field", ["client_provider", "provider"])
async def test_enterprise_metric_labels_llm_success_are_exact_and_provider_wins(
    telemetry_env: SimpleNamespace,
    provider_field: str,
) -> None:
    telemetry_env.parent.set_attribute("jiuwenclaw.channel.id", "web")
    telemetry_env.parent.set_attribute("jiuwenclaw.agent.name", "main-agent")
    telemetry_env.parent.set_attribute("gen_ai.provider.name", "span-provider")

    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "hello"}],
        model="mock-model",
        model_client_config={provider_field: "OpenAI"},
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=SimpleNamespace(
            content="ok",
            tool_calls=[],
            finish_reason="stop",
            usage_metadata=SimpleNamespace(input_tokens=4, output_tokens=2),
        ),
    )

    duration_points = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.operation.duration",
    )
    count_points = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.operation.count",
    )
    token_points = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.token.usage",
    )
    assert len(duration_points) == 1
    assert len(count_points) == 1
    assert len(token_points) == 2
    assert dict(duration_points[0].attributes) == {
        "gen_ai.request.model": "mock-model",
        "gen_ai.system": "openai",
        "jiuwenclaw.channel.id": "web",
    }
    assert dict(count_points[0].attributes) == {
        "gen_ai.request.model": "mock-model",
        "status": "success",
        "jiuwenclaw.channel.id": "web",
    }
    assert {
        point.attributes["gen_ai.token.type"]: dict(point.attributes)
        for point in token_points
    } == {
        "input": {
            "gen_ai.request.model": "mock-model",
            "gen_ai.system": "openai",
            "jiuwenclaw.channel.id": "web",
            "gen_ai.token.type": "input",
        },
        "output": {
            "gen_ai.request.model": "mock-model",
            "gen_ai.system": "openai",
            "jiuwenclaw.channel.id": "web",
            "gen_ai.token.type": "output",
        },
    }


@pytest.mark.asyncio
async def test_enterprise_metric_labels_llm_error_uses_current_core_span_provider(
    telemetry_env: SimpleNamespace,
) -> None:
    telemetry_env.parent.set_attribute("jiuwenclaw.channel.id", "cli")

    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "fail"}],
        model="error-model",
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_CALL_ERROR,
        error=RuntimeError("model failed"),
    )

    duration_points = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.operation.duration",
    )
    count_points = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.operation.count",
    )
    assert len(duration_points) == 1
    assert len(count_points) == 1
    assert dict(duration_points[0].attributes) == {
        "gen_ai.request.model": "error-model",
        "gen_ai.system": "openjiuwen-agent-teams",
        "jiuwenclaw.channel.id": "cli",
    }
    assert dict(count_points[0].attributes) == {
        "gen_ai.request.model": "error-model",
        "status": "error",
        "jiuwenclaw.channel.id": "cli",
    }


@pytest.mark.asyncio
async def test_enterprise_metric_labels_tool_error_are_exact_without_call_id(
    telemetry_env: SimpleNamespace,
) -> None:
    telemetry_env.parent.set_attribute("jiuwenclaw.channel.id", "web")

    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="weather",
        tool_id="call-1",
        inputs={},
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_ERROR,
        tool_name="weather",
        tool_id="call-1",
        error=RuntimeError("tool failed"),
    )
    telemetry_env.provider.force_flush()

    expected = {
        "gen_ai.tool.name": "weather",
        "jiuwenclaw.channel.id": "web",
    }
    for metric_name in (
        "gen_ai.tool.duration",
        "gen_ai.tool.call.count",
        "gen_ai.tool.error.count",
    ):
        points = _metric_points(telemetry_env.reader, metric_name)
        assert len(points) == 1
        assert dict(points[0].attributes) == expected

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "tool.weather"
    ][0]
    assert span.attributes["gen_ai.tool.call.id"] == "call-1"


@pytest.mark.asyncio
async def test_enterprise_metric_labels_filter_empty_values_and_provider_is_unknown() -> None:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=TelemetryMetrics(meter_provider),
        config=TelemetryConfig(enabled=True, service_name="must-not-be-provider"),
    )
    framework = AsyncCallbackFramework()
    await callbacks.register(framework)
    try:
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[],
            model="",
        )
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=SimpleNamespace(content="ok", tool_calls=[]),
        )
        await framework.trigger(AgentEvents.AGENT_INVOKE_INPUT, {})
        await framework.trigger(AgentEvents.AGENT_INVOKE_OUTPUT, result="done")
        await framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name="",
            tool_id="",
            inputs={},
        )
        await framework.trigger(
            ToolCallEvents.TOOL_CALL_FINISHED,
            tool_name="",
            tool_id="",
            result={"ok": True},
        )

        llm_duration = _metric_points(
            reader,
            "gen_ai.client.operation.duration",
        )
        llm_count = _metric_points(reader, "gen_ai.client.operation.count")
        agent_duration = _metric_points(reader, "jiuwenclaw.agent.duration")
        tool_duration = _metric_points(reader, "gen_ai.tool.duration")
        tool_count = _metric_points(reader, "gen_ai.tool.call.count")
        assert len(llm_duration) == 1
        assert len(llm_count) == 1
        assert len(agent_duration) == 1
        assert len(tool_duration) == 1
        assert len(tool_count) == 1
        assert dict(llm_duration[0].attributes) == {"gen_ai.system": "unknown"}
        assert dict(llm_count[0].attributes) == {"status": "success"}
        assert dict(agent_duration[0].attributes) == {}
        assert dict(tool_duration[0].attributes) == {}
        assert dict(tool_count[0].attributes) == {}
        assert callbacks._metric_state.active_count() == 0
        assert callbacks._span_state.active_count() == 0
    finally:
        await callbacks.unregister(framework)
        meter_provider.shutdown()


@pytest.mark.asyncio
async def test_message_policy_keeps_shape_without_content_and_redacts_separately(
    telemetry_env: SimpleNamespace,
) -> None:
    telemetry_env.callbacks._config = TelemetryConfig(
        enabled=True,
        log_messages=False,
        redact_prompts=True,
        redact_completions=False,
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "prompt-secret"}],
        model="policy-model",
    )
    result = SimpleNamespace(content="completion-visible", tool_calls=[])
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=result,
    )
    telemetry_env.provider.force_flush()

    finished = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    assert finished.attributes["gen_ai.input.messages.count"] == 1
    assert finished.attributes["gen_ai.input.messages.total_length"] == 13
    assert "gen_ai.input.messages" not in finished.attributes
    assert "gen_ai.output.messages" not in finished.attributes
    assert not any(
        event.name.startswith("gen_ai.user.message") for event in finished.events
    )
    assert not any(
        event.name == "gen_ai.assistant.message" for event in finished.events
    )


@pytest.mark.asyncio
async def test_tool_and_skill_metrics_use_call_identity_and_cleanup(
    telemetry_env: SimpleNamespace,
    request_metric_channel: None,
) -> None:
    del request_metric_channel
    inputs = {
        "skill_name": "forecast",
        "skill_id": "skill-1",
        "version": "v2",
        "session_id": "session-1",
    }
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_tool",
        tool_id="call-skill",
        inputs=inputs,
    )
    assert telemetry_env.callbacks._span_state.active_count() == 1
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="skill_tool",
        tool_id="call-skill",
        inputs=inputs,
        result={
            "success": True,
            "skill": {"name": "forecast", "id": "skill-1", "version": "v2"},
        },
    )
    telemetry_env.provider.force_flush()

    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "tool.skill_tool"
    ][0]
    assert span.attributes["gen_ai.skill.name"] == "forecast"
    assert span.attributes["gen_ai.skill.id"] == "skill-1"
    assert span.attributes["gen_ai.skill.version"] == "v2"
    assert span.attributes["gen_ai.span.type"] == "tool"
    assert span.attributes["gen_ai.operation.name"] == "load_skill"
    assert any(event.name == "skill.loaded" for event in span.events)
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1
    skill_calls = _metric_points(
        telemetry_env.reader,
        "gen_ai.skill.call.count",
    )
    assert len(skill_calls) == 1
    assert dict(skill_calls[0].attributes) == {
        "gen_ai.skill.name": "forecast",
        "gen_ai.skill.version": "v2",
        "gen_ai.system": "jiuwenclaw",
        "jiuwenclaw.channel.id": "web",
    }
    assert not _metric_points(telemetry_env.reader, "gen_ai.skill.duration")
    assert telemetry_env.callbacks._skill_sessions.active_count() == 1

    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_complete",
        tool_id="release-skill",
        inputs={"skill_name": "forecast", "session_id": "session-1"},
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="skill_complete",
        tool_id="release-skill",
        inputs={
            "skill_name": "forecast",
            "version": "ignored-completion-version",
            "session_id": "session-1",
        },
        result={"success": True},
    )
    telemetry_env.provider.force_flush()

    skill_durations = _metric_points(
        telemetry_env.reader,
        "gen_ai.skill.duration",
    )
    assert len(skill_durations) == 1
    assert dict(skill_durations[0].attributes) == dict(skill_calls[0].attributes)
    assert telemetry_env.callbacks._skill_sessions.active_count() == 0

    for tool_id in ("duplicate-start", "duplicate-finish"):
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED
            if tool_id.endswith("start")
            else ToolCallEvents.TOOL_CALL_FINISHED,
            tool_name="skill_complete",
            tool_id="duplicate",
            inputs={"skill_name": "forecast", "session_id": "session-1"},
            **({"result": {"success": True}} if tool_id.endswith("finish") else {}),
        )
    telemetry_env.provider.force_flush()
    assert len(_metric_points(telemetry_env.reader, "gen_ai.skill.duration")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "operation", "event_name"),
    [
        ("skill_tool", "load_skill", "skill.loaded"),
        ("skill_complete", "release_skill", "skill.released"),
    ],
)
async def test_real_tool_wrapper_enriches_skill_output_without_result_identity(
    telemetry_env: SimpleNamespace,
    tool_name: str,
    operation: str,
    event_name: str,
) -> None:
    tool = _RealSkillLifecycleTool(tool_name)
    result = await tool.invoke(
        {
            "skill_name": "forecast",
            "skill_id": "skill-forecast",
            "version": "v2",
            "relative_file_path": "forecast/SKILL.md",
        }
    )
    telemetry_env.provider.force_flush()

    assert isinstance(result, ToolOutput)
    assert result.success is True
    spans = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == f"tool.{tool_name}"
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["gen_ai.skill.name"] == "forecast"
    assert span.attributes["gen_ai.skill.id"] == "skill-forecast"
    assert span.attributes["gen_ai.skill.version"] == "v2"
    assert span.attributes["gen_ai.operation.name"] == operation
    assert [event.name for event in span.events].count(event_name) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.skill.call.count")) == (
        1 if tool_name == "skill_tool" else 0
    )
    assert not _metric_points(telemetry_env.reader, "gen_ai.skill.duration")
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_real_tool_wrapper_records_skill_error_metrics_from_started_inputs(
    telemetry_env: SimpleNamespace,
) -> None:
    tool = _RealSkillLifecycleTool(
        "skill_tool",
        error=RuntimeError("skill load failed"),
    )

    with pytest.raises(RuntimeError, match="skill load failed"):
        await tool.invoke(
            {
                "skill_name": "forecast",
                "skill_id": "skill-forecast",
                "version": "v2",
            }
        )

    assert len(_metric_points(telemetry_env.reader, "gen_ai.skill.call.count")) == 1
    assert not _metric_points(telemetry_env.reader, "gen_ai.skill.duration")
    assert len(_metric_points(telemetry_env.reader, "gen_ai.skill.error.count")) == 1
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_unsuccessful_tool_output_records_tool_and_skill_error_metrics(
    telemetry_env: SimpleNamespace,
) -> None:
    result = await _RealSkillLifecycleTool("skill_tool", success=False).invoke(
        {"skill_name": "forecast", "skill_id": "skill-forecast"}
    )

    assert result.success is False
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.error.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.skill.error.count")) == 1
    assert not _metric_points(telemetry_env.reader, "gen_ai.skill.duration")


@pytest.mark.asyncio
async def test_skill_completion_error_reports_error_and_cleans_activation(
    telemetry_env: SimpleNamespace,
) -> None:
    inputs = {"skill_name": "forecast", "version": "v2", "session_id": "s1"}
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_tool",
        tool_id="load",
        inputs=inputs,
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="skill_tool",
        tool_id="load",
        inputs=inputs,
        result={"success": True},
    )
    assert telemetry_env.callbacks._skill_sessions.active_count() == 1

    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_complete",
        tool_id="release",
        inputs=inputs,
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_ERROR,
        tool_name="skill_complete",
        tool_id="release",
        inputs=inputs,
        error=RuntimeError("release failed"),
    )
    telemetry_env.provider.force_flush()

    assert telemetry_env.callbacks._skill_sessions.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "gen_ai.skill.error.count")) == 1
    assert not _metric_points(telemetry_env.reader, "gen_ai.skill.duration")


@pytest.mark.asyncio
async def test_unsampled_llm_still_records_metrics_without_a_span() -> None:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=TelemetryMetrics(meter_provider),
        config=TelemetryConfig(enabled=True),
    )
    framework = AsyncCallbackFramework()
    await callbacks.register(framework)
    try:
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[{"role": "user", "content": "no-span"}],
            model="dropped-model",
        )
        result = SimpleNamespace(
            content="answer",
            tool_calls=[],
            usage_metadata=SimpleNamespace(input_tokens=4, output_tokens=2),
        )
        returned = await framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=result,
        )

        assert returned[0] is result
        assert callbacks._span_state.active_count() == 0
        assert callbacks._metric_state.active_count() == 0
        assert len(_metric_points(reader, "gen_ai.client.operation.count")) == 1
        assert len(_metric_points(reader, "gen_ai.client.operation.duration")) == 1
        assert len(_metric_points(reader, "gen_ai.client.token.usage")) == 2
    finally:
        await callbacks.unregister(framework)
        meter_provider.shutdown()


@pytest.mark.asyncio
async def test_unsampled_enterprise_metric_labels_match_sampled_contract(
    unsampled_telemetry_env: SimpleNamespace,
    request_metric_channel: None,
) -> None:
    del request_metric_channel
    await unsampled_telemetry_env.framework.trigger(
        AgentEvents.AGENT_INVOKE_INPUT,
        {
            "query": "unsampled",
            "agent_name": "unsampled-agent",
        },
    )
    await unsampled_telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "hello"}],
        model="unsampled-model",
        model_client_config={"client_provider": "OpenAI"},
    )
    await unsampled_telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=SimpleNamespace(
            content="ok",
            tool_calls=[],
            usage_metadata=SimpleNamespace(input_tokens=4, output_tokens=2),
        ),
    )
    await unsampled_telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="weather",
        tool_id="unsampled-call",
        inputs={},
    )
    await unsampled_telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_ERROR,
        tool_name="weather",
        tool_id="unsampled-call",
        error=RuntimeError("tool failed"),
    )
    await unsampled_telemetry_env.framework.trigger(
        AgentEvents.AGENT_INVOKE_OUTPUT,
        result="done",
    )

    agent_points = _metric_points(
        unsampled_telemetry_env.reader,
        "jiuwenclaw.agent.duration",
    )
    llm_duration_points = _metric_points(
        unsampled_telemetry_env.reader,
        "gen_ai.client.operation.duration",
    )
    llm_count_points = _metric_points(
        unsampled_telemetry_env.reader,
        "gen_ai.client.operation.count",
    )
    token_points = _metric_points(
        unsampled_telemetry_env.reader,
        "gen_ai.client.token.usage",
    )
    assert len(agent_points) == 1
    assert len(llm_duration_points) == 1
    assert len(llm_count_points) == 1
    assert len(token_points) == 2
    assert dict(agent_points[0].attributes) == {
        "jiuwenclaw.agent.name": "unsampled-agent",
        "jiuwenclaw.channel.id": "web",
    }
    assert dict(llm_duration_points[0].attributes) == {
        "gen_ai.request.model": "unsampled-model",
        "gen_ai.system": "openai",
        "jiuwenclaw.channel.id": "web",
    }
    assert dict(llm_count_points[0].attributes) == {
        "gen_ai.request.model": "unsampled-model",
        "status": "success",
        "jiuwenclaw.channel.id": "web",
    }
    assert {
        point.attributes["gen_ai.token.type"]: dict(point.attributes)
        for point in token_points
    } == {
        "input": {
            "gen_ai.request.model": "unsampled-model",
            "gen_ai.system": "openai",
            "jiuwenclaw.channel.id": "web",
            "gen_ai.token.type": "input",
        },
        "output": {
            "gen_ai.request.model": "unsampled-model",
            "gen_ai.system": "openai",
            "jiuwenclaw.channel.id": "web",
            "gen_ai.token.type": "output",
        },
    }

    expected_tool_attributes = {
        "gen_ai.tool.name": "weather",
        "jiuwenclaw.channel.id": "web",
    }
    for metric_name in (
        "gen_ai.tool.duration",
        "gen_ai.tool.call.count",
        "gen_ai.tool.error.count",
    ):
        points = _metric_points(unsampled_telemetry_env.reader, metric_name)
        assert len(points) == 1
        assert dict(points[0].attributes) == expected_tool_attributes
        assert "gen_ai.tool.call.id" not in points[0].attributes

    assert unsampled_telemetry_env.exporter.get_finished_spans() == ()
    assert unsampled_telemetry_env.callbacks._metric_state.active_count() == 0
    assert unsampled_telemetry_env.callbacks._span_state.active_count() == 0

    await unsampled_telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[],
        model="after-agent",
        model_client_config={"client_provider": "OpenAI"},
    )
    await unsampled_telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=SimpleNamespace(content="ok", tool_calls=[]),
    )
    after_agent = [
        point
        for point in _metric_points(
            unsampled_telemetry_env.reader,
            "gen_ai.client.operation.duration",
        )
        if point.attributes.get("gen_ai.request.model") == "after-agent"
    ]
    assert len(after_agent) == 1
    assert dict(after_agent[0].attributes) == {
        "gen_ai.request.model": "after-agent",
        "gen_ai.system": "openai",
        "jiuwenclaw.channel.id": "web",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_request_channel_survives_unpaired_agent_failure_for_later_metrics(
    error_type: type[BaseException],
) -> None:
    metrics = _RecordingMetrics()
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=metrics,
        config=TelemetryConfig(enabled=True),
    )
    channel_token = metrics_channel_id.set("request-channel")

    async def failing_agent() -> None:
        await callbacks._on_agent_input(
            {"agent_name": "inner-agent"},
        )
        raise error_type("business failure")

    try:
        with pytest.raises(error_type):
            await failing_agent()
        await callbacks._on_llm_invoke_input(
            messages=[],
            model="outer-model",
            model_client_config={"provider": "OpenAI"},
        )
        await callbacks._on_llm_invoke_output(
            result=SimpleNamespace(content="ok", tool_calls=[]),
        )
        await callbacks._on_tool_input(
            tool_name="outer-tool",
            tool_id="outer-call",
            inputs={},
        )
        await callbacks._on_tool_error(
            tool_name="outer-tool",
            tool_id="outer-call",
            error=RuntimeError("tool failed"),
        )
    finally:
        metrics_channel_id.reset(channel_token)

    assert [
        call[3]
        for call in metrics.calls
        if call[1] == "gen_ai.client.operation.duration"
    ] == [
        {
            "gen_ai.request.model": "outer-model",
            "gen_ai.system": "openai",
            "jiuwenclaw.channel.id": "request-channel",
        }
    ]
    assert [
        call[3]
        for call in metrics.calls
        if call[1] == "gen_ai.tool.error.count"
    ] == [
        {
            "gen_ai.tool.name": "outer-tool",
            "jiuwenclaw.channel.id": "request-channel",
        }
    ]
    assert not hasattr(callbacks, "_agent_label_states")


@pytest.mark.asyncio
async def test_agent_and_common_attributes_and_parent_token_totals(
    telemetry_env: SimpleNamespace,
) -> None:
    identity_token = IdentityStore.set_identity(
        IdentityInfo(user_id="user-1", domain_id="domain-1", app_id="app-1")
    )
    session = SimpleNamespace(get_session_id=lambda: "session-1")
    telemetry_env.parent.set_attribute("agentteam.member.name", "main-agent")
    try:
        await telemetry_env.framework.trigger(
            AgentEvents.AGENT_INVOKE_INPUT,
            {
                "query": "what is the weather",
                "conversation_id": "conversation-1",
                "mode": "react",
                "request_id": "request-1",
                "channel_id": "web",
                "parent_session_id": "parent-1",
                "iteration": 3,
            },
            session=session,
        )
        await telemetry_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[{"role": "user", "content": "weather"}],
            model="model",
        )
        await telemetry_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=SimpleNamespace(
                content="sunny",
                tool_calls=[],
                usage_metadata=SimpleNamespace(input_tokens=9, output_tokens=4),
            ),
        )
        agent_result = object()
        returned = await telemetry_env.framework.trigger(
            AgentEvents.AGENT_INVOKE_OUTPUT,
            result=agent_result,
        )
    finally:
        IdentityStore.clear(identity_token)

    assert returned[0] is agent_result
    attrs = telemetry_env.parent.attributes
    assert attrs["gen_ai.conversation.id"] == "conversation-1"
    assert attrs["jiuwenclaw.session.id"] == "session-1"
    assert attrs["jiuwenclaw.request.id"] == "request-1"
    assert attrs["jiuwenclaw.channel.id"] == "web"
    assert attrs["jiuwenclaw.agent.parent"] == "parent-1"
    assert attrs["jiuwenclaw.iteration"] == 3
    assert attrs["jiuwenclaw.agent.mode"] == "react"
    assert attrs["gen_ai.agent.name"] == "main-agent"
    assert attrs["jiuwenclaw.agent.name"] == "main-agent"
    assert attrs["gen_ai.span.type"] == "agent"
    assert attrs["user.id"] == "user-1"
    assert attrs["domain.id"] == "domain-1"
    assert attrs["app.id"] == "app-1"
    assert attrs["jiuwenclaw.user.id"] == "user-1"
    assert attrs["jiuwenclaw.claw.id"] == "claw-config"
    assert attrs["service.version"] == "1.2.3"
    assert attrs["gen_ai.usage.input_tokens"] == 9
    assert attrs["gen_ai.usage.output_tokens"] == 4
    duration_points = _metric_points(
        telemetry_env.reader,
        "jiuwenclaw.agent.duration",
    )
    assert len(duration_points) == 1
    assert dict(duration_points[0].attributes) == {
        "jiuwenclaw.agent.name": "main-agent",
        "jiuwenclaw.channel.id": "web",
        "user_id": "user-1",
        "domain_id": "domain-1",
        "app_id": "app-1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redact_prompts", "redact_completions", "input_expected", "output_expected"),
    [
        (True, False, "[REDACTED]", "completion-secret"),
        (False, True, "prompt-secret", "[REDACTED]"),
    ],
)
async def test_prompt_and_completion_redaction_are_independent(
    telemetry_env: SimpleNamespace,
    redact_prompts: bool,
    redact_completions: bool,
    input_expected: str,
    output_expected: str,
) -> None:
    telemetry_env.callbacks._config = TelemetryConfig(
        enabled=True,
        log_messages=True,
        redact_prompts=redact_prompts,
        redact_completions=redact_completions,
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "prompt-secret"}],
        model="redaction-model",
        tools=[
            {
                "function": {
                    "name": "weather",
                    "description": "tool-secret",
                }
            }
        ],
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=SimpleNamespace(content="completion-secret", tool_calls=[]),
    )
    telemetry_env.provider.force_flush()

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    assert input_expected in span.attributes["gen_ai.input.messages"]
    assert output_expected in span.attributes["gen_ai.output.messages"]
    if redact_prompts:
        assert "prompt-secret" not in span.attributes["gen_ai.input.messages"]
    if redact_completions:
        assert "completion-secret" not in span.attributes["gen_ai.output.messages"]
    assert "gen_ai.tool.definitions" in span.attributes
    assistant_events = [
        event for event in span.events if event.name == "gen_ai.assistant.message"
    ]
    assert len(assistant_events) == 1
    assert output_expected in assistant_events[0].attributes["content"]


@pytest.mark.asyncio
async def test_completion_redaction_hides_tool_call_arguments(
    telemetry_env: SimpleNamespace,
) -> None:
    telemetry_env.callbacks._config = TelemetryConfig(
        enabled=True,
        log_messages=True,
        redact_completions=True,
        attribute_value_max_length=256,
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_STREAM_INPUT,
        [{"role": "user", "content": "call a tool"}],
        model="redaction-model",
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_OUTPUT,
        is_stream=True,
        response="completion-secret",
        reasoning_content="reasoning-secret",
        tool_calls=[
            {
                "id": "call-secret",
                "function": {
                    "name": "private_tool",
                    "arguments": '{"password":"super-secret"}',
                },
            }
        ],
    )
    telemetry_env.provider.force_flush()

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    output = span.attributes["gen_ai.output.messages"]
    assert "completion-secret" not in output
    assert "reasoning-secret" not in output
    assert "super-secret" not in output
    assert "private_tool" in output
    assert "[REDACTED]" in output
    assistant_events = [
        event for event in span.events if event.name == "gen_ai.assistant.message"
    ]
    assert len(assistant_events) == 1
    event_content = assistant_events[0].attributes["content"]
    assert event_content == output
    assert len(event_content) <= 256
    assert "completion-secret" not in event_content
    assert "reasoning-secret" not in event_content
    assert "super-secret" not in event_content


@pytest.mark.asyncio
async def test_log_messages_false_suppresses_tool_payload_attributes_and_events(
    telemetry_env: SimpleNamespace,
) -> None:
    telemetry_env.callbacks._config = TelemetryConfig(
        enabled=True,
        log_messages=False,
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="private_tool",
        tool_id="private-call",
        inputs={"password": "prompt-secret"},
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="private_tool",
        tool_id="private-call",
        result={"token": "completion-secret"},
    )
    telemetry_env.provider.force_flush()

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "tool.private_tool"
    ][0]
    assert span.attributes["gen_ai.tool.name"] == "private_tool"
    assert span.attributes["gen_ai.span.type"] == "tool"
    assert "gen_ai.tool.arguments" not in span.attributes
    assert "gen_ai.tool.result" not in span.attributes
    assert not any(
        event.name in {"tool.arguments", "tool.result"} for event in span.events
    )
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1


@pytest.mark.asyncio
async def test_llm_error_records_metrics_and_cleans_both_state_registries(
    telemetry_env: SimpleNamespace,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "fail"}],
        model="error-model",
    )
    error = RuntimeError("model failed")
    await telemetry_env.framework.trigger(LLMCallEvents.LLM_CALL_ERROR, error=error)
    telemetry_env.provider.force_flush()

    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    assert span.attributes["error.type"] == "RuntimeError"
    count_points = _metric_points(telemetry_env.reader, "gen_ai.client.operation.count")
    assert len(count_points) == 1
    assert count_points[0].value == 1
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.duration"))
        == 1
    )


@pytest.mark.asyncio
async def test_llm_cancellation_marks_the_core_span_and_cleans_state(
    telemetry_env: SimpleNamespace,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[],
        model="cancel-model",
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_CALL_ERROR,
        error=asyncio.CancelledError(),
    )
    telemetry_env.provider.force_flush()

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    assert span.attributes["error.type"] == "CancelledError"
    assert span.attributes["jiuwenclaw.canceled"] is True
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_common_enrichment_does_not_overwrite_existing_core_attributes(
    telemetry_env: SimpleNamespace,
) -> None:
    telemetry_env.parent.set_attribute("user.id", "core-user")
    telemetry_env.parent.set_attribute("jiuwenclaw.user.id", "stale-alias")
    token = IdentityStore.set_identity(
        IdentityInfo(user_id="enriched-user", domain_id="domain", app_id="app")
    )
    try:
        await telemetry_env.framework.trigger(
            AgentEvents.AGENT_INVOKE_INPUT,
            {"query": "hello"},
        )
        await telemetry_env.framework.trigger(
            AgentEvents.AGENT_INVOKE_OUTPUT,
            result="done",
        )
    finally:
        IdentityStore.clear(token)

    assert telemetry_env.parent.attributes["user.id"] == "core-user"
    assert telemetry_env.parent.attributes["jiuwenclaw.user.id"] == "core-user"


@pytest.mark.asyncio
async def test_skill_release_sets_enterprise_operation_aliases(
    telemetry_env: SimpleNamespace,
) -> None:
    inputs = {"skill_name": "forecast", "skill_id": "skill-1"}
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_complete",
        tool_id="release-call",
        inputs=inputs,
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="skill_complete",
        tool_id="release-call",
        inputs=inputs,
        result={"success": True},
    )
    telemetry_env.provider.force_flush()

    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "tool.skill_complete"
    ][0]
    assert span.attributes["gen_ai.span.type"] == "tool"
    assert span.attributes["gen_ai.operation.name"] == "release_skill"
    assert span.attributes["gen_ai.skill.name"] == "forecast"
    assert any(event.name == "skill.released" for event in span.events)
    assert not _metric_points(telemetry_env.reader, "gen_ai.skill.duration")


@pytest.mark.asyncio
async def test_real_agent_stream_only_finalizes_on_last_chunk(
    telemetry_env: SimpleNamespace,
) -> None:
    first = SimpleNamespace(index=0, last_chunk=False)
    final = SimpleNamespace(index=1, last_chunk=True)

    async def stream(inputs):
        assert inputs["query"] == "stream query"
        yield first
        yield final

    wrapped = _decorate_like_real_agent_stream(
        telemetry_env.framework,
        stream,
    )
    iterator = wrapped({"query": "stream query"})

    assert await anext(iterator) is first
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    assert telemetry_env.callbacks._span_state.active_count() == 1
    assert not _metric_points(telemetry_env.reader, "jiuwenclaw.agent.duration")

    assert await anext(iterator) is final
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "jiuwenclaw.agent.duration")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["serializer", "event"])
async def test_llm_input_field_failure_keeps_state_until_normal_output(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    if failure_point == "serializer":

        def fail_serializer(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("input serialization failed")

        monkeypatch.setattr(
            callbacks_module,
            "serialize_input_messages",
            fail_serializer,
        )
    else:
        span_type = type(telemetry_env.parent)
        original_add_event = span_type.add_event

        def fail_input_event(self, name, *args, **kwargs):
            if name == "gen_ai.user.message":
                raise RuntimeError("input event failed")
            return original_add_event(self, name, *args, **kwargs)

        monkeypatch.setattr(span_type, "add_event", fail_input_event)

    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "hello"}],
        model="resilient-model",
    )

    assert warnings == ["LLM telemetry input enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    assert telemetry_env.callbacks._span_state.active_count() == 1

    result = SimpleNamespace(
        content="world",
        finish_reason="stop",
        tool_calls=[],
        usage_metadata=SimpleNamespace(
            input_tokens=7,
            output_tokens=3,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=4,
            reasoning_tokens=1,
        ),
    )
    returned = await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=result,
    )
    telemetry_env.provider.force_flush()

    assert returned[0] is result
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    assert span.attributes["gen_ai.response.finish_reason"] == "stop"
    assert span.attributes["gen_ai.usage.input_tokens"] == 7
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 2
    assert span.attributes["gen_ai.usage.cache_creation.input_tokens"] == 4
    assert span.attributes["gen_ai.usage.reasoning.output_tokens"] == 1
    assert telemetry_env.parent.attributes["gen_ai.usage.input_tokens"] == 7
    assert telemetry_env.parent.attributes["gen_ai.usage.output_tokens"] == 3
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.count")) == 1
    )
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.duration"))
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["arguments", "skill", "attribute"],
)
async def test_tool_input_field_failure_keeps_state_until_normal_output(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    restore = None
    if failure_point == "arguments":
        original = telemetry_env.callbacks._bounded_json

        def fail_arguments(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("tool argument serialization failed")

        monkeypatch.setattr(telemetry_env.callbacks, "_bounded_json", fail_arguments)

        def restore():
            monkeypatch.setattr(
                telemetry_env.callbacks,
                "_bounded_json",
                original,
            )

    elif failure_point == "skill":
        original = callbacks_module.extract_skill

        def fail_skill(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("skill extraction failed")

        monkeypatch.setattr(callbacks_module, "extract_skill", fail_skill)

        def restore():
            monkeypatch.setattr(
                callbacks_module,
                "extract_skill",
                original,
            )

    else:
        original = telemetry_env.callbacks._set_if_empty

        def fail_attribute(span, key, value):
            if key == "gen_ai.tool.call.id":
                raise RuntimeError("tool attribute failed")
            return original(span, key, value)

        monkeypatch.setattr(
            telemetry_env.callbacks,
            "_set_if_empty",
            fail_attribute,
        )

        def restore():
            monkeypatch.setattr(
                telemetry_env.callbacks,
                "_set_if_empty",
                original,
            )

    inputs = {"skill_name": "forecast", "skill_id": "skill-1"}
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_tool",
        tool_id="resilient-tool-call",
        inputs=inputs,
    )

    assert warnings == ["Tool telemetry input enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    assert telemetry_env.callbacks._span_state.active_count() == 1
    assert restore is not None
    restore()

    result = {"success": True}
    returned = await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="skill_tool",
        tool_id="resilient-tool-call",
        inputs=inputs,
        result=result,
    )

    assert returned[0] is result
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1


@pytest.mark.asyncio
async def test_agent_input_field_failure_keeps_state_until_normal_output(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []

    def fail_agent_name(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("agent name enrichment failed")

    monkeypatch.setattr(
        telemetry_env.callbacks,
        "_write_agent_name",
        fail_agent_name,
    )
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )

    await telemetry_env.framework.trigger(
        AgentEvents.AGENT_INVOKE_INPUT,
        {"query": "hello", "conversation_id": "conversation-1"},
    )

    assert warnings == ["Agent telemetry input enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    assert telemetry_env.callbacks._span_state.active_count() == 1

    result = object()
    returned = await telemetry_env.framework.trigger(
        AgentEvents.AGENT_INVOKE_OUTPUT,
        result=result,
    )

    assert returned[0] is result
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "jiuwenclaw.agent.duration")) == 1


@pytest.mark.asyncio
async def test_nested_agent_callbacks_finish_each_call_in_lifo_order(
    telemetry_env: SimpleNamespace,
) -> None:
    await telemetry_env.callbacks._on_agent_input(
        {"query": "outer", "conversation_id": "outer-conversation"}
    )
    await telemetry_env.callbacks._on_agent_input(
        {"query": "inner", "conversation_id": "inner-conversation"}
    )

    assert telemetry_env.callbacks._metric_state.active_count() == 2

    inner_result = object()
    assert (
        await telemetry_env.callbacks._on_agent_invoke_output(result=inner_result)
        is inner_result
    )
    assert telemetry_env.callbacks._metric_state.active_count() == 1

    outer_result = object()
    assert (
        await telemetry_env.callbacks._on_agent_invoke_output(result=outer_result)
        is outer_result
    )
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    duration_points = _metric_points(
        telemetry_env.reader,
        "jiuwenclaw.agent.duration",
    )
    assert sum(point.count for point in duration_points) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["serializer", "assistant_event"])
async def test_llm_output_field_failure_preserves_remaining_enrichment_and_metrics(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "hello"}],
        model="resilient-output-model",
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    if failure_point == "serializer":

        def fail_serializer(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("output serialization failed")

        monkeypatch.setattr(
            callbacks_module,
            "serialize_output_message",
            fail_serializer,
        )
    else:
        span_type = type(telemetry_env.parent)
        original_add_event = span_type.add_event

        def fail_assistant_event(self, name, *args, **kwargs):
            if name == "gen_ai.assistant.message":
                raise RuntimeError("assistant event failed")
            return original_add_event(self, name, *args, **kwargs)

        monkeypatch.setattr(span_type, "add_event", fail_assistant_event)

    result = SimpleNamespace(
        content="world",
        finish_reason="stop",
        tool_calls=[],
        usage_metadata=SimpleNamespace(
            input_tokens=11,
            output_tokens=5,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
            reasoning_tokens=1,
        ),
    )
    returned = await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=result,
    )
    telemetry_env.provider.force_flush()

    assert returned[0] is result
    assert warnings == ["LLM telemetry output enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    span = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "llm.call"
    ][0]
    assert span.attributes["gen_ai.response.finish_reason"] == "stop"
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["gen_ai.usage.output_tokens"] == 5
    assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert span.attributes["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert span.attributes["gen_ai.usage.reasoning.output_tokens"] == 1
    assert telemetry_env.parent.attributes["gen_ai.usage.input_tokens"] == 11
    assert telemetry_env.parent.attributes["gen_ai.usage.output_tokens"] == 5
    output_messages = span.attributes.get("gen_ai.output.messages")
    assistant_events = [
        event for event in span.events if event.name == "gen_ai.assistant.message"
    ]
    if failure_point == "serializer":
        assert output_messages is None
    else:
        assert "world" in output_messages
    assert assistant_events == []
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.count")) == 1
    )
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.duration"))
        == 1
    )
    token_points = {
        point.attributes["gen_ai.token.type"]: point.value
        for point in _metric_points(
            telemetry_env.reader,
            "gen_ai.client.token.usage",
        )
    }
    assert token_points == {
        "input": 11,
        "output": 5,
        "cache_read": 3,
        "cache_creation": 2,
        "reasoning": 1,
    }


@pytest.mark.asyncio
async def test_sequential_llm_usage_overrides_inherited_parent_totals(
    telemetry_env: SimpleNamespace,
) -> None:
    for index, (input_tokens, output_tokens) in enumerate(((7, 2), (11, 4))):
        call_id = f"sequential-{index}"
        model = f"sequential-model-{index}"
        await telemetry_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[{"role": "user", "content": f"prompt-{index}"}],
            model=model,
            call_id=call_id,
        )
        await telemetry_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=SimpleNamespace(
                content=f"result-{index}",
                finish_reason="stop",
                tool_calls=[],
                usage_metadata=SimpleNamespace(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            ),
            call_id=call_id,
        )

    telemetry_env.provider.force_flush()
    spans_by_model = {
        span.attributes["gen_ai.request.model"]: span
        for span in telemetry_env.exporter.get_finished_spans()
        if span.name == "llm.call"
    }
    second_span = spans_by_model["sequential-model-1"]
    assert second_span.attributes["gen_ai.usage.input_tokens"] == 11
    assert second_span.attributes["gen_ai.usage.output_tokens"] == 4
    assert second_span.attributes["gen_ai.usage.total_tokens"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", ["tool", "agent"])
@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
async def test_tool_and_agent_input_failures_preserve_or_cleanup_state(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
    error_type: type[BaseException],
) -> None:
    error = error_type("enrichment failed")
    warnings: list[str] = []

    def fail_common(span):
        del span
        raise error

    monkeypatch.setattr(
        telemetry_env.callbacks,
        "_write_common_attributes",
        fail_common,
    )
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    if callback_kind == "tool":
        monkeypatch.setattr(
            telemetry_env.callbacks,
            "_find_tool_span",
            lambda kwargs: telemetry_env.parent,
        )
        call = telemetry_env.callbacks._on_tool_input(
            tool_name="failing_tool",
            tool_id="failing-call",
            inputs={"value": 1},
        )
    else:
        call = telemetry_env.callbacks._on_agent_input({"query": "hello"})

    if issubclass(error_type, Exception):
        await call
        assert warnings == [
            f"{callback_kind.title()} telemetry input enrichment failed"
        ]
        assert telemetry_env.callbacks._metric_state.active_count() == 1
        assert telemetry_env.callbacks._span_state.active_count() == 1
        if callback_kind == "tool":
            result = object()
            returned = await telemetry_env.callbacks._on_tool_output(
                tool_name="failing_tool",
                tool_id="failing-call",
                inputs={"value": 1},
                result=result,
            )
            assert returned is result
            assert (
                len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
            )
            assert (
                len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1
            )
        else:
            result = object()
            returned = await telemetry_env.callbacks._on_agent_invoke_output(
                result=result,
            )
            assert returned is result
            assert (
                len(_metric_points(telemetry_env.reader, "jiuwenclaw.agent.duration"))
                == 1
            )
    else:
        with pytest.raises(error_type):
            await call
        assert warnings == []

    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_tool_input_failure_before_state_creation_degrades_cleanly(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []

    class BrokenToolName:
        def __str__(self) -> str:
            raise RuntimeError("broken tool name")

    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )

    await telemetry_env.callbacks._on_tool_input(
        tool_name=BrokenToolName(),
        tool_id="broken-call",
        inputs={},
    )

    assert warnings == ["Tool telemetry input enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_agent_stream_control_failure_cleans_state_before_propagating(
    telemetry_env: SimpleNamespace,
) -> None:
    class BrokenFinalChunk:
        @property
        def last_chunk(self) -> bool:
            raise KeyboardInterrupt("control")

    await telemetry_env.callbacks._on_agent_input({"query": "stream"})

    with pytest.raises(KeyboardInterrupt):
        await telemetry_env.callbacks._on_agent_stream_output(
            result=BrokenFinalChunk(),
        )

    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
async def test_llm_first_chunk_ordinary_failure_degrades_and_preserves_chunk(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_STREAM_INPUT,
        [{"role": "user", "content": "stream"}],
        model="fragile-stream-model",
    )
    original_span_key = telemetry_env.callbacks._span_key

    def fail_span_key(span):
        del span
        raise RuntimeError("first chunk enrichment failed")

    warnings: list[str] = []
    monkeypatch.setattr(telemetry_env.callbacks, "_span_key", fail_span_key)
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    chunk = SimpleNamespace(content="first")

    returned = await telemetry_env.callbacks._on_llm_stream_output(result=chunk)

    assert returned is chunk
    assert warnings == ["LLM telemetry stream output enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    assert telemetry_env.callbacks._span_state.active_count() == 1
    monkeypatch.setattr(telemetry_env.callbacks, "_span_key", original_span_key)
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_OUTPUT,
        is_stream=True,
        response="done",
        usage=SimpleNamespace(input_tokens=2, output_tokens=1),
    )
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
)
async def test_llm_first_chunk_control_failure_cleans_state_before_propagating(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    control_error: BaseException,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_STREAM_INPUT,
        [{"role": "user", "content": "stream"}],
        model="control-stream-model",
    )
    original_span_key = telemetry_env.callbacks._span_key

    def fail_span_key(span):
        del span
        raise control_error

    monkeypatch.setattr(telemetry_env.callbacks, "_span_key", fail_span_key)
    chunk = SimpleNamespace(content="first")

    with pytest.raises(type(control_error)):
        await telemetry_env.callbacks._on_llm_stream_output(result=chunk)

    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    monkeypatch.setattr(telemetry_env.callbacks, "_span_key", original_span_key)
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_OUTPUT,
        is_stream=True,
        response="done",
    )


@pytest.mark.asyncio
async def test_tool_output_enrichment_failure_preserves_business_result_and_cleans_state(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="fragile_tool",
        tool_id="fragile-call",
        inputs={"value": 1},
    )
    result = {"business": "value"}
    warnings: list[str] = []
    original_bounded_json = telemetry_env.callbacks._bounded_json

    def fail_serialization(value, *, redact):
        del value, redact
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(telemetry_env.callbacks, "_bounded_json", fail_serialization)
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    returned = await telemetry_env.callbacks._on_tool_output(
        tool_name="fragile_tool",
        tool_id="fragile-call",
        result=result,
    )

    assert returned is result
    assert warnings == ["Tool telemetry output enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1
    monkeypatch.setattr(
        telemetry_env.callbacks,
        "_bounded_json",
        original_bounded_json,
    )
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="fragile_tool",
        tool_id="fragile-call",
        result=result,
    )
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1


@pytest.mark.asyncio
async def test_tool_skill_failure_preserves_result_and_settles_basic_metrics_once(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = {"skill_name": "fragile-skill", "skill_id": "skill-1"}
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_STARTED,
        tool_name="skill_tool",
        tool_id="fragile-skill-call",
        inputs=inputs,
    )
    original_extract_skill = callbacks_module.extract_skill

    def fail_skill(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("skill extraction failed")

    warnings: list[str] = []
    monkeypatch.setattr(callbacks_module, "extract_skill", fail_skill)
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    result = {"success": True}

    returned = await telemetry_env.callbacks._on_tool_output(
        tool_name="skill_tool",
        tool_id="fragile-skill-call",
        inputs=inputs,
        result=result,
    )

    assert returned is result
    assert warnings == ["Tool telemetry output enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1
    monkeypatch.setattr(callbacks_module, "extract_skill", original_extract_skill)
    await telemetry_env.framework.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="skill_tool",
        tool_id="fragile-skill-call",
        inputs=inputs,
        result=result,
    )
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 1
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.duration")) == 1


@pytest.mark.asyncio
async def test_llm_output_enrichment_failure_preserves_business_result_and_cleans_state(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        [{"role": "user", "content": "hello"}],
        model="fragile-model",
    )
    warnings: list[str] = []
    original_serializer = callbacks_module.serialize_output_message

    def fail_serialization(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(
        callbacks_module, "serialize_output_message", fail_serialization
    )
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    result = SimpleNamespace(
        content="business result",
        tool_calls=[],
        usage_metadata=SimpleNamespace(input_tokens=7, output_tokens=3),
    )

    returned = await telemetry_env.callbacks._on_llm_invoke_output(result=result)

    assert returned is result
    assert warnings == ["LLM telemetry output enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.count")) == 1
    )
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.duration"))
        == 1
    )
    token_points = {
        point.attributes["gen_ai.token.type"]: point.value
        for point in _metric_points(
            telemetry_env.reader,
            "gen_ai.client.token.usage",
        )
    }
    assert token_points == {"input": 7, "output": 3}
    monkeypatch.setattr(
        callbacks_module,
        "serialize_output_message",
        original_serializer,
    )
    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=result,
    )
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.count")) == 1
    )


@pytest.mark.asyncio
async def test_agent_output_enrichment_failure_preserves_business_result_and_cleans_state(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await telemetry_env.callbacks._on_agent_input({"query": "hello"})
    warnings: list[str] = []

    def fail_find(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("span lookup failed")

    monkeypatch.setattr(telemetry_env.registry, "find", fail_find)
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )
    result = object()

    returned = await telemetry_env.callbacks._on_agent_invoke_output(result=result)

    assert returned is result
    assert warnings == ["Agent telemetry output enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "jiuwenclaw.agent.duration")) == 1


@pytest.mark.asyncio
async def test_tool_error_and_empty_ids_cleanup_without_cross_call_reuse(
    telemetry_env: SimpleNamespace,
) -> None:
    for tool_name in ("first", "second"):
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name=tool_name,
            tool_id="",
            inputs={"name": tool_name},
        )
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_ERROR,
            tool_name=tool_name,
            tool_id="",
            error=TimeoutError(tool_name),
        )

    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.call.count")) == 2
    assert len(_metric_points(telemetry_env.reader, "gen_ai.tool.error.count")) == 2
    telemetry_env.provider.force_flush()
    spans = [
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name in {"tool.first", "tool.second"}
    ]
    assert {item.attributes["error.type"] for item in spans} == {"TimeoutError"}
    assert all(item.attributes["jiuwenclaw.timeout"] is True for item in spans)


@pytest.mark.asyncio
async def test_tool_error_enrichment_uses_agentcore_context_without_otel_attachment(
    telemetry_env: SimpleNamespace,
) -> None:
    invalid_context_token = otel_context.attach(
        trace.set_span_in_context(trace.INVALID_SPAN)
    )
    try:
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name="detached",
            tool_id="detached-call",
            inputs={"value": 1},
        )
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_ERROR,
            tool_name="detached",
            tool_id="detached-call",
            error=TimeoutError("detached"),
        )
    finally:
        otel_context.detach(invalid_context_token)

    telemetry_env.provider.force_flush()
    span = next(
        item
        for item in telemetry_env.exporter.get_finished_spans()
        if item.name == "tool.detached"
    )
    assert span.attributes["error.type"] == "TimeoutError"
    assert span.attributes["jiuwenclaw.timeout"] is True
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.callbacks._metric_state.active_count() == 0


@pytest.mark.asyncio
async def test_token_count_timeout_only_drops_context_breakdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_count(messages, tools):
        del messages, tools
        time.sleep(0.2)
        raise AssertionError("cancelled work must not affect callback")

    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.callbacks.count_context_tokens",
        slow_count,
    )
    metrics = _RecordingMetrics()
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=metrics,
        config=SimpleNamespace(token_count_timeout_seconds=0.01),
    )
    framework = AsyncCallbackFramework()
    await callbacks.register(framework)
    started_at = time.monotonic()
    try:
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[{"role": "user", "content": "quick"}],
            model="timeout-model",
        )
        result = object()
        returned = await framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT, result=result
        )
    finally:
        await callbacks.unregister(framework)

    assert time.monotonic() - started_at < 0.15
    assert returned[0] is result
    assert callbacks._metric_state.active_count() == 0
    assert {call[1] for call in metrics.calls} >= {
        "gen_ai.client.operation.count",
        "gen_ai.client.operation.duration",
    }


@pytest.mark.asyncio
async def test_serialize_must_not_block_event_loop(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bug-207 regression: ``_enrich_llm_input`` 的 serialize 必须不跑在
    事件循环线程上。

    现象：8/28 10:40 用户发送 query 后智能体长时间无响应，前端报
    "智能体连接不稳定"。根因：对 ~10 万 token 上下文，
    ``serialize_input_messages`` / ``serialize_tool_definitions`` 同步跑在
    事件循环线程上，数十秒无法响应 WebSocket ping，最终连接被判定
    ``close_code=1006`` 超时关闭。

    判定方式：monkeypatch ``serialize_input_messages`` 为"记录当前线程并阻塞"
    的桩，检查 serialize 实际跑在哪个线程：

    * 未修复（同步 serialize）：桩跑在事件循环线程 →
      ``serialize_on_loop=True`` → FAIL，错误信息点明 bug。
    * 已修复（``asyncio.to_thread``）：桩跑在 worker thread →
      ``serialize_on_loop=False`` → PASS。

    这个判定不依赖 framework 的回调调度方式（同步 await 或 create_task），
    只看 serialize 真正跑在哪个线程。
    """
    serialize_called = threading.Event()
    serialize_release = threading.Event()
    real_serialize = callbacks_module.serialize_input_messages

    loop_thread = threading.get_ident()
    serialize_ran_on: list[int] = []

    def blocking_serialize(messages, *, max_chars):
        serialize_called.set()
        serialize_ran_on.append(threading.get_ident())
        # 阻塞至测试放行，给主协程足够时间做判定
        serialize_release.wait(timeout=10.0)
        return real_serialize(messages, max_chars=max_chars)

    monkeypatch.setattr(callbacks_module, "serialize_input_messages", blocking_serialize)
    # 让 token count 立即返回，避免 tiktoken 对大 messages 真实计数拖慢/超时，
    # 确保流程进入 log_messages 的 serialize 分支。
    monkeypatch.setattr(
        callbacks_module,
        "count_context_tokens",
        lambda messages, tools: callbacks_module.ContextTokenBreakdown(),
    )

    big_messages = [{"role": "user", "content": "x" * 8000}] * 64

    trigger_task = asyncio.ensure_future(
        telemetry_env.framework.trigger(
            LLMCallEvents.LLM_STREAM_INPUT,
            big_messages,
            model="bug-207-model",
        )
    )

    # 等 serialize 被触发（最多 3s）
    for _ in range(60):
        if serialize_called.is_set():
            break
        await asyncio.sleep(0.05)
    assert serialize_called.is_set(), (
        "serialize 桩未触发——未进入 _enrich_llm_input 的 serialize 分支。"
        "检查：LLM span 是否已建立、log_messages 是否为 True。"
    )

    serialize_on_loop = serialize_ran_on[0] == loop_thread

    serialize_release.set()  # 放行 serialize，让 trigger 收尾
    await asyncio.wait_for(trigger_task, timeout=5.0)

    # 补发 LLM_OUTPUT 让 span/metric state 正常收尾（input 单独 trigger 不收尾）
    try:
        await asyncio.wait_for(
            telemetry_env.framework.trigger(
                LLMCallEvents.LLM_OUTPUT,
                is_stream=True,
                response="ok",
            ),
            timeout=5.0,
        )
    except Exception:
        pass

    # serialize 必须不跑在事件循环线程上（否则会阻塞事件循环，导致
    # WebSocket ping / 用户 query 无法被处理——bug-207）。
    assert not serialize_on_loop, (
        f"bug-207：_enrich_llm_input 的 serialize_input_messages 跑在事件循环"
        f"线程上（serialize thread={serialize_ran_on[0]}, "
        f"loop thread={loop_thread}），同步阻塞事件循环——WebSocket ping / "
        f"用户 query 无法被处理，最终触发'智能体连接不稳定'。"
        f"修复方向：serialize 改走 asyncio.to_thread。"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
)
async def test_control_metric_errors_propagate_after_state_cleanup(
    control_error: BaseException,
) -> None:
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=_RecordingMetrics(control_error),
        config=TelemetryConfig(enabled=True),
    )
    framework = AsyncCallbackFramework()
    await callbacks.register(framework)
    try:
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[],
            model="control-model",
        )
        with pytest.raises(type(control_error)):
            await framework.trigger(
                LLMCallEvents.LLM_INVOKE_OUTPUT,
                result=SimpleNamespace(content="answer", tool_calls=[]),
            )
        assert callbacks._metric_state.active_count() == 0
    finally:
        await callbacks.unregister(framework)


@pytest.mark.asyncio
async def test_register_rolls_back_partial_failure_and_unregister_is_retryable() -> (
    None
):
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=_Metrics(),
        config=TelemetryConfig(),
    )
    failing_register = _FailingFramework(fail_register_at=4)
    with pytest.raises(RuntimeError, match="register failed"):
        await callbacks.register(failing_register)
    assert callbacks._registered == []
    assert callbacks._framework is None
    assert not failing_register.list_events(namespace=NAMESPACE)

    framework = _FailingFramework()
    await callbacks.register(framework)
    assert callbacks._framework is framework
    framework.fail_unregister_callback = callbacks._registered[-1][1]
    with pytest.raises(RuntimeError, match="unregister failed"):
        await callbacks.unregister(framework)
    assert len(callbacks._registered) == 1
    assert callbacks._framework is framework
    await callbacks.unregister(framework)
    assert callbacks._registered == []
    assert callbacks._framework is None
    assert not framework.list_events(namespace=NAMESPACE)

    rollback_failure = _FailingFramework(
        fail_register_at=4,
        fail_unregister_at=1,
    )
    with pytest.raises(RuntimeError, match="register failed"):
        await callbacks.register(rollback_failure)
    assert len(callbacks._registered) == 1
    assert callbacks._framework is rollback_failure
    assert len(rollback_failure.list_events(namespace=NAMESPACE)) == 1
    await callbacks.unregister(rollback_failure)
    assert callbacks._registered == []
    assert callbacks._framework is None
    assert not rollback_failure.list_events(namespace=NAMESPACE)


@pytest.mark.asyncio
async def test_unregister_rejects_a_different_framework_without_losing_state() -> None:
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=_Metrics(),
        config=TelemetryConfig(),
    )
    registered_framework = AsyncCallbackFramework()
    other_framework = AsyncCallbackFramework()
    await callbacks.register(registered_framework)
    registered = list(callbacks._registered)

    with pytest.raises(ValueError, match="different callback framework"):
        await callbacks.unregister(other_framework)

    assert callbacks._registered == registered
    assert callbacks._framework is registered_framework
    assert len(registered_framework.list_events(namespace=NAMESPACE)) == 13
    assert not other_framework.list_events(namespace=NAMESPACE)

    await callbacks.unregister(registered_framework)
    assert callbacks._registered == []
    assert callbacks._framework is None
    assert not registered_framework.list_events(namespace=NAMESPACE)


@pytest.mark.asyncio
async def test_registration_is_idempotent_and_core_ordering_is_verified(
    telemetry_env: SimpleNamespace,
) -> None:
    registered = list(telemetry_env.callbacks._registered)
    await telemetry_env.callbacks.register(telemetry_env.framework)

    assert telemetry_env.callbacks._registered == registered
    assert telemetry_env.callbacks.validate_core_ordering(telemetry_env.framework)

    await telemetry_env.callbacks.unregister(telemetry_env.framework)
    assert not telemetry_env.callbacks.validate_core_ordering(telemetry_env.framework)
    await telemetry_env.callbacks.unregister(telemetry_env.framework)


@pytest.mark.asyncio
async def test_context_token_metrics_are_emitted_without_a_recording_span(
    monkeypatch: pytest.MonkeyPatch,
    request_metric_channel: None,
) -> None:
    del request_metric_channel
    monkeypatch.setattr(
        callbacks_module,
        "count_context_tokens",
        lambda messages, tools: ContextTokenBreakdown(
            tool_definitions=14,
            per_tool_tokens=(("search", 4), ("weather", 7)),
            skill=12,
            per_skill_tokens=(("research", 5), ("weather", 7)),
        ),
    )
    metrics = _RecordingMetrics()
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=metrics,
        config=TelemetryConfig(enabled=True),
    )
    framework = AsyncCallbackFramework()
    await callbacks.register(framework)
    try:
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[],
            tools=[{"function": {"name": "search"}}],
            model="token-model",
        )
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=SimpleNamespace(content="ok", tool_calls=[]),
        )
    finally:
        await callbacks.unregister(framework)

    tool_points = [
        call for call in metrics.calls if call[1] == "gen_ai.tool.token.usage"
    ]
    skill_points = [
        call for call in metrics.calls if call[1] == "gen_ai.skill.token.usage"
    ]
    assert [(call[2], call[3]) for call in tool_points] == [
        (
            4,
            {
                "gen_ai.tool.name": "search",
                "gen_ai.request.model": "token-model",
                "jiuwenclaw.channel.id": "web",
            },
        ),
        (
            7,
            {
                "gen_ai.tool.name": "weather",
                "gen_ai.request.model": "token-model",
                "jiuwenclaw.channel.id": "web",
            },
        ),
    ]
    assert {(call[2], frozenset(call[3].items())) for call in skill_points} == {
        (
            5,
            frozenset(
                {
                    "gen_ai.skill.name": "research",
                    "gen_ai.request.model": "token-model",
                    "jiuwenclaw.channel.id": "web",
                }.items()
            ),
        ),
        (
            7,
            frozenset(
                {
                    "gen_ai.skill.name": "weather",
                    "gen_ai.request.model": "token-model",
                    "jiuwenclaw.channel.id": "web",
                }.items()
            ),
        ),
    }


@pytest.mark.parametrize(
    "control_error",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
)
def test_input_control_errors_propagate_after_state_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    control_error: BaseException,
) -> None:
    async def _inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def _inline_wait_for(aw, timeout=None):
        del timeout
        return await aw

    # KeyboardInterrupt/SystemExit inside pytest-asyncio (or asyncio.wait_for's
    # nested task) aborts the whole pytest session. Drive the coroutine with
    # an explicitly closed event loop so pytest.raises can catch them without
    # leaving its selector sockets behind after a prior async test.
    monkeypatch.setattr(callbacks_module.asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(callbacks_module.asyncio, "wait_for", _inline_wait_for)

    def raise_control(messages, tools):
        del messages, tools
        raise control_error

    monkeypatch.setattr(callbacks_module, "count_context_tokens", raise_control)
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=_Metrics(),
        config=TelemetryConfig(enabled=True),
    )

    async def _exercise() -> None:
        await callbacks._on_llm_invoke_input(messages=[], model="control-model")

    def _run_in_closed_loop() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_exercise())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    with pytest.raises(type(control_error)):
        _run_in_closed_loop()

    assert callbacks._metric_state.active_count() == 0
    assert callbacks._span_state.active_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interceptor_error",
    [asyncio.CancelledError("intercepted"), KeyboardInterrupt("intercepted")],
)
async def test_real_framework_cancellation_closes_core_llm_span_and_metrics_once(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    interceptor_error: BaseException,
) -> None:
    token_count_started = threading.Event()
    release_token_count = threading.Event()

    def slow_token_count(messages, tools):
        del messages, tools
        token_count_started.set()
        release_token_count.wait(timeout=3)
        return ContextTokenBreakdown()

    monkeypatch.setattr(callbacks_module, "count_context_tokens", slow_token_count)
    third_party_calls: list[BaseException] = []

    async def intercept_error(**kwargs):
        third_party_calls.append(kwargs["error"])
        raise interceptor_error

    telemetry_env.framework.register_sync(
        LLMCallEvents.LLM_CALL_ERROR,
        intercept_error,
        priority=50,
        namespace="test.control.interceptor",
    )
    baseline_spans = telemetry_env.registry.active_count()
    task = asyncio.create_task(
        telemetry_env.framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[{"role": "user", "content": "cancel me"}],
            model="slow-token-model",
        )
    )
    try:
        assert await asyncio.to_thread(token_count_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_token_count.set()
        telemetry_env.framework.unregister_sync(
            LLMCallEvents.LLM_CALL_ERROR,
            intercept_error,
        )

    telemetry_env.provider.force_flush()
    assert third_party_calls == []
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert telemetry_env.registry.active_count() == baseline_spans
    assert (
        telemetry_env.registry.find_latest(
            telemetry_env.parent.get_span_context().trace_id,
            name="llm.call",
        )
        is None
    )
    spans = [
        span
        for span in telemetry_env.exporter.get_finished_spans()
        if span.name == "llm.call"
    ]
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"
    operation_counts = _metric_points(
        telemetry_env.reader,
        "gen_ai.client.operation.count",
    )
    assert len(operation_counts) == 1
    assert operation_counts[0].value == 1


@pytest.mark.asyncio
async def test_ordinary_enrichment_error_degrades_without_breaking_business_call(
    telemetry_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        callbacks_module,
        "serialize_input_messages",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry failed")),
    )
    monkeypatch.setattr(
        callbacks_module._LOGGER,
        "warning",
        lambda message, **kwargs: warnings.append(message),
    )

    await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_INPUT,
        messages=[{"role": "user", "content": "hello"}],
        model="degraded-model",
    )
    assert telemetry_env.callbacks._metric_state.active_count() == 1
    assert telemetry_env.callbacks._span_state.active_count() == 1
    result = object()
    returned = await telemetry_env.framework.trigger(
        LLMCallEvents.LLM_INVOKE_OUTPUT,
        result=result,
    )

    assert returned[0] is result
    assert warnings == ["LLM telemetry input enrichment failed"]
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.count")) == 1
    )
    assert (
        len(_metric_points(telemetry_env.reader, "gen_ai.client.operation.duration"))
        == 1
    )


@pytest.mark.asyncio
async def test_state_registries_enforce_capacity_ttl_and_remove_indexes() -> None:
    now = time.monotonic()
    span_states = EnrichmentStateRegistry(max_entries=2, ttl_seconds=1)
    for sequence in range(1, 4):
        span_states.put(
            (sequence, sequence),
            callbacks_module._CallState(started_at=now, sequence=sequence),
        )
    assert span_states.active_count() == 2
    assert span_states.get((1, 1)) is None
    assert span_states.prune(now=now + 1) == 2
    assert span_states.active_count() == 0

    metric_states = MetricCallStateRegistry(max_entries=2, ttl_seconds=1)
    for identity in ("one", "two", "three"):
        metric_states.start(
            "llm",
            identity,
            callbacks_module._CallState(started_at=now),
        )
    assert metric_states.active_count() == 2
    assert metric_states.finish("llm", "one") is None
    assert metric_states.prune(now=now + 1) == 2
    assert metric_states.active_count() == 0
    assert metric_states.finish("llm", "two") is None

    expired_span_states = EnrichmentStateRegistry(max_entries=1, ttl_seconds=1)
    expired_span_states.put(
        (9, 9),
        callbacks_module._CallState(started_at=now - 2, sequence=1),
    )
    assert expired_span_states.get((9, 9)) is None

    expired_metric_states = MetricCallStateRegistry(max_entries=1, ttl_seconds=1)
    expired_metric_states.start(
        "llm",
        "expired",
        callbacks_module._CallState(started_at=now - 2),
    )
    assert expired_metric_states.finish("llm", "expired") is None

    skill_sessions = SkillSessionRegistry(max_entries=2, ttl_seconds=1)
    replacement = callbacks_module._SkillSession(
        started_at=now,
        version="replacement",
        channel_id="web",
    )
    skill_sessions.start(
        1,
        "session-1",
        "one",
        callbacks_module._SkillSession(now - 0.5, "old", "cli"),
    )
    skill_sessions.start(1, "session-1", "one", replacement)
    assert skill_sessions.active_count() == 1
    assert skill_sessions.finish(1, "session-1", "one") == replacement
    for index in range(3):
        skill_sessions.start(
            index,
            f"session-{index}",
            f"skill-{index}",
            callbacks_module._SkillSession(now + index / 10, str(index), "web"),
        )
    assert skill_sessions.active_count() == 2
    assert skill_sessions.finish(0, "session-0", "skill-0") is None
    assert skill_sessions.prune(now=now + 2) == 2
    assert skill_sessions.active_count() == 0


@pytest.mark.parametrize(
    "registry_type",
    [EnrichmentStateRegistry, MetricCallStateRegistry, SkillSessionRegistry],
)
def test_state_registries_reject_invalid_bounds(registry_type) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        registry_type(max_entries=0, ttl_seconds=1)
    with pytest.raises(ValueError, match="ttl_seconds"):
        registry_type(max_entries=1, ttl_seconds=0)


@pytest.mark.asyncio
async def test_skill_sessions_isolate_invalid_trace_by_session_across_tasks() -> None:
    registry = SkillSessionRegistry(max_entries=4, ttl_seconds=60)
    gate = asyncio.Event()
    ready = 0

    async def worker(session_id: str, version: str):
        nonlocal ready
        session = callbacks_module._SkillSession(
            started_at=time.monotonic(),
            version=version,
            channel_id="web",
        )
        registry.start(0, session_id, "forecast", session)
        ready += 1
        if ready == 2:
            gate.set()
        await gate.wait()
        return registry.finish(0, session_id, "forecast")

    first, second = await asyncio.gather(
        worker("session-a", "v1"),
        worker("session-b", "v2"),
    )

    assert (first.version, second.version) == ("v1", "v2")
    assert registry.active_count() == 0


@pytest.mark.asyncio
async def test_empty_id_concurrent_calls_are_isolated_by_task() -> None:
    metrics = _RecordingMetrics()
    callbacks = RichTelemetryCallbacks(
        span_registry=SpanRegistryProcessor(max_spans=8, ttl_seconds=60),
        metrics=metrics,
        config=TelemetryConfig(enabled=True),
    )
    framework = AsyncCallbackFramework()
    await callbacks.register(framework)
    ready: list[str] = []
    gate = asyncio.Event()

    async def worker(model: str, tool_name: str) -> None:
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_INPUT,
            messages=[],
            model=model,
        )
        await framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name=tool_name,
            tool_id="",
            inputs={},
        )
        ready.append(model)
        if len(ready) == 2:
            gate.set()
        await gate.wait()
        await framework.trigger(
            ToolCallEvents.TOOL_CALL_FINISHED,
            tool_name=tool_name,
            tool_id="",
            result={"ok": True},
        )
        await framework.trigger(
            LLMCallEvents.LLM_INVOKE_OUTPUT,
            result=SimpleNamespace(content="ok", tool_calls=[]),
        )

    try:
        await asyncio.gather(
            worker("model-a", "tool-a"),
            worker("model-b", "tool-b"),
        )
    finally:
        await callbacks.unregister(framework)

    operation_counts = [
        call for call in metrics.calls if call[1] == "gen_ai.client.operation.count"
    ]
    tool_counts = [
        call for call in metrics.calls if call[1] == "gen_ai.tool.call.count"
    ]
    assert {call[3]["gen_ai.request.model"] for call in operation_counts} == {
        "model-a",
        "model-b",
    }
    assert {call[3]["gen_ai.tool.name"] for call in tool_counts} == {
        "tool-a",
        "tool-b",
    }
    assert callbacks._metric_state.active_count() == 0


@pytest.mark.asyncio
async def test_same_tool_id_concurrent_calls_keep_state_and_spans_task_local(
    telemetry_env: SimpleNamespace,
) -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_finished = asyncio.Event()

    async def first_call() -> None:
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name="shared",
            tool_id="static-card-id",
            inputs={"owner": "first"},
        )
        first_started.set()
        await second_started.wait()
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_FINISHED,
            tool_name="shared",
            tool_id="static-card-id",
            result={"owner": "first"},
        )
        first_finished.set()

    async def second_call() -> None:
        await first_started.wait()
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_STARTED,
            tool_name="shared",
            tool_id="static-card-id",
            inputs={"owner": "second"},
        )
        second_started.set()
        await first_finished.wait()
        await telemetry_env.framework.trigger(
            ToolCallEvents.TOOL_CALL_FINISHED,
            tool_name="shared",
            tool_id="static-card-id",
            result={"owner": "second"},
        )

    await asyncio.gather(first_call(), second_call())
    telemetry_env.provider.force_flush()

    spans = [
        span
        for span in telemetry_env.exporter.get_finished_spans()
        if span.name == "tool.shared"
    ]
    assert len(spans) == 2
    observed_pairs = {
        (
            span.attributes["gen_ai.tool.arguments"],
            span.attributes["gen_ai.tool.result"],
        )
        for span in spans
    }
    assert observed_pairs == {
        ('{"owner": "first"}', '{"owner": "first"}'),
        ('{"owner": "second"}', '{"owner": "second"}'),
    }
    assert telemetry_env.callbacks._metric_state.active_count() == 0
    assert telemetry_env.callbacks._span_state.active_count() == 0
