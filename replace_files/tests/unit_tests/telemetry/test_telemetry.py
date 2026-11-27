# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenclaw.telemetry module.

Strategy: Instead of patching lazy imports inside instrumentor functions,
we directly test the tracing logic by calling the wrapped functions with
mocked dependencies, bypassing instrument_*() which requires real classes.
"""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

# Prevent fastmcp import from failing when rich version is too old.
# The import chain: agent_client_proxy → gateway/__init__ → agentserver → fastmcp
if "fastmcp" not in sys.modules:
    _fake_fastmcp = types.ModuleType("fastmcp")
    _fake_fastmcp.FastMCP = MagicMock  # type: ignore[attr-defined]
    _fake_fastmcp.Context = MagicMock  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fake_fastmcp

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
from opentelemetry.trace import SpanKind, StatusCode


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

def _make_otel_providers():
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader])
    return tp, mp, exporter, reader


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. Config tests
# ---------------------------------------------------------------------------

class TestTelemetryConfig:
    @staticmethod
    def test_default_config():
        with patch.dict("os.environ", {}, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                from jiuwenclaw.telemetry.config import load_telemetry_config
                cfg = load_telemetry_config()
                assert cfg.enabled is False
                assert cfg.exporter == "none"
                assert cfg.headers == {}
                assert cfg.protocol == "grpc"
                assert cfg.traces_exporter == "none"
                assert cfg.metrics_exporter == "none"
                assert cfg.traces_endpoint == "http://localhost:4317"
                assert cfg.metrics_endpoint == "http://localhost:4317"
                assert cfg.log_messages is True
                assert cfg.service_name == "jiuwenclaw"
                assert cfg.claw_id is None

    @staticmethod
    def test_env_vars_override():
        env = {
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_TYPE": "console",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://custom:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer common",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://traces:4318",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "Authorization=Bearer trace",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOG_MESSAGES": "false",
            "OTEL_SERVICE_NAME": "test-service",
            "OTEL_CLAW_ID": "gateway-sh-01",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                from jiuwenclaw.telemetry.config import load_telemetry_config
                cfg = load_telemetry_config()
                assert cfg.enabled is True
                assert cfg.exporter == "console"
                assert cfg.endpoint == "http://custom:4317"
                assert cfg.protocol == "http"
                assert cfg.headers == {"Authorization": "Bearer common"}
                assert cfg.traces_exporter == "otlp"
                assert cfg.traces_endpoint == "http://traces:4318"
                assert cfg.traces_protocol == "http"
                assert cfg.traces_headers == {"Authorization": "Bearer trace"}
                assert cfg.metrics_exporter == "none"
                assert cfg.metrics_endpoint == "http://custom:4317"
                assert cfg.log_messages is False
                assert cfg.service_name == "test-service"
                assert cfg.claw_id == "gateway-sh-01"

    @staticmethod
    def test_yaml_config_fallback():
        yaml_cfg = {
            "telemetry": {
                "enabled": True,
                "exporter": "console",
                "endpoint": "http://yaml:4317",
                "protocol": "http",
                "headers": {"Authorization": "Bearer yaml-common"},
                "log_messages": False,
                "service_name": "yaml-service",
                "claw_id": "agentserver-sh-01",
                "traces": {
                    "exporter": "otlp",
                    "endpoint": "http://trace-yaml:4318",
                    "headers": {"Authorization": "Bearer trace-yaml"},
                },
                "metrics": {
                    "exporter": "none",
                },
            }
        }
        with patch.dict("os.environ", {}, clear=True):
            with patch("jiuwenclaw.config.get_config", return_value=yaml_cfg):
                from jiuwenclaw.telemetry.config import load_telemetry_config
                cfg = load_telemetry_config()
                assert cfg.enabled is True
                assert cfg.exporter == "console"
                assert cfg.endpoint == "http://yaml:4317"
                assert cfg.headers == {"Authorization": "Bearer yaml-common"}
                assert cfg.traces_exporter == "otlp"
                assert cfg.traces_endpoint == "http://trace-yaml:4318"
                assert cfg.traces_protocol == "http"
                assert cfg.traces_headers == {"Authorization": "Bearer trace-yaml"}
                assert cfg.metrics_exporter == "none"
                assert cfg.metrics_endpoint == "http://yaml:4317"
                assert cfg.service_name == "yaml-service"
                assert cfg.claw_id == "agentserver-sh-01"

    @staticmethod
    def test_empty_claw_id_env_disables_yaml_fallback():
        yaml_cfg = {
            "telemetry": {
                "claw_id": "yaml-claw",
            }
        }
        env = {
            "OTEL_CLAW_ID": "   ",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("jiuwenclaw.config.get_config", return_value=yaml_cfg):
                from jiuwenclaw.telemetry.config import load_telemetry_config

                cfg = load_telemetry_config()

                assert cfg.claw_id is None

    @staticmethod
    def test_signal_specific_env_overrides_common_headers():
        env = {
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer common, X-Scope-OrgID=global",
            "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "Authorization=Bearer metrics",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                from jiuwenclaw.telemetry.config import load_telemetry_config
                cfg = load_telemetry_config()
                assert cfg.traces_headers == {
                    "Authorization": "Bearer common",
                    "X-Scope-OrgID": "global",
                }
                assert cfg.metrics_headers == {"Authorization": "Bearer metrics"}

    @staticmethod
    def test_session_config_values_are_normalized_to_float():
        yaml_cfg = {
            "telemetry": {
                "session": {
                    "stuck_threshold_ms": 1234,
                    "stuck_check_interval_s": "9.5",
                }
            }
        }
        with patch.dict("os.environ", {}, clear=True):
            with patch("jiuwenclaw.config.get_config", return_value=yaml_cfg):
                from jiuwenclaw.telemetry.config import load_telemetry_config

                cfg = load_telemetry_config()

                assert cfg.session_stuck_threshold_ms == 1234.0
                assert isinstance(cfg.session_stuck_threshold_ms, float)
                assert cfg.session_stuck_check_interval_s == 9.5
                assert isinstance(cfg.session_stuck_check_interval_s, float)

    @staticmethod
    def test_invalid_session_config_values_fall_back_to_float_defaults():
        yaml_cfg = {
            "telemetry": {
                "session": {
                    "stuck_threshold_ms": "bad-threshold",
                    "stuck_check_interval_s": None,
                }
            }
        }
        env = {
            "OTEL_SESSION_STUCK_THRESHOLD_MS": "bad-env-threshold",
            "OTEL_SESSION_STUCK_CHECK_INTERVAL_S": "bad-env-interval",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("jiuwenclaw.config.get_config", return_value=yaml_cfg):
                from jiuwenclaw.telemetry.config import load_telemetry_config

                cfg = load_telemetry_config()

                assert cfg.session_stuck_threshold_ms == 300000.0
                assert isinstance(cfg.session_stuck_threshold_ms, float)
                assert cfg.session_stuck_check_interval_s == 30.0
                assert isinstance(cfg.session_stuck_check_interval_s, float)

    @staticmethod
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("1234", 1234.0),
            ("ab", 300000.0),
            ("", 300000.0),
        ],
    )
    def test_session_stuck_threshold_env_value_normalization(env_value, expected):
        env = {"OTEL_SESSION_STUCK_THRESHOLD_MS": env_value}
        with patch.dict("os.environ", env, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                from jiuwenclaw.telemetry.config import load_telemetry_config

                cfg = load_telemetry_config()

                assert cfg.session_stuck_threshold_ms == expected
                assert isinstance(cfg.session_stuck_threshold_ms, float)


# ---------------------------------------------------------------------------
# 2. Attributes tests
# ---------------------------------------------------------------------------


class TestAttributes:
    @staticmethod
    def test_genai_attributes_defined():
        from jiuwenclaw.telemetry.attributes import (
            GEN_AI_SYSTEM, GEN_AI_REQUEST_MODEL,
            GEN_AI_USAGE_INPUT_TOKENS, GEN_AI_USAGE_OUTPUT_TOKENS,
            GEN_AI_TOOL_NAME, JIUWENCLAW_CHANNEL_ID, JIUWENCLAW_CLAW_ID, JIUWENCLAW_SESSION_ID,
        )
        assert GEN_AI_SYSTEM == "gen_ai.system"
        assert GEN_AI_REQUEST_MODEL == "gen_ai.request.model"
        assert GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
        assert GEN_AI_USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
        assert GEN_AI_TOOL_NAME == "gen_ai.tool.name"
        assert JIUWENCLAW_CLAW_ID == "jiuwenclaw.claw.id"
        assert JIUWENCLAW_CHANNEL_ID == "jiuwenclaw.channel.id"
        assert JIUWENCLAW_SESSION_ID == "jiuwenclaw.session.id"


# ---------------------------------------------------------------------------
# 3. Context propagation tests
# ---------------------------------------------------------------------------


class TestContextPropagation:
    @staticmethod
    def test_inject_and_extract_roundtrip():
        tp, _, exporter, _ = _make_otel_providers()
        trace.set_tracer_provider(tp)

        from jiuwenclaw.telemetry.context_propagation import (
            inject_trace_context, extract_trace_context,
        )

        tracer = tp.get_tracer("test")
        with tracer.start_as_current_span("parent") as parent_span:
            carrier = {}
            inject_trace_context(carrier)
            assert "traceparent" in carrier

            ctx = extract_trace_context(carrier)
            with tracer.start_as_current_span("child", context=ctx) as child_span:
                assert parent_span.get_span_context().trace_id == child_span.get_span_context().trace_id
        tp.shutdown()

    @staticmethod
    def test_extract_empty_carrier():
        from jiuwenclaw.telemetry.context_propagation import extract_trace_context
        assert extract_trace_context(None) is not None
        assert extract_trace_context({}) is not None


# ---------------------------------------------------------------------------
# 4. Entry instrumentor — direct function test
# ---------------------------------------------------------------------------

class TestEntryInstrumentor:
    @staticmethod
    def test_process_stream_creates_entry_span():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.entry as entry_mod
        entry_mod._tracer = tp.get_tracer("jiuwenclaw.entry")

        mock_req = MagicMock()
        mock_req.channel_id = "web"
        mock_req.session_id = "sess_123"
        mock_req.request_id = "req_456"
        mock_req.metadata = {}

        original_fn = AsyncMock()

        async def traced_process_stream(self_handler, req, session_id):
            with entry_mod._tracer.start_as_current_span(
                "channel.request",
                attributes={
                    "jiuwenclaw.channel.id": req.channel_id or "",
                    "jiuwenclaw.session.id": session_id or "",
                    "jiuwenclaw.request.id": req.request_id or "",
                },
            ) as span:
                await original_fn(self_handler, req, session_id)

        _run(traced_process_stream(MagicMock(), mock_req, "sess_123"))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "channel.request"
        assert span.attributes["jiuwenclaw.channel.id"] == "web"
        assert span.attributes["jiuwenclaw.session.id"] == "sess_123"
        assert span.attributes["jiuwenclaw.request.id"] == "req_456"
        tp.shutdown()


# ---------------------------------------------------------------------------
# 5. Agent instrumentor — direct function test
# ---------------------------------------------------------------------------

class TestAgentInstrumentor:
    @staticmethod
    def test_process_message_creates_agent_span():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.agent as agent_mod
        agent_mod._tracer = tp.get_tracer("jiuwenclaw.agent")

        mock_request = MagicMock()
        mock_request.channel_id = "feishu"
        mock_request.session_id = "sess_abc"
        mock_request.request_id = "req_def"
        mock_request.metadata = {}

        mock_instance = MagicMock()
        mock_instance._agent_name = "test_agent"

        original_fn = AsyncMock(return_value={"output": "ok"})

        async def traced_process_message(self_agent, request):
            from jiuwenclaw.telemetry.context_propagation import extract_trace_context
            parent_ctx = extract_trace_context(request.metadata)
            with agent_mod._tracer.start_as_current_span(
                "jiuwenclaw.agent.invoke",
                context=parent_ctx,
                attributes={
                    "jiuwenclaw.agent.name": getattr(self_agent, "_agent_name", ""),
                    "jiuwenclaw.session.id": request.session_id or "",
                    "jiuwenclaw.channel.id": request.channel_id or "",
                    "jiuwenclaw.request.id": request.request_id or "",
                },
            ):
                return await original_fn(self_agent, request)

        result = _run(traced_process_message(mock_instance, mock_request))
        assert result == {"output": "ok"}

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "jiuwenclaw.agent.invoke"
        assert span.attributes["jiuwenclaw.agent.name"] == "test_agent"
        assert span.attributes["jiuwenclaw.session.id"] == "sess_abc"
        assert span.attributes["jiuwenclaw.channel.id"] == "feishu"
        tp.shutdown()

    @staticmethod
    def test_instrument_agent_marks_unary_and_stream_spans_as_server():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.agent as agent_mod

        fake_agentserver_pkg = types.ModuleType("jiuwenclaw.agentserver")
        fake_agentserver_pkg.__path__ = []
        fake_interface_module = types.ModuleType("jiuwenclaw.agentserver.interface")

        class JiuWenClaw:
            def __init__(self):
                self._agent_name = "test_agent"
                self._instance = types.SimpleNamespace()

            async def process_message(self, request):
                return {"request_id": request.request_id}

            async def process_message_stream(self, request):
                yield {"request_id": request.request_id}

        fake_interface_module.JiuWenClaw = JiuWenClaw
        fake_agentserver_pkg.interface = fake_interface_module

        original_tracer = agent_mod._tracer
        try:
            agent_mod._tracer = tp.get_tracer("jiuwenclaw.agent")
            with patch.dict(
                sys.modules,
                {
                    "jiuwenclaw.agentserver": fake_agentserver_pkg,
                    "jiuwenclaw.agentserver.interface": fake_interface_module,
                },
            ):
                agent_mod.instrument_agent()

                request = types.SimpleNamespace(
                    channel_id="web",
                    session_id="sess_server_kind",
                    request_id="req_server_kind",
                    metadata={},
                )
                server = JiuWenClaw()

                unary_result = _run(server.process_message(request))
                assert unary_result == {"request_id": "req_server_kind"}

                async def consume_stream():
                    chunks = []
                    async for chunk in server.process_message_stream(request):
                        chunks.append(chunk)
                    return chunks

                stream_chunks = _run(consume_stream())
                assert stream_chunks == [{"request_id": "req_server_kind"}]

            spans = exporter.get_finished_spans()
            unary_span = next(s for s in spans if s.name == "jiuwenclaw.agent.invoke")
            stream_span = next(s for s in spans if s.name == "jiuwenclaw.agent.invoke.stream")
            assert unary_span.kind == SpanKind.SERVER
            assert stream_span.kind == SpanKind.SERVER
        finally:
            agent_mod._tracer = original_tracer
            tp.shutdown()


# ---------------------------------------------------------------------------
# 5b. Gateway AgentServerClient proxy
# ---------------------------------------------------------------------------

class TestGatewayAgentClientProxy:
    @staticmethod
    def test_wrap_agent_client_returns_original_when_telemetry_disabled():
        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod

        original_initialized = tel_mod._initialized
        try:
            tel_mod._initialized = False
            client = MagicMock()
            assert proxy_mod.wrap_agent_client(client) is client
        finally:
            tel_mod._initialized = original_initialized

    @staticmethod
    def test_wrap_agent_client_is_idempotent():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                return AgentResponse(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    ok=True,
                    payload={"content": "ok"},
                )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            wrapped = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            wrapped_again = proxy_mod.wrap_agent_client(wrapped, target_uri="ws://127.0.0.1:18092/ws")

            assert wrapped_again is wrapped

            envelope = E2AEnvelope(
                request_id="req_proxy_idempotent",
                channel="web",
                session_id="sess_proxy_idempotent",
                method="chat.send",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    return await wrapped_again.send_request(envelope)

            response = _run(run())
            assert response.ok is True

            client_spans = [
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            ]
            assert len(client_spans) == 1
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_wrap_agent_client_returns_original_for_instrumented_client_class():
        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod

        original_initialized = tel_mod._initialized

        class FakeClient:
            pass

        setattr(FakeClient, proxy_mod.GATEWAY_CLIENT_INSTRUMENTED_ATTR, True)

        try:
            tel_mod._initialized = True
            client = FakeClient()
            assert proxy_mod.wrap_agent_client(client) is client
        finally:
            tel_mod._initialized = original_initialized

    @staticmethod
    def test_wrap_agent_client_extension_caches_wrapped_client_and_preserves_metadata():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            def __init__(self):
                self.connected_uri = None

            async def connect(self, uri: str) -> None:
                self.connected_uri = uri

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                return AgentResponse(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    ok=True,
                    payload={"content": "ok"},
                )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        class FakeExtension:
            def __init__(self, client):
                self._client = client
                self.metadata = types.SimpleNamespace(name="fake-extension")

            async def initialize(self, config):
                return None

            async def shutdown(self):
                return None

            def get_client(self):
                return self._client

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True

            extension = proxy_mod.wrap_agent_client_extension(FakeExtension(FakeClient()))
            assert extension.metadata.name == "fake-extension"

            client = extension.get_client()
            assert extension.get_client() is client

            _run(client.connect("ws://127.0.0.1:18092/ws"))

            envelope = E2AEnvelope(
                request_id="req_proxy_ext_cache",
                channel="web",
                session_id="sess_proxy_ext_cache",
                method="chat.send",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    return await client.send_request(envelope)

            response = _run(run())
            assert response.ok is True

            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            assert client_span.attributes["server.address"] == "127.0.0.1"
            assert client_span.attributes["server.port"] == 18092
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_creates_client_span_between_entry_and_agent():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse
        from jiuwenclaw.telemetry.context_propagation import extract_trace_context

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                assert envelope.channel_context["source"] == "test"
                assert "traceparent" in envelope.channel_context
                parent_ctx = extract_trace_context(envelope.channel_context)
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke",
                    context=parent_ctx,
                    kind=SpanKind.SERVER,
                ):
                    return AgentResponse(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        ok=True,
                        payload={"content": "ok"},
                    )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(
                FakeClient(),
                target_uri="ws://127.0.0.1:18092/ws",
            )
            envelope = E2AEnvelope(
                request_id="req_proxy_unary",
                channel="web",
                session_id="sess_proxy_unary",
                method="chat.send",
                channel_context={"source": "test"},
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    return await client.send_request(envelope)

            response = _run(run())
            assert response.ok is True

            spans = exporter.get_finished_spans()
            entry_span = next(s for s in spans if s.name == "channel.request")
            client_span = next(s for s in spans if s.name == "jiuwenclaw.gateway.agent.request")
            agent_span = next(s for s in spans if s.name == "jiuwenclaw.agent.invoke")

            assert client_span.kind == SpanKind.CLIENT
            assert agent_span.kind == SpanKind.SERVER
            assert client_span.parent.span_id == entry_span.context.span_id
            assert agent_span.parent.span_id == client_span.context.span_id
            assert client_span.attributes["server.address"] == "127.0.0.1"
            assert client_span.attributes["server.port"] == 18092
            assert client_span.attributes["network.protocol.name"] == "websocket"
            assert client_span.attributes["jiuwenclaw.req.method"] == "chat.send"
            assert envelope.channel_context["source"] == "test"
            assert "traceparent" in envelope.channel_context
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_connect_backfills_target_uri_and_supports_non_chat_method():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            def __init__(self):
                self.connected_uri = None

            async def connect(self, uri: str) -> None:
                self.connected_uri = uri

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                return AgentResponse(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    ok=True,
                    payload={"content": "ok"},
                )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        inner = FakeClient()
        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(inner)
            _run(client.connect("ws://10.0.0.8:18093/ws"))
            assert inner.connected_uri == "ws://10.0.0.8:18093/ws"

            envelope = E2AEnvelope(
                request_id="req_proxy_history",
                channel="web",
                session_id="sess_proxy_history",
                method="history.get",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    return await client.send_request(envelope)

            response = _run(run())
            assert response.ok is True

            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            assert client_span.attributes["server.address"] == "10.0.0.8"
            assert client_span.attributes["server.port"] == 18093
            assert client_span.attributes["jiuwenclaw.req.method"] == "history.get"
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_normalizes_non_dict_channel_context():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                assert isinstance(envelope.channel_context, dict)
                assert "traceparent" in envelope.channel_context
                return AgentResponse(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    ok=True,
                    payload={"content": "ok"},
                )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            envelope = E2AEnvelope(
                request_id="req_proxy_bad_context",
                channel="web",
                session_id="sess_proxy_bad_context",
                method="chat.send",
                channel_context="bad-context",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    return await client.send_request(envelope)

            response = _run(run())
            assert response.ok is True
            assert isinstance(envelope.channel_context, dict)
            assert "traceparent" in envelope.channel_context
            assert len(
                [s for s in exporter.get_finished_spans() if s.name == "jiuwenclaw.gateway.agent.request"]
            ) == 1
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_preserves_business_fields_and_refreshes_stale_traceparent():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse
        from jiuwenclaw.telemetry.context_propagation import extract_trace_context

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")

        stale_traceparent = "00-11111111111111111111111111111111-2222222222222222-01"

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                assert envelope.channel_context["source"] == "test"
                assert envelope.channel_context["user_hint"] == "keep-me"
                assert envelope.channel_context["traceparent"] != stale_traceparent
                parent_ctx = extract_trace_context(envelope.channel_context)
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke",
                    context=parent_ctx,
                    kind=SpanKind.SERVER,
                ):
                    return AgentResponse(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        ok=True,
                        payload={"content": "ok"},
                    )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            envelope = E2AEnvelope(
                request_id="req_proxy_refresh",
                channel="web",
                session_id="sess_proxy_refresh",
                method="chat.interrupt",
                channel_context={
                    "source": "test",
                    "user_hint": "keep-me",
                    "traceparent": stale_traceparent,
                },
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    return await client.send_request(envelope)

            response = _run(run())
            assert response.ok is True

            entry_span = next(s for s in exporter.get_finished_spans() if s.name == "channel.request")
            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            agent_span = next(s for s in exporter.get_finished_spans() if s.name == "jiuwenclaw.agent.invoke")

            assert envelope.channel_context["source"] == "test"
            assert envelope.channel_context["user_hint"] == "keep-me"
            assert envelope.channel_context["traceparent"] != stale_traceparent
            assert client_span.attributes["jiuwenclaw.req.method"] == "chat.interrupt"
            assert client_span.parent.span_id == entry_span.context.span_id
            assert agent_span.parent.span_id == client_span.context.span_id
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_error_sets_error_status_and_records_exception():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope):
                raise RuntimeError("proxy unary boom")

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            envelope = E2AEnvelope(
                request_id="req_proxy_error",
                channel="web",
                session_id="sess_proxy_error",
                method="chat.send",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    await client.send_request(envelope)

            with pytest.raises(RuntimeError, match="proxy unary boom"):
                _run(run())

            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            assert client_span.status.status_code == StatusCode.ERROR
            assert len([e for e in client_span.events if e.name == "exception"]) >= 1
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_cancelled_marks_span_cancelled():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope):
                raise asyncio.CancelledError()

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            envelope = E2AEnvelope(
                request_id="req_proxy_cancelled",
                channel="web",
                session_id="sess_proxy_cancelled",
                method="chat.send",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    await client.send_request(envelope)

            with pytest.raises(asyncio.CancelledError):
                _run(run())

            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            assert client_span.attributes["jiuwenclaw.cancelled"] is True
            assert len([e for e in client_span.events if e.name == "exception"]) == 0
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_stream_keeps_client_span_open_for_full_stream():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponseChunk
        from jiuwenclaw.telemetry.context_propagation import extract_trace_context

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope):
                raise AssertionError("send_request should not be called")

            async def send_request_stream(self, envelope: E2AEnvelope):
                assert envelope.is_stream is True
                assert envelope.channel_context["source"] == "test"
                assert "traceparent" in envelope.channel_context

                parent_ctx = extract_trace_context(envelope.channel_context)
                span = agent_tracer.start_span(
                    "jiuwenclaw.agent.invoke.stream",
                    context=parent_ctx,
                    kind=SpanKind.SERVER,
                )
                try:
                    yield AgentResponseChunk(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        payload={"content": "part-1"},
                        is_complete=False,
                    )
                    yield AgentResponseChunk(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        payload={"content": "part-2"},
                        is_complete=True,
                    )
                finally:
                    span.end()

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(
                FakeClient(),
                target_uri="ws://127.0.0.1:18092/ws",
            )
            envelope = E2AEnvelope(
                request_id="req_proxy_stream",
                channel="web",
                session_id="sess_proxy_stream",
                method="chat.send",
                channel_context={"source": "test"},
            )

            async def run():
                chunks = []
                with entry_tracer.start_as_current_span("channel.request"):
                    async for chunk in client.send_request_stream(envelope):
                        chunks.append(chunk)
                return chunks

            chunks = _run(run())
            assert [chunk.payload["content"] for chunk in chunks] == ["part-1", "part-2"]

            spans = exporter.get_finished_spans()
            entry_span = next(s for s in spans if s.name == "channel.request")
            client_span = next(s for s in spans if s.name == "jiuwenclaw.gateway.agent.request")
            agent_span = next(s for s in spans if s.name == "jiuwenclaw.agent.invoke.stream")

            assert client_span.kind == SpanKind.CLIENT
            assert agent_span.kind == SpanKind.SERVER
            assert client_span.parent.span_id == entry_span.context.span_id
            assert agent_span.parent.span_id == client_span.context.span_id
            assert client_span.attributes["jiuwenclaw.stream"] is True
            assert envelope.channel_context["source"] == "test"
            assert "traceparent" in envelope.channel_context
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_stream_error_sets_error_status_and_ends_span():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponseChunk

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope):
                raise AssertionError("send_request should not be called")

            async def send_request_stream(self, envelope: E2AEnvelope):
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload={"content": "part-1"},
                    is_complete=False,
                )
                raise RuntimeError("proxy stream boom")

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            envelope = E2AEnvelope(
                request_id="req_proxy_stream_error",
                channel="web",
                session_id="sess_proxy_stream_error",
                method="chat.send",
            )

            async def run():
                chunks = []
                with entry_tracer.start_as_current_span("channel.request"):
                    async for chunk in client.send_request_stream(envelope):
                        chunks.append(chunk)
                return chunks

            with pytest.raises(RuntimeError, match="proxy stream boom"):
                _run(run())

            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            assert client_span.status.status_code == StatusCode.ERROR
            assert len([e for e in client_span.events if e.name == "exception"]) >= 1
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_send_request_stream_cancelled_marks_span_cancelled_and_ends_span():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponseChunk

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")

        class FakeClient:
            async def connect(self, uri: str) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope):
                raise AssertionError("send_request should not be called")

            async def send_request_stream(self, envelope: E2AEnvelope):
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload={"content": "part-1"},
                    is_complete=False,
                )
                raise asyncio.CancelledError()

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True
            client = proxy_mod.wrap_agent_client(FakeClient(), target_uri="ws://127.0.0.1:18092/ws")
            envelope = E2AEnvelope(
                request_id="req_proxy_stream_cancelled",
                channel="web",
                session_id="sess_proxy_stream_cancelled",
                method="chat.send",
            )

            async def run():
                with entry_tracer.start_as_current_span("channel.request"):
                    async for _chunk in client.send_request_stream(envelope):
                        pass

            with pytest.raises(asyncio.CancelledError):
                _run(run())

            client_span = next(
                s for s in exporter.get_finished_spans()
                if s.name == "jiuwenclaw.gateway.agent.request"
            )
            assert client_span.attributes["jiuwenclaw.cancelled"] is True
            assert len([e for e in client_span.events if e.name == "exception"]) == 0
        finally:
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()


# ---------------------------------------------------------------------------
# 5c. Gateway AgentServerClient instrumentor
# ---------------------------------------------------------------------------

class TestGatewayAgentClientInstrumentor:
    @staticmethod
    def test_instrument_gateway_agent_client_patches_builtin_client_class():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse
        from jiuwenclaw.telemetry.context_propagation import extract_trace_context

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")

        fake_gateway_pkg = types.ModuleType("jiuwenclaw.gateway")
        fake_gateway_pkg.__path__ = []
        fake_agent_client_module = types.ModuleType("jiuwenclaw.gateway.agent_client")

        class AgentServerClient:
            pass

        class WebSocketAgentServerClient(AgentServerClient):
            def __init__(self, *args, **kwargs):
                self._uri = None

            async def connect(self, uri: str) -> None:
                self._uri = uri

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                assert "traceparent" in envelope.channel_context
                parent_ctx = extract_trace_context(envelope.channel_context)
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke",
                    context=parent_ctx,
                    kind=SpanKind.SERVER,
                ):
                    return AgentResponse(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        ok=True,
                        payload={"content": "ok"},
                    )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        fake_agent_client_module.AgentServerClient = AgentServerClient
        fake_agent_client_module.WebSocketAgentServerClient = WebSocketAgentServerClient
        fake_gateway_pkg.agent_client = fake_agent_client_module

        fake_extensions_pkg = types.ModuleType("jiuwenclaw.extensions")
        fake_extensions_pkg.__path__ = []
        fake_registry_module = types.ModuleType("jiuwenclaw.extensions.registry")

        class ExtensionRegistry:
            def __init__(self) -> None:
                self._agent_server_client = None

            def register_agent_server_client(self, extension) -> None:
                self._agent_server_client = extension

        fake_registry_module.ExtensionRegistry = ExtensionRegistry
        fake_extensions_pkg.registry = fake_registry_module

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True

            with patch.dict(
                sys.modules,
                {
                    "jiuwenclaw.gateway": fake_gateway_pkg,
                    "jiuwenclaw.gateway.agent_client": fake_agent_client_module,
                    "jiuwenclaw.extensions": fake_extensions_pkg,
                    "jiuwenclaw.extensions.registry": fake_registry_module,
                },
            ):
                sys.modules.pop("jiuwenclaw.telemetry.instrumentors.gateway_agent_client", None)
                gateway_client_mod = importlib.import_module(
                    "jiuwenclaw.telemetry.instrumentors.gateway_agent_client"
                )
                gateway_client_mod = importlib.reload(gateway_client_mod)
                gateway_client_mod.instrument_gateway_agent_client()
                gateway_client_mod.instrument_gateway_agent_client()

                client = WebSocketAgentServerClient()
                _run(client.connect("ws://127.0.0.1:18092/ws"))

                envelope = E2AEnvelope(
                    request_id="req_auto_builtin",
                    channel="web",
                    session_id="sess_auto_builtin",
                    method="chat.send",
                    channel_context={"source": "test"},
                )

                async def run():
                    with entry_tracer.start_as_current_span("channel.request"):
                        return await client.send_request(envelope)

                response = _run(run())
                assert response.ok is True

            spans = exporter.get_finished_spans()
            entry_span = next(s for s in spans if s.name == "channel.request")
            client_span = next(s for s in spans if s.name == "jiuwenclaw.gateway.agent.request")
            agent_span = next(s for s in spans if s.name == "jiuwenclaw.agent.invoke")

            assert client_span.parent.span_id == entry_span.context.span_id
            assert agent_span.parent.span_id == client_span.context.span_id
            assert client_span.attributes["server.address"] == "127.0.0.1"
            assert client_span.attributes["server.port"] == 18092
            assert client_span.kind == SpanKind.CLIENT
        finally:
            sys.modules.pop("jiuwenclaw.telemetry.instrumentors.gateway_agent_client", None)
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()

    @staticmethod
    def test_instrument_gateway_agent_client_wraps_registered_extension_client():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry as tel_mod
        import jiuwenclaw.telemetry.agent_client_proxy as proxy_mod
        from jiuwenclaw.e2a.models import E2AEnvelope
        from jiuwenclaw.schema.agent import AgentResponse
        from jiuwenclaw.telemetry.context_propagation import extract_trace_context

        original_tracer = proxy_mod._tracer
        original_initialized = tel_mod._initialized
        entry_tracer = tp.get_tracer("jiuwenclaw.entry")
        agent_tracer = tp.get_tracer("jiuwenclaw.agent")

        fake_gateway_pkg = types.ModuleType("jiuwenclaw.gateway")
        fake_gateway_pkg.__path__ = []
        fake_agent_client_module = types.ModuleType("jiuwenclaw.gateway.agent_client")

        class AgentServerClient:
            pass

        class WebSocketAgentServerClient(AgentServerClient):
            async def connect(self, uri: str) -> None:
                self._uri = uri

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope):
                raise AssertionError("builtin client should not be used in this test")

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        fake_agent_client_module.AgentServerClient = AgentServerClient
        fake_agent_client_module.WebSocketAgentServerClient = WebSocketAgentServerClient
        fake_gateway_pkg.agent_client = fake_agent_client_module

        fake_extensions_pkg = types.ModuleType("jiuwenclaw.extensions")
        fake_extensions_pkg.__path__ = []
        fake_registry_module = types.ModuleType("jiuwenclaw.extensions.registry")

        class ExtensionRegistry:
            def __init__(self):
                self._agent_server_client = None

            def register_agent_server_client(self, extension) -> None:
                self._agent_server_client = extension

            def get_agent_server_client_extension(self):
                return self._agent_server_client

        fake_registry_module.ExtensionRegistry = ExtensionRegistry
        fake_extensions_pkg.registry = fake_registry_module

        class CustomClient(AgentServerClient):
            def __init__(self):
                self.connected_uri = None

            async def connect(self, uri: str) -> None:
                self.connected_uri = uri

            async def disconnect(self) -> None:
                return None

            async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
                assert "traceparent" in envelope.channel_context
                parent_ctx = extract_trace_context(envelope.channel_context)
                with agent_tracer.start_as_current_span(
                    "jiuwenclaw.agent.invoke",
                    context=parent_ctx,
                    kind=SpanKind.SERVER,
                ):
                    return AgentResponse(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        ok=True,
                        payload={"content": "ok"},
                    )

            async def send_request_stream(self, envelope: E2AEnvelope):
                if False:
                    yield envelope

        class FakeExtension:
            def __init__(self, client):
                self._client = client
                self.metadata = types.SimpleNamespace(name="custom-extension")

            def get_client(self):
                return self._client

        try:
            proxy_mod._tracer = tp.get_tracer("jiuwenclaw.gateway.agent_client")
            tel_mod._initialized = True

            with patch.dict(
                sys.modules,
                {
                    "jiuwenclaw.gateway": fake_gateway_pkg,
                    "jiuwenclaw.gateway.agent_client": fake_agent_client_module,
                    "jiuwenclaw.extensions": fake_extensions_pkg,
                    "jiuwenclaw.extensions.registry": fake_registry_module,
                },
            ):
                sys.modules.pop("jiuwenclaw.telemetry.instrumentors.gateway_agent_client", None)
                gateway_client_mod = importlib.import_module(
                    "jiuwenclaw.telemetry.instrumentors.gateway_agent_client"
                )
                gateway_client_mod = importlib.reload(gateway_client_mod)
                gateway_client_mod.instrument_gateway_agent_client()

                registry = ExtensionRegistry()
                registry.register_agent_server_client(FakeExtension(CustomClient()))

                extension = registry.get_agent_server_client_extension()
                assert extension.metadata.name == "custom-extension"

                client = extension.get_client()
                assert extension.get_client() is client

                _run(client.connect("ws://10.0.0.8:18093/ws"))

                envelope = E2AEnvelope(
                    request_id="req_auto_extension",
                    channel="web",
                    session_id="sess_auto_extension",
                    method="history.get",
                )

                async def run():
                    with entry_tracer.start_as_current_span("channel.request"):
                        return await client.send_request(envelope)

                response = _run(run())
                assert response.ok is True

            spans = exporter.get_finished_spans()
            entry_span = next(s for s in spans if s.name == "channel.request")
            client_span = next(s for s in spans if s.name == "jiuwenclaw.gateway.agent.request")
            agent_span = next(s for s in spans if s.name == "jiuwenclaw.agent.invoke")

            assert client_span.parent.span_id == entry_span.context.span_id
            assert agent_span.parent.span_id == client_span.context.span_id
            assert client_span.attributes["server.address"] == "10.0.0.8"
            assert client_span.attributes["server.port"] == 18093
            assert client_span.attributes["jiuwenclaw.req.method"] == "history.get"
        finally:
            sys.modules.pop("jiuwenclaw.telemetry.instrumentors.gateway_agent_client", None)
            proxy_mod._tracer = original_tracer
            tel_mod._initialized = original_initialized
            tp.shutdown()


# ---------------------------------------------------------------------------
# 6. LLM instrumentor — direct function test
# ---------------------------------------------------------------------------

class TestLLMInstrumentor:
    @staticmethod
    def test_call_llm_creates_genai_span_with_tokens():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")
        llm_mod.set_log_messages(True)

        # Mock result
        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_tokens = 10  # UsageMetadata uses cache_tokens

        mock_result = MagicMock()
        mock_result.content = "Hello, I can help."
        mock_result.tool_calls = []
        mock_result.usage_metadata = mock_usage
        mock_result.finish_reason = "stop"

        original_fn = AsyncMock(return_value=mock_result)

        # Mock messages
        mock_sys = MagicMock(role="system", content="You are helpful.")
        mock_user = MagicMock(role="user", content="Hello")
        messages = [mock_sys, mock_user]

        async def traced_call_llm(self_agent, msgs, tools, session, chunk_threshold):
            model_name = "deepseek-chat"
            system = "openai"
            with llm_mod._tracer.start_as_current_span(
                "gen_ai.chat",
                attributes={
                    "gen_ai.system": system,
                    "gen_ai.request.model": model_name,
                    "gen_ai.operation.name": "chat",
                },
            ) as span:
                if llm_mod._log_messages:
                    llm_mod._record_input_messages(span, msgs)

                result = await original_fn(self_agent, msgs, tools, session, chunk_threshold)

                llm_mod._record_token_usage(span, result, model_name, system)

                finish_reason = getattr(result, "finish_reason", None)
                if finish_reason:
                    span.set_attribute("gen_ai.response.finish_reasons", [str(finish_reason)])

                if llm_mod._log_messages:
                    llm_mod._record_output_message(span, result)

                return result

        result = _run(traced_call_llm(MagicMock(), messages, None, None, 10))
        assert result == mock_result

        spans = exporter.get_finished_spans()
        llm_spans = [s for s in spans if s.name == "gen_ai.chat"]
        assert len(llm_spans) == 1

        span = llm_spans[0]
        assert span.attributes["gen_ai.system"] == "openai"
        assert span.attributes["gen_ai.request.model"] == "deepseek-chat"
        assert span.attributes["gen_ai.operation.name"] == "chat"
        assert span.attributes["gen_ai.usage.input_tokens"] == 100
        assert span.attributes["gen_ai.usage.output_tokens"] == 50
        assert span.attributes["gen_ai.usage.cache_read_tokens"] == 10
        assert span.attributes["gen_ai.response.finish_reasons"] == ("stop",)

        event_names = [e.name for e in span.events]
        assert "gen_ai.system.message" in event_names
        assert "gen_ai.user.message" in event_names
        assert "gen_ai.assistant.message" in event_names
        tp.shutdown()

    @staticmethod
    def test_call_llm_error_sets_error_status():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")

        original_fn = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        async def traced_call_llm_error():
            with llm_mod._tracer.start_as_current_span(
                "gen_ai.chat",
                attributes={"gen_ai.request.model": "gpt-4", "gen_ai.system": "openai"},
            ) as span:
                try:
                    await original_fn()
                except Exception as exc:
                    span.set_status(StatusCode.ERROR, str(exc)[:256])
                    span.record_exception(exc)
                    raise

        with pytest.raises(RuntimeError, match="LLM timeout"):
            _run(traced_call_llm_error())

        spans = exporter.get_finished_spans()
        llm_spans = [s for s in spans if s.name == "gen_ai.chat"]
        assert len(llm_spans) == 1
        assert llm_spans[0].status.status_code.name == "ERROR"
        tp.shutdown()

    @staticmethod
    def test_log_messages_disabled_no_events():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")
        llm_mod.set_log_messages(False)

        mock_result = MagicMock()
        mock_result.content = "response"
        mock_result.tool_calls = []
        mock_result.usage_metadata = None
        mock_result.finish_reason = "stop"

        mock_msg = MagicMock(role="user", content="secret")

        async def traced_no_log():
            with llm_mod._tracer.start_as_current_span("gen_ai.chat") as span:
                if llm_mod._log_messages:
                    llm_mod._record_input_messages(span, [mock_msg])
                    llm_mod._record_output_message(span, mock_result)

        _run(traced_no_log())

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert len(spans[0].events) == 0

        llm_mod.set_log_messages(True)
        tp.shutdown()

    @staticmethod
    def test_record_input_messages_all_roles():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")

        msgs = [
            MagicMock(role="system", content="sys prompt"),
            MagicMock(role="user", content="user input"),
            MagicMock(role="assistant", content="assistant reply"),
        ]

        with llm_mod._tracer.start_as_current_span("test") as span:
            llm_mod._record_input_messages(span, msgs)

        spans = exporter.get_finished_spans()
        events = spans[0].events
        assert len(events) == 3
        assert events[0].name == "gen_ai.system.message"
        assert events[1].name == "gen_ai.user.message"
        assert events[2].name == "gen_ai.assistant.message"
        tp.shutdown()

    @staticmethod
    def test_infer_gen_ai_system():
        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod

        agent = MagicMock()
        agent._config.model_client_config = {"client_provider": "OpenAI"}
        assert llm_mod._infer_gen_ai_system(agent) == "openai"

        agent._config.model_client_config = {"client_provider": "SiliconFlow"}
        assert llm_mod._infer_gen_ai_system(agent) == "siliconflow"

        agent._config.model_client_config = {}
        assert llm_mod._infer_gen_ai_system(agent) == "unknown"

        agent_without_config = object()
        assert llm_mod._infer_gen_ai_system(agent_without_config) == "unknown"

    @staticmethod
    def test_infer_gen_ai_system_logs_and_falls_back_on_exception():
        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod

        class BrokenAgent:
            @property
            def _config(self):
                raise RuntimeError("boom")

        with patch.object(llm_mod.logger, "debug") as mock_debug:
            assert llm_mod._infer_gen_ai_system(BrokenAgent()) == "unknown"

        mock_debug.assert_called_once()
        assert mock_debug.call_args.args[0] == "[Telemetry] Failed to infer gen_ai.system: %s"
        assert str(mock_debug.call_args.args[1]) == "boom"
        assert mock_debug.call_args.kwargs["exc_info"] is True

# ---------------------------------------------------------------------------
# 7. Tool instrumentor — direct function test
# ---------------------------------------------------------------------------


class TestToolInstrumentor:
    @staticmethod
    def test_tool_call_and_result_creates_span():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.tool as tool_mod
        tool_mod._tracer = tp.get_tracer("jiuwenclaw.tool")
        tool_mod._active_tool_spans.clear()

        mock_tool_call = MagicMock()
        mock_tool_call.name = "memory_search"
        mock_tool_call.id = "call_001"
        mock_tool_call.arguments = {"query": "test"}

        # Simulate what _traced_emit_tool_call does
        span = tool_mod._tracer.start_span(
            f"gen_ai.tool.execute: {mock_tool_call.name}",
            attributes={
                "gen_ai.tool.name": mock_tool_call.name,
                "gen_ai.tool.call.id": mock_tool_call.id,
            },
        )
        span.add_event("tool.arguments", {"arguments": str(mock_tool_call.arguments)})
        tool_mod._active_tool_spans[mock_tool_call.id] = (span, time.monotonic())

        # Simulate _traced_emit_tool_result
        entry = tool_mod._active_tool_spans.pop(mock_tool_call.id)
        s, start_time = entry
        result_str = "found 3 items"
        s.add_event("tool.result", {"result": result_str})
        s.set_status(StatusCode.OK)
        s.end()

        spans = exporter.get_finished_spans()
        tool_spans = [s for s in spans if "gen_ai.tool.execute" in s.name]
        assert len(tool_spans) == 1

        span = tool_spans[0]
        assert span.attributes["gen_ai.tool.name"] == "memory_search"
        assert span.attributes["gen_ai.tool.call.id"] == "call_001"
        assert span.status.status_code.name == "OK"

        event_names = [e.name for e in span.events]
        assert "tool.arguments" in event_names
        assert "tool.result" in event_names
        tp.shutdown()

    @staticmethod
    def test_tool_error_sets_error_status():
        tp, _, exporter, _ = _make_otel_providers()

        import jiuwenclaw.telemetry.instrumentors.tool as tool_mod
        tool_mod._tracer = tp.get_tracer("jiuwenclaw.tool")
        tool_mod._active_tool_spans.clear()

        mock_tool_call = MagicMock()
        mock_tool_call.name = "browser_navigate"
        mock_tool_call.id = "call_002"

        span = tool_mod._tracer.start_span(
            f"gen_ai.tool.execute: {mock_tool_call.name}",
            attributes={
                "gen_ai.tool.name": mock_tool_call.name,
                "gen_ai.tool.call.id": mock_tool_call.id,
            },
        )
        tool_mod._active_tool_spans[mock_tool_call.id] = (span, time.monotonic())

        entry = tool_mod._active_tool_spans.pop(mock_tool_call.id)
        s, _ = entry
        result_str = "Error: Connection timeout"
        s.add_event("tool.result", {"result": result_str})
        s.set_status(StatusCode.ERROR, result_str[:256])
        s.end()

        spans = exporter.get_finished_spans()
        tool_spans = [s for s in spans if "gen_ai.tool.execute" in s.name]
        assert len(tool_spans) == 1
        assert tool_spans[0].status.status_code.name == "ERROR"
        tp.shutdown()

# ---------------------------------------------------------------------------
# 8. Init telemetry tests
# ---------------------------------------------------------------------------


class TestApplyInstrumentors:
    @staticmethod
    def test_apply_instrumentors_calls_gateway_agent_client_instrumentor():
        import jiuwenclaw.telemetry.instrumentors as instrumentors_mod

        # Mock architecture detection to force officeclaw mode (ReActAgent)
        # so set_log_messages is called from llm module, not telemetry_rail
        with patch.object(instrumentors_mod, "_detect_architecture", return_value=False):
            with patch("jiuwenclaw.telemetry.instrumentors.llm.set_log_messages") as mock_set_log_messages:
                with patch("jiuwenclaw.telemetry.instrumentors.entry.instrument_entry") as mock_entry:
                    with patch(
                        "jiuwenclaw.telemetry.instrumentors.gateway_agent_client.instrument_gateway_agent_client"
                    ) as mock_gateway_agent_client:
                        with patch("jiuwenclaw.telemetry.instrumentors.agent.instrument_agent") as mock_agent:
                            with patch("jiuwenclaw.telemetry.instrumentors.llm.instrument_llm") as mock_llm:
                                with patch("jiuwenclaw.telemetry.instrumentors.tool.instrument_tools") as mock_tools:
                                    with patch(
                                        "jiuwenclaw.telemetry.instrumentors.session.instrument_session"
                                    ) as mock_session:
                                        instrumentors_mod.apply_instrumentors(
                                            log_messages=False,
                                            session_stuck_threshold_ms=1234.0,
                                            session_stuck_check_interval_s=12.0,
                                        )

        mock_set_log_messages.assert_called_once_with(False)
        mock_entry.assert_called_once_with()
        mock_gateway_agent_client.assert_called_once_with()
        mock_agent.assert_called_once_with()
        mock_llm.assert_called_once_with()
        mock_tools.assert_called_once_with()
        mock_session.assert_called_once_with(
            stuck_threshold_ms=1234.0,
            stuck_check_interval_s=12.0,
        )


class TestInitTelemetry:
    @staticmethod
    def test_noop_by_default_when_disabled():
        import jiuwenclaw.telemetry as tel_mod
        tel_mod._initialized = False

        with patch.dict("os.environ", {}, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                with patch("jiuwenclaw.telemetry.provider.init_providers") as mock_providers:
                    with patch("jiuwenclaw.telemetry.instrumentors.apply_instrumentors") as mock_instr:
                        tel_mod.init_telemetry()
                        mock_providers.assert_not_called()
                        mock_instr.assert_not_called()
                        assert tel_mod.is_telemetry_initialized() is False
        tel_mod._initialized = False

    @staticmethod
    def test_noop_when_disabled():
        import jiuwenclaw.telemetry as tel_mod
        tel_mod._initialized = False

        with patch.dict("os.environ", {"OTEL_ENABLED": "false"}, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                with patch("jiuwenclaw.telemetry.provider.init_providers") as mock_init:
                    tel_mod.init_telemetry()
                    mock_init.assert_not_called()
        tel_mod._initialized = False

    @staticmethod
    def test_initializes_when_enabled():
        import jiuwenclaw.telemetry as tel_mod
        tel_mod._initialized = False

        with patch.dict("os.environ", {"OTEL_ENABLED": "true", "OTEL_EXPORTER_TYPE": "none"}, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                with patch("jiuwenclaw.telemetry.provider.init_providers") as mock_providers:
                    with patch("jiuwenclaw.telemetry.instrumentors.apply_instrumentors") as mock_instr:
                        tel_mod.init_telemetry()
                        mock_providers.assert_called_once()
                        mock_instr.assert_called_once()
        tel_mod._initialized = False

    @staticmethod
    def test_idempotent():
        import jiuwenclaw.telemetry as tel_mod
        tel_mod._initialized = False

        with patch.dict("os.environ", {"OTEL_ENABLED": "true", "OTEL_EXPORTER_TYPE": "none"}, clear=True):
            with patch("jiuwenclaw.config.get_config", side_effect=Exception("no config")):
                with patch("jiuwenclaw.telemetry.provider.init_providers"):
                    with patch("jiuwenclaw.telemetry.instrumentors.apply_instrumentors") as mock_instr:
                        tel_mod.init_telemetry()
                        tel_mod.init_telemetry()
                        assert mock_instr.call_count == 1
        tel_mod._initialized = False


# ---------------------------------------------------------------------------
# 9. Session configuration normalization tests
# ---------------------------------------------------------------------------

class TestSessionConfigNormalization:
    @staticmethod
    def test_instrument_session_normalizes_module_config_to_float():
        import jiuwenclaw.telemetry.instrumentors.session as session_mod

        fake_agentserver_pkg = types.ModuleType("jiuwenclaw.agentserver")
        fake_agentserver_pkg.__path__ = []
        fake_interface_module = types.ModuleType("jiuwenclaw.agentserver.interface")

        class JiuWenClaw:
            def __init__(self):
                self._session_processors = {}
                self._session_queues = {}
                self._session_priorities = {}
                self._session_tasks = {}

            async def _ensure_session_processor(self, session_id: str) -> None:
                return None

            async def _cancel_session_task(self, session_id: str, log_msg_prefix: str = "") -> None:
                return None

        fake_interface_module.JiuWenClaw = JiuWenClaw
        fake_agentserver_pkg.interface = fake_interface_module

        original_threshold = session_mod._stuck_threshold_ms
        original_interval = session_mod._stuck_check_interval_s
        try:
            with patch.dict(
                sys.modules,
                {
                    "jiuwenclaw.agentserver": fake_agentserver_pkg,
                    "jiuwenclaw.agentserver.interface": fake_interface_module,
                },
            ):
                session_mod.instrument_session(
                    stuck_threshold_ms=1234,
                    stuck_check_interval_s="9.0",
                )

            assert session_mod._stuck_threshold_ms == 1234.0
            assert isinstance(session_mod._stuck_threshold_ms, float)
            assert session_mod._stuck_check_interval_s == 9.0
            assert isinstance(session_mod._stuck_check_interval_s, float)
        finally:
            session_mod._stuck_threshold_ms = original_threshold
            session_mod._stuck_check_interval_s = original_interval


# ---------------------------------------------------------------------------
# 10. Provider initialization tests
# ---------------------------------------------------------------------------

class TestProviderInitialization:
    @staticmethod
    def test_build_default_providers_supports_none_exporters():
        from jiuwenclaw.telemetry.config import TelemetryConfig
        from jiuwenclaw.telemetry.provider import build_default_providers

        bundle = build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="none",
            )
        )

        assert bundle.tracer_provider is not None
        assert bundle.meter_provider is not None
        assert bundle.tracer_provider._active_span_processor._span_processors == ()
        assert bundle.meter_provider._sdk_config.metric_readers == []

    @staticmethod
    def test_build_default_providers_adds_claw_id_resource_attribute():
        from jiuwenclaw.telemetry.attributes import JIUWENCLAW_CLAW_ID
        from jiuwenclaw.telemetry.config import TelemetryConfig
        from jiuwenclaw.telemetry.provider import build_default_providers

        bundle = build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="none",
                claw_id="gateway-sh-01",
            )
        )

        assert bundle.tracer_provider.resource.attributes[JIUWENCLAW_CLAW_ID] == "gateway-sh-01"
        assert bundle.meter_provider._sdk_config.resource.attributes[JIUWENCLAW_CLAW_ID] == "gateway-sh-01"

    @staticmethod
    def test_create_signal_specific_http_exporters_with_headers():
        from jiuwenclaw.telemetry.config import TelemetryConfig
        from jiuwenclaw.telemetry.provider import (
            _create_otlp_metric_exporter,
            _create_otlp_span_exporter,
        )

        cfg = TelemetryConfig(
            enabled=True,
            traces_protocol="http",
            traces_endpoint="http://trace.example.com",
            traces_headers={"Authorization": "Bearer trace"},
            metrics_protocol="http",
            metrics_endpoint="http://metric.example.com",
            metrics_headers={"Authorization": "Bearer metric"},
        )

        span_exporter = _create_otlp_span_exporter(cfg, signal="traces")
        metric_exporter = _create_otlp_metric_exporter(cfg, signal="metrics")

        assert span_exporter._endpoint == "http://trace.example.com/v1/traces"
        assert span_exporter._headers == {"Authorization": "Bearer trace"}
        assert metric_exporter._endpoint == "http://metric.example.com/v1/metrics"
        assert metric_exporter._headers == {"Authorization": "Bearer metric"}

    @staticmethod
    def test_init_providers_uses_registered_telemetry_provider_extension():
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.extensions.sdk.telemetry_provider import TelemetryProviderExtension
        from jiuwenclaw.telemetry.config import TelemetryConfig
        from jiuwenclaw.telemetry.provider import ProviderBundle, init_providers

        ExtensionRegistry.reset_instance()

        tracer_provider = TracerProvider()
        meter_provider = MeterProvider()

        class FakeExt(TelemetryProviderExtension):
            async def initialize(self, config):
                pass

            def build_providers(self, cfg):
                return ProviderBundle(
                    tracer_provider=tracer_provider,
                    meter_provider=meter_provider,
                )

            async def shutdown(self):
                return None

        registry = ExtensionRegistry.create_instance(
            callback_framework=MagicMock(),
            config={},
            logger=MagicMock(),
        )
        registry.register_telemetry_provider(FakeExt())

        with patch("jiuwenclaw.telemetry.provider.install_providers") as mock_install:
            bundle = init_providers(TelemetryConfig(enabled=True))

        assert bundle.tracer_provider is tracer_provider
        assert bundle.meter_provider is meter_provider
        mock_install.assert_called_once_with(bundle)

        ExtensionRegistry.reset_instance()


# ---------------------------------------------------------------------------
# 11. Span hierarchy test
# ---------------------------------------------------------------------------

class TestSpanHierarchy:
    @staticmethod
    def test_entry_agent_llm_tool_hierarchy():
        tp, _, exporter, _ = _make_otel_providers()
        tracer = tp.get_tracer("test.hierarchy")

        with tracer.start_as_current_span("channel.request") as entry_span:
            with tracer.start_as_current_span("jiuwenclaw.agent.invoke") as agent_span:
                with tracer.start_as_current_span("gen_ai.chat") as llm_span:
                    with tracer.start_as_current_span("gen_ai.tool.execute: search") as tool_span:
                        pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 4

        span_map = {s.name: s for s in spans}
        entry = span_map["channel.request"]
        agent = span_map["jiuwenclaw.agent.invoke"]
        llm = span_map["gen_ai.chat"]
        tool = span_map["gen_ai.tool.execute: search"]

        # Parent-child chain
        assert agent.parent.span_id == entry.context.span_id
        assert llm.parent.span_id == agent.context.span_id
        assert tool.parent.span_id == llm.context.span_id

        # Same trace_id
        tid = entry.context.trace_id
        assert agent.context.trace_id == tid
        assert llm.context.trace_id == tid
        assert tool.context.trace_id == tid
        tp.shutdown()


# ---------------------------------------------------------------------------
# 12. New attributes tests (gen_ai.span.type, temperature, top_p, etc.)
# ---------------------------------------------------------------------------

class TestNewAttributes:
    """Tests for attributes added in the span attributes expansion."""

    @staticmethod
    def test_llm_span_type_attribute():
        """gen_ai.span.type=model is set on LLM span."""
        tp, _, exporter, _ = _make_otel_providers()
        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_usage.cache_tokens = 0
        mock_result = MagicMock()
        mock_result.content = "ok"
        mock_result.tool_calls = []
        mock_result.usage_metadata = mock_usage
        mock_result.finish_reason = "stop"

        mock_config = MagicMock()
        mock_config.model_name = "test-model"
        mock_config.model_config_obj = MagicMock(temperature=0.7, top_p=0.9)
        mock_config.model_client_config = MagicMock(client_provider="openai")

        mock_agent = MagicMock()
        mock_agent._config = mock_config

        original_fn = AsyncMock(return_value=mock_result)

        async def run():
            model_name = mock_config.model_name
            system = "openai"
            model_cfg = mock_config.model_config_obj
            span_attrs = {
                "gen_ai.system": system,
                "gen_ai.request.model": model_name,
                "gen_ai.response.model": model_name,
                "gen_ai.operation.name": "chat",
                "gen_ai.span.type": "model",
                "gen_ai.request.temperature": float(model_cfg.temperature),
                "gen_ai.request.top_p": float(model_cfg.top_p),
            }
            with llm_mod._tracer.start_as_current_span("gen_ai.chat", attributes=span_attrs) as span:
                result = await original_fn(mock_agent, [], None, None, 10)
                llm_mod._record_token_usage(span, result, model_name, system)
                finish_reason = getattr(result, "finish_reason", None)
                if finish_reason and str(finish_reason) != "null":
                    span.set_attribute("gen_ai.response.finish_reasons", [str(finish_reason)])
                    span.set_attribute("gen_ai.response.finish_reason", str(finish_reason))
                return result

        _run(run())
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.attributes["gen_ai.span.type"] == "model"
        assert s.attributes["gen_ai.request.temperature"] == 0.7
        assert s.attributes["gen_ai.request.top_p"] == 0.9
        assert s.attributes["gen_ai.response.model"] == "test-model"
        assert s.attributes["gen_ai.response.finish_reason"] == "stop"
        assert s.attributes["gen_ai.response.finish_reasons"] == ("stop",)
        tp.shutdown()

    @staticmethod
    def test_finish_reason_null_is_filtered():
        """finish_reason='null' default value is not written to span."""
        tp, _, exporter, _ = _make_otel_providers()
        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")

        mock_result = MagicMock()
        mock_result.finish_reason = "null"

        with llm_mod._tracer.start_as_current_span("gen_ai.chat") as span:
            finish_reason = getattr(mock_result, "finish_reason", None)
            if finish_reason and str(finish_reason) != "null":
                span.set_attribute("gen_ai.response.finish_reason", str(finish_reason))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert "gen_ai.response.finish_reason" not in spans[0].attributes
        tp.shutdown()

    @staticmethod
    def test_agent_span_new_attributes():
        """gen_ai.agent.name, gen_ai.conversation.id, gen_ai.span.type=agent."""
        tp, _, exporter, _ = _make_otel_providers()
        tracer = tp.get_tracer("jiuwenclaw.agent")

        with tracer.start_as_current_span(
            "jiuwenclaw.agent.invoke",
            attributes={
                "jiuwenclaw.agent.name": "main_agent",
                "gen_ai.agent.name": "main_agent",
                "gen_ai.conversation.id": "sess_abc123",
                "gen_ai.span.type": "agent",
            },
        ):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.attributes["gen_ai.agent.name"] == "main_agent"
        assert s.attributes["gen_ai.conversation.id"] == "sess_abc123"
        assert s.attributes["gen_ai.span.type"] == "agent"
        tp.shutdown()

    @staticmethod
    def test_entry_span_type_workflow():
        """gen_ai.span.type=workflow is set on entry span."""
        tp, _, exporter, _ = _make_otel_providers()
        tracer = tp.get_tracer("jiuwenclaw.entry")

        with tracer.start_as_current_span(
            "channel.request",
            attributes={
                "jiuwenclaw.channel.id": "web",
                "gen_ai.span.type": "workflow",
            },
        ):
            pass

        spans = exporter.get_finished_spans()
        assert spans[0].attributes["gen_ai.span.type"] == "workflow"
        tp.shutdown()

    @staticmethod
    def test_tool_span_type_tool():
        """gen_ai.span.type=tool is set on tool span."""
        tp, _, exporter, _ = _make_otel_providers()
        tracer = tp.get_tracer("jiuwenclaw.tool")

        with tracer.start_as_current_span(
            "gen_ai.tool.execute: search",
            attributes={
                "gen_ai.tool.name": "search",
                "gen_ai.span.type": "tool",
            },
        ):
            pass

        spans = exporter.get_finished_spans()
        assert spans[0].attributes["gen_ai.span.type"] == "tool"
        tp.shutdown()

    @staticmethod
    def test_cache_tokens_field_name():
        """cache_tokens (not cache_read_input_tokens) is read from UsageMetadata."""
        tp, _, exporter, _ = _make_otel_providers()
        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")

        mock_usage = MagicMock(spec=["input_tokens", "output_tokens", "cache_tokens"])
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_tokens = 20
        mock_result = MagicMock()
        mock_result.usage_metadata = mock_usage

        with llm_mod._tracer.start_as_current_span("gen_ai.chat") as span:
            llm_mod._record_token_usage(span, mock_result, "test-model", "openai")

        spans = exporter.get_finished_spans()
        s = spans[0]
        assert s.attributes["gen_ai.usage.cache_read_tokens"] == 20
        assert s.attributes.get("gen_ai.usage.cache_creation_tokens") is None
        tp.shutdown()

    @staticmethod
    def test_llm_span_carries_channel_id_and_request_id():
        """LLM span includes jiuwenclaw.channel.id and jiuwenclaw.request.id."""
        tp, _, exporter, _ = _make_otel_providers()
        import jiuwenclaw.telemetry.instrumentors.llm as llm_mod
        llm_mod._tracer = tp.get_tracer("jiuwenclaw.llm")

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_usage.cache_tokens = 0
        mock_result = MagicMock()
        mock_result.content = "ok"
        mock_result.tool_calls = []
        mock_result.usage_metadata = mock_usage
        mock_result.finish_reason = "stop"

        original_fn = AsyncMock(return_value=mock_result)

        mock_agent = MagicMock()
        mock_agent._config = MagicMock(
            model_name="test-model",
            model_config_obj=MagicMock(temperature=None, top_p=None),
            model_client_config=MagicMock(client_provider="openai"),
        )
        mock_agent.otel_channel_id = "feishu"
        mock_agent.otel_session_id = "sess_001"
        mock_agent.otel_request_id = "req_001"

        async def run():
            channel_id = getattr(mock_agent, "otel_channel_id", "")
            session_id = getattr(mock_agent, "otel_session_id", "")
            request_id = getattr(mock_agent, "otel_request_id", "")
            span_attrs = {
                "gen_ai.system": "openai",
                "gen_ai.request.model": "test-model",
                "gen_ai.operation.name": "chat",
                "gen_ai.span.type": "model",
                "jiuwenclaw.session.id": session_id,
                "jiuwenclaw.channel.id": channel_id,
                "jiuwenclaw.request.id": request_id,
            }
            with llm_mod._tracer.start_as_current_span("gen_ai.chat", attributes=span_attrs) as span:
                result = await original_fn(mock_agent, [], None, None, 10)
                llm_mod._record_token_usage(span, result, "test-model", "openai", channel_id)
                return result

        _run(run())
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.attributes["jiuwenclaw.channel.id"] == "feishu"
        assert s.attributes["jiuwenclaw.request.id"] == "req_001"
        assert s.attributes["jiuwenclaw.session.id"] == "sess_001"
        tp.shutdown()  # llm span test end

    @staticmethod
    def test_tool_span_carries_channel_id_and_request_id():
        """Tool span includes jiuwenclaw.channel.id and jiuwenclaw.request.id."""
        tp, _, exporter, _ = _make_otel_providers()
        import jiuwenclaw.telemetry.instrumentors.tool as tool_mod
        tool_mod._tracer = tp.get_tracer("jiuwenclaw.tool")
        tool_mod._active_tool_spans.clear()

        span = tool_mod._tracer.start_span(
            "gen_ai.tool.execute: web_search",
            attributes={
                "gen_ai.tool.name": "web_search",
                "gen_ai.tool.call.id": "call_001",
                "gen_ai.span.type": "tool",
                "jiuwenclaw.session.id": "sess_001",
                "jiuwenclaw.channel.id": "feishu",
                "jiuwenclaw.request.id": "req_001",
            },
        )
        span.set_status(StatusCode.OK)
        span.end()

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.attributes["jiuwenclaw.channel.id"] == "feishu"
        assert s.attributes["jiuwenclaw.request.id"] == "req_001"
        assert s.attributes["jiuwenclaw.session.id"] == "sess_001"
        tp.shutdown()  # tool span test end


# ---------------------------------------------------------------------------
# Queue instrumentor tests
# ---------------------------------------------------------------------------

class TestQueueInstrumentor:
    """Tests for jiuwenclaw.telemetry.instrumentors.queue."""

    @staticmethod
    def test_enqueue_increments_counter():
        """publish_user_messages should increment queue_enqueued counter."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        msg = MagicMock()
        msg.channel_id = "web"

        with patch.object(metrics_mod.queue_enqueued, "add") as mock_add:
            queue_mod._on_enqueue(msg, "user")

        mock_add.assert_called_once()
        call_args = mock_add.call_args
        assert call_args[0][0] == 1  # value
        assert call_args[0][1]["queue"] == "user"
        assert call_args[0][1]["jiuwenclaw.channel.id"] == "web"
        assert hasattr(msg, "_otel_enqueue_time")

    @staticmethod
    def test_dequeue_increments_counter_and_records_wait():
        """consume should increment dequeued, message_processed, and record wait_duration."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        msg = MagicMock()
        msg.channel_id = "feishu"
        msg._otel_enqueue_time = time.monotonic() - 0.05  # 50ms ago

        with patch.object(metrics_mod.queue_dequeued, "add") as mock_dequeued, \
             patch.object(metrics_mod.message_processed, "add") as mock_processed, \
             patch.object(metrics_mod.queue_wait_duration, "record") as mock_wait:
            queue_mod._on_dequeue(msg, "user")

        expected_base_attrs = {"queue": "user", "jiuwenclaw.channel.id": "feishu"}
        mock_dequeued.assert_called_once()
        mock_processed.assert_called_once()
        mock_wait.assert_called_once()
        recorded_ms = mock_wait.call_args[0][0]
        assert recorded_ms >= 40  # at least ~40ms

    @staticmethod
    def test_dequeue_without_enqueue_time_skips_wait_duration():
        """If msg has no _otel_enqueue_time, wait_duration should not be recorded."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        msg = MagicMock(spec=["channel_id"])
        msg.channel_id = "web"

        with patch.object(metrics_mod.queue_dequeued, "add"), \
             patch.object(metrics_mod.message_processed, "add"), \
             patch.object(metrics_mod.queue_wait_duration, "record") as mock_wait:
            queue_mod._on_dequeue(msg, "robot")

        mock_wait.assert_not_called()

    @staticmethod
    def test_depth_observer_returns_queue_sizes():
        """Depth observer callback should return Observation for both queues."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod

        mock_handler = MagicMock()
        mock_handler.user_messages_size = 3
        mock_handler.robot_messages_size = 7

        mock_cls = MagicMock()
        mock_cls.get_instance.return_value = mock_handler

        queue_mod._setup_depth_observer(mock_cls)

        from jiuwenclaw.telemetry.metrics import _queue_depth_observer
        result = _queue_depth_observer()

        assert len(result) == 2
        assert result[0].value == 3
        assert result[0].attributes == {"queue": "user"}
        assert result[1].value == 7
        assert result[1].attributes == {"queue": "robot"}

    @staticmethod
    def test_patch_publish_async():
        """Async publish wrapper should call _on_enqueue then original."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        original_called = []

        class FakeHandler:
            # pylint: disable=unused-argument
            async def publish_user_messages(self, msg):
                original_called.append(msg)

        queue_mod._patch_publish(FakeHandler, "publish_user_messages", "user", is_async=True)

        handler = FakeHandler()
        msg = MagicMock()
        msg.channel_id = "web"

        with patch.object(metrics_mod.queue_enqueued, "add"):
            _run(handler.publish_user_messages(msg))

        assert len(original_called) == 1
        assert hasattr(msg, "_otel_enqueue_time")

    @staticmethod
    def test_patch_publish_nowait():
        """Sync publish_nowait wrapper should call _on_enqueue then original."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        original_called = []

        class FakeHandler:
            # pylint: disable=unused-argument
            def publish_user_messages_nowait(self, msg):
                original_called.append(msg)

        queue_mod._patch_publish(FakeHandler, "publish_user_messages_nowait", "user", is_async=False)

        handler = FakeHandler()
        msg = MagicMock()
        msg.channel_id = "dingtalk"

        with patch.object(metrics_mod.queue_enqueued, "add"):
            handler.publish_user_messages_nowait(msg)

        assert len(original_called) == 1
        assert hasattr(msg, "_otel_enqueue_time")

    @staticmethod
    def test_patch_consume_calls_on_dequeue():
        """Consume wrapper should call _on_dequeue when msg is not None."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        msg = MagicMock()
        msg.channel_id = "web"
        msg._otel_enqueue_time = time.monotonic()

        class FakeHandler:
            async def consume_user_messages(self, timeout=None):
                return msg

        queue_mod._patch_consume(FakeHandler, "consume_user_messages", "user")

        handler = FakeHandler()
        with patch.object(metrics_mod.queue_dequeued, "add") as mock_dequeued, \
             patch.object(metrics_mod.message_processed, "add"), \
             patch.object(metrics_mod.queue_wait_duration, "record"):
            result = _run(handler.consume_user_messages(timeout=1.0))

        assert result is msg
        mock_dequeued.assert_called_once()

    @staticmethod
    def test_patch_consume_none_skips_dequeue():
        """Consume wrapper should NOT call _on_dequeue when msg is None."""
        import jiuwenclaw.telemetry.instrumentors.queue as queue_mod
        import jiuwenclaw.telemetry.metrics as metrics_mod

        class FakeHandler:
            async def consume_robot_messages(self, timeout=None):
                return None

        queue_mod._patch_consume(FakeHandler, "consume_robot_messages", "robot")

        handler = FakeHandler()
        with patch.object(metrics_mod.queue_dequeued, "add") as mock_dequeued:
            result = _run(handler.consume_robot_messages(timeout=0.1))

        assert result is None
        mock_dequeued.assert_not_called()
