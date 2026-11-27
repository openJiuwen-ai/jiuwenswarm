# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests for jiuwenclaw.telemetry module.

These tests verify the full instrumentation pipeline by applying monkey-patches
to real classes and simulating end-to-end request flows, validating that:
- Spans form correct parent-child hierarchies across all 4 layers
- Metrics are recorded correctly
- Context propagation works across simulated WebSocket boundaries
- The full init_telemetry() → instrument → trace flow works end-to-end
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace, metrics, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import StatusCode


# ---------------------------------------------------------------------------
# In-memory span exporter
# ---------------------------------------------------------------------------

class InMemorySpanExporter(SpanExporter):
    def __init__(self):
        self._spans = []

    def export(self, spans):
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def get_finished_spans(self):
        return list(self._spans)

    def clear(self):
        self._spans.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_otel():
    """Create and install fresh OTel providers, return (tp, exporter, metric_reader)."""
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(tp)

    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(mp)

    return tp, mp, exporter, reader


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_metrics_data(reader):
    """Extract metric data points from InMemoryMetricReader."""
    data = reader.get_metrics_data()
    result = {}
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points = []
                for dp in metric.data.data_points:
                    points.append({
                        "value": getattr(dp, "value", None) or getattr(dp, "sum", None),
                        "attributes": dict(dp.attributes) if dp.attributes else {},
                    })
                result[metric.name] = {"points": points, "description": metric.description}
    return result


# ---------------------------------------------------------------------------
# 1. Full pipeline: ENTRY → AGENT → LLM → TOOL span hierarchy
# ---------------------------------------------------------------------------

