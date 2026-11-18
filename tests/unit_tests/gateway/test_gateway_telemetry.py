from types import SimpleNamespace
import asyncio
import logging
import threading

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agent_gateway import telemetry
from agent_gateway.extensions.agentos.agentos_router.agent_manager import AgentManager


@pytest.fixture(autouse=True)
def reset_runtime(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.setattr(telemetry, "_context_warning_at", float("-inf"))
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)
    for name in ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
                 "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                 "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
                 "OTEL_BSP_MAX_QUEUE_SIZE", "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
                 "OTEL_BSP_SCHEDULE_DELAY", "OTEL_BSP_EXPORT_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    yield
    telemetry._runtime = None


def test_disabled_and_environment_override(monkeypatch):
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    assert telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c")
    telemetry._runtime = None
    monkeypatch.setenv("OTEL_ENABLED", "false")
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True}})
    assert telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c") is None


@pytest.mark.parametrize("settings", ["invalid", ["invalid"], {"enabled": []}, {"enabled": "invalid"}])
def test_invalid_configuration_is_non_fatal(settings):
    telemetry.configure_gateway_telemetry({"telemetry": settings})
    assert telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c") is None


def test_false_string_and_unknown_protocol_do_not_enable_export():
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": "false"}})
    assert telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c") is None
    telemetry._runtime = None
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "protocol": "invalid"}})
    assert telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c") is None


def test_scope_isolation_and_wire_propagation():
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    first = telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c")
    with telemetry.bind_request_trace(first):
        second = telemetry.start_request_trace("s", "models.list", channel="web", connection_id="c")
        assert first.trace_id != second.trace_id
        envelope = SimpleNamespace(request_id="r", channel_context={"business": "kept"}, traceparent=None)
        telemetry.inject_request_trace(envelope)
        remote = trace.get_current_span(TraceContextTextMapPropagator().extract({"traceparent": envelope.traceparent})).get_span_context()
        assert format(remote.trace_id, "032x") == first.trace_id
        assert remote.span_id == first.span.get_span_context().span_id
        assert envelope.channel_context == {"business": "kept"}
        mismatch = SimpleNamespace(request_id="s", channel_context={"business": "kept"}, traceparent=None)
        telemetry.inject_request_trace(mismatch)
        assert mismatch.channel_context == {"business": "kept"}
        assert mismatch.traceparent is None
        with telemetry.bind_request_trace(None):
            assert telemetry.current_request_trace() is None
        assert telemetry.current_request_trace() is first
    assert telemetry.current_request_trace() is None
    first.finish("success")
    first.finish("failed")
    assert first.finished


def test_unsampled_context_still_propagates():
    provider = TracerProvider(sampler=ALWAYS_OFF)
    telemetry._runtime = telemetry.GatewayTelemetry(tracer=provider.get_tracer("test"), provider=provider)
    operation = telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c")
    assert operation.trace_id != "0" * 32
    with telemetry.bind_request_trace(operation):
        envelope = SimpleNamespace(request_id="r", channel_context={})
        telemetry.inject_request_trace(envelope)
    assert envelope.traceparent.endswith("-00")
    assert envelope.channel_context == {}
    provider.shutdown()


@pytest.mark.asyncio
async def test_repeated_configuration_and_shutdown():
    config = {"telemetry": {"enabled": True, "exporter": "none"}}
    runtime = telemetry.configure_gateway_telemetry(config)
    assert telemetry.configure_gateway_telemetry(config) is runtime
    await telemetry.shutdown_gateway_telemetry()
    await telemetry.shutdown_gateway_telemetry()
    assert telemetry._runtime is None


def test_http_exporter_options_and_precedence(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as http_exporter
    import opentelemetry.sdk.trace.export as sdk_export
    captured = {}

    def exporter(**kwargs):
        captured.update(kwargs)
        return object()

    class Processor:
        def __init__(self, exporter, **kwargs):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=None):
            return True

        def on_start(self, span, parent_context=None):
            pass

        def on_end(self, span):
            pass

    monkeypatch.setattr(http_exporter, "OTLPSpanExporter", exporter)
    monkeypatch.setattr(sdk_export, "BatchSpanProcessor", Processor)
    telemetry.configure_gateway_telemetry({"telemetry": {
        "enabled": True, "exporter": "none", "endpoint": "http://common:4318",
        "protocol": "grpc", "headers": {"public": "value"},
        "traces": {"exporter": "otlp", "protocol": "http", "endpoint": "http://collector:4318", "headers": None},
    }})
    assert captured == {"endpoint": "http://collector:4318/v1/traces", "headers": {"public": "value"}, "timeout": 5}
    telemetry._runtime.provider.shutdown()


