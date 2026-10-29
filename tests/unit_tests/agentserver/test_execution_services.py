# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Delivery injection retains wire semantics without eagerly loading adapters."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.worker.execution_services import (
    HostCapabilities,
    ProductionHostServices,
    TransportClosed,
)


def test_shared_helpers_and_services_import_without_delivery_adapters(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc, sys
sys.path.insert(0, {str(root)!r})
blocked = ('websockets', 'jiuwenswarm.common.e2a.wire_codec',
    'jiuwenswarm.server.ws_send', 'gateway.common.ws_diagnostics',
    'jiuwenswarm.common.hook_event', 'jiuwenswarm.common.hooks_context')
class RejectAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('eager delivery adapter: ' + fullname)
sys.meta_path.insert(0, RejectAdapters())
from jiuwenswarm.server.worker.execution_services import ProductionHostServices
from jiuwenswarm.server.worker.execution_support import _StreamKeepalive, _TurnOutcomeTracker
services = ProductionHostServices()
assert services.capabilities.keepalive
assert not any(name in sys.modules for name in blocked)
print('LAZY_EXECUTION_SERVICES_OK')
"""
    env = dict(os.environ, JIUWENSWARM_DATA_DIR=str(tmp_path / "data"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LAZY_EXECUTION_SERVICES_OK" in result.stdout


@pytest.mark.asyncio
async def test_delivery_normalizes_closed_sink_and_preserves_other_failures():
    from websockets.exceptions import ConnectionClosedError

    services = ProductionHostServices()
    closed = ConnectionClosedError(None, None)
    sink = SimpleNamespace(send=AsyncMock(side_effect=closed))
    with pytest.raises(TransportClosed) as raised:
        await services.send(sink, {"request_id": "closed"})
    assert raised.value.__cause__ is closed
    assert services.is_transport_closed(raised.value)
    assert services.is_transport_closed(closed)
    details = services.diagnostics(sink, raised.value, {"request_id": "closed"})
    assert "ConnectionClosedError" in details
    assert "request_id='closed'" in details

    failure = RuntimeError("write failed")
    sink.send.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        await services.send(sink, {})
    assert raised.value is failure
    sink.send.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await services.send(sink, {})


@pytest.mark.asyncio
async def test_delivery_preserves_size_fallback_result(monkeypatch):
    from jiuwenswarm.server import ws_send

    bounded_send = AsyncMock(return_value=False)
    monkeypatch.setattr(ws_send, "send_wire_payload", bounded_send)
    sink, payload = object(), {"request_id": "oversized"}
    assert await ProductionHostServices().send(sink, payload) is False
    bounded_send.assert_awaited_once_with(sink, payload)


def test_response_and_error_encoders_keep_existing_wire_contract(monkeypatch):
    from jiuwenswarm.common.e2a import wire_codec
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk

    encoded = {}
    calls = []

    def encode(message, **kwargs):
        calls.append((message, kwargs))
        return encoded

    monkeypatch.setattr(wire_codec, "encode_agent_response_for_wire", encode)
    monkeypatch.setattr(wire_codec, "encode_agent_chunk_for_wire", encode)
    services = ProductionHostServices()
    request = AgentRequest(
        request_id="req",
        channel_id="web",
        metadata={"trace": "existing"},
    )
    assert services.error_payload(request, "bad", "DENIED") is encoded
    message, kwargs = calls[-1]
    assert message.ok is False
    assert message.payload == {"error": "bad", "code": "DENIED"}
    assert message.metadata == request.metadata
    assert kwargs == {"response_id": "req", "sequence": 0}
    chunk = AgentResponseChunk(request_id="req", channel_id="web", payload={})
    assert services.encode_chunk(chunk, response_id="req", sequence=-1) is encoded
    assert calls[-1] == (
        chunk,
        {"response_id": "req", "sequence": -1, "is_stream": True},
    )


@pytest.mark.asyncio
async def test_injected_before_chat_hook_and_disabled_capability():
    hooks = SimpleNamespace(before_chat=AsyncMock())
    request = object()
    await ProductionHostServices(hooks=hooks).before_chat(request)
    hooks.before_chat.assert_awaited_once_with(request)
    hooks.before_chat.reset_mock()
    services = ProductionHostServices(
        hooks=hooks,
        capabilities=HostCapabilities(before_chat_hook=False),
    )
    await services.before_chat(request)
    hooks.before_chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"query": "before"}, None])
async def test_default_hook_preserves_mutable_request_context(monkeypatch, params):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.common.hook_event import AgentServerHookEvents
    from gateway.extensions.registry import ExtensionRegistry

    request = AgentRequest(
        request_id="hook",
        channel_id="web",
        session_id="session",
        req_method=ReqMethod.CHAT_SEND,
        params=params,
    )

    async def trigger(event, context):
        assert event == AgentServerHookEvents.BEFORE_CHAT_REQUEST
        assert context.request_id == "hook"
        assert context.channel_id == "web"
        assert context.session_id == "session"
        assert context.req_method == "chat.send"
        assert context.params is request.params
        context.params["query"] = "after"

    registry = SimpleNamespace(trigger=AsyncMock(side_effect=trigger))
    monkeypatch.setattr(ExtensionRegistry, "get_instance", lambda: registry)
    await ProductionHostServices().before_chat(request)
    assert request.params == {"query": "after"}
    registry.trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_keepalive_uses_injected_sink_and_neutral_closed_contract(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.worker import execution_support

    monkeypatch.setattr(execution_support, "_STREAM_KEEPALIVE_INTERVAL_SECONDS", 0.001)
    sent = asyncio.Event()
    chunks = []
    payload = {"custom_transport": True}
    sink = object()

    def encode(chunk, **kwargs):
        chunks.append((chunk, kwargs))
        return payload

    async def send(target, message):
        assert target is sink
        assert message is payload
        sent.set()
        raise TransportClosed("closed")

    services = SimpleNamespace(
        capabilities=HostCapabilities(),
        encode_chunk=encode,
        send=send,
    )
    keepalive = execution_support._StreamKeepalive(
        sink,
        AgentRequest(request_id="idle", channel_id="web"),
        asyncio.Lock(),
        services=services,
    )
    keepalive.start()
    await asyncio.wait_for(sent.wait(), timeout=1)
    await keepalive.stop()
    assert len(chunks) == 1
    assert chunks[0][0].payload == {"event_type": "keepalive"}
    assert chunks[0][1] == {"response_id": "idle", "sequence": -1}
    assert keepalive._task.done()


@pytest.mark.asyncio
async def test_host_can_disable_keepalive_without_creating_a_task():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.worker.execution_support import _StreamKeepalive

    services = SimpleNamespace(capabilities=HostCapabilities(keepalive=False))
    keepalive = _StreamKeepalive(
        object(),
        AgentRequest(request_id="no-idle", channel_id="web"),
        asyncio.Lock(),
        services=services,
    )
    keepalive.start()
    await keepalive.stop()
    assert keepalive._task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "streaming,failed", [(False, False), (True, False), (True, True)]
)
async def test_request_execution_uses_injected_encoding_and_delivery(streaming, failed):
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.server.worker.execution_requests import ExecutionRequests

    wire = {"injected": True}
    services = SimpleNamespace(
        encode_response=Mock(return_value=wire),
        encode_chunk=Mock(return_value=wire),
        send=AsyncMock(return_value=False),
    )
    host = SimpleNamespace(services=services)
    sink = object()
    event = RuntimeEvent(
        request_id="injected",
        channel_id="web",
        session_id="session",
        payload={"event_type": "runtime.error", "error": "failed"}
        if failed
        else {"event_type": "chat.delta", "content": "hello"},
        ok=not failed,
    )
    sent_original = await ExecutionRequests._send_runtime_event(
        host,
        sink,
        event,
        asyncio.Lock(),
        streaming=streaming,
        sequence=4,
    )
    assert sent_original is False
    services.send.assert_awaited_once_with(sink, wire)
    if streaming and not failed:
        services.encode_chunk.assert_called_once()
        assert services.encode_chunk.call_args.kwargs == {
            "response_id": "injected",
            "sequence": 4,
        }
        services.encode_response.assert_not_called()
    else:
        services.encode_response.assert_called_once()
        services.encode_chunk.assert_not_called()
        if failed:
            assert services.encode_response.call_args.args[0].payload == {
                "error": "failed"
            }


@pytest.mark.asyncio
async def test_request_hook_and_error_delegate_to_injected_services():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.worker.execution_requests import ExecutionRequests

    payload = {"custom_error": True}
    services = SimpleNamespace(
        before_chat=AsyncMock(),
        error_payload=Mock(return_value=payload),
    )
    host = SimpleNamespace(
        services=services,
        _should_trigger_before_chat_request_hook=ExecutionRequests._should_trigger_before_chat_request_hook,
    )
    request = AgentRequest(
        request_id="hook", channel_id="web", req_method=ReqMethod.CHAT_SEND
    )
    await ExecutionRequests._trigger_before_chat_request_hook(host, request)
    services.before_chat.assert_awaited_once_with(request)
    assert (
        ExecutionRequests._send_error_response(
            host, object(), request, asyncio.Lock(), "failure", "ERROR"
        )
        is payload
    )
    services.error_payload.assert_called_once_with(request, "failure", "ERROR")
    request.req_method = ReqMethod.CONFIG_GET
    await ExecutionRequests._trigger_before_chat_request_hook(host, request)
    services.before_chat.assert_awaited_once()
