# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
    _MethodHandlerInvocation,
)


class _FakeRequestHeaders:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._mapping.get(key.lower(), default)


class FakeWebSocket:
    def __init__(
        self,
        *,
        user_id: str | None = None,
        query_user_id: str | None = None,
        remote_address: tuple[str, int] | None = ("127.0.0.1", 12345),
    ) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.closed = False
        self.remote_address = remote_address
        if query_user_id is not None:
            WebChannel._resolve_connection_user_id({"user_id": query_user_id}, self)
        elif user_id is not None:
            self.request = type(
                "Request",
                (),
                {"headers": _FakeRequestHeaders({"X-User-Id": user_id})},
            )()
            WebChannel._resolve_connection_user_id({}, self)

    async def send(self, data: str) -> None:
        self.sent_frames.append(json.loads(data))


class FakeWebChannelForHandlers:
    # 与真实 WebChannel.channel_id 保持一致(production handler 通过
    # ``channel.channel_id`` 读取,见 app_web_handlers._session_create)
    channel_id = "web"

    def __init__(self) -> None:
        self.methods: dict[str, object] = {}
        self.responses: list[dict[str, Any]] = []

    def register_method(self, name: str, handler: object) -> None:
        self.methods[name] = handler

    def on_connect(self, handler: object) -> None:
        return None

    async def send_response(
        self,
        ws: Any,
        req_id: str,
        *,
        ok: bool,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        code: str | None = None,
    ) -> None:
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload,
                "error": error,
                "code": code,
            }
        )


class FakeSessionCreateClient:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.requests: list[Any] = []

    async def send_request(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            ok=True,
            payload={
                "session_id": self.session_id,
                "sessionId": self.session_id,
                "projectId": "default",
                "projectDir": "",
                "workMode": "work",
                "prewarm_hit": True,
                "prewarm_status": "ready",
            },
        )


def test_extract_query_user_id():
    assert WebChannel._extract_query_user_id({"user_id": "alice"}) == "alice"
    assert WebChannel._extract_query_user_id({"user_id": "  bob  "}) == "bob"
    assert WebChannel._extract_query_user_id({}) is None
    assert WebChannel._extract_query_user_id({"user_id": "   "}) is None


def test_extract_ws_header_user_id_case_insensitive():
    ws_lower = type(
        "Ws",
        (),
        {"request_headers": _FakeRequestHeaders({"x-user-id": "bob"})},
    )()
    ws_upper = type(
        "Ws",
        (),
        {"request": type("Request", (), {"headers": _FakeRequestHeaders({"X-User-Id": "  carol  "})})()},
    )()
    ws_empty = type("Ws", (), {"request_headers": _FakeRequestHeaders({})})()

    assert WebChannel._extract_ws_header_user_id(ws_lower) == "bob"
    assert WebChannel._extract_ws_header_user_id(ws_upper) == "carol"
    assert WebChannel._extract_ws_header_user_id(ws_empty) is None


def test_resolve_connection_user_id_query_over_header():
    ws = type(
        "Ws",
        (),
        {"request": type("Request", (), {"headers": _FakeRequestHeaders({"X-User-Id": "header_user"})})()},
    )()
    uid = WebChannel._resolve_connection_user_id({"user_id": "query_user"}, ws)
    assert uid == "query_user"
    assert WebChannel._connection_user_id(ws) == "query_user"


def test_resolve_connection_user_id_header_only():
    ws = type(
        "Ws",
        (),
        {"request": type("Request", (), {"headers": _FakeRequestHeaders({"X-User-Id": "header_only"})})()},
    )()
    uid = WebChannel._resolve_connection_user_id({}, ws)
    assert uid == "header_only"


def test_resolve_connection_user_id_empty():
    ws = type("Ws", (), {})()
    uid = WebChannel._resolve_connection_user_id({}, ws)
    assert uid is None


def test_routing_key_user_id_fallback():
    assert WebChannel._routing_key_user_id("alice", ("127.0.0.1", 1)) == "alice"
    assert WebChannel._routing_key_user_id(None, ("127.0.0.1", 1)) == "('127.0.0.1', 1)"
    assert WebChannel._routing_key_user_id(None, None) == "unknown"


@pytest.mark.asyncio
async def test_web_channel_handle_raw_message_uses_connection_user_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = FakeWebSocket(query_user_id="alice")
    seen = []

    async def on_message(msg):
        seen.append(msg)

    channel.on_message(on_message)

    await channel._handle_raw_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-user",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-user",
                    "content": "hello",
                },
            },
            ensure_ascii=False,
        ),
        {"user_id": ["alice"]},
    )
    await channel.unregister_ws(ws)

    assert len(seen) == 1
    assert seen[0].user_id == "alice"
    assert seen[0].metadata.get("user_id") == "alice"
    assert "user_id" not in (seen[0].metadata or {}).get("routing", {})