def test_config_error_does_not_expose_headers(monkeypatch, caplog):
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as http_exporter

    def fail(**kwargs):
        raise ValueError("secret-token")

    monkeypatch.setattr(http_exporter, "OTLPSpanExporter", fail)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {
        "enabled": True, "exporter": "otlp", "protocol": "http", "headers": {"authorization": "secret-token"},
    }})
    assert runtime.tracer is None
    assert "secret-token" not in caplog.text


def test_attributes_events_and_formatter_failure(monkeypatch):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._runtime = telemetry.GatewayTelemetry(
        tracer=provider.get_tracer("test"), provider=provider, user_id_formatter=lambda value: "masked"
    )
    operation = telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c", user_id="private", session_id="s")
    messages = []
    monkeypatch.setattr(telemetry.logger, "info", lambda *args: messages.append(args))
    operation.event("received", {"queue_wait_ms": 3, "user_id": "private", "payload": "secret"})
    operation.finish("success")
    span = exporter.get_finished_spans()[0]
    assert span.name == "gw.session.message"
    assert span.attributes["user_id"] == "masked"
    assert span.attributes["method"] == "models.list"
    assert next(event for event in span.events if "queue_wait_ms" in event.attributes).attributes == {"queue_wait_ms": 3}
    assert "masked" in messages[0]
    assert "private" not in str(messages) and "secret" not in str(messages)

    def broken(value):
        raise ValueError("private")

    telemetry._runtime.user_id_formatter = broken
    operation = telemetry.start_request_trace("r2", "models.list", channel="web", connection_id="c", user_id="private")
    assert operation is not None
    operation.finish("failed")
    assert "user_id" not in exporter.get_finished_spans()[1].attributes
    provider.shutdown()


def test_bind_attach_failure_restores_scope(monkeypatch):
    from opentelemetry import context
    monkeypatch.setattr(context, "attach", lambda value: (_ for _ in ()).throw(RuntimeError("broken")))
    operation = object()
    with telemetry.bind_request_trace(operation):
        assert telemetry.current_request_trace() is operation
    assert telemetry.current_request_trace() is None


def test_failure_and_send_summaries_survive_span_failure(monkeypatch):
    messages = []
    monkeypatch.setattr(telemetry.logger, "info", lambda *args: messages.append(args))

    class BrokenSpan:
        def get_span_context(self):
            raise RuntimeError("broken")

        def add_event(self, *args):
            raise RuntimeError("broken")

        def set_attribute(self, *args):
            raise RuntimeError("broken")

    operation = telemetry.TraceOperation("r", "models.list", "web", "c", BrokenSpan(), None, 0)
    assert operation.trace_id == ""
    operation.event("send.returned", {"outcome": "failed", "elapsed_ms": 4, "send_ms": 2, "payload": "secret"})
    operation.finish("failed", reason="send_failed")
    assert "outcome=failed" in messages[0][-1]
    assert "elapsed_ms=4" in messages[0][-1]
    assert "send_ms=2" in messages[0][-1]
    assert "secret" not in str(messages)
    assert messages[1][-2:] == ("failed", "send_failed")
    assert operation.finished


def test_boundary_and_failure_summaries_survive_event_limit(monkeypatch):
    messages = []
    monkeypatch.setattr(telemetry.logger, "info", lambda *args: messages.append(args))
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace("r", "chat.send", channel="web", connection_id="c")
    try:
        for _ in range(100):
            operation.event("preprocess.credentials", {"duration_ms": 1})
        for _ in range(100):
            operation.event("send.returned", {"outcome": "written"})
            operation.event("client.written", {"outcome": "written"})
            operation.event("preprocess.credentials", {"duration_ms": 1})
        operation.finish("failed", reason="response_failed")

        names = [event.name for event in operation.span.events]
        assert len(names) <= 67
        assert names.count("received") == 1
        assert names.count("send.returned") == 1
        assert names.count("client.written") == 1
        summaries = [args[1] for args in messages if args[0].startswith("event=%s")]
        assert summaries == ["received", "send.returned", "client.written"]
        assert messages[-1][-2:] == ("failed", "response_failed")
    finally:
        runtime.provider.shutdown()


