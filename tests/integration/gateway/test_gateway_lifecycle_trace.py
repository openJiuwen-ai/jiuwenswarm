# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway lifecycle tracing over real local HTTP and WS connections."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from websockets.legacy.server import serve

from agent_gateway import telemetry
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from agent_gateway.extensions.agentos.agentos_router.registry_client import RegistryClient, RegistryConfig
from agent_gateway.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient


def _setup_trace(monkeypatch, request_id="r", method="chat.send", connection_id="c"):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = telemetry.start_request_trace(
        request_id, method, channel="web", connection_id=connection_id,
        trace_context={"traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                       "tracestate": "vendor=value"},
    )
    return runtime, exporter, parent


def _check_trace_headers(headers, span):
    headers = {key.lower(): value for key, value in headers.items()}
    parts = headers["traceparent"].split("-")
    assert int(parts[1], 16) == span.context.trace_id
    assert int(parts[2], 16) == span.context.span_id
    assert UUID(headers["x-trace-id"]).int == span.context.trace_id
    assert headers.get("tracestate", "") == span.context.trace_state.to_header()



pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_gateway_startup_sweep_tracing_is_configured_before_connect(monkeypatch, enabled):
    from agent_gateway import app_gateway
    from agent_gateway.extensions.registry import ExtensionRegistry
    from agent_gateway.common import config
    from agent_gateway.extensions.agentos.agentos_router.registry_client import InstanceRecord
    from agent_gateway.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
    from agent_gateway.extensions.manager import ExtensionManager

    class StopAfterSweep(Exception):
        pass

    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    monkeypatch.setattr(config, "get_config", lambda: {
        "gateway": {"agent_client": {"type": "agentos_router"}},
        "telemetry": {"enabled": enabled, "exporter": "none"},
    })
    exporter = InMemorySpanExporter()
    router = object.__new__(AgentOSRouterClient)
    router._closed = True
    router._stale_cleanup_task = None
    router._ensure_idle_reaper_task = Mock()
    router._agent_manager = SimpleNamespace(list_all_agents=AsyncMock(return_value=[]))
    router._registry = SimpleNamespace(
        enabled=True,
        list_instances=AsyncMock(return_value=[InstanceRecord(service_id="service", instance_id="sandbox")]),
        unregister_instance=AsyncMock(return_value={"deleted": True}),
    )

    async def connect_frontend(uri):
        runtime = telemetry._runtime
        assert runtime is not None
        assert (runtime.provider is not None) is enabled
        if enabled:
            runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))

    router._yuanrong = SimpleNamespace(connect=connect_frontend, delete_sandbox=AsyncMock())

    class Client:
        async def connect(self, uri):
            await router.connect(uri)
            await router._stale_cleanup_task
            raise StopAfterSweep

    extension = SimpleNamespace(metadata=SimpleNamespace(id="agentos_router", name="agentos_router"), get_client=Client)
    registry = SimpleNamespace(
        get_agent_server_client_extension=lambda: extension,
        get_third_agent_extension=lambda: None,
    )
    manager = SimpleNamespace(load_all_extensions=AsyncMock(), list_extensions=lambda: [])
    monkeypatch.setattr(ExtensionRegistry, "create_instance", lambda **kwargs: registry)
    monkeypatch.setattr(ExtensionManager, "__new__", lambda cls, **kwargs: manager)

    with pytest.raises(StopAfterSweep):
        await app_gateway._run_with_shutdown_signals(
            agent_server_url="", web_host="127.0.0.1", web_port=0, web_path="/ws",
        )

    assert telemetry._runtime is None
    router._registry.list_instances.assert_awaited_once_with(include_unhealthy=True)
    router._yuanrong.delete_sandbox.assert_awaited_once_with("sandbox")
    assert [span.name for span in exporter.get_finished_spans()] == (["gw.agent.sweep"] if enabled else [])


@pytest.fixture
def prep_tracing(monkeypatch):
    runtime, exporter, parent = _setup_trace(monkeypatch, request_id="request", connection_id="connection")
    try:
        yield parent, exporter
    finally:
        parent.complete()
        runtime.provider.shutdown()