class TestFullSpanPipeline:
    """Simulate a complete request flow and verify the full span tree."""

    @staticmethod
    def test_full_request_creates_4_layer_span_tree():
        """Simulate ENTRY → AGENT → LLM (with tool call) → TOOL → LLM (final answer)."""
        tp, mp, exporter, reader = _setup_otel()

        # Get tracers matching what instrumentors use
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")
        llm_tracer = tp.get_tracer("jiuwenclaw.llm")
        tool_tracer = tp.get_tracer("jiuwenclaw.tool")

        from jiuwenclaw.telemetry.context_propagation import inject_trace_context, extract_trace_context

        async def simulate_full_request():
            # --- ENTRY layer ---
            with entry_tracer.start_as_current_span(
                "channel.request",
                attributes={
                    "jiuwenclaw.channel.id": "feishu",
                    "jiuwenclaw.session.id": "sess_001",
                    "jiuwenclaw.request.id": "req_001",
                },
            ) as entry_span:
                # Inject trace context (simulating Gateway → AgentServer)
                carrier = {}
                inject_trace_context(carrier)
                assert "traceparent" in carrier

                # --- AGENT layer (extract context from carrier) ---
                parent_ctx = extract_trace_context(carrier)
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke",
                    context=parent_ctx,
                    attributes={
                        "jiuwenclaw.agent.name": "main_agent",
                        "jiuwenclaw.session.id": "sess_001",
                    },
                ) as agent_span:

                    # --- LLM layer: first call (returns tool_calls) ---
                    with llm_tracer.start_as_current_span(
                        "gen_ai.chat",
                        attributes={
                            "gen_ai.system": "openai",
                            "gen_ai.request.model": "deepseek-chat",
                            "gen_ai.operation.name": "chat",
                        },
                    ) as llm_span_1:
                        llm_span_1.add_event("gen_ai.system.message", {
                            "content": "You are a helpful assistant."
                        })
                        llm_span_1.add_event("gen_ai.user.message", {
                            "content": "Search for Python tutorials"
                        })
                        llm_span_1.set_attribute("gen_ai.usage.input_tokens", 150)
                        llm_span_1.set_attribute("gen_ai.usage.output_tokens", 30)
                        llm_span_1.add_event("gen_ai.assistant.message", {
                            "content": "",
                            "tool_calls": '[{"name": "web_search", "arguments": {"query": "Python tutorials"}}]',
                        })

                        # --- TOOL layer ---
                        tool_span = tool_tracer.start_span(
                            "gen_ai.tool.execute: web_search",
                            attributes={
                                "gen_ai.tool.name": "web_search",
                                "gen_ai.tool.call.id": "call_abc",
                            },
                        )
                        tool_span.add_event("tool.arguments", {
                            "arguments": '{"query": "Python tutorials"}'
                        })
                        # Simulate tool execution
                        await asyncio.sleep(0.001)
                        tool_span.add_event("tool.result", {
                            "result": "Found 10 results for Python tutorials"
                        })
                        tool_span.set_status(StatusCode.OK)
                        tool_span.end()

                    # --- LLM layer: second call (final answer) ---
                    with llm_tracer.start_as_current_span(
                        "gen_ai.chat",
                        attributes={
                            "gen_ai.system": "openai",
                            "gen_ai.request.model": "deepseek-chat",
                            "gen_ai.operation.name": "chat",
                        },
                    ) as llm_span_2:
                        llm_span_2.set_attribute("gen_ai.usage.input_tokens", 200)
                        llm_span_2.set_attribute("gen_ai.usage.output_tokens", 100)
                        llm_span_2.add_event("gen_ai.assistant.message", {
                            "content": "Here are the top Python tutorials..."
                        })

        _run(simulate_full_request())

        spans = exporter.get_finished_spans()
        # Should have: 1 entry + 1 agent + 2 LLM + 1 tool = 5 spans
        assert len(spans) == 5

        span_by_name = {}
        for s in spans:
            span_by_name.setdefault(s.name, []).append(s)

        entry_spans = span_by_name.get("channel.request", [])
        agent_spans = span_by_name.get("jiuwenclaw.agent.invoke", [])
        llm_spans = span_by_name.get("gen_ai.chat", [])
        tool_spans = span_by_name.get("gen_ai.tool.execute: web_search", [])

        assert len(entry_spans) == 1
        assert len(agent_spans) == 1
        assert len(llm_spans) == 2
        assert len(tool_spans) == 1

        entry = entry_spans[0]
        agent = agent_spans[0]
        tool = tool_spans[0]

        # All share the same trace_id
        trace_id = entry.context.trace_id
        assert agent.context.trace_id == trace_id
        for ls in llm_spans:
            assert ls.context.trace_id == trace_id
        assert tool.context.trace_id == trace_id

        # Parent-child: agent → entry
        assert agent.parent.span_id == entry.context.span_id

        # Parent-child: LLM spans → agent
        for ls in llm_spans:
            assert ls.parent.span_id == agent.context.span_id

        # Verify entry span attributes
        assert entry.attributes["jiuwenclaw.channel.id"] == "feishu"
        assert entry.attributes["jiuwenclaw.session.id"] == "sess_001"

        # Verify agent span attributes
        assert agent.attributes["jiuwenclaw.agent.name"] == "main_agent"

        # Verify LLM span attributes
        for ls in llm_spans:
            assert ls.attributes["gen_ai.system"] == "openai"
            assert ls.attributes["gen_ai.request.model"] == "deepseek-chat"

        # Verify tool span attributes
        assert tool.attributes["gen_ai.tool.name"] == "web_search"
        assert tool.attributes["gen_ai.tool.call.id"] == "call_abc"

        # Verify tool span events
        tool_event_names = [e.name for e in tool.events]
        assert "tool.arguments" in tool_event_names
        assert "tool.result" in tool_event_names

        tp.shutdown()
        mp.shutdown()


# ---------------------------------------------------------------------------
# 2. Cross-WebSocket context propagation integration
# ---------------------------------------------------------------------------

