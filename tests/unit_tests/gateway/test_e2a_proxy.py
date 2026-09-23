# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 统一薄代理（e2a_proxy.proxy_unary_request）单元测试。

覆盖：成功转发（envelope 契约）、目标不可达、超时、异常、AgentServer 失败响应。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.agent_client import (
    DuplicateRequestIdError,
    WebSocketAgentServerClient,
)
from jiuwenswarm.gateway.routing.agent_request_timeout import (
    AGENT_SERVER_TIMEOUT_CODE,
    AGENT_SERVER_TIMEOUT_ERROR,
    AgentRequestTimeoutError,
)
from jiuwenswarm.gateway.routing.e2a_proxy import (
    SERVICE_UNAVAILABLE_CODE,
    _new_fetch_request_id,
    fetch_agent_unary,
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
async def test_proxy_does_not_substitute_front_control_methods(monkeypatch) -> None:
    """session.list is Front Control; Gateway must not run a local adapter."""
    local_client = WebSocketAgentServerClient()  # server_ready defaults to False
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_all_sessions_metadata",
        lambda *, limit, offset: ([{"session_id": "legacy", "mode": "agent"}], 1),
    )
    channel = FakeChannel()

    await _invoke(channel, local_client)

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == SERVICE_UNAVAILABLE_CODE
    assert not local_client.server_ready


@pytest.mark.asyncio
async def test_proxy_forwards_control_methods_after_failed_ack(monkeypatch) -> None:
    """FAILED ack keeps transport up so Front control RPCs are not blocked."""
    client = WebSocketAgentServerClient()
    client._apply_connection_ack(
        {
            "type": "event",
            "event": "connection.ack",
            "payload": {"status": "failed", "readiness": "FAILED"},
        }
    )
    envelopes: list[object] = []

    async def _send(envelope):
        envelopes.append(envelope)
        return type("Resp", (), {"ok": True, "payload": {"state": "FAILED"}})()

    monkeypatch.setattr(client, "send_request", _send)
    channel = FakeChannel()

    await _invoke(
        channel,
        client,
        req_method=ReqMethod.HEALTH_CHECK_GET_CONF,
        params={},
        label="health_check.get_conf",
    )

    assert client.server_ready is True
    assert client.agent_ready is False
    assert len(envelopes) == 1
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["state"] == "FAILED"


@pytest.mark.asyncio
async def test_proxy_offline_session_delete_is_unavailable() -> None:
    """Gateway must not load Runtime adapters when AgentServer is unreachable."""
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
    assert response["ok"] is False
    assert response["code"] == SERVICE_UNAVAILABLE_CODE


@pytest.mark.asyncio
async def test_proxy_offline_permissions_is_unavailable() -> None:
    """Permissions are execution-plane; offline Gateway does not substitute them."""
    local_client = WebSocketAgentServerClient()  # server_ready defaults to False
    channel = FakeChannel()

    await _invoke(channel, local_client, req_method=ReqMethod.PERMISSIONS_TOOLS_GET)

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == SERVICE_UNAVAILABLE_CODE


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


def test_new_fetch_request_id_is_unique_in_process() -> None:
    """request_id 撞号会让客户端拒绝注册队列（请求根本发不出去），必须唯一。

    历史上只用 ``time.time_ns()``：Windows 上墙钟只有 100ns 步进，密集调用
    会重复；归档等长耗时 RPC 与 2s 生命周期轮询共用连接时表现为偶发失败。
    """
    generated = [_new_fetch_request_id() for _ in range(20000)]
    assert len(set(generated)) == len(generated)
    assert all(rid.startswith("fetch-") for rid in generated)


class DuplicateRidAgentClient:
    """前 ``fail_times`` 次 send_request 抛出 request_id 撞号错误。"""

    def __init__(self, fail_times: int = 1) -> None:
        self.server_ready = True
        self.fail_times = fail_times
        self.request_ids: list[str] = []

    async def send_request(self, envelope, *, timeout=None):
        self.request_ids.append(envelope.request_id)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise DuplicateRequestIdError(envelope.request_id)
        return type("Resp", (), {"ok": True, "payload": {"archived": 3}})()


@pytest.mark.asyncio
async def test_fetch_agent_unary_retries_with_new_request_id_on_duplicate() -> None:
    agent = DuplicateRidAgentClient(fail_times=1)

    ok, payload = await fetch_agent_unary(
        agent_client=agent,
        req_method=ReqMethod.PROJECT_SESSIONS_ARCHIVE,
        params={"project_id": "p1"},
        session_id=None,
        user_id="u1",
        channel_id="web",
    )

    assert ok is True
    assert payload == {"archived": 3}
    assert len(agent.request_ids) == 2
    assert agent.request_ids[0] != agent.request_ids[1]


@pytest.mark.asyncio
async def test_fetch_agent_unary_gives_up_after_single_retry() -> None:
    agent = DuplicateRidAgentClient(fail_times=5)

    ok, payload = await fetch_agent_unary(
        agent_client=agent,
        req_method=ReqMethod.PROJECT_SESSIONS_ARCHIVE,
        params={"project_id": "p1"},
        session_id=None,
        user_id="u1",
        channel_id="web",
    )

    assert ok is False
    assert payload["code"] == SERVICE_UNAVAILABLE_CODE
    assert len(agent.request_ids) == 2
