"""Real E2A parsing/dispatch and callback framework, without starting an Agent."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.common.e2a import wire_trace
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.memory_profile import MEMORY_PROFILE_EVENTS


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def registry(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)
    return registry


async def request(method, params, *, session_id=None, is_stream=False):
    server = object.__new__(AgentWebSocketServer)
    server._agent_manager = None  # Any accidental Agent dispatch fails this test.
    server._trigger_before_chat_request_hook = AsyncMock(side_effect=AssertionError("chat hook"))
    envelope = e2a_from_agent_fields(
        request_id="ui-request", channel_id="desktop", session_id=session_id,
        req_method=method, params=params, is_stream=is_stream, timestamp=0.0,
    )
    socket = Socket()
    await server._handle_message(socket, json.dumps(envelope.to_dict()), asyncio.Lock())
    assert len(socket.sent) == 1
    server._trigger_before_chat_request_hook.assert_not_called()
    wire = socket.sent[0]
    assert wire["request_id"] == "ui-request"
    assert wire["channel"] == "desktop"
    assert wire["is_final"] is True
    assert wire["is_stream"] is False
    return wire


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params", [
    (ReqMethod.MEMORY_PROFILE_SETTINGS_GET, {}),
    (ReqMethod.MEMORY_PROFILE_SETTINGS_SET, {"enabled": False}),
    (ReqMethod.MEMORY_PROFILE_GET, {}),
    (ReqMethod.MEMORY_PROFILE_MODIFY, {"instruction": "private profile"}),
    (ReqMethod.MEMORY_PROFILE_RESET, {}),
])
async def test_memory_methods_reach_only_the_selected_hook(registry, method, params):
    seen = []

    async def provider(context):
        seen.append(context)
        context.handled = True
        context.result = {"enabled": False}

    for candidate, event in MEMORY_PROFILE_EVENTS.items():
        registry.register(event, provider if candidate == method else AsyncMock(side_effect=AssertionError))
    wire = await request(method, {**params, "project_dir": "C:\\工作区"}, session_id="session-1")
    assert wire["status"] == "succeeded"
    assert wire["body"]["result"] == {"enabled": False}
    assert len(seen) == 1
    assert seen[0].req_method == method.value
    assert seen[0].session_id == "session-1"
    assert seen[0].params["project_dir"] == "C:\\工作区"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,params,options", [
    (ReqMethod.MEMORY_PROFILE_SETTINGS_SET, {"enabled": "false"}, {}),
    (ReqMethod.MEMORY_PROFILE_SETTINGS_SET, {"enabled": 0}, {}),
    (ReqMethod.MEMORY_PROFILE_MODIFY, {"instruction": " "}, {}),
    (ReqMethod.MEMORY_PROFILE_MODIFY, {"content": "old API"}, {}),
    (ReqMethod.MEMORY_PROFILE_MODIFY, {"instruction": "x", "expected_revision": "sha256:x"}, {}),
    (ReqMethod.MEMORY_PROFILE_GET, {"userId": "another-user"}, {}),
    (ReqMethod.MEMORY_PROFILE_GET, {"project_dir": 1}, {}),
    (ReqMethod.MEMORY_PROFILE_GET, {"session_id": "b"}, {"session_id": "a"}),
    (ReqMethod.MEMORY_PROFILE_GET, {}, {"is_stream": True}),
    (ReqMethod.MEMORY_PROFILE_RESET, {"userId": "another-user"}, {}),
    (ReqMethod.MEMORY_PROFILE_RESET, {"dryRun": True}, {}),
    (ReqMethod.MEMORY_PROFILE_RESET, {"instruction": "clear"}, {}),
    (ReqMethod.MEMORY_PROFILE_RESET, {}, {"is_stream": True}),
])
async def test_invalid_requests_never_trigger_hooks(registry, method, params, options):
    registry.trigger = AsyncMock()
    wire = await request(method, params, **options)
    assert wire["body"]["code"] == "BAD_REQUEST"
    assert wire["body"]["details"]["code"] == "BAD_REQUEST"
    registry.trigger.assert_not_called()


@pytest.mark.asyncio
async def test_no_provider_returns_explicit_error(registry):
    wire = await request(ReqMethod.MEMORY_PROFILE_GET, {})
    assert wire["body"]["code"] == "MEMORY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_provider_stage_failure_is_preserved_without_retry(registry):
    seen = []

    async def provider(context):
        seen.append(context)
        context.handled = True
        context.error = {"code": "MEMORY_OPERATION_FAILED", "message": "Memory operation failed",
                         "details": {"result": {"storeCompleted": True, "triggerCompleted": False}}}

    registry.register(MEMORY_PROFILE_EVENTS[ReqMethod.MEMORY_PROFILE_MODIFY], provider)
    wire = await request(ReqMethod.MEMORY_PROFILE_MODIFY, {"instruction": "private"})
    assert len(seen) == 1
    assert wire["status"] == "failed"
    assert wire["body"]["details"]["result"]["storeCompleted"] is True
    assert "private" not in json.dumps(wire)


def test_raw_trace_never_persists_memory_text(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_E2A_TRACE", "1")
    monkeypatch.setenv("JIUWENSWARM_E2A_TRACE_DIR", str(tmp_path))
    wire_trace.trace_inbound({"method": "memory.profile.modify", "request_id": "private-test",
                              "params": {"instruction": "private text"}})
    wire_trace.trace_outbound({"request_id": "private-test", "body": {"result": "private result"}})
    records = [json.loads(line) for path in tmp_path.rglob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(records) == 2
    assert all(record["data"]["redacted"] for record in records)
    assert all("params" not in record["data"] and "body" not in record["data"] for record in records)


@pytest.mark.asyncio
async def test_claimed_request_without_result_is_not_a_safe_unavailable_retry(registry):
    async def incomplete_provider(context):
        context.handled = True

    registry.register(MEMORY_PROFILE_EVENTS[ReqMethod.MEMORY_PROFILE_MODIFY], incomplete_provider)
    wire = await request(ReqMethod.MEMORY_PROFILE_MODIFY, {"instruction": "private"})
    assert wire["body"]["code"] == "MEMORY_OPERATION_FAILED"
    assert wire["body"]["details"]["outcomeUnknown"] is True


def test_trace_toggle_mid_request_still_redacts_response(monkeypatch, tmp_path):
    monkeypatch.setattr(wire_trace, "_enabled", lambda: False)
    wire_trace.trace_inbound({"method": "memory.profile.get", "request_id": "toggle-test", "params": {}})
    monkeypatch.setattr(wire_trace, "_enabled", lambda: True)
    monkeypatch.setenv("JIUWENSWARM_E2A_TRACE_DIR", str(tmp_path))
    wire_trace.trace_outbound({"request_id": "toggle-test", "body": {"result": "private"}})
    record = json.loads(next(tmp_path.rglob("*.jsonl")).read_text())
    assert record["data"]["redacted"] is True
    assert "body" not in record["data"]