def test_bind_detach_failure_restores_scope(monkeypatch):
    from opentelemetry import context
    monkeypatch.setattr(context, "detach", lambda value: (_ for _ in ()).throw(RuntimeError("broken")))
    with telemetry.bind_request_trace(None):
        assert telemetry.current_request_trace() is None
    assert telemetry.current_request_trace() is None


def test_traceparent_wins_and_outbound_uses_gateway_span():
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace(
        "r", "models.list", channel="web", connection_id="c",
        trace_context={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=value",
            "trace_id": "1" * 32,
        },
    )
    assert operation.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert operation.span.parent.span_id == int("00f067aa0ba902b7", 16)
    assert operation.span.parent.is_remote
    envelope = SimpleNamespace(request_id="r", traceparent="stale", channel_context={"business": "kept", "traceparent": "stale", "tracestate": "stale=value"})
    with telemetry.bind_request_trace(operation):
        telemetry.inject_request_trace(envelope)
    sent = trace.get_current_span(TraceContextTextMapPropagator().extract({"traceparent": envelope.traceparent})).get_span_context()
    assert sent.span_id == operation.span.get_span_context().span_id
    assert sent.trace_id == operation.span.parent.trace_id
    assert list(operation.span.get_span_context().trace_state.items()) == [("vendor", "value")]
    assert not sent.trace_state
    assert envelope.channel_context == {"business": "kept", "traceparent": "stale", "tracestate": "stale=value"}
    operation.finish("success")