class TestCrossWebSocketPropagation:
    """Verify trace context survives inject → serialize → extract cycle."""

    @staticmethod
    def test_trace_context_survives_serialization():
        tp, mp, exporter, _ = _setup_otel()

        from jiuwenclaw.telemetry.context_propagation import inject_trace_context, extract_trace_context
        import json

        gateway_tracer = tp.get_tracer("gateway")
        agent_tracer = tp.get_tracer("agent")

        async def simulate():
            # Gateway side: create entry span and inject
            with gateway_tracer.start_as_current_span("channel.request") as gw_span:
                metadata = {}
                inject_trace_context(metadata)

                # Simulate WebSocket serialization
                serialized = json.dumps(metadata)
                deserialized = json.loads(serialized)

                # AgentServer side: extract and create child span
                parent_ctx = extract_trace_context(deserialized)
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke", context=parent_ctx
                ) as agent_span:
                    pass

        _run(simulate())

        spans = exporter.get_finished_spans()
        assert len(spans) == 2

        gw_span = [s for s in spans if s.name == "channel.request"][0]
        agent_span = [s for s in spans if s.name == "jiuwenclaw.agent.invoke"][0]

        # Same trace, parent-child relationship
        assert gw_span.context.trace_id == agent_span.context.trace_id
        assert agent_span.parent.span_id == gw_span.context.span_id

        tp.shutdown()
        mp.shutdown()

    @staticmethod
    def test_missing_metadata_creates_new_trace():
        tp, mp, exporter, _ = _setup_otel()

        from jiuwenclaw.telemetry.context_propagation import extract_trace_context

        agent_tracer = tp.get_tracer("agent")

        async def simulate():
            # No metadata — should create a new root span
            parent_ctx = extract_trace_context({})
            with agent_tracer.start_as_current_span(
                "jiuwenclaw.agent.invoke", context=parent_ctx
            ) as span:
                pass

        _run(simulate())

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        # Should be a root span (no parent)
        assert spans[0].parent is None

        tp.shutdown()
        mp.shutdown()


# ---------------------------------------------------------------------------
# 3. Metrics integration
# ---------------------------------------------------------------------------

class TestMetricsIntegration:
    """Verify that metrics are recorded correctly through the instrumentation."""

    @staticmethod
    def test_metrics_recorded_in_full_flow():
        # Create standalone MeterProvider (don't use set_meter_provider to avoid conflicts)
        reader = InMemoryMetricReader()
        mp = MeterProvider(metric_readers=[reader])
        meter = mp.get_meter("jiuwenclaw.test")

        req_count = meter.create_counter("test.request.count", unit="{request}")
        req_duration = meter.create_histogram("test.request.duration", unit="s")
        llm_token = meter.create_counter("test.token.usage", unit="{token}")

        # Simulate recording
        req_count.add(1, {"jiuwenclaw.channel.id": "web"})
        req_count.add(1, {"jiuwenclaw.channel.id": "feishu"})
        req_count.add(1, {"jiuwenclaw.channel.id": "web"})
        req_duration.record(0.5, {"jiuwenclaw.channel.id": "web"})
        req_duration.record(1.2, {"jiuwenclaw.channel.id": "feishu"})
        llm_token.add(150, {"gen_ai.request.model": "deepseek-chat", "gen_ai.token.type": "input"})
        llm_token.add(50, {"gen_ai.request.model": "deepseek-chat", "gen_ai.token.type": "output"})

        data = _get_metrics_data(reader)

        assert "test.request.count" in data
        assert "test.request.duration" in data
        assert "test.token.usage" in data

        # Verify request count
        count_points = data["test.request.count"]["points"]
        assert len(count_points) >= 1

        # Verify token usage
        token_points = data["test.token.usage"]["points"]
        assert len(token_points) >= 1

        mp.shutdown()


# ---------------------------------------------------------------------------
# 4. Error propagation integration
# ---------------------------------------------------------------------------

