# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for §11.4 (canceled/timeout) and §11.5/§11.6 (streaming/iteration) attributes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def span_capture(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider, raising=False)
    trace.set_tracer_provider(provider)

    import jiuwenclaw.telemetry.instrumentors.telemetry_rail as mod
    monkeypatch.setattr(mod, "_tracer", trace.get_tracer("jiuwenclaw.telemetry_rail"))

    yield exporter
    exporter.clear()


def _model_ctx(streaming: bool = False):
    return SimpleNamespace(
        model=SimpleNamespace(
            model_name="gpt-4o",
            model_client_config={"client_provider": "openai"},
            streaming=streaming,
        ),
        model_config=SimpleNamespace(temperature=0.7, top_p=1.0),
        messages=[],
    )


async def test_streaming_attribute_set(span_capture):
    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail

    rail = TelemetryRail()
    ctx = _model_ctx(streaming=True)
    await rail.before_model_call(ctx)
    rail.record_first_token(ctx)
    ctx.result = SimpleNamespace(
        content="hi",
        usage_metadata=SimpleNamespace(input_tokens=5, output_tokens=2),
        finish_reason="stop",
    )
    await rail.after_model_call(ctx)

    spans = span_capture.get_finished_spans()
    llm_spans = [s for s in spans if s.name == "gen_ai.chat"]
    assert llm_spans, "expected gen_ai.chat span"
    assert llm_spans[0].attributes.get("gen_ai.request.streaming") is True
    assert any(ev.name == "gen_ai.first_token" for ev in llm_spans[0].events)


async def test_iteration_attribute_increments(span_capture):
    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail

    rail = TelemetryRail()
    await rail.before_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="c1")))

    for _ in range(3):
        ctx = _model_ctx()
        await rail.before_model_call(ctx)
        ctx.result = SimpleNamespace(
            content="r",
            usage_metadata=SimpleNamespace(input_tokens=1, output_tokens=1),
            finish_reason="stop",
        )
        await rail.after_model_call(ctx)

    await rail.after_invoke(SimpleNamespace())
    spans = [s for s in span_capture.get_finished_spans() if s.name == "gen_ai.chat"]
    iterations = sorted(s.attributes.get("jiuwenclaw.iteration") for s in spans)
    assert iterations == [1, 2, 3]


async def test_canceled_attribute(span_capture):
    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail

    rail = TelemetryRail()
    await rail.before_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="c1")))
    ctx = SimpleNamespace(error=asyncio.CancelledError())
    await rail.after_invoke(ctx)

    spans = [s for s in span_capture.get_finished_spans() if s.name == "jiuwenclaw.agent.invoke"]
    assert spans, "expected agent span"
    assert spans[0].attributes.get("jiuwenclaw.canceled") is True


async def test_timeout_error_type(span_capture):
    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail

    rail = TelemetryRail()
    await rail.before_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="c1")))
    ctx = SimpleNamespace(error=asyncio.TimeoutError("timeout"))
    await rail.after_invoke(ctx)

    spans = [s for s in span_capture.get_finished_spans() if s.name == "jiuwenclaw.agent.invoke"]
    assert spans
    assert spans[0].attributes.get("error.type") == "TimeoutError"


async def test_nested_agent_llm_parents_to_inner_agent(span_capture):
    """Sub-agent's LLM span should parent to the inner agent span, not the outer."""
    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail

    outer = TelemetryRail()
    inner = TelemetryRail()

    await outer.before_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="outer")))
    await inner.before_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="inner")))

    ctx = _model_ctx()
    await inner.before_model_call(ctx)
    ctx.result = SimpleNamespace(
        content="r",
        usage_metadata=SimpleNamespace(input_tokens=1, output_tokens=1),
        finish_reason="stop",
    )
    await inner.after_model_call(ctx)

    await inner.after_invoke(SimpleNamespace())
    await outer.after_invoke(SimpleNamespace())

    spans = span_capture.get_finished_spans()
    llm = next(s for s in spans if s.name == "gen_ai.chat")
    agents = [s for s in spans if s.name == "jiuwenclaw.agent.invoke"]
    inner_agent = next(
        s for s in agents if s.attributes.get("gen_ai.conversation.id") == "inner"
    )

    assert llm.parent is not None, "gen_ai.chat should have a parent"
    assert llm.parent.span_id == inner_agent.context.span_id, (
        "gen_ai.chat must parent to the INNER agent.invoke, not the outer"
    )


async def test_llm_span_parents_to_active_context_not_agent_field(span_capture):
    from opentelemetry import context as otel_context, trace as otel_trace

    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail

    rail = TelemetryRail()
    await rail.before_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="c1")))

    tracer = otel_trace.get_tracer("test.intermediate")
    intermediate = tracer.start_span("intermediate.step")
    token = otel_context.attach(otel_trace.set_span_in_context(intermediate))
    try:
        ctx = _model_ctx()
        await rail.before_model_call(ctx)
        ctx.result = SimpleNamespace(
            content="r",
            usage_metadata=SimpleNamespace(input_tokens=1, output_tokens=1),
            finish_reason="stop",
        )
        await rail.after_model_call(ctx)
    finally:
        otel_context.detach(token)
        intermediate.end()

    await rail.after_invoke(SimpleNamespace())

    spans = span_capture.get_finished_spans()
    llm = next(s for s in spans if s.name == "gen_ai.chat")
    inter = next(s for s in spans if s.name == "intermediate.step")

    assert llm.parent is not None
    assert llm.parent.span_id == inter.context.span_id, (
        "gen_ai.chat must parent to the intermediate span (current context), "
        "not jump over it to self._agent_span"
    )
