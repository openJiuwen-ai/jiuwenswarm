"""Concurrent request isolation and terminal cleanup integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from openjiuwen.agent_teams.observability import (
    ObservabilityConfig,
    init_observability,
    shutdown_observability,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import LLMCallEvents, ToolCallEvents

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.enrichment.callbacks import RichTelemetryCallbacks
from jiuwenswarm.telemetry.metrics import TelemetryMetrics
from jiuwenswarm.telemetry.request_context import TraceBindingRegistry
from jiuwenswarm.telemetry.span_registry import SpanRegistryProcessor

# CI Python 3.11: asyncio.Runner.close() can raise during pytest-asyncio
# teardown; pytest.ini ``filterwarnings = error`` would turn that into ERROR.
pytestmark = pytest.mark.filterwarnings(
    "ignore:An exception occurred during teardown of an asyncio.Runner:RuntimeWarning"
)


@pytest.fixture
async def concurrent_env() -> AsyncIterator[SimpleNamespace]:
    shutdown_observability()
    identity_token = IdentityStore.set_identity(None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "concurrent-integration"})
    )
    span_registry = SpanRegistryProcessor(max_spans=256, ttl_seconds=60)
    provider.add_span_processor(span_registry)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    callbacks = RichTelemetryCallbacks(
        span_registry=span_registry,
        metrics=TelemetryMetrics(meter_provider),
        config=TelemetryConfig(enabled=True),
    )
    init_observability(
        ObservabilityConfig(enabled=True, backend="otlp"),
        tracer_provider_override=provider,
        owns_provider=False,
    )
    await callbacks.register(Runner.callback_framework)
    runtime = SimpleNamespace(
        is_unified_active=lambda: True,
        tracer_provider=provider,
        span_registry=span_registry,
        trace_bindings=TraceBindingRegistry(max_bindings=64, ttl_seconds=60),
    )
    try:
        yield SimpleNamespace(
            callbacks=callbacks,
            exporter=exporter,
            framework=Runner.callback_framework,
            metric_reader=metric_reader,
            meter_provider=meter_provider,
            provider=provider,
            runtime=runtime,
            span_registry=span_registry,
        )
    finally:
        try:
            await callbacks.unregister(Runner.callback_framework)
            shutdown_observability()
            provider.force_flush()
            meter_provider.shutdown()
            provider.shutdown()
        finally:
            IdentityStore.clear(identity_token)
            await asyncio.sleep(0)


def _metric_points(reader: InMemoryMetricReader, name: str) -> list[object]:
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


@pytest.mark.asyncio
async def test_two_sessions_keep_identity_metrics_and_cancellation_isolated(
    concurrent_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import agent_observability

    monkeypatch.setattr(
        agent_observability,
        "_get_unified_runtime",
        lambda: concurrent_env.runtime,
    )
    started = {name: asyncio.Event() for name in ("a", "b")}
    release_b = asyncio.Event()
    handles: dict[str, object] = {}
    results: dict[str, object] = {}
    warmup_usage = {"a": (3, 1), "b": (7, 2)}

    async def _run_session(name: str) -> object:
        session_id = f"session-{name}"
        request_id = f"request-{name}"
        call_id = f"llm-{name}"
        token = IdentityStore.set_identity(
            IdentityInfo(
                user_id=f"user-{name}",
                domain_id=f"domain-{name}",
                app_id=f"app-{name}",
            )
        )
        handle = agent_observability.open_agent_run_span(
            session_id=session_id,
            request_id=request_id,
            channel_id=f"channel-{name}",
            mode="code.normal",
        )
        assert handle is not None
        handles[name] = handle
        try:
            warmup_call_id = f"warmup-{name}"
            await concurrent_env.framework.trigger(
                LLMCallEvents.LLM_INVOKE_INPUT,
                messages=[{"role": "user", "content": f"warmup-{name}"}],
                model=f"warmup-model-{name}",
                call_id=warmup_call_id,
            )
            warmup_input, warmup_output = warmup_usage[name]
            warmup_result = SimpleNamespace(
                content=f"warmup-result-{name}",
                finish_reason="stop",
                tool_calls=[],
                usage_metadata=SimpleNamespace(
                    input_tokens=warmup_input,
                    output_tokens=warmup_output,
                ),
            )
            await concurrent_env.framework.trigger(
                LLMCallEvents.LLM_INVOKE_OUTPUT,
                result=warmup_result,
                call_id=warmup_call_id,
            )
            await concurrent_env.framework.trigger(
                LLMCallEvents.LLM_INVOKE_INPUT,
                messages=[{"role": "user", "content": f"prompt-{name}"}],
                model=f"active-model-{name}",
                call_id=call_id,
            )
            started[name].set()
            if name == "a":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    await concurrent_env.framework.trigger(
                        LLMCallEvents.LLM_CALL_ERROR,
                        error=error,
                        call_id=call_id,
                    )
                    raise
            await release_b.wait()
            result = SimpleNamespace(
                content=f"result-{name}",
                finish_reason="stop",
                tool_calls=[],
                usage_metadata=SimpleNamespace(
                    input_tokens=11,
                    output_tokens=4,
                ),
            )
            callbacks = await concurrent_env.framework.trigger(
                LLMCallEvents.LLM_INVOKE_OUTPUT,
                result=result,
                call_id=call_id,
            )
            await concurrent_env.framework.trigger(
                ToolCallEvents.TOOL_CALL_STARTED,
                tool_name="tool-b",
                tool_id="tool-b",
                inputs=(("input-b",), {}),
            )
            await concurrent_env.framework.trigger(
                ToolCallEvents.TOOL_CALL_FINISHED,
                tool_name="tool-b",
                tool_id="tool-b",
                inputs=(("input-b",), {}),
                result={"session": name},
            )
            results[name] = result
            assert callbacks[0] is result
            return result
        finally:
            agent_observability.close_agent_run_span(
                handle,
                session_id=session_id,
            )
            IdentityStore.clear(token)

    task_a = asyncio.create_task(_run_session("a"))
    task_b = asyncio.create_task(_run_session("b"))
    await asyncio.wait_for(
        asyncio.gather(started["a"].wait(), started["b"].wait()),
        timeout=2.0,
    )
    task_a.cancel()
    outcome_a = (await asyncio.gather(task_a, return_exceptions=True))[0]
    assert isinstance(outcome_a, asyncio.CancelledError)
    try:
        assert not task_b.done()
        root_b = handles["b"].root_span
        assert root_b.is_recording()
        live_b = concurrent_env.runtime.trace_bindings.resolve(
            "session-b",
            "request-b",
        )
        assert live_b is not None
        assert live_b.root_span is root_b
    finally:
        release_b.set()
        outcome_b = await task_b

    assert outcome_b is results["b"]
    concurrent_env.provider.force_flush()
    spans = list(concurrent_env.exporter.get_finished_spans())
    root_a = handles["a"].root_span
    trace_a = root_a.get_span_context().trace_id
    trace_b = root_b.get_span_context().trace_id
    assert trace_a != trace_b
    spans_a = [span for span in spans if span.context.trace_id == trace_a]
    spans_b = [span for span in spans if span.context.trace_id == trace_b]

    assert {span.attributes.get("user.id") for span in spans_a} == {"user-a"}
    assert {span.attributes.get("user.id") for span in spans_b} == {"user-b"}
    assert [span.name for span in spans_a].count("llm.call") == 2
    assert [span.name for span in spans_b].count("llm.call") == 2
    assert "tool.tool-b" not in [span.name for span in spans_a]
    assert "tool.tool-b" in [span.name for span in spans_b]
    assert {span.attributes.get("jiuwenclaw.request.id") for span in spans_a} == {
        "request-a"
    }
    assert {span.attributes.get("jiuwenclaw.request.id") for span in spans_b} == {
        "request-b"
    }
    cancelled_span = next(
        span
        for span in spans_a
        if span.name == "llm.call" and span.attributes.get("jiuwenclaw.canceled")
    )
    assert cancelled_span.attributes["jiuwenclaw.canceled"] is True
    usage_a = {
        (
            span.attributes.get("gen_ai.usage.input_tokens"),
            span.attributes.get("gen_ai.usage.output_tokens"),
        )
        for span in spans_a
        if span.name == "llm.call" and "gen_ai.usage.input_tokens" in span.attributes
    }
    usage_b = {
        (
            span.attributes.get("gen_ai.usage.input_tokens"),
            span.attributes.get("gen_ai.usage.output_tokens"),
        )
        for span in spans_b
        if span.name == "llm.call" and "gen_ai.usage.input_tokens" in span.attributes
    }
    assert usage_a == {(3, 1)}
    assert usage_b == {(7, 2), (11, 4)}

    operation_points = _metric_points(
        concurrent_env.metric_reader,
        "gen_ai.client.operation.count",
    )
    assert {point.attributes["gen_ai.request.model"] for point in operation_points} == {
        "warmup-model-a",
        "warmup-model-b",
        "active-model-a",
        "active-model-b",
    }
    token_points = _metric_points(
        concurrent_env.metric_reader,
        "gen_ai.client.token.usage",
    )
    token_values = {
        (
            point.attributes["gen_ai.request.model"],
            point.attributes["gen_ai.token.type"],
        ): point.value
        for point in token_points
    }
    assert token_values == {
        ("warmup-model-a", "input"): 3,
        ("warmup-model-a", "output"): 1,
        ("warmup-model-b", "input"): 7,
        ("warmup-model-b", "output"): 2,
        ("active-model-b", "input"): 11,
        ("active-model-b", "output"): 4,
    }
    assert (
        concurrent_env.runtime.trace_bindings.resolve("session-a", "request-a") is None
    )
    assert (
        concurrent_env.runtime.trace_bindings.resolve("session-b", "request-b") is None
    )
    assert concurrent_env.span_registry.active_count() == 0
    assert concurrent_env.callbacks._span_state.active_count() == 0
    assert concurrent_env.callbacks._metric_state.active_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "business_error",
    [ValueError("business failed"), TimeoutError("model timed out")],
    ids=["error", "timeout"],
)
async def test_error_and_timeout_preserve_original_exception_and_cleanup(
    concurrent_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    business_error: Exception,
) -> None:
    from jiuwenswarm.agents.harness import agent_observability

    monkeypatch.setattr(
        agent_observability,
        "_get_unified_runtime",
        lambda: concurrent_env.runtime,
    )
    handle = agent_observability.open_agent_run_span(
        session_id="session-error",
        request_id="request-error",
        channel_id="channel-error",
        mode="agent.plan",
    )
    assert handle is not None
    is_tool_timeout = isinstance(business_error, TimeoutError)

    async def _run_failing_business_operation() -> None:
        if is_tool_timeout:
            await concurrent_env.framework.trigger(
                ToolCallEvents.TOOL_CALL_STARTED,
                tool_name="slow_tool",
                tool_id="timeout-tool",
                inputs=(("wait",), {}),
            )
        else:
            await concurrent_env.framework.trigger(
                LLMCallEvents.LLM_INVOKE_INPUT,
                messages=[{"role": "user", "content": "fail"}],
                model="error-model",
                call_id="error-call",
            )
        try:
            raise business_error
        except BaseException as error:
            if is_tool_timeout:
                await concurrent_env.framework.trigger(
                    ToolCallEvents.TOOL_CALL_ERROR,
                    error=error,
                    tool_name="slow_tool",
                    tool_id="timeout-tool",
                )
            else:
                await concurrent_env.framework.trigger(
                    LLMCallEvents.LLM_CALL_ERROR,
                    error=error,
                    call_id="error-call",
                )
            raise

    try:
        with pytest.raises(type(business_error)) as raised:
            await _run_failing_business_operation()
    finally:
        agent_observability.close_agent_run_span(
            handle,
            session_id="session-error",
        )

    assert raised.value is business_error
    concurrent_env.provider.force_flush()
    operation_span = next(
        span
        for span in concurrent_env.exporter.get_finished_spans()
        if span.name == ("tool.slow_tool" if is_tool_timeout else "llm.call")
    )
    assert operation_span.attributes["error.type"] == type(business_error).__name__
    if is_tool_timeout:
        assert operation_span.attributes["jiuwenclaw.timeout"] is True
    assert (
        concurrent_env.runtime.trace_bindings.resolve("session-error", "request-error")
        is None
    )
    assert concurrent_env.span_registry.active_count() == 0
    assert concurrent_env.callbacks._span_state.active_count() == 0
    assert concurrent_env.callbacks._metric_state.active_count() == 0