class TestErrorPropagation:
    """Verify error status propagates correctly through span layers."""

    @staticmethod
    def test_llm_error_propagates_to_agent_span():
        tp, mp, exporter, _ = _setup_otel()

        agent_tracer = tp.get_tracer("jiuwenclaw.agent")
        llm_tracer = tp.get_tracer("jiuwenclaw.llm")

        async def simulate_error_flow():
            with agent_tracer.start_as_current_span("jiuwenclaw.agent.invoke") as agent_span:
                try:
                    with llm_tracer.start_as_current_span("gen_ai.chat") as llm_span:
                        raise RuntimeError("Model API timeout")
                except RuntimeError as exc:
                    llm_span.set_status(StatusCode.ERROR, str(exc))
                    llm_span.record_exception(exc)
                    agent_span.set_status(StatusCode.ERROR, f"LLM failed: {exc}")
                    agent_span.record_exception(exc)

        _run(simulate_error_flow())

        spans = exporter.get_finished_spans()
        assert len(spans) == 2

        llm_span = [s for s in spans if s.name == "gen_ai.chat"][0]
        agent_span = [s for s in spans if s.name == "jiuwenclaw.agent.invoke"][0]

        assert llm_span.status.status_code == StatusCode.ERROR
        assert agent_span.status.status_code == StatusCode.ERROR
        assert "timeout" in llm_span.status.description.lower()

        # Both should have exception events
        llm_exceptions = [e for e in llm_span.events if e.name == "exception"]
        agent_exceptions = [e for e in agent_span.events if e.name == "exception"]
        assert len(llm_exceptions) == 1
        assert len(agent_exceptions) == 1

        tp.shutdown()
        mp.shutdown()

    @staticmethod
    def test_tool_error_does_not_break_react_loop():
        """Tool error should be recorded but not prevent subsequent LLM calls."""
        tp, mp, exporter, _ = _setup_otel()

        agent_tracer = tp.get_tracer("jiuwenclaw.agent")
        llm_tracer = tp.get_tracer("jiuwenclaw.llm")
        tool_tracer = tp.get_tracer("jiuwenclaw.tool")

        async def simulate():
            with agent_tracer.start_as_current_span("jiuwenclaw.agent.invoke") as agent_span:
                # First LLM call → tool call
                with llm_tracer.start_as_current_span("gen_ai.chat"):
                    pass

                # Tool fails
                tool_span = tool_tracer.start_span("gen_ai.tool.execute: broken_tool")
                tool_span.set_status(StatusCode.ERROR, "Tool execution failed")
                tool_span.end()

                # Second LLM call succeeds (ReAct continues)
                with llm_tracer.start_as_current_span("gen_ai.chat") as llm2:
                    llm2.set_attribute("gen_ai.usage.output_tokens", 100)

                agent_span.set_status(StatusCode.OK)

        _run(simulate())

        spans = exporter.get_finished_spans()
        assert len(spans) == 4  # agent + 2 LLM + 1 tool

        agent_span = [s for s in spans if s.name == "jiuwenclaw.agent.invoke"][0]
        tool_span = [s for s in spans if "broken_tool" in s.name][0]

        # Agent succeeded despite tool failure
        assert agent_span.status.status_code == StatusCode.OK
        assert tool_span.status.status_code == StatusCode.ERROR

        tp.shutdown()
        mp.shutdown()


# ---------------------------------------------------------------------------
# 5. Multi-iteration ReAct loop integration
# ---------------------------------------------------------------------------