@pytest.mark.parametrize("carrier", [
    {"trace_id": "4BF92F3577B34DA6A3CE929D0E0E4736"},
    {"trace_id": "4bf92f35-77b3-4da6-a3ce-929d0e0e4736"},
    {"traceparent": "invalid", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"},
    {"trace_id": "invalid", "X-Trace-Id": "4bf92f35-77b3-4da6-a3ce-929d0e0e4736"},
    {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "X-Trace-Id": "1" * 32},
])
def test_standalone_id_is_a_real_root_and_does_not_leak(carrier):
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c", trace_context=carrier)
    assert operation.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert operation.span.parent is None
    assert operation.span.attributes["gateway.trace_source"] == "trace_id"
    with telemetry.bind_request_trace(operation):
        envelope = SimpleNamespace(request_id="r", channel_context={"tracestate": "stale=value"})
        telemetry.inject_request_trace(envelope)
        unrelated = telemetry.start_request_trace("r", "models.list", channel="web", connection_id="other")
    sent = trace.get_current_span(TraceContextTextMapPropagator().extract({"traceparent": envelope.traceparent})).get_span_context()
    assert format(sent.trace_id, "032x") == operation.trace_id
    assert sent.span_id == operation.span.get_span_context().span_id
    assert envelope.channel_context == {"tracestate": "stale=value"}
    assert unrelated.trace_id != operation.trace_id
    assert unrelated.span.parent is None
    assert telemetry._root_trace_id.get() == 0
    operation.finish("success")
    unrelated.finish("success")


@pytest.mark.parametrize("carrier", [
    None, [], "invalid", {}, {"trace_id": "0" * 32}, {"trace_id": 123},
    {"trace_id": "{4bf92f35-77b3-4da6-a3ce-929d0e0e4736}"},
    {"traceparent": "00-" + "0" * 32 + "-00f067aa0ba902b7-01"},
    {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01"},
    {"traceparent": ["invalid"]}, {"traceparent": "a" * 513},
    {"tracestate": "orphan=value"},
])
def test_missing_or_invalid_context_creates_fresh_roots(carrier):
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operations = [telemetry.start_request_trace("same", "models.list", channel="web", connection_id="c", trace_context=carrier) for _ in range(2)]
    assert operations[0].trace_id != operations[1].trace_id
    for operation in operations:
        assert int(operation.trace_id, 16) != 0
        assert operation.span.parent is None
        assert not operation.span.get_span_context().trace_state
        operation.finish("success")


@pytest.mark.parametrize("state", ["secret\ninvalid=value", "vendor=value,vendor=other", "a=" + "x" * 513, ["secret"]])
def test_invalid_tracestate_keeps_parent_and_diagnostics_are_bounded(state, caplog, monkeypatch):
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    warnings = []
    monkeypatch.setattr(telemetry.logger, "warning", lambda *args: warnings.append(args))
    for _ in range(3):
        operation = telemetry.start_request_trace(
            "r", "models.list", channel="web", connection_id="c",
            trace_context={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "tracestate": state},
        )
        assert operation.span.parent.span_id == int("00f067aa0ba902b7", 16)
        assert not operation.span.get_span_context().trace_state
        operation.finish("success")
    assert warnings == [("Gateway trace context ignored: reason=%s", "invalid_tracestate")]
    assert "secret" not in str(warnings) + caplog.text


@pytest.mark.parametrize("state, expected", [
    ("a=1,,b=2", [("a", "1"), ("b", "2")]),
    (", \t,a=1, \t,b=2,", [("a", "1"), ("b", "2")]),
    (" \t,, \t", []),
])
def test_tracestate_empty_members_are_ignored(state, expected, caplog, monkeypatch):
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    warnings = []
    monkeypatch.setattr(telemetry.logger, "warning", lambda *args: warnings.append(args))
    caplog.clear()
    operation = telemetry.start_request_trace(
        "r", "models.list", channel="web", connection_id="c",
        trace_context={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "tracestate": state},
    )
    assert operation.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert operation.span.parent.span_id == int("00f067aa0ba902b7", 16)
    assert operation.span.parent.is_remote
    assert list(operation.span.get_span_context().trace_state.items()) == expected
    with telemetry.bind_request_trace(operation):
        headers = telemetry.inject_request_headers({"tracestate": "stale=value"})
    sent = trace.get_current_span(TraceContextTextMapPropagator().extract(headers)).get_span_context()
    assert sent.trace_id == operation.span.parent.trace_id
    assert sent.span_id == operation.span.get_span_context().span_id
    assert list(sent.trace_state.items()) == expected
    if expected:
        assert headers["tracestate"] == "a=1,b=2"
    else:
        assert "tracestate" not in headers
    assert not warnings
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    operation.finish("success")


@pytest.mark.parametrize("sampler,arg", [("parentbased_always_off", None), ("parentbased_traceidratio", "0")])
def test_browser_sampled_flag_does_not_override_server_policy(monkeypatch, sampler, arg):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", sampler)
    if arg is not None:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", arg)
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace(
        "r", "models.list", channel="web", connection_id="c",
        trace_context={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    assert operation.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert not operation.span.is_recording()
    with telemetry.bind_request_trace(operation):
        envelope = SimpleNamespace(request_id="r", channel_context={})
        telemetry.inject_request_trace(envelope)
    assert envelope.traceparent.endswith("-00")
    assert envelope.channel_context == {}
    operation.finish("success")


@pytest.mark.asyncio
async def test_dedicated_provider_does_not_modify_or_shutdown_global_provider(monkeypatch):
    provider = TracerProvider()
    original_generator = provider.id_generator
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    closed = []
    monkeypatch.setattr(provider, "shutdown", lambda: closed.append(True))
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    assert runtime.owned and runtime.provider is not provider
    operation = telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c", trace_context={"trace_id": "1" * 32})
    assert operation.trace_id == "1" * 32
    operation.finish("success")
    await telemetry.shutdown_gateway_telemetry()
    assert provider.id_generator is original_generator
    assert trace.get_tracer_provider() is provider
    assert not closed


@pytest.mark.asyncio
async def test_concurrent_requests_share_a_trace_without_sharing_spans():
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})

    async def run(index):
        await asyncio.sleep(0)
        operation = telemetry.start_request_trace(
            "same", "models.list", channel="web", connection_id=str(index),
            trace_context={"trace_id": "1" * 32},
        )
        with telemetry.bind_request_trace(operation):
            await asyncio.sleep(0)
            assert telemetry.current_request_trace() is operation
            assert telemetry._root_trace_id.get() == 0
        operation.finish("success")
        return operation

    operations = await asyncio.gather(*(run(index) for index in range(8)))
    assert {operation.trace_id for operation in operations} == {"1" * 32}
    assert len({operation.span.get_span_context().span_id for operation in operations}) == 8
    assert all(operation.span.parent is None for operation in operations)
    assert telemetry.current_request_trace() is None


def test_id_override_is_consumed_once_and_restored_when_span_creation_fails(monkeypatch):
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})

    def fail(*args, **kwargs):
        generator = runtime.provider.id_generator
        assert generator.generate_trace_id() == int("1" * 32, 16)
        assert generator.generate_trace_id() != int("1" * 32, 16)
        raise RuntimeError("SDK failure")

    monkeypatch.setattr(runtime.tracer, "start_span", fail)
    assert telemetry.start_request_trace("r", "models.list", channel="web", connection_id="c", trace_context={"trace_id": "1" * 32}) is None
    assert telemetry._root_trace_id.get() == 0