@pytest.mark.asyncio
async def test_create_poll_and_registry_headers_reach_real_http_server(prep_tracing):
    parent, exporter = prep_tracing
    requests = []
    poll_count = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self):
            nonlocal poll_count
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size)) if size else {}
            requests.append((self.command, self.path, dict(self.headers), body))
            if self.path == "/api/agent":
                response = {"code": 200, "instance_id": "instance-one"}
            elif self.path.startswith("/api/agent/"):
                poll_count += 1
                response = {"code": 200, "instance": {"status": "starting" if poll_count == 1 else "running"}}
            elif "launch-spec" in self.path:
                response = {"framework": "third", "framework_version": "v1"}
            else:
                response = {"service_id": "service-one", **body}
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = respond
        do_POST = respond
        do_PATCH = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    frontend = YuanrongFrontendAgentClient(frontend_endpoint=endpoint, function_version_urn="fn",
                                           wait_running_interval_s=0)
    registry = RegistryClient(RegistryConfig(endpoint=endpoint))
    try:
        await frontend.connect("")
        with telemetry.bind_request_trace(parent):
            sandbox = await frontend.create_sandbox(namespace="default", name="agent", workspace="/workspace",
                runtime_spec={"runtime": "docker", "rootfs": {"imageurl": "runtime:latest"}})
            assert sandbox.sandbox_id == "instance-one"
            assert (await frontend.wait_until_running(sandbox.sandbox_id))["status"] == "running"
            await registry.get_launch_spec("third")
            await registry.register_instance(service_id="service-one", kind="agent", framework="third",
                framework_version="v1", node="", address="", user="user", instance_id=sandbox.sandbox_id)
            await registry.update_instance("service-one", node="node-one", address="10.0.0.1")
        spans = [span for span in exporter.get_finished_spans() if span.name != "gw.frontend.wait_running"]
        assert len(requests) == len(spans) == 6
        for request, span in zip(requests, spans):
            _check_trace_headers(request[2], span)
            assert span.attributes["http.response.status_code"] == 200
        assert requests[0][3]["name"] == "agent"
        assert requests[-1][3]["address"] == "10.0.0.1"
    finally:
        await registry.close()
        await frontend.disconnect()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
async def test_preparation_headers_reach_real_websocket_handshake(prep_tracing):
    parent, exporter = prep_tracing
    headers = {}

    async def handler(socket, path):
        headers.update(dict(socket.request_headers))
        await socket.send(json.dumps({"type": "event", "event": "connection.ack",
                                      "payload": {"readiness": "AGENT_READY"}}))
        await socket.wait_closed()

    server = await serve(handler, "127.0.0.1", 0)
    client = WebSocketAgentServerClient()
    try:
        with telemetry.bind_request_trace(parent):
            await client.connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                 extra_headers={"Authorization": "test-auth", "X-Trace-Id": "legacy"})
            assert telemetry.current_request_trace() is parent
        span = next(span for span in exporter.get_finished_spans() if span.name == "gw.agent.ws.connect")
        _check_trace_headers(headers, span)
        assert headers["authorization"] == "test-auth"
        assert client.server_ready and client.agent_ready
        assert span.parent.span_id == parent.span.get_span_context().span_id
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cas_mismatch", [False, True])
async def test_delete_headers_and_cas_result_over_real_http(monkeypatch, cas_mismatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    runtime = telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_DELETE(self):
            observed.append((self.path, dict(self.headers)))
            registry_call = self.path.startswith("/api/instances/")
            status = 409 if registry_call and cas_mismatch else 200
            body = {"deleted": not cas_mismatch} if registry_call else {"code": 200}
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    frontend = YuanrongFrontendAgentClient(frontend_endpoint=endpoint, function_version_urn="fn")
    registry = RegistryClient(RegistryConfig(endpoint=endpoint))
    try:
        await frontend.connect("")
        with telemetry.trace_operation("gw.agent.delete", root=True, attributes={"source": "explicit"}) as operation:
            with telemetry.bind_cleanup_trace(operation):
                assert await frontend.delete_sandbox("instance-one") is None
                result = await registry.unregister_instance("service-one", expected_instance_id="instance-one")
        assert result["deleted"] is (not cas_mismatch)
        assert parse_qs(urlsplit(observed[1][0]).query) == {"instance_id": ["instance-one"]}
        spans = [span for span in exporter.get_finished_spans() if span.name != "gw.agent.delete"]
        assert [span.name for span in spans] == ["gw.frontend.delete", "registry.unregister"]
        for (_, headers), span in zip(observed, spans):
            _check_trace_headers(headers, span)
            assert span.context.trace_id == operation.span.get_span_context().trace_id
            assert span.parent.span_id == operation.span.get_span_context().span_id
            assert span.attributes["http.request.method"] == "DELETE"
        assert operation.span.attributes["cleanup.result"] == ("partial" if cas_mismatch else "success")
        assert operation.outcome == ("incomplete" if cas_mismatch else "success")
    finally:
        await frontend.disconnect()
        await registry.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        runtime.provider.shutdown()