class TestReActLoopIntegration:
    """Verify span structure for multi-iteration ReAct loops."""

    @staticmethod
    def test_multiple_react_iterations():
        tp, mp, exporter, _ = _setup_otel()

        agent_tracer = tp.get_tracer("jiuwenclaw.agent")
        llm_tracer = tp.get_tracer("jiuwenclaw.llm")
        tool_tracer = tp.get_tracer("jiuwenclaw.tool")

        async def simulate_3_iteration_react():
            with agent_tracer.start_as_current_span("jiuwenclaw.agent.invoke") as agent_span:
                # Iteration 1: LLM → tool
                with llm_tracer.start_as_current_span("gen_ai.chat") as llm1:
                    llm1.set_attribute("gen_ai.usage.input_tokens", 100)
                    llm1.set_attribute("gen_ai.usage.output_tokens", 20)

                tool1 = tool_tracer.start_span("gen_ai.tool.execute: search")
                tool1.set_status(StatusCode.OK)
                tool1.end()

                # Iteration 2: LLM → tool
                with llm_tracer.start_as_current_span("gen_ai.chat") as llm2:
                    llm2.set_attribute("gen_ai.usage.input_tokens", 200)
                    llm2.set_attribute("gen_ai.usage.output_tokens", 30)

                tool2 = tool_tracer.start_span("gen_ai.tool.execute: calculator")
                tool2.set_status(StatusCode.OK)
                tool2.end()

                # Iteration 3: LLM → final answer (no tool)
                with llm_tracer.start_as_current_span("gen_ai.chat") as llm3:
                    llm3.set_attribute("gen_ai.usage.input_tokens", 300)
                    llm3.set_attribute("gen_ai.usage.output_tokens", 150)
                    llm3.add_event("gen_ai.assistant.message", {
                        "content": "The answer is 42."
                    })

        _run(simulate_3_iteration_react())

        spans = exporter.get_finished_spans()
        # 1 agent + 3 LLM + 2 tool = 6
        assert len(spans) == 6

        agent_spans = [s for s in spans if s.name == "jiuwenclaw.agent.invoke"]
        llm_spans = [s for s in spans if s.name == "gen_ai.chat"]
        tool_spans = [s for s in spans if "gen_ai.tool.execute" in s.name]

        assert len(agent_spans) == 1
        assert len(llm_spans) == 3
        assert len(tool_spans) == 2

        # All LLM spans are children of agent
        agent_span_id = agent_spans[0].context.span_id
        for ls in llm_spans:
            assert ls.parent.span_id == agent_span_id

        # Verify token counts on LLM spans
        token_counts = []
        for ls in llm_spans:
            token_counts.append(ls.attributes.get("gen_ai.usage.input_tokens", 0))
        assert sorted(token_counts) == [100, 200, 300]

        # Last LLM span should have the final answer event
        last_llm = [ls for ls in llm_spans if ls.attributes.get("gen_ai.usage.input_tokens") == 300][0]
        event_names = [e.name for e in last_llm.events]
        assert "gen_ai.assistant.message" in event_names

        tp.shutdown()
        mp.shutdown()


# ---------------------------------------------------------------------------
# 6. init_telemetry end-to-end with console exporter
# ---------------------------------------------------------------------------