def test_browser_cannot_select_an_id_that_forces_ratio_sampling(monkeypatch):
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")
    monkeypatch.setattr(RandomIdGenerator, "generate_trace_id", lambda self: (1 << 128) - 1)
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace(
        "r", "models.list", channel="web", connection_id="c",
        trace_context={"traceparent": "00-00000000000000000000000000000001-00f067aa0ba902b7-01"},
    )
    assert operation.trace_id == "0" * 31 + "1"
    assert not operation.span.is_recording()
    operation.finish("success")


def test_unassociated_transport_masks_gateway_owner_and_restores_sdk_scope():
    from opentelemetry import context

    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace("same", "models.list", channel="web", connection_id="c")
    unrelated = runtime.provider.get_tracer("other").start_span("other")
    envelope = SimpleNamespace(request_id="same", method="models.list", channel="cli")
    with telemetry.bind_request_trace(operation):
        token = context.attach(trace.set_span_in_context(unrelated, context.Context()))
        try:
            with telemetry.bind_agent_request_trace(envelope) as scoped:
                assert scoped is None
                assert telemetry.current_request_trace() is None
                assert trace.get_current_span() is unrelated
                assert telemetry.inject_request_headers({"business": "kept"}) == {"business": "kept"}
            assert telemetry.current_request_trace() is operation
            assert trace.get_current_span() is unrelated
        finally:
            context.detach(token)
    unrelated.end()
    operation.finish("success")


def test_explicit_owner_wins_over_same_id_on_a_different_connection():
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    first = telemetry.start_request_trace("same", "models.list", channel="web", connection_id="first")
    second = telemetry.start_request_trace("same", "models.list", channel="web", connection_id="second")
    envelope = SimpleNamespace(request_id="same", method="models.list", channel="web", _gateway_trace_operation=second)
    with telemetry.bind_request_trace(first):
        with telemetry.bind_agent_request_trace(envelope) as scoped:
            assert scoped is second
            assert telemetry.current_request_trace() is second
            assert telemetry.inject_request_headers({})["traceparent"].split("-")[1] == second.trace_id
        assert telemetry.current_request_trace() is first
    first.finish("success")
    second.finish("success")


def test_nested_derived_request_reuses_its_span_and_inherits_sampling():
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    parent = telemetry.start_request_trace("main", "chat.send", channel="web", connection_id="c")
    envelope = SimpleNamespace(request_id="derived", method="cron.jobs.sync", channel="web", _gateway_trace_operation=parent)
    with telemetry.bind_request_trace(parent):
        with telemetry.bind_agent_request_trace(envelope) as derived:
            assert derived is not parent and derived.trace_id == parent.trace_id
            assert derived.span.parent.span_id == parent.span.get_span_context().span_id
            with telemetry.bind_agent_request_trace(envelope) as nested:
                assert nested is derived
            assert not derived.finished
        assert derived.finished
        assert telemetry.current_request_trace() is parent
    parent.finish("success")


def test_clearing_request_scope_also_clears_response_owner():
    from e2a.message import Message

    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace("r", "history.get", channel="web", connection_id="c")
    response = Message(id="r", type="event", channel_id="web", session_id="s", params={}, timestamp=0, ok=True)
    telemetry.attach_message_trace(response, operation)
    with telemetry.bind_message_trace(response):
        assert telemetry.current_response_trace() is operation
        with telemetry.bind_request_trace(None):
            assert telemetry.current_response_trace() is None
            assert telemetry.current_request_trace() is None
        assert telemetry.current_response_trace() is operation
    assert telemetry.current_response_trace() is None
    operation.finish("success")


def test_late_response_does_not_rebind_to_same_id_on_another_connection():
    from e2a.message import Message

    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    first = telemetry.start_request_trace("same", "chat.send", channel="web", connection_id="first")
    second = telemetry.start_request_trace("same", "chat.send", channel="web", connection_id="second")
    response = Message(id="same", type="event", channel_id="web", session_id="s", params={}, timestamp=0, ok=True)
    telemetry.attach_message_trace(response, first)
    first.finish("incomplete", reason="closed")
    with telemetry.bind_request_trace(second):
        assert telemetry.attach_message_trace(response) is None
        assert telemetry.queue_response_trace(response) is response
    assert response._gateway_trace_operation is first
    assert second.pending_deliveries == 0
    second.complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_ws, expected", [("origin", "incomplete"), ("observer", "success")])
