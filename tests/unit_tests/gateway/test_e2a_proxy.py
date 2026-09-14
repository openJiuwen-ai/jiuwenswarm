# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 统一薄代理（e2a_proxy.proxy_unary_request）单元测试。

覆盖：成功转发（envelope 契约）、目标不可达、超时、异常、AgentServer 失败响应。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.gateway.routing.agent_request_timeout import (
    AGENT_SERVER_TIMEOUT_CODE,
    AGENT_SERVER_TIMEOUT_ERROR,
    AgentRequestTimeoutError,
)
from jiuwenswarm.gateway.routing.e2a_proxy import (
    SERVICE_UNAVAILABLE_CODE,
    proxy_unary_request,
)


class FakeChannel:
    def __init__(self):
        self.channel_id = "web"
        self.responses: list[dict] = []

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload,
                "error": error,
                "code": code,
            }
        )


class FakeAgentClient:
    def __init__(self, server_ready=True):
        self.server_ready = server_ready
        self.envelopes: list = []
        self.behavior = "ok"

    async def send_request(self, envelope):
        self.envelopes.append(envelope)
        if self.behavior == "ok":
            return type("Resp", (), {"ok": True, "payload": {"sessions": [], "total": 0}})()
        if self.behavior == "agent_error":
            return type(
                "Resp",
                (),
                {"ok": False, "payload": {"error": "boom", "code": "SESSION_LIST_FAILED"}},
            )()
        if self.behavior == "timeout":
            raise AgentRequestTimeoutError()
        raise RuntimeError("connection lost")


async def _invoke(channel, agent_client, **overrides):
    kwargs = {
        "channel": channel,
        "agent_client": agent_client,
        "ws": object(),
        "req_id": "req-1",
        "params": {"limit": 5},
        "session_id": "current-session",
        "user_id": "user-42",
        "req_method": ReqMethod.SESSION_LIST,
        "label": "session.list",
    }
    kwargs.update(overrides)
    return await proxy_unary_request(**kwargs)


@pytest.mark.asyncio
async def test_proxy_forwards_envelope_and_ok_response() -> None:
    channel = FakeChannel()
    agent = FakeAgentClient()
    result = await _invoke(channel, agent, extra_params={"user_id": "user-42"})

    assert result is True
    assert len(agent.envelopes) == 1
    env = agent.envelopes[0]
    assert env.method == "session.list"
    assert env.channel == "web"
    assert env.user_id == "user-42"
    assert env.session_id == "current-session"
    # 独立 user_id 参数非空时，extra_params 中重复的 user_id 被移除（envelope.user_id 唯一承载）
    assert env.params == {"limit": 5}
    assert env.is_stream is False

    resp = channel.responses[-1]
    assert resp["ok"] is True
    assert resp["payload"]["total"] == 0
    assert resp["error"] is None


@pytest.mark.asyncio
async def test_proxy_agent_unavailable() -> None:
    channel = FakeChannel()
    result = await _invoke(channel, None)

    assert result is True
    resp = channel.responses[-1]
    assert resp["ok"] is False
    assert resp["code"] == SERVICE_UNAVAILABLE_CODE

    channel2 = FakeChannel()
    agent = FakeAgentClient(server_ready=False)
    await _invoke(channel2, agent)
    assert channel2.responses[-1]["code"] == SERVICE_UNAVAILABLE_CODE


@pytest.mark.asyncio
async def test_proxy_uses_adapter_fallback_only_for_local_websocket_client(monkeypatch) -> None:
    """A local shared-directory AgentServer outage must preserve Web behavior."""
    local_client = WebSocketAgentServerClient()  # server_ready defaults to False
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_all_sessions_metadata",
        lambda *, limit, offset: ([{"session_id": "legacy", "mode": "agent"}], 1),
    )
    channel = FakeChannel()

    await _invoke(channel, local_client)

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["sessions"][0]["session_id"] == "legacy"


@pytest.mark.asyncio
async def test_proxy_offline_session_delete_uses_maintenance_runtime(monkeypatch) -> None:
    """The shared-directory fallback enters Runtime without enabling KVC."""
    from jiuwenswarm.runtime.session_delete import SessionDeleteResult

    async def _delete_offline_session(**kwargs):
        return SessionDeleteResult(
            ok=True,
            session_id=kwargs["session_id"],
            deleted=True,
        )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.offline_session_cleanup.delete_offline_session",
        _delete_offline_session,
    )
    local_client = WebSocketAgentServerClient()  # server_ready defaults to False
    channel = FakeChannel()

    await _invoke(
        channel,
        local_client,
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "legacy"},
        session_id="legacy",
    )

    response = channel.responses[-1]
    assert response["ok"] is True
    assert response["payload"] == {"session_id": "legacy"}


@pytest.mark.asyncio
async def test_proxy_keeps_permissions_fallback_for_local_websocket_client(monkeypatch) -> None:
    """Permissions kept their pre-refactor shared-directory availability path."""
    local_client = WebSocketAgentServerClient()  # server_ready defaults to False
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc

    monkeypatch.setattr(
        permissions_config_rpc,
        "get_permissions_config_req_methods",
        lambda: frozenset({ReqMethod.PERMISSIONS_TOOLS_GET}),
    )
    monkeypatch.setattr(
        permissions_config_rpc,
        "dispatch_permissions_config_request",
        lambda request: type("Resp", (), {"ok": True, "payload": {"tools": []}})(),
    )
    channel = FakeChannel()

    await _invoke(channel, local_client, req_method=ReqMethod.PERMISSIONS_TOOLS_GET)

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"tools": []}


@pytest.mark.asyncio
async def test_proxy_timeout_maps_to_agent_server_timeout() -> None:
    channel = FakeChannel()
    agent = FakeAgentClient()
    agent.behavior = "timeout"
    await _invoke(channel, agent)

    resp = channel.responses[-1]
    assert resp["ok"] is False
    assert resp["code"] == AGENT_SERVER_TIMEOUT_CODE
    assert resp["error"] == AGENT_SERVER_TIMEOUT_ERROR


@pytest.mark.asyncio
async def test_proxy_agent_error_response_passthrough() -> None:
    channel = FakeChannel()
    agent = FakeAgentClient()
    agent.behavior = "agent_error"
    await _invoke(channel, agent)

    resp = channel.responses[-1]
    assert resp["ok"] is False
    assert resp["error"] == "boom"
    assert resp["code"] == "SESSION_LIST_FAILED"
    assert resp["payload"] is None