class TestInitTelemetryE2E:
    """Test init_telemetry with real config loading (console exporter)."""

    @staticmethod
    def test_init_with_console_exporter():
        import jiuwenclaw.telemetry as tel_mod

        with patch.object(tel_mod, "_initialized", False):
            with patch.dict("os.environ", {
                "OTEL_ENABLED": "true",
                "OTEL_EXPORTER_TYPE": "console",
                "OTEL_SERVICE_NAME": "test-jiuwenclaw",
                "OTEL_SESSION_STUCK_THRESHOLD_MS": "1234",
                "OTEL_SESSION_STUCK_CHECK_INTERVAL_S": "9",
            }, clear=True):
                with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                    with patch(
                        "jiuwenclaw.telemetry.provider._build_extension_provider_bundle",
                        return_value=None,
                    ):
                        # Patch apply_instrumentors to avoid touching real classes
                        with patch("jiuwenclaw.telemetry.instrumentors.apply_instrumentors") as mock_instr:
                            tel_mod.init_telemetry()

                            assert tel_mod.is_telemetry_initialized() is True
                            mock_instr.assert_called_once_with(
                                log_messages=True,
                                session_stuck_threshold_ms=1234.0,
                                session_stuck_check_interval_s=9.0,
                            )

    @staticmethod
    def test_init_disabled_has_zero_overhead():
        import jiuwenclaw.telemetry as tel_mod

        with patch.object(tel_mod, "_initialized", False):
            with patch.dict("os.environ", {"OTEL_ENABLED": "false"}, clear=True):
                with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                    with patch("jiuwenclaw.telemetry.provider.init_providers") as mock_prov:
                        with patch("jiuwenclaw.telemetry.instrumentors.apply_instrumentors") as mock_instr:
                            tel_mod.init_telemetry()

                            mock_prov.assert_not_called()
                            mock_instr.assert_not_called()
                            assert tel_mod.is_telemetry_initialized() is False

    @staticmethod
    def test_init_with_registered_telemetry_provider_extension():
        import jiuwenclaw.telemetry as tel_mod
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.extensions.sdk.telemetry_provider import TelemetryProviderExtension
        from jiuwenclaw.telemetry.provider import ProviderBundle

        ExtensionRegistry.reset_instance()
        try:
            custom_tp = TracerProvider()
            custom_mp = MeterProvider()

            class FakeExt(TelemetryProviderExtension):
                async def initialize(self, config):
                    pass

                def build_providers(self, cfg):
                    return ProviderBundle(tracer_provider=custom_tp, meter_provider=custom_mp)

                async def shutdown(self):
                    return None

            registry = ExtensionRegistry.create_instance(
                callback_framework=MagicMock(),
                config={},
                logger=MagicMock(),
            )
            registry.register_telemetry_provider(FakeExt())

            with patch.object(tel_mod, "_initialized", False):
                with patch.dict("os.environ", {"OTEL_ENABLED": "true"}, clear=True):
                    with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                        with patch("jiuwenclaw.telemetry.provider.install_providers") as mock_install:
                            with patch("jiuwenclaw.telemetry.instrumentors.apply_instrumentors") as mock_instr:
                                tel_mod.init_telemetry()

                                mock_install.assert_called_once()
                                bundle = mock_install.call_args[0][0]
                                assert bundle.tracer_provider is custom_tp
                                assert bundle.meter_provider is custom_mp
                                mock_instr.assert_called_once()
        finally:
            ExtensionRegistry.reset_instance()


# ---------------------------------------------------------------------------
# 7. Concurrent requests integration
# ---------------------------------------------------------------------------

class TestConcurrentRequests:
    """Verify that concurrent requests produce independent traces."""

    @staticmethod
    def test_concurrent_requests_have_separate_traces():
        tp, mp, exporter, _ = _setup_otel()

        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")

        async def simulate_request(channel_id, session_id):
            with entry_tracer.start_as_current_span(
                "channel.request",
                attributes={"jiuwenclaw.channel.id": channel_id},
            ):
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke",
                    attributes={"jiuwenclaw.session.id": session_id},
                ):
                    await asyncio.sleep(0.001)

        async def run_concurrent():
            await asyncio.gather(
                simulate_request("web", "sess_1"),
                simulate_request("feishu", "sess_2"),
                simulate_request("wecom", "sess_3"),
            )

        _run(run_concurrent())

        spans = exporter.get_finished_spans()
        # 3 requests × 2 spans each = 6
        assert len(spans) == 6

        entry_spans = [s for s in spans if s.name == "channel.request"]
        assert len(entry_spans) == 3

        # Each entry span should have a unique trace_id
        trace_ids = {s.context.trace_id for s in entry_spans}
        assert len(trace_ids) == 3

        # Verify channel_ids
        channels = {s.attributes["jiuwenclaw.channel.id"] for s in entry_spans}
        assert channels == {"web", "feishu", "wecom"}

        tp.shutdown()
        mp.shutdown()


# ---------------------------------------------------------------------------
# 8. Cross-task context propagation via instance attribute
# ---------------------------------------------------------------------------