async def test_fanout_missing_origin_channel_does_not_silently_succeed(target_ws, expected):
    from agent_gateway.routing.keys import WebDeliveryTarget
    from agent_gateway.routing.session_sharing import SessionDispatcher

    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    operation = telemetry.start_request_trace("r", "chat.send", channel="web", connection_id="c")
    message = SimpleNamespace(id="r", channel_id="web", metadata={"ws_id": "origin"}, type="event")
    telemetry.attach_message_trace(message, operation)
    await SessionDispatcher._send_to_container(
        message, SimpleNamespace(intent="godview"),
        [SimpleNamespace(delivery=WebDeliveryTarget(ws_id=target_ws))],
        SessionDispatcher._ContainerKey("web", "default", target_ws),
        SimpleNamespace(get_channel=lambda key: None, enabled_channels=[]),
    )
    operation.complete()
    assert operation.outcome == expected
    if expected == "incomplete":
        assert operation.reason == "fanout_no_channel"



@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    runtime = telemetry.configure_gateway_telemetry(
        {"telemetry": {"enabled": True, "exporter": "none"}},
        user_id_formatter=lambda value: "masked-user",
    )
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield runtime, exporter
    runtime.provider.shutdown()


def test_operation_parent_links_independent_root_and_scope_restoration(tracing):
    _, exporter = tracing
    parent = telemetry.start_request_trace("r", "chat.send", channel="web", connection_id="c", session_id="s")
    with telemetry.bind_request_trace(parent):
        with telemetry.trace_operation("gw.agent.create", attributes={"user_id": "alice"}) as child:
            assert child.trace_id == parent.trace_id
            assert child.span.parent.span_id == parent.span.get_span_context().span_id
            assert trace.get_current_span() is child.span
            assert child.span.attributes["user_id"] == "masked-user"
            assert child.span.attributes["session_id"] == "s"
        with telemetry.trace_operation("gw.agent.warmup", root=True, links=[child]) as warmup:
            assert warmup.trace_id != parent.trace_id and warmup.span.parent is None
            assert warmup.span.links[0].context.span_id == child.span.get_span_context().span_id
        assert trace.get_current_span() is parent.span
        assert telemetry.current_request_trace() is parent
    parent.complete()
    assert len(exporter.get_finished_spans()) == 3


@pytest.mark.parametrize("error, outcome", [(RuntimeError("failed"), "failed"),
                                          (TimeoutError("slow"), "timeout"),
                                          (asyncio.CancelledError(), "cancelled")])
def test_operation_failure_keeps_exception_parent_and_log_volume(tracing, caplog, error, outcome):
    _, exporter = tracing
    parent = telemetry.start_request_trace("r", "history.get", channel="web", connection_id="c")
    with caplog.at_level(logging.INFO, logger="gateway.telemetry"):
        caplog.clear()
        with telemetry.bind_request_trace(parent):
            with pytest.raises(type(error)):
                with telemetry.trace_operation("gw.frontend.get") as operation:
                    raise error
            assert telemetry.current_request_trace() is parent
        assert parent.outcome == "success" and not parent.finished
        assert not [record for record in caplog.records if record.levelno >= logging.INFO]
    span = exporter.get_finished_spans()[0]
    assert span.attributes["gateway.outcome"] == outcome
    parent.complete()