@pytest.mark.asyncio
async def test_web_channel_local_tenant_handler_uses_handshake_scope():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = FakeWebSocket(query_user_id="alice")
    seen_params: list[dict[str, Any]] = []

    async def handler(_ws, _req_id, params, _session_id):
        seen_params.append(params)

    channel.register_method("cron.job.list", handler)
    await channel._handle_raw_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-scope",
                "method": "cron.job.list",
                "params": {"user_id": "payload-user"},
            }
        ),
        {
            "user_id": ["alice"],
            "group_id": ["group-1"],
            "bot_id": ["bot-1"],
        },
    )
    await channel.unregister_ws(ws)

    assert seen_params == [
        {
            "user_id": "alice",
            "group_id": "group-1",
            "bot_id": "bot-1",
        }
    ]


@pytest.mark.asyncio
async def test_web_channel_runtime_scope_does_not_replace_business_bot_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = FakeWebSocket(query_user_id="alice")
    seen_params: list[dict[str, Any]] = []

    async def handler(_ws, _req_id, params, _session_id):
        seen_params.append(params)

    channel.register_method("channel.wecom.set_conf", handler)
    await channel._handle_raw_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-business-bot",
                "method": "channel.wecom.set_conf",
                "params": {"bot_id": "wecom-business-bot"},
            }
        ),
        {"user_id": ["alice"], "group_id": ["group-1"], "bot_id": ["runtime-bot"]},
    )
    await channel.unregister_ws(ws)

    assert seen_params == [{"bot_id": "wecom-business-bot"}]


@pytest.mark.asyncio
async def test_web_channel_handle_raw_message_ignores_params_user_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = FakeWebSocket(query_user_id="alice")
    seen = []

    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-override",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-user",
                    "content": "hello",
                    "user_id": "evil",
                },
            },
            ensure_ascii=False,
        ),
        {"user_id": ["alice"]},
    )
    await channel.unregister_ws(ws)

    assert len(seen) == 1
    assert seen[0].user_id == "alice"


@pytest.mark.asyncio
async def test_web_channel_handle_raw_message_without_user_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = FakeWebSocket()
    seen = []

    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-empty",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-empty",
                    "content": "hello",
                },
            },
            ensure_ascii=False,
        ),
        {},
    )
    await channel.unregister_ws(ws)

    assert len(seen) == 1
    assert seen[0].user_id is None
    assert seen[0].metadata.get("user_id") is None


@pytest.mark.asyncio
async def test_web_channel_invoke_method_handler_injects_user_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    captured: list[str | None] = []

    async def handler(ws, req_id, params, session_id, user_id=None):
        captured.append(user_id)

    ws = FakeWebSocket(query_user_id="alice")
    await channel._invoke_method_handler(
        _MethodHandlerInvocation(ws, "test.method", "req-1", {}, "sess-1", handler),
    )

    assert captured == ["alice"]


@pytest.mark.asyncio
async def test_openai_account_unexpected_error_uses_method_dispatcher(monkeypatch):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    _register_web_handlers(WebHandlersBindParams(channel=channel))
    responses: list[dict[str, Any]] = []

    def raise_unexpected_error():
        raise RuntimeError("unexpected OAuth failure")

    async def capture_response(ws, req_id, *, ok, payload=None, error=None, code=None):
        responses.append({
            "id": req_id,
            "ok": ok,
            "payload": payload,
            "error": error,
            "code": code,
        })

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._openai_account_auth_status_payload",
        raise_unexpected_error,
    )
    monkeypatch.setattr(channel, "send_response", capture_response)
    handler = channel._method_handlers["openai_account.auth.status"]

    handled = await channel._invoke_method_handler(
        _MethodHandlerInvocation(
            FakeWebSocket(),
            "openai_account.auth.status",
            "req-oauth-error",
            {},
            "sess-oauth-error",
            handler,
        ),
    )

    assert handled is False
    assert responses == [{
        "id": "req-oauth-error",
        "ok": False,
        "payload": None,
        "error": "handler error: unexpected OAuth failure",
        "code": "INTERNAL_ERROR",
    }]


@pytest.mark.asyncio
async def test_session_create_uses_connection_user_id(tmp_path, monkeypatch):
    channel = FakeWebChannelForHandlers()
    agent_client = FakeSessionCreateClient("web_allocated_1")
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    ws = FakeWebSocket(query_user_id="alice")
    await channel.methods["session.create"](
        ws,
        "req-create",
        {"mode": "agent", "create_token": "web-create-1"},
        "new",
        user_id="alice",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["session_id"] == "web_allocated_1"
    assert agent_client.requests[-1].user_id == "alice"
    assert agent_client.requests[-1].params["user_id"] == "alice"


@pytest.mark.asyncio
async def test_session_create_ignores_params_user_id(tmp_path, monkeypatch):
    channel = FakeWebChannelForHandlers()
    agent_client = FakeSessionCreateClient("web_allocated_2")
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    ws = FakeWebSocket(query_user_id="alice")
    await channel.methods["session.create"](
        ws,
        "req-create",
        {
            "mode": "agent",
            "user_id": "victim",
            "create_token": "web-create-2",
        },
        "new",
        user_id="alice",
    )

    assert channel.responses[-1]["ok"] is True
    assert agent_client.requests[-1].user_id == "alice"
    assert agent_client.requests[-1].params["user_id"] == "alice"