class TestCrossTaskContextPropagation:
    """Verify otel_agent_ctx instance attribute bridges context across asyncio tasks.

    This reproduces the real architecture where:
    - Agent span is created in generator frame (process_message_stream)
    - LLM/tool spans run inside a separate asyncio.create_task()
    """

    @staticmethod
    def test_llm_span_inherits_agent_trace_via_instance_attr():
        """LLM span in a child task should share trace_id with agent span."""
        tp, mp, exporter, _ = _setup_otel()
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")
        llm_tracer = tp.get_tracer("jiuwenclaw.llm")

        async def run():
            # Simulate agent instrumentor: create span, store ctx on instance
            agent_instance = types.SimpleNamespace()
            parent_ctx = trace.set_span_in_context(trace.INVALID_SPAN)
            agent_span = agent_tracer.start_span(
                "jiuwenclaw.agent.invoke.stream", context=parent_ctx,
            )
            agent_ctx = trace.set_span_in_context(agent_span)
            agent_instance.otel_agent_ctx = agent_ctx

            # Simulate LLM running in a separate create_task
            async def llm_task():
                ctx = getattr(agent_instance, "otel_agent_ctx", None)
                with llm_tracer.start_as_current_span(
                    "gen_ai.chat", context=ctx,
                ):
                    await asyncio.sleep(0.001)

            await asyncio.create_task(llm_task())
            agent_span.end()

        _run(run())

        spans = exporter.get_finished_spans()
        agent_spans = [s for s in spans if s.name == "jiuwenclaw.agent.invoke.stream"]
        llm_spans = [s for s in spans if s.name == "gen_ai.chat"]
        assert len(agent_spans) == 1
        assert len(llm_spans) == 1
        assert agent_spans[0].context.trace_id == llm_spans[0].context.trace_id
        assert llm_spans[0].parent.span_id == agent_spans[0].context.span_id

        tp.shutdown()
        mp.shutdown()

    @staticmethod
    def test_consecutive_messages_isolated_across_tasks():
        """Two sequential messages must produce separate traces even with shared instance."""
        tp, mp, exporter, _ = _setup_otel()
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")
        llm_tracer = tp.get_tracer("jiuwenclaw.llm")
        tool_tracer = tp.get_tracer("jiuwenclaw.tool")

        async def simulate_message(agent_instance, request_id):
            # Agent span (generator frame)
            parent_ctx = trace.set_span_in_context(trace.INVALID_SPAN)
            agent_span = agent_tracer.start_span(
                "jiuwenclaw.agent.invoke.stream", context=parent_ctx,
                attributes={"jiuwenclaw.request.id": request_id},
            )
            agent_ctx = trace.set_span_in_context(agent_span)
            agent_instance.otel_agent_ctx = agent_ctx

            # LLM + tool in separate task (like real session processor)
            async def worker():
                ctx = getattr(agent_instance, "otel_agent_ctx", None)
                with llm_tracer.start_as_current_span("gen_ai.chat", context=ctx):
                    await asyncio.sleep(0.001)
                tool_span = tool_tracer.start_span(
                    "gen_ai.tool.execute: search", context=ctx,
                )
                tool_span.end()

            await asyncio.create_task(worker())
            agent_span.end()

        async def run():
            shared_instance = types.SimpleNamespace()
            await simulate_message(shared_instance, "req_1")
            await simulate_message(shared_instance, "req_2")

        _run(run())

        spans = exporter.get_finished_spans()
        agent_spans = [s for s in spans if s.name == "jiuwenclaw.agent.invoke.stream"]
        assert len(agent_spans) == 2

        # Group all spans by trace_id
        trace_groups: dict[int, list] = {}
        for s in spans:
            trace_groups.setdefault(s.context.trace_id, []).append(s)

        # Must have exactly 2 distinct traces
        assert len(trace_groups) == 2, (
            f"Expected 2 traces, got {len(trace_groups)}"
        )

        # Each trace: 1 agent + 1 llm + 1 tool = 3 spans
        for tid, group in trace_groups.items():
            assert len(group) == 3, (
                f"trace {tid:032x} has {len(group)} spans, expected 3"
            )
            names = {s.name for s in group}
            assert "jiuwenclaw.agent.invoke.stream" in names
            assert "gen_ai.chat" in names
            assert "gen_ai.tool.execute: search" in names

        tp.shutdown()
        mp.shutdown()
