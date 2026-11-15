# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 统一薄代理（e2a_proxy.proxy_unary_request）单元测试。

覆盖：成功转发（envelope 契约）、目标不可达、超时、异常、AgentServer 失败响应。
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


from agent_gateway import telemetry
from agent_gateway.routing.agent_client import (
    DuplicateRequestIdError,
    WebSocketAgentServerClient,
)
from agent_gateway.routing.agent_request_timeout import (
    AGENT_SERVER_TIMEOUT_CODE,
    AGENT_SERVER_TIMEOUT_ERROR,
    AgentRequestTimeoutError,
)
from agent_gateway.routing.e2a_proxy import (
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


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ['session.list', 'file.import_url'])
async def test_proxy_preserves_authoritative_routing_metadata(method):
    from types import SimpleNamespace
    from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import identity_headers

    client = FakeAgentClient()
    await proxy_unary_request(
        channel=FakeChannel(), agent_client=client,
        ws=SimpleNamespace(_web_routing={"user_id": "u", "group_id": "g", "bot_id": "b"}),
        req_id="file-req", params={"files": []}, session_id="s", user_id="u",
        req_method=method,
    )
    headers = identity_headers(client.envelopes[0], accept="application/json")
    assert headers["X-Group-Id"] == "g"
    assert headers["X-Bot-Id"] == "b"
    assert "group_id" not in client.envelopes[0].params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_id", "expected"),
    [("web-app", "web-app"), ("", "default")],
)
async def test_metadata_proxy_preserves_web_application_for_runtime_replay(app_id, expected):
    # 与 app_web_handlers 创建 cron 任务时的口径一致：空 app_id 归一为
    # default，恢复请求与首轮执行身份保持同一 app 作用域。
    from types import SimpleNamespace

    channel = FakeChannel()
    client = FakeAgentClient()
    await proxy_unary_request(
        channel=channel, agent_client=client, ws=SimpleNamespace(_jiuwen_app_id=app_id),
        req_id="metadata", params={"session_id": "session"}, session_id="session",
        user_id="owner", req_method='session.get_metadata',
    )
    assert client.envelopes[0].channel_context["app_id"] == expected
    assert client.envelopes[0].user_id == "owner"


async def _invoke(channel, agent_client, **overrides):
    kwargs = {
        "channel": channel,
        "agent_client": agent_client,
        "ws": object(),
        "req_id": "req-1",
        "params": {"limit": 5},
        "session_id": "current-session",
        "user_id": "user-42",
        "req_method": 'session.list',
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
        req_method='health_check.get_conf',
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
        req_method='session.delete',
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

    await _invoke(channel, local_client, req_method='permissions.tools.get')

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
        req_method='project.sessions.archive',
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
        req_method='project.sessions.archive',
        params={"project_id": "p1"},
        session_id=None,
        user_id="u1",
        channel_id="web",
    )

    assert ok is False
    assert payload["code"] == SERVICE_UNAVAILABLE_CODE
    assert len(agent.request_ids) == 2


@pytest.fixture
def proxy_trace_exporter(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        telemetry, "_runtime",
        telemetry.GatewayTelemetry(tracer=provider.get_tracer("test-e2a-proxy"), provider=provider),
    )
    yield exporter
    provider.shutdown()


class TracedFakeAgentClient(FakeAgentClient):
    def __init__(self, behavior="ok"):
        super().__init__()
        self.behavior = behavior
        self.operations = []

    async def send_request(self, envelope):
        self.operations.append(telemetry.current_request_trace())
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        return await super().send_request(envelope)


class TracedFakeChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.operations = []

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        operation = telemetry.current_request_trace()
        self.operations.append(operation)
        if operation is not None and not ok:
            operation.record_outcome("failed", reason="response_failed")
        await super().send_response(ws, req_id, ok=ok, payload=payload, error=error, code=code)


@pytest.mark.asyncio
async def test_proxy_starts_one_request_trace_without_web_entry(proxy_trace_exporter) -> None:
    channel = TracedFakeChannel()
    agent = TracedFakeAgentClient()

    assert await _invoke(channel, agent) is True

    operation = agent.operations[0]
    assert operation is not None
    assert channel.operations == [operation]
    assert operation.finished
    assert telemetry.current_request_trace() is None
    spans = proxy_trace_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "gw.session.message"
    assert spans[0].parent is None
    assert spans[0].attributes["request_id"] == "req-1"
    assert spans[0].attributes["method"] == "session.list"
    assert spans[0].attributes["gateway.outcome"] == "success"
    assert channel.responses[0]["ok"] is True


