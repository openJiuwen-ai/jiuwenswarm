import asyncio
import json

import pytest

from jiuwenclaw.agentserver import agent_ws_server as agent_ws_server_module
from jiuwenclaw.schema.agent import AgentRequest
from jiuwenclaw.schema.message import ReqMethod


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class AgentWebSocketServerHarness(agent_ws_server_module.AgentWebSocketServer):
    async def handle_command_add_dir_for_test(self, ws, request, send_lock):
        await self._handle_command_add_dir(ws, request, send_lock)

    async def handle_command_compact_for_test(self, ws, request, send_lock):
        await self._handle_command_compact(ws, request, send_lock)

    async def handle_command_diff_for_test(self, ws, request, send_lock):
        await self._handle_command_diff(ws, request, send_lock)

    async def handle_command_model_for_test(self, ws, request, send_lock):
        await self._handle_command_model(ws, request, send_lock)

    async def handle_command_resume_for_test(self, ws, request, send_lock):
        await self._handle_command_resume(ws, request, send_lock)

    async def handle_command_session_for_test(self, ws, request, send_lock):
        await self._handle_command_session(ws, request, send_lock)


def fake_encode_agent_response_for_wire(resp, response_id):
    return {
        "response_id": response_id,
        "payload": resp.payload,
        "ok": resp.ok,
    }


@pytest.fixture
def server():
    return AgentWebSocketServerHarness()


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.fixture(autouse=True)
def patch_wire_encoder(monkeypatch):
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )


@pytest.mark.asyncio
async def test_handle_command_add_dir_returns_path_and_remember(
    server, fake_ws, monkeypatch
):
    persist_stub = {
        "ok": True,
        "normalized": "/tmp/demo",
        "path_pattern": "re:^/tmp/demo(?:$|/)",
        "shell_pattern": "re:.*/tmp/demo.*",
        "tiered_overrides": True,
    }
    monkeypatch.setattr(
        agent_ws_server_module,
        "persist_cli_trusted_directory",
        lambda _raw: persist_stub,
    )
    request = AgentRequest(
        request_id="req-add-dir",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_ADD_DIR,
        params={"path": "/tmp/demo", "remember": True},
    )

    await server.handle_command_add_dir_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-add-dir",
            "payload": {
                "path": "/tmp/demo",
                "remember": True,
                "persist": persist_stub,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_compact_returns_custom_instructions(server, fake_ws):
    request = AgentRequest(
        request_id="req-compact",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_COMPACT,
        params={"instructions": "focus on architecture"},
    )

    await server.handle_command_compact_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-compact",
            "payload": {"instructions": "focus on architecture"},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_diff_returns_summary_payload(server, fake_ws):
    request = AgentRequest(
        request_id="req-diff",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_DIFF,
        params={},
    )

    await server.handle_command_diff_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-diff",
            "payload": {
                "type": "list",
                "turns": [],
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_model_no_action_shows_current(
    server, fake_ws, monkeypatch
):
    """No action → returns current model from os.environ and available list."""
    monkeypatch.setenv("MODEL_NAME", "test-model")
    request = AgentRequest(
        request_id="req-model",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_MODEL,
        params={},
    )

    await server.handle_command_model_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-model",
            "payload": {
                "current": "test-model",
                "available": ["default-model"],
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_model_add_model(server, fake_ws):
    """action=add_model → returns model_added confirmation."""
    request = AgentRequest(
        request_id="req-add",
        channel_id="cli",
        req_method=ReqMethod.COMMAND_MODEL,
        params={"action": "add_model", "target": "my-model", "config": {}},
    )

    await server.handle_command_model_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-add",
            "payload": {"type": "model_added", "name": "my-model"},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_resume_returns_mock_session(server, fake_ws):
    request = AgentRequest(
        request_id="req-resume",
        channel_id="tui",
        req_method=ReqMethod.COMMAND_RESUME,
        params={"query": "sess_123"},
    )

    await server.handle_command_resume_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-resume",
            "payload": {
                "session_id": "sess_123",
                "query": "sess_123",
                "resumed": True,
                "preview": "Mock resumed conversation",
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_command_session_returns_remote_handoff(server, fake_ws):
    request = AgentRequest(
        request_id="req-session",
        channel_id="tui",
        session_id="sess_demo",
        req_method=ReqMethod.COMMAND_SESSION,
        params={},
    )

    await server.handle_command_session_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-session",
            "payload": {
                "session_id": "sess_demo",
                "remote_url": "https://example.com/session/sess_demo",
                "qr_text": "session:sess_demo",
            },
            "ok": True,
        }
    ]