def test_disabled_operation_preserves_existing_sdk_context(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    span_context = trace.SpanContext(1, 2, False)
    external = trace.NonRecordingSpan(span_context)
    token = context.attach(trace.set_span_in_context(external))
    try:
        with telemetry.trace_operation("gw.frontend.get") as operation:
            assert operation is None
            assert trace.get_current_span() is external
    finally:
        context.detach(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_shared_create_wait_links_owner_and_does_not_cancel_creator(tracing, cancel_waiter):
    _, exporter = tracing
    manager = AgentManager()
    owner = telemetry.start_request_trace("owner", "chat.send", channel="web", connection_id="one")
    waiter = telemetry.start_request_trace("waiter", "chat.send", channel="web", connection_id="two")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create(info):
        entered.set()
        await release.wait()
        info.sandbox_id = "instance-one"
        return info

    with telemetry.bind_request_trace(owner):
        create_task = asyncio.create_task(manager.get_or_create_agent("alice", "jiuwenswarm", creator=create))
    await entered.wait()
    with telemetry.bind_request_trace(waiter):
        wait_task = asyncio.create_task(manager.get_or_create_agent("alice", "jiuwenswarm", creator=create))
    await asyncio.sleep(0)
    if cancel_waiter:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        assert not create_task.done()
    release.set()
    created = await create_task
    if not cancel_waiter:
        assert (await wait_task).info.sandbox_id == created.info.sandbox_id
    wait_span = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.create.wait")
    assert wait_span.context.trace_id == waiter.span.get_span_context().trace_id
    assert wait_span.links[0].context.span_id == owner.span.get_span_context().span_id
    assert wait_span.attributes["gateway.outcome"] == ("cancelled" if cancel_waiter else "success")
    assert all(not hasattr(runtime, "_gateway_create_trace") for runtime in manager._runtimes.values())
    owner.complete()
    waiter.complete()


def test_batch_options_validate_and_bound_environment(monkeypatch):
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "3")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "100000")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "invalid")
    monkeypatch.setenv("OTEL_BSP_EXPORT_TIMEOUT", "999999")
    assert telemetry._batch_options() == {"MAX_QUEUE_SIZE": 3, "MAX_EXPORT_BATCH_SIZE": 3,
                                          "SCHEDULE_DELAY": 5000, "EXPORT_TIMEOUT": 5000}


@pytest.mark.asyncio
async def test_shutdown_does_not_report_force_flush_as_success():
    class Provider:
        def force_flush(self, timeout_millis):
            raise AssertionError("force_flush must not be called")

        def shutdown(self):
            self.closed = True

    provider = Provider()
    telemetry._runtime = telemetry.GatewayTelemetry(provider=provider, owned=True)
    await telemetry.shutdown_gateway_telemetry()
    assert provider.closed


def test_admission_drop_never_calls_logger_on_business_thread(monkeypatch):
    diagnostics = telemetry._ExportDiagnostics(object())
    diagnostics.capacity = 0
    processor = telemetry._CountingProcessor(object(), diagnostics)
    monkeypatch.setattr(telemetry.logger, "warning", lambda *args: pytest.fail("synchronous logging"))
    span = SimpleNamespace(context=SimpleNamespace(trace_flags=SimpleNamespace(sampled=True)))
    processor.on_end(span)
    assert diagnostics.counts["admission_dropped"] == 1


def test_enqueue_and_shutdown_are_serialized_with_real_bsp():
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    diagnostics = telemetry._ExportDiagnostics(exporter)
    diagnostics.capacity = 2
    bsp = BatchSpanProcessor(diagnostics, max_queue_size=2, max_export_batch_size=1)
    original_on_end = bsp.on_end
    entered, release = threading.Event(), threading.Event()

    def gated_on_end(span):
        entered.set()
        assert release.wait(3)
        original_on_end(span)

    bsp.on_end = gated_on_end

    class Processor(telemetry._CountingProcessor, SpanProcessor):
        pass

    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(Processor(bsp, diagnostics))
    span = provider.get_tracer("test").start_span("race")
    ending = threading.Thread(target=span.end)
    ending.start()
    assert entered.wait(2)
    closing = threading.Thread(target=provider.shutdown)
    closing.start()
    release.set()
    ending.join(3)
    closing.join(3)
    assert not ending.is_alive() and not closing.is_alive()
    assert len(exporter.get_finished_spans()) == 1
    assert diagnostics.counts["pending"] == 0
    assert diagnostics.counts["exported"] == 1


def test_processor_failure_is_counted_without_escaping():
    class FailingProcessor:
        def on_end(self, span):
            raise ValueError("test")

    diagnostics = telemetry._ExportDiagnostics(object())
    diagnostics.capacity = 1
    processor = telemetry._CountingProcessor(FailingProcessor(), diagnostics)
    span = SimpleNamespace(context=SimpleNamespace(trace_flags=SimpleNamespace(sampled=True)))
    processor.on_end(span)
    assert diagnostics.counts["pending"] == 0
    assert diagnostics.counts["admission_dropped"] == 1


@pytest.mark.parametrize("key", ["endpoint", "protocol", "exporter"])
def test_mapping_is_not_a_missing_scalar_option(key):
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, key: {}}})
    assert runtime.tracer is None