@pytest.mark.asyncio
async def test_proxy_reuses_existing_request_trace_without_finishing_it(proxy_trace_exporter) -> None:
    operation = telemetry.start_request_trace("req-1", "session.list", channel="web", connection_id="ws")
    agent = TracedFakeAgentClient()
    channel = TracedFakeChannel()

    with telemetry.bind_request_trace(operation):
        await _invoke(channel, agent)
        assert telemetry.current_request_trace() is operation
        assert not operation.finished
        assert proxy_trace_exporter.get_finished_spans() == ()

    assert agent.operations == [operation]
    assert channel.operations == [operation]
    operation.complete()
    spans = proxy_trace_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "gw.session.message"


@pytest.mark.asyncio
async def test_proxy_completion_waits_for_pending_delivery(proxy_trace_exporter) -> None:
    class PendingChannel(TracedFakeChannel):
        async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
            operation = telemetry.current_request_trace()
            operation.retain_delivery()
            await super().send_response(ws, req_id, ok=ok, payload=payload, error=error, code=code)

    channel = PendingChannel()
    agent = TracedFakeAgentClient()

    await _invoke(channel, agent)

    operation = agent.operations[0]
    assert operation.producer_done
    assert not operation.finished
    assert proxy_trace_exporter.get_finished_spans() == ()
    operation.release_delivery()
    assert operation.finished
    assert len(proxy_trace_exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior,outcome",
    [("error", "failed"), ("agent_error", "failed"), ("timeout", "timeout"), ("cancel", "cancelled")],
)
async def test_proxy_trace_finishes_on_failure_and_restores_scope(proxy_trace_exporter, behavior, outcome) -> None:
    agent = TracedFakeAgentClient(behavior)
    channel = TracedFakeChannel()
    unrelated = telemetry.start_request_trace("other", "chat.send", channel="web", connection_id="other-ws")

    with telemetry.bind_request_trace(unrelated):
        if behavior == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await _invoke(channel, agent)
        else:
            await _invoke(channel, agent)
            assert channel.responses[0]["ok"] is False
        assert telemetry.current_request_trace() is unrelated
        assert not unrelated.finished

    operation = agent.operations[0]
    assert operation is not None and operation is not unrelated
    assert operation.finished
    spans = proxy_trace_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "gw.session.message"
    assert spans[0].attributes["gateway.outcome"] == outcome
    unrelated.complete()


@pytest.mark.asyncio
async def test_background_fetch_does_not_create_message_trace(proxy_trace_exporter) -> None:
    agent = TracedFakeAgentClient()

    ok, payload = await fetch_agent_unary(
        agent_client=agent,
        req_method='session.list',
        params={},
        session_id=None,
        user_id="user-42",
        channel_id="web",
    )

    assert ok is True
    assert payload == {"sessions": [], "total": 0}
    assert agent.operations == [None]
    assert proxy_trace_exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_proxy_without_telemetry_preserves_response(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_runtime", None)
    channel = TracedFakeChannel()
    agent = TracedFakeAgentClient()

    assert await _invoke(channel, agent) is True

    assert agent.operations == [None]
    assert channel.operations == [None]
    assert channel.responses == [{
        "id": "req-1", "ok": True, "payload": {"sessions": [], "total": 0}, "error": None, "code": None,
    }]


@pytest.mark.asyncio
async def test_proxy_without_telemetry_preserves_external_span(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_runtime", None)
    seen_spans = []

    class ExternalTraceAgent(FakeAgentClient):
        async def send_request(self, envelope):
            seen_spans.append(trace.get_current_span())
            return await super().send_request(envelope)

    class ExternalTraceChannel(FakeChannel):
        async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
            seen_spans.append(trace.get_current_span())
            await super().send_response(ws, req_id, ok=ok, payload=payload, error=error, code=code)

    provider = TracerProvider()
    try:
        with provider.get_tracer("external-test").start_as_current_span("external") as external:
            assert telemetry.current_request_trace() is None
            assert await _invoke(ExternalTraceChannel(), ExternalTraceAgent()) is True
            assert seen_spans == [external, external]
            assert trace.get_current_span() is external
    finally:
        provider.shutdown()
@pytest.mark.asyncio
async def test_fetch_agent_unary_forwards_reported_http_status() -> None:
    class _Client:
        server_ready = True

        async def send_request(self, envelope, *, timeout=None):
            return type(
                "Resp",
                (),
                {"ok": False, "payload": {"error": "missing", "code": "NOT_FOUND", "status": 404}},
            )()

    ok, payload = await fetch_agent_unary(
        agent_client=_Client(),
        req_method='diagnosis.get',
        params={"session_id": "s1"},
        session_id="s1",
        user_id=None,
        channel_id="web",
        label="diagnosis",
    )

    assert ok is False
    assert payload == {"error": "missing", "code": "NOT_FOUND", "status": 404}
