# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OTLP exporter behavior over controlled local HTTP and gRPC services."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_gateway import telemetry



pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def reset_runtime(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    for name in ("OTEL_ENABLED", "OTEL_TRACES_SAMPLER", "OTEL_EXPORTER_OTLP_PROTOCOL",
                 "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
                 "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS",
                 "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_BSP_MAX_QUEUE_SIZE",
                 "OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "OTEL_BSP_SCHEDULE_DELAY", "OTEL_BSP_EXPORT_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    yield
    telemetry._runtime = None


@pytest.mark.asyncio
async def test_real_http_export_and_environment_precedence(monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get("x-test"), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-test=public")
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True}})
        runtime.tracer.start_span("test").end()
        await telemetry.shutdown_gateway_telemetry()
        assert len(received) == 1
        assert received[0][0:2] == ("/v1/traces", "public")
        assert received[0][2]
        assert runtime.diagnostics.counts["exported"] == 1
        assert runtime.diagnostics.counts["pending"] == 0
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_rejected_export_is_counted_and_never_exposes_error_in_gateway_log(monkeypatch, caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(400, "sensitive-reason")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "protocol": "http",
                    "endpoint": f"http://127.0.0.1:{server.server_port}"}})
        runtime.tracer.start_span("test").end()
        await telemetry.shutdown_gateway_telemetry()
        assert runtime.diagnostics.counts["export_failed"] == 1
        assert "sensitive-reason" not in " ".join(r.getMessage() for r in caplog.records if r.name == telemetry.__name__)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_admission_limit_and_unsampled(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            release.wait(3)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "2")
        monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "protocol": "http",
                    "endpoint": f"http://127.0.0.1:{server.server_port}"}})
        start = time.monotonic()
        runtime.tracer.start_span("first").end()
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(10):
            runtime.tracer.start_span("additional").end()
        stats = telemetry.gateway_telemetry_stats()
        assert stats["pending"] == 2
        assert stats["admission_dropped"] == 9
        assert time.monotonic() - start < 2
        release.set()
        await telemetry.shutdown_gateway_telemetry()
        assert runtime.diagnostics.counts["exported"] == 2
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "protocol": "http",
                    "endpoint": f"http://127.0.0.1:{server.server_port}"}})
        runtime.tracer.start_span("unsampled").end()
        assert telemetry.gateway_telemetry_stats()["ended"] == 0
        await telemetry.shutdown_gateway_telemetry()
    finally:
        release.set()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_slow_http_request(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            release.wait(8)
            try:
                self.send_response(200)
                self.end_headers()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "protocol": "http",
                    "endpoint": f"http://127.0.0.1:{server.server_port}"}})
        runtime.tracer.start_span("slow").end()
        assert await asyncio.to_thread(entered.wait, 2)
        started = time.monotonic()
        await telemetry.shutdown_gateway_telemetry()
        assert time.monotonic() - started < 6
        assert runtime.diagnostics.stopped.wait(1)
        assert runtime.diagnostics.counts["exported"] == 0
    finally:
        release.set()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_real_grpc_export_preserves_trace_resource_and_headers(monkeypatch):
    import grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

    received = []

    class Service(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):
            received.append((request, dict(context.invocation_metadata())))
            return trace_service_pb2.ExportTraceServiceResponse()

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-test=grpc-value")
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True}})
        span = runtime.tracer.start_span("grpc-test")
        trace_id = span.get_span_context().trace_id
        span.end()
        await telemetry.shutdown_gateway_telemetry()
        assert len(received) == 1
        request, metadata = received[0]
        resource_spans = request.resource_spans[0]
        assert {item.key: item.value.string_value for item in resource_spans.resource.attributes}["service.name"] == "jiuwenswarm-gateway"
        assert int.from_bytes(resource_spans.scope_spans[0].spans[0].trace_id, "big") == trace_id
        assert metadata["x-test"] == "grpc-value"
        assert runtime.diagnostics.counts["exported"] == 1
    finally:
        server.stop(0).wait()


@pytest.mark.asyncio
async def test_grpc_unavailable_has_finite_export_budget(monkeypatch):
    import grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

    calls = []

    class Service(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):
            calls.append(1)
            context.abort(grpc.StatusCode.UNAVAILABLE, "collector unavailable")

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        monkeypatch.setenv("OTEL_BSP_EXPORT_TIMEOUT", "200")
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True,
                    "endpoint": f"http://127.0.0.1:{port}"}})
        runtime.tracer.start_span("grpc-failure").end()
        started = time.monotonic()
        await telemetry.shutdown_gateway_telemetry()
        assert time.monotonic() - started < 2
        assert calls
        assert runtime.diagnostics.counts["export_failed"] == 1
        assert runtime.diagnostics.stopped.is_set()
    finally:
        server.stop(0).wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["http", "grpc"])
@pytest.mark.parametrize(
    ("headers", "trace_headers", "expected"),
    [
        ({"authorization": "common-secret"}, {}, "common-secret"),
        ({}, {}, "trace-secret"),
        ({"authorization": "common-secret"}, {"authorization": "configured-trace"}, "configured-trace"),
    ],
)
async def test_trace_headers_fallback(monkeypatch, protocol, headers, trace_headers, expected):
    received = []
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=env-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "authorization=trace-secret")
    if protocol == "http":
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.headers.get("authorization"))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
    else:
        import grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

        class Service(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request, context):
                received.append(dict(context.invocation_metadata()).get("authorization"))
                return trace_service_pb2.ExportTraceServiceResponse()

        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Service(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
    try:
        runtime = telemetry.configure_gateway_telemetry({"telemetry": {
            "enabled": True, "protocol": protocol, "endpoint": f"http://127.0.0.1:{port}",
            "headers": headers, "traces": {"headers": trace_headers},
        }})
        runtime.tracer.start_span("test").end()
        await telemetry.shutdown_gateway_telemetry()
        assert received == [expected]
        assert runtime.diagnostics.counts["exported"] == 1
    finally:
        if protocol == "http":
            server.shutdown()
            server.server_close()
        else:
            server.stop(0).wait()
