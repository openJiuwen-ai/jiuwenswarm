# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.tui import tui_connect as tui_connect_module
from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CliHandlersBindParams,
    CliRouteBindParams,
    build_cli_route_binding,
    register_cli_handlers,
)
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.gateway.heartbeat import HeartbeatServiceUnavailableError


class FakeGatewayServer:
    """Fake GatewayServer for testing CLI handler registration."""

    def __init__(self):
        self.local_handlers: dict[str, dict] = {}  # path -> {method: handler}
        self.responses = []
        self.session_owners = {}
        self.channel_id = "tui"  # 与 TUI 真实通道一致，供 e2a_proxy 回落构造 envelope 使用

    def register_local_handler(self, path, method, handler):
        if path not in self.local_handlers:
            self.local_handlers[path] = {}
        self.local_handlers[path][method] = handler

    def bind_session_owner(self, channel_id, session_id, ws):
        self.session_owners[(channel_id, session_id)] = ws

    def is_session_bound_to_client(self, channel_id, session_id, ws):
        return self.session_owners.get((channel_id, session_id)) is ws

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload or {},
                "error": error,
                "code": code,
            }
        )


@pytest.mark.asyncio
async def test_heartbeat_cli_reports_unavailable_and_missing_job_codes():
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(channel=server, heartbeat_controller=None, path="/tui")
    )
    await server.local_handlers["/tui"]["heartbeat.job.list"](
        object(), "list-unavailable", {}, "session-current"
    )
    assert server.responses[-1]["code"] == "SERVICE_UNAVAILABLE"

    class Controller:
        async def get_job(self, *_args, **_kwargs):
            raise KeyError("missing")

        async def get_meta(self, **_kwargs):
            raise HeartbeatServiceUnavailableError("agentserver offline")

    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            heartbeat_controller=Controller(),
            path="/tui",
        )
    )
    await server.local_handlers["/tui"]["heartbeat.job.get"](
        object(), "get-missing", {"id": "missing"}, "session-current"
    )
    assert server.responses[-1]["code"] == "NOT_FOUND"
    await server.local_handlers["/tui"]["heartbeat.job.meta"](
        object(), "meta-unavailable", {}, "session-current"
    )
    assert server.responses[-1]["code"] == "SERVICE_UNAVAILABLE"


class FakeMessageHandler:
    def __init__(self):
        self.cancelled = []
        self.scheduled = []
        self.scheduled_delays = []
        self.reconnected = []
        self.disconnected_websockets = []

    async def unregister_ws_subscriptions(self, channel_id, ws_id):
        self.disconnected_websockets.append((channel_id, ws_id))
        return 1

    async def cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, user_id=None
    ):
        self.cancelled.append((session_keys, stale_request_keys or []))
        return True

    async def schedule_cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, delay_seconds=60.0, user_id=None
    ):
        self.scheduled.append((session_keys, stale_request_keys or []))
        self.scheduled_delays.append(delay_seconds)

    def cancel_scheduled_disconnect_cancel(self, channel_id, session_id):
        self.reconnected.append((channel_id, session_id))
        return True


class BlockingScheduledDisconnectMessageHandler(FakeMessageHandler):
    def __init__(self):
        super().__init__()
        self.schedule_started = asyncio.Event()
        self.release_schedule = asyncio.Event()

    async def schedule_cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, delay_seconds=60.0, user_id=None
    ):
        self.schedule_started.set()
        await self.release_schedule.wait()
        await super().schedule_cancel_agent_sessions_on_disconnect(
            session_keys,
            stale_request_keys=stale_request_keys,
            delay_seconds=delay_seconds,
            user_id=user_id,
        )


class FailedOnceScheduledDisconnectMessageHandler(FakeMessageHandler):
    def __init__(self):
        super().__init__()
        self.schedule_attempts = 0

    async def schedule_cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, delay_seconds=60.0, user_id=None
    ):
        self.schedule_attempts += 1
        if self.schedule_attempts == 1:
            raise RuntimeError("cleanup scheduling failed")
        await super().schedule_cancel_agent_sessions_on_disconnect(
            session_keys,
            stale_request_keys=stale_request_keys,
            delay_seconds=delay_seconds,
            user_id=user_id,
        )


@pytest.mark.asyncio
async def test_register_cli_handlers_registers_local_methods():
    server = FakeGatewayServer()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    cli_handlers = server.local_handlers["/tui"]
    assert "config.get" in cli_handlers
    assert "config.validate_model" in cli_handlers
    assert "session.list" in cli_handlers
    assert "chat.send" in cli_handlers
    assert "chat.resume" in cli_handlers
    assert "history.get" in cli_handlers
    assert "tui.disconnect" in cli_handlers
    assert "harmonyos.dev_init" in cli_handlers
    assert "harmonyos.dev_init_cancel" in cli_handlers
    assert "harmonyos.project_init" in cli_handlers
    assert "memory.list" in cli_handlers
    assert "memory.edit" in cli_handlers
    assert "memory.status" in cli_handlers
    assert "memory.toggle" in cli_handlers
    assert "memory.open" in cli_handlers

    await cli_handlers["chat.send"](object(), "req-1", {}, "sess-1")

    assert server.responses == [
        {
            "id": "req-1",
            "ok": True,
            "payload": {"accepted": True, "session_id": "sess-1"},
            "error": None,
            "code": None,
        }
    ]


@pytest.mark.asyncio
async def test_memory_list_forwards_to_agentserver_with_routing_user(monkeypatch):
    server = FakeGatewayServer()
    calls = []

    async def fake_proxy_unary_request(
        *, channel, agent_client, ws, req_id, params, session_id,
        user_id, req_method, label, **kwargs,
    ):
        calls.append(
            {
                "req_method": req_method,
                "user_id": user_id,
                "params": params,
                "label": label,
                "session_id": session_id,
            }
        )
        await channel.send_response(ws, req_id, ok=True, payload={"files": [], "mode": "code"})
        return True

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.proxy_unary_request",
        fake_proxy_unary_request,
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    await server.local_handlers["/tui"]["memory.list"](
        object(), "req-memory", {"mode": "code"}, "sess-memory", "user-a"
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["req_method"] == ReqMethod.MEMORY_LIST
    assert call["user_id"] == "user-a"
    assert call["params"] == {"mode": "code"}
    assert call["label"] == "memory.list"
    assert server.responses[-1]["ok"] is True
    assert server.responses[-1]["payload"] == {"files": [], "mode": "code"}


def _install_hanging_dev_init_send(monkeypatch, *, record=None):
    """monkeypatch TUI 的 send_agent_request_with_timeout 为挂起实现。

    返回 ``(started, cancelled)`` 事件：请求进入即置 started，任务被取消时置
    cancelled。``record`` 非空时记录 ``(env, label)`` 供断言。
    """
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_send_agent_request_with_timeout(
        _client, env, *, label, timeout_seconds=None
    ):
        if record is not None:
            record.append((env, label))
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        tui_connect_module,
        "send_agent_request_with_timeout",
        fake_send_agent_request_with_timeout,
    )
    return started, cancelled


def _install_echo_dev_init_send(monkeypatch, *, record=None):
    """monkeypatch 为立即返回成功的实现（记录 ``(env, label)``）。"""
    async def fake_send_agent_request_with_timeout(
        _client, env, *, label, timeout_seconds=None
    ):
        if record is not None:
            record.append((env, label))
        return AgentResponse(
            request_id=env.request_id,
            channel_id="tui",
            ok=True,
            payload={"ok": True},
        )

    monkeypatch.setattr(
        tui_connect_module,
        "send_agent_request_with_timeout",
        fake_send_agent_request_with_timeout,
    )


@pytest.mark.asyncio
async def test_harmonyos_dev_init_cancel_waits_for_background_cleanup(monkeypatch):
    server = FakeGatewayServer()
    received_envs = []
    started, cancelled = _install_hanging_dev_init_send(
        monkeypatch, record=received_envs
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    ws = type("FakeWs", (), {})()
    handlers = server.local_handlers["/tui"]

    await handlers["harmonyos.dev_init"](
        ws,
        "req-init",
        {"operationId": "dev-init-op-1", "skipDevecocliUpdate": True},
        "sess-1",
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(received_envs) == 1
    env, label = received_envs[0]
    assert env.method == ReqMethod.HARMONYOS_DEV_INIT.value
    assert env.params == {"skipDevecocliUpdate": True}
    assert label == "tui harmonyos.dev_init"
    assert all(response["id"] != "req-init" for response in server.responses)

    await asyncio.wait_for(
        handlers["harmonyos.dev_init_cancel"](
            ws,
            "req-cancel",
            {"operationId": "dev-init-op-1"},
            "sess-1",
        ),
        timeout=1,
    )

    assert cancelled.is_set()
    assert server.responses[-2] == {
        "id": "req-init",
        "ok": False,
        "payload": {},
        "error": "HarmonyOS Dev Init operation was cancelled",
        "code": "CANCELLED",
    }
    assert server.responses[-1] == {
        "id": "req-cancel",
        "ok": True,
        "payload": {
            "operationId": "dev-init-op-1",
            "cancelRequested": True,
            "cancelled": True,
        },
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_harmonyos_dev_init_is_cancelled_when_websocket_disconnects(monkeypatch):
    server = FakeGatewayServer()
    started, cancelled = _install_hanging_dev_init_send(monkeypatch)
    binding = build_cli_route_binding(
        CliRouteBindParams(path="/tui", agent_client=object())
    )
    binding.install(server)
    ws = type("FakeWs", (), {})()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        ws,
        "req-disconnect-init",
        {"operationId": "dev-init-disconnect-op"},
        "sess-disconnect",
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(binding.disconnect_handler(ws, [], []), timeout=1)

    assert cancelled.is_set()
    assert server.responses[-1]["id"] == "req-disconnect-init"
    assert server.responses[-1]["code"] == "CANCELLED"


@pytest.mark.asyncio
async def test_harmonyos_dev_init_rejects_untrackable_websocket(monkeypatch):
    server = FakeGatewayServer()
    sent = []
    _install_echo_dev_init_send(monkeypatch, record=sent)
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    class SlottedWs:
        __slots__ = ()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        SlottedWs(),
        "req-untrackable",
        {"operationId": "dev-init-untrackable-op"},
        "sess-untrackable",
    )

    assert sent == []
    assert server.responses[-1] == {
        "id": "req-untrackable",
        "ok": False,
        "payload": {},
        "error": "websocket does not support HarmonyOS Dev Init task tracking",
        "code": "INTERNAL_ERROR",
    }


def _install_failing_dev_init_send(monkeypatch):
    """monkeypatch send_agent_request_with_timeout 为传输层失败实现。"""

    async def fake_send_agent_request_with_timeout(
        _client, env, *, label, timeout_seconds=None
    ):
        raise RuntimeError("connection closed")

    monkeypatch.setattr(
        tui_connect_module,
        "send_agent_request_with_timeout",
        fake_send_agent_request_with_timeout,
    )


@pytest.mark.asyncio
async def test_harmonyos_dev_init_legacy_client_falls_back_to_local(monkeypatch):
    """单用户共享目录 client E2A 不可达时，在 Gateway 进程内本地执行（迁移前行为）。"""
    server = FakeGatewayServer()
    _install_failing_dev_init_send(monkeypatch)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_legacy_shared_directory_client",
        lambda client: True,
    )

    local_calls = []

    async def fake_local_init(params):
        local_calls.append(dict(params))
        return {"ok": True, "source": "local-fallback"}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.harmonyos.harmonyos_dev.run_harmonyos_dev_init",
        fake_local_init,
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    ws = type("FakeWs", (), {})()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        ws,
        "req-fallback",
        {"operationId": "dev-init-fallback-op", "skipDevecocliUpdate": True},
        "sess-1",
    )
    await asyncio.sleep(0.05)

    assert local_calls == [{"skipDevecocliUpdate": True}]
    assert server.responses[-1] == {
        "id": "req-fallback",
        "ok": True,
        "payload": {"ok": True, "source": "local-fallback"},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_harmonyos_dev_init_remote_client_error_without_fallback(monkeypatch):
    """远程/AgentOS client 传输失败时不回退本地，直接报错（不做部署目录 fallback）。"""
    server = FakeGatewayServer()
    _install_failing_dev_init_send(monkeypatch)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_legacy_shared_directory_client",
        lambda client: False,
    )

    local_calls = []

    async def fake_local_init(params):
        local_calls.append(dict(params))
        return {"ok": True}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.harmonyos.harmonyos_dev.run_harmonyos_dev_init",
        fake_local_init,
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    ws = type("FakeWs", (), {})()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        ws,
        "req-remote-error",
        {"operationId": "dev-init-remote-op"},
        "sess-1",
    )
    await asyncio.sleep(0.05)

    assert local_calls == []
    assert server.responses[-1]["id"] == "req-remote-error"
    assert server.responses[-1]["ok"] is False
    assert server.responses[-1]["code"] == "INTERNAL_ERROR"
    assert "connection closed" in server.responses[-1]["error"]


@pytest.mark.asyncio
async def test_harmonyos_dev_init_business_failure_does_not_rerun_locally(monkeypatch):
    """AgentServer 正常执行但业务失败（ok=False）时不得本地重跑长时操作。"""
    server = FakeGatewayServer()

    async def business_failure_send(_client, env, *, label, timeout_seconds=None):
        return AgentResponse(
            request_id=env.request_id,
            channel_id="tui",
            ok=False,
            payload={"error": "harmonyos project invalid", "code": "BAD_REQUEST"},
        )

    monkeypatch.setattr(
        tui_connect_module,
        "send_agent_request_with_timeout",
        business_failure_send,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_legacy_shared_directory_client",
        lambda client: True,
    )

    local_calls = []

    async def fake_local_init(params):
        local_calls.append(dict(params))
        return {"ok": True}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.harmonyos.harmonyos_dev.run_harmonyos_dev_init",
        fake_local_init,
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=object(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    ws = type("FakeWs", (), {})()

    await server.local_handlers["/tui"]["harmonyos.dev_init"](
        ws,
        "req-business-failure",
        {"operationId": "dev-init-biz-op"},
        "sess-1",
    )
    await asyncio.sleep(0.05)

    assert local_calls == []
    assert server.responses[-1]["id"] == "req-business-failure"
    assert server.responses[-1]["ok"] is False
    assert "harmonyos project invalid" in server.responses[-1]["error"]


@pytest.mark.asyncio
async def test_tui_disconnect_handler_schedules_session_cleanup():
    server = FakeGatewayServer()
    handler = FakeMessageHandler()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=handler,
            on_config_saved=None,
            path="/tui",
        )
    )

    ws = object()
    server.bind_session_owner("tui", "sess-exit", ws)
    await server.local_handlers["/tui"]["tui.disconnect"](
        ws,
        "req-exit",
        {"reason": "user_exit"},
        "sess-exit",
    )

    assert handler.cancelled == []
    assert handler.scheduled == [([("tui", "sess-exit")], [])]
    assert handler.scheduled_delays == [1.0]
    assert server.responses[-1] == {
        "id": "req-exit",
        "ok": True,
        "payload": {"accepted": True, "session_id": "sess-exit"},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_tui_disconnect_ack_waits_until_cleanup_is_scheduled():
    server = FakeGatewayServer()
    handler = BlockingScheduledDisconnectMessageHandler()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=handler,
            on_config_saved=None,
            path="/tui",
        )
    )

    ws = type("FakeWs", (), {})()
    server.bind_session_owner("tui", "sess-exit-order", ws)
    disconnect = asyncio.create_task(
        server.local_handlers["/tui"]["tui.disconnect"](
            ws,
            "req-exit-order",
            {"reason": "user_exit"},
            "sess-exit-order",
        )
    )

    await asyncio.wait_for(handler.schedule_started.wait(), timeout=1)
    assert server.responses == []

    handler.release_schedule.set()
    await disconnect

    assert handler.scheduled == [([("tui", "sess-exit-order")], [])]
    assert server.responses[-1]["id"] == "req-exit-order"


@pytest.mark.asyncio
async def test_tui_disconnect_handler_does_not_cancel_session_owned_by_another_ws():
    server = FakeGatewayServer()
    handler = FakeMessageHandler()
    owner_ws = object()
    exiting_ws = object()
    server.bind_session_owner("tui", "sess-shared", owner_ws)

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=handler,
            on_config_saved=None,
            path="/tui",
        )
    )

    await server.local_handlers["/tui"]["tui.disconnect"](
        exiting_ws,
        "req-exit-other",
        {"reason": "user_exit"},
        "sess-shared",
    )

    assert handler.cancelled == []
    assert server.responses[-1] == {
        "id": "req-exit-other",
        "ok": True,
        "payload": {"accepted": True, "session_id": "sess-shared"},
        "error": None,
        "code": None,
    }


def test_build_cli_route_binding_creates_route_and_install_hook():
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui"))
    server = FakeGatewayServer()

    assert binding.path == "/tui"
    assert binding.channel_id == "tui"
    assert "chat.send" in binding.forward_methods
    assert "history.get" in binding.forward_methods
    assert "team.mq.publish" in binding.forward_methods
    assert "team.mq.publish" in binding.forward_no_local_handler_methods
    assert binding.install is not None

    binding.install(server)

    cli_handlers = server.local_handlers["/tui"]
    assert "config.get" in cli_handlers
    assert "config.validate_model" in cli_handlers
    assert "session.list" in cli_handlers
    assert "chat.send" in cli_handlers


@pytest.mark.asyncio
async def test_tui_route_disconnect_schedules_cancel_for_transport_close():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))

    await binding.disconnect_handler(
        object(),
        [("tui", "sess-drop")],
        [("tui", "req-drop")],
    )

    assert handler.scheduled == [([("tui", "sess-drop")], [("tui", "req-drop")])]
    assert handler.cancelled == []


@pytest.mark.asyncio
async def test_tui_route_disconnect_unregisters_physical_subscriptions():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))
    ws = type("FakeWs", (), {"_jiuwen_ws_id": "tui-ws-dead"})()

    await binding.disconnect_handler(ws, [("tui", "sess-drop")], [])

    assert handler.disconnected_websockets == [("tui", "tui-ws-dead")]


@pytest.mark.asyncio
async def test_tui_route_disconnect_skips_scheduled_cancel_after_explicit_exit():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))
    ws = type("FakeWs", (), {})()
    ws._jiuwenswarm_tui_user_exit = True  # pylint: disable=protected-access

    await binding.disconnect_handler(ws, [("tui", "sess-exit")], [])

    assert handler.scheduled == []


@pytest.mark.asyncio
async def test_tui_route_disconnect_retries_when_cleanup_scheduling_is_cancelled():
    server = FakeGatewayServer()
    handler = BlockingScheduledDisconnectMessageHandler()
    binding = build_cli_route_binding(
        CliRouteBindParams(path="/tui", message_handler=handler)
    )
    binding.install(server)
    ws = type("FakeWs", (), {})()
    server.bind_session_owner("tui", "sess-exit-race", ws)

    explicit_exit = asyncio.create_task(
        server.local_handlers["/tui"]["tui.disconnect"](
            ws,
            "req-exit-race",
            {"reason": "user_exit"},
            "sess-exit-race",
        )
    )
    await handler.schedule_started.wait()
    explicit_exit.cancel()
    await asyncio.gather(explicit_exit, return_exceptions=True)
    handler.release_schedule.set()

    await binding.disconnect_handler(
        ws,
        [("tui", "sess-exit-race")],
        [],
    )

    assert handler.scheduled == [([("tui", "sess-exit-race")], [])]


@pytest.mark.asyncio
async def test_tui_route_disconnect_retries_when_cleanup_scheduling_fails():
    server = FakeGatewayServer()
    handler = FailedOnceScheduledDisconnectMessageHandler()
    binding = build_cli_route_binding(
        CliRouteBindParams(path="/tui", message_handler=handler)
    )
    binding.install(server)
    ws = type("FakeWs", (), {})()
    server.bind_session_owner("tui", "sess-exit-failed", ws)

    await server.local_handlers["/tui"]["tui.disconnect"](
        ws,
        "req-exit-failed",
        {"reason": "user_exit"},
        "sess-exit-failed",
    )
    await binding.disconnect_handler(
        ws,
        [("tui", "sess-exit-failed")],
        [],
    )

    assert handler.schedule_attempts == 2
    assert handler.scheduled == [([("tui", "sess-exit-failed")], [])]


def test_tui_session_bind_handler_cancels_pending_disconnect_cancel():
    handler = FakeMessageHandler()
    binding = build_cli_route_binding(CliRouteBindParams(path="/tui", message_handler=handler))

    binding.session_bind_handler("tui", "sess-reconnect")

    assert handler.reconnected == [("tui", "sess-reconnect")]


@pytest.mark.asyncio
async def test_config_validate_model_handler_uses_local_probe(monkeypatch):
    server = FakeGatewayServer()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=None,
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    cli_handlers = server.local_handlers["/tui"]

    class FakeModel:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def invoke(self, *args, **kwargs):
            return {"content": "hello"}

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.tui.tui_connect.Model", FakeModel)

    await cli_handlers["config.validate_model"](
        object(),
        "req-validate",
        {
            "model_provider": "openai",
            "model": "gpt-4.1",
            "api_base": "https://api.openai.com/v1",
            "api_key": "secret",
        },
        "sess-1",
    )

    assert server.responses[-1] == {
        "id": "req-validate",
        "ok": True,
        "payload": {
            "provider": "OpenAI",
            "model": "gpt-4.1",
            "response": "hello",
        },
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_command_model_switch_sends_scoped_agent_reload(monkeypatch):
    server = FakeGatewayServer()
    sent_envs = []
    defaults = [
        {
            "alias": "glm",
            "model_client_config": {
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "model_name": "GLM-5",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
        {
            "alias": "other",
            "model_client_config": {
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "model_name": "other-model",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
    ]

    async def fake_send_tui_agent_request(_client, env, *, label):
        sent_envs.append((env, label))

    def fake_update_config(mutator, **kwargs):
        data = {"models": {"defaults": [dict(d) for d in defaults]}}
        return mutator(data)

    monkeypatch.setattr(tui_connect_module, "_send_tui_agent_request", fake_send_tui_agent_request)
    monkeypatch.setattr(tui_connect_module, "update_config", fake_update_config)
    monkeypatch.setattr(
        tui_connect_module,
        "get_config_raw",
        lambda: {"models": {"defaults": defaults}},
    )
    monkeypatch.setattr(tui_connect_module, "get_config", lambda: {"models": {"defaults": defaults}})

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    await server.local_handlers["/tui"]["command.model"](
        object(),
        "req-switch",
        {"model": "glm"},
        "tui_session_1",
    )
    await asyncio.sleep(0)

    assert server.responses[-1] == {
        "id": "req-switch",
        "ok": True,
        "payload": {
            "current": "GLM-5",
            "requested": "glm",
            "type": "switched",
            "applied": True,
        },
        "error": None,
        "code": None,
    }
    assert len(sent_envs) == 1
    env, label = sent_envs[0]
    assert label == "command.model.switch"
    assert env.params["target_channel_id"] == "tui"
    assert env.params["target_session_id"] == "tui_session_1"
    assert env.params["reason"] == "model_switch"


@pytest.mark.asyncio
async def test_command_model_delete_matches_by_name_when_index_drifted(monkeypatch):
    """回归测试：删除确认页停留期间 defaults 被切换重排，前端持有的 index 漂移，
    后端 delete_model 必须按 model 字段（model_name/alias）稳态匹配删除，
    而非按过期 index pop 删错条目。

    复现：初始 [GLM-5@0, GLM-5.2@1]；前端选中 GLM-5（index=0）进入删除确认页；
    另一窗口切换 GLM-5.2 为默认 → defaults 重排为 [GLM-5.2, GLM-5]；
    前端确认删除时仍发 index=0 + model="GLM-5"。修复前：pop(0) 删了 GLM-5.2（错）；
    修复后：按 model="GLM-5" 匹配，正确删除 GLM-5。
    """
    server = FakeGatewayServer()
    sent_envs = []
    # 重排后的当前 defaults：GLM-5.2 在前（被切换为默认），GLM-5 在后
    current_defaults = [
        {
            "alias": "other",
            "model_client_config": {
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "model_name": "GLM-5.2",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
        {
            "alias": "glm",
            "model_client_config": {
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "model_name": "GLM-5",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
    ]

    async def fake_send_tui_agent_request(_client, env, *, label):
        sent_envs.append((env, label))

    def fake_update_config(mutator, **kwargs):
        data = {"models": {"defaults": current_defaults}}
        return mutator(data)

    monkeypatch.setattr(tui_connect_module, "_send_tui_agent_request", fake_send_tui_agent_request)
    monkeypatch.setattr(tui_connect_module, "update_config", fake_update_config)
    monkeypatch.setattr(
        tui_connect_module,
        "get_config_raw",
        lambda: {"models": {"defaults": current_defaults}},
    )
    monkeypatch.setattr(
        tui_connect_module, "get_config", lambda: {"models": {"defaults": current_defaults}}
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    # 前端持有过期 index=0（原本指向 GLM-5），但当前 index=0 是 GLM-5.2；
    # model="GLM-5" 是稳态标识。期望删除 GLM-5（即 index=1 的条目）。
    await server.local_handlers["/tui"]["command.model"](
        object(),
        "req-delete",
        {"action": "delete_model", "index": 0, "model": "GLM-5"},
        "tui_session_1",
    )
    await asyncio.sleep(0)

    assert server.responses[-1]["ok"] is True
    # 后端回包 name 必须是真实删除的 GLM-5（而非 index=0 漂移后的 GLM-5.2）
    assert server.responses[-1]["payload"]["name"] == "GLM-5"
    assert server.responses[-1]["payload"]["type"] == "model_deleted"
    # 当前 defaults 仅剩 GLM-5.2
    assert len(current_defaults) == 1
    assert current_defaults[0]["model_client_config"]["model_name"] == "GLM-5.2"


@pytest.mark.asyncio
async def test_command_model_delete_rejects_when_index_and_name_mismatch(monkeypatch):
    """回归测试：同名多条目 + index 漂移到非候选条目时，后端必须拒绝并报"列表已变"，
    不得静默删除 index 当前指向的错条目。
    """
    server = FakeGatewayServer()
    sent_envs = []
    # 两个同名 GLM，provider/api_base 不同（不构成 _mk 冲突）
    current_defaults = [
        {
            "model_client_config": {
                "api_key": "key1",
                "api_base": "https://a.test/v1",
                "model_name": "GLM",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
        {
            "model_client_config": {
                "api_key": "key2",
                "api_base": "https://b.test/v1",
                "model_name": "GLM",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
        {
            "model_client_config": {
                "api_key": "key3",
                "api_base": "https://c.test/v1",
                "model_name": "OTHER",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        },
    ]

    async def fake_send_tui_agent_request(_client, env, *, label):
        sent_envs.append((env, label))

    def fake_update_config(mutator, **kwargs):
        data = {"models": {"defaults": current_defaults}}
        return mutator(data)

    monkeypatch.setattr(tui_connect_module, "_send_tui_agent_request", fake_send_tui_agent_request)
    monkeypatch.setattr(tui_connect_module, "update_config", fake_update_config)
    monkeypatch.setattr(
        tui_connect_module,
        "get_config_raw",
        lambda: {"models": {"defaults": current_defaults}},
    )
    monkeypatch.setattr(
        tui_connect_module, "get_config", lambda: {"models": {"defaults": current_defaults}}
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    # 前端发 model="GLM"（匹配两条）+ index=2（漂移到 OTHER，不在候选 [0,1] 内）。
    # 期望：拒绝删除，报错而非静默删 OTHER。
    await server.local_handlers["/tui"]["command.model"](
        object(),
        "req-delete",
        {"action": "delete_model", "index": 2, "model": "GLM"},
        "tui_session_1",
    )
    await asyncio.sleep(0)

    assert server.responses[-1]["ok"] is False
    assert "refresh" in server.responses[-1]["error"].lower() or "changed" in server.responses[-1]["error"].lower()
    # 列表未被修改
    assert len(current_defaults) == 3


@pytest.mark.asyncio
async def test_command_model_lists_agentos_models_without_defaults(monkeypatch):
    """AgentOS backup models remain selectable when defaults is empty."""
    server = FakeGatewayServer()
    agentos_models = [
        {
            "alias": "backup",
            "model_client_config": {
                "api_key": "backup-key",
                "api_base": "test-endpoint",
                "model_name": "backup-model",
                "client_provider": "openai",
            },
            "model_config_obj": {},
        }
    ]
    monkeypatch.setattr(tui_connect_module, "get_model_names", lambda: [])
    monkeypatch.setattr(
        tui_connect_module,
        "get_config_raw",
        lambda: {"models": {"defaults": [], "agentos": agentos_models}},
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    await server.local_handlers["/tui"]["command.model"](
        object(), "req-model-list", {}, "tui_session_1"
    )

    payload = server.responses[-1]["payload"]
    assert payload["available_models"] == ["backup"]
    assert payload["models"] == [
        {
            "index": "a0",
            "name": "backup",
            "alias": "backup",
            "model_name": "backup-model",
            "model_provider": "openai",
            "api_base": "test-endpoint",
            "reasoning_level": "",
            "api_key_suffix": "-key",
            "is_current": False,
            "is_agentos": True,
        }
    ]

@pytest.mark.asyncio
async def test_session_list_returns_agent_timeout_before_tui_request_timeout(monkeypatch):
    server = FakeGatewayServer()

    class HangingAgentClient:
        async def send_request(self, env):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.agent_request_timeout._TUI_DEFAULT_UNARY_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=HangingAgentClient(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )

    await asyncio.wait_for(
        server.local_handlers["/tui"]["session.list"](
            object(),
            "req-session-list",
            {"limit": 10},
            "sess-1",
        ),
        timeout=0.2,
    )

    assert server.responses[-1] == {
        "id": "req-session-list",
        "ok": False,
        "payload": {},
        "error": "AgentServer request timed out",
        "code": "AGENT_SERVER_TIMEOUT",
    }


def test_get_model_names_skips_empty_name_entries(tmp_path, monkeypatch):
    """Bug #2665: get_model_names() should skip entries with unresolved env vars
    (empty resolved name), so available_models indices don't match _raw_defaults indices.
    The frontend should use models[].index instead of available_models array index.
    """
    import yaml

    from jiuwenswarm.common.config import get_model_names

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "defaults": [
                        {
                            "model_client_config": {
                                "api_key": "${API_KEY}",
                                "api_base": "${API_BASE}",
                                "model_name": "${MODEL_NAME}",
                                "client_provider": "${MODEL_PROVIDER}",
                            },
                            "model_config_obj": {"temperature": 0.95},
                            "is_default": True,
                        },
                        {
                            "model_client_config": {
                                "api_key": "sk-test-key",
                                "api_base": "https://dashscope.aliyuncs.com/v1",
                                "model_name": "glm-5",
                                "client_provider": "OpenAI",
                            },
                            "model_config_obj": {"temperature": 0.95},
                        },
                    ]
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg, raising=False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config._CONFIG_YAML_PATH", cfg, raising=False,
    )

    import jiuwenswarm.common.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", cfg)
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", cfg)

    for var in ("API_KEY", "API_BASE", "MODEL_NAME", "MODEL_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    names = get_model_names()
    assert names == ["glm-5"], (
        f"get_model_names() should skip the placeholder entry with unresolved env vars, "
        f"got {names}"
    )


def test_model_meta_index_field_matches_raw_defaults_position():
    """Bug #2665: _model_meta must include the _raw_defaults index
    so the frontend can send the correct index for model switching.
    """
    from jiuwenswarm.common.config import resolve_env_vars

    defaults = [
        {
            "model_client_config": {
                "api_key": "${API_KEY}",
                "api_base": "${API_BASE}",
                "model_name": "${MODEL_NAME}",
                "client_provider": "${MODEL_PROVIDER}",
            },
            "model_config_obj": {"temperature": 0.95},
            "is_default": True,
        },
        {
            "model_client_config": {
                "api_key": "sk-test-key",
                "api_base": "https://dashscope.aliyuncs.com/v1",
                "model_name": "glm-5",
                "client_provider": "OpenAI",
            },
            "model_config_obj": {"temperature": 0.95},
        },
    ]

    def _model_meta(i, e):
        mcc = e.get("model_client_config") or {}
        mco = e.get("model_config_obj") or {}
        _alias = e.get("alias", "")
        _resolved_alias = resolve_env_vars(str(_alias)) if _alias else ""
        _model_name = resolve_env_vars(str(mcc.get("model_name", "")))
        _api_key = resolve_env_vars(str(mcc.get("api_key", "")))
        return {
            "index": i,
            "name": _resolved_alias or _model_name,
            "alias": _resolved_alias,
            "model_name": _model_name,
            "model_provider": resolve_env_vars(str(mcc.get("client_provider", ""))),
            "api_base": resolve_env_vars(str(mcc.get("api_base", ""))),
            "reasoning_level": resolve_env_vars(str(mco.get("reasoning_level", ""))),
            "api_key_suffix": _api_key[-4:] if _api_key else "",
            "is_current": i == 0,
        }

    import os
    for var in ("API_KEY", "API_BASE", "MODEL_NAME", "MODEL_PROVIDER"):
        os.environ.pop(var, None)

    models = [_model_meta(i, e) for i, e in enumerate(defaults) if isinstance(e, dict)]

    assert models[0]["index"] == 0
    assert models[0]["name"] == ""
    assert models[1]["index"] == 1
    assert models[1]["name"] == "glm-5"

    selectable = [m for m in models if m["name"] and m["name"].lower() not in ("video", "audio", "vision")]
    assert len(selectable) == 1
    assert selectable[0]["index"] == 1, (
        "Frontend should use selectable[0]['index']=1 as origIdx, "
        "not the available_models array index (0)"
    )


# ── 单用户 AgentServer 不可达时的本地回落（P2 遗留修复回归） ──────────────────

def _offline_local_client() -> WebSocketAgentServerClient:
    """server_ready=False 的本地 WebSocketAgentServerClient（共享目录单用户）。"""
    return WebSocketAgentServerClient()  # server_ready defaults to False


@pytest.mark.asyncio
async def test_session_color_set_falls_back_to_local_adapter_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentServer 不可达时，TUI session.color_set 经薄代理回落到本地适配器查询。"""
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_session_metadata",
        lambda *args, **kwargs: {"session_id": "sess-1", "accent_color": "blue"},
    )

    await server.local_handlers["/tui"]["session.color_set"](
        object(), "req-color", {"session_id": "sess-1"}, "sess-1"
    )

    assert server.responses[-1]["ok"] is True
    assert server.responses[-1]["payload"]["accent_color"] == "blue"


@pytest.mark.asyncio
async def test_session_list_falls_back_to_local_adapter_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TUI resume list must not become empty during a local AgentServer restart."""
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_all_sessions_metadata",
        lambda *, limit, offset: (
            [{"session_id": "legacy", "channel_id": "tui", "last_message_at": 1}],
            1,
        ),
    )

    await server.local_handlers["/tui"]["session.list"](
        object(), "req-list", {"all_projects": True}, "current"
    )

    assert server.responses[-1]["ok"] is True
    assert server.responses[-1]["payload"]["sessions"][0]["session_id"] == "legacy"


@pytest.mark.asyncio
async def test_session_preview_falls_back_to_local_adapter_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentServer 不可达时，TUI session.preview 经薄代理回落到本地适配器。"""
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.load_history_records",
        lambda *args, **kwargs: [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "event_type": "chat.final", "content": "hello"},
        ],
    )

    await server.local_handlers["/tui"]["session.preview"](
        object(), "req-preview", {"session_id": "sess-1"}, "sess-1"
    )

    assert server.responses[-1]["ok"] is True
    messages = server.responses[-1]["payload"]["preview_messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_session_delete_falls_back_to_shared_dir_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """AgentServer 不可达时，TUI session.delete 恢复迁移前本地删除路径。"""
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    session_dir = tmp_path / "sessions" / "sess-del"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: {"mode": "agent"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.resolve_session_dir",
        lambda *args, **kwargs: (session_dir, None),
    )

    class _Session:
        async def release_kvc(self):
            return True

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )

    await server.local_handlers["/tui"]["session.delete"](
        object(), "req-del", {"session_id": "sess-del"}, "sess-del"
    )

    assert server.responses[-1]["ok"] is True
    assert server.responses[-1]["payload"] == {"session_id": "sess-del"}
    assert not session_dir.exists()


@pytest.mark.asyncio
async def test_session_rebind_project_falls_back_to_shared_dir_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentServer 不可达时，TUI session.rebind_project 恢复迁移前本地重绑路径。"""
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    project = type(
        "Project",
        (),
        {
            "project_id": "proj-1",
            "project_dir": "/home/user/projects/foo",
            "project_name": "foo",
            "name": "foo",
            "work_mode": "code",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: {"session_id": "sess-1"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda *args, **kwargs: project,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.rebind_session_project",
        lambda *args, **kwargs: True,
    )

    await server.local_handlers["/tui"]["session.rebind_project"](
        object(),
        "req-rebind",
        {"session_id": "sess-1", "project_dir": "/home/user/projects/foo"},
        "sess-1",
    )

    assert server.responses[-1]["ok"] is True
    assert server.responses[-1]["payload"]["project_id"] == "proj-1"


@pytest.mark.asyncio
async def test_session_rebind_project_does_not_read_gateway_state_for_remote_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the local WebSocket client may use the legacy shared-dir fallback."""
    server = FakeGatewayServer()

    class RemoteAgentClient:
        server_ready = False

    register_cli_handlers(
        CliHandlersBindParams(channel=server, agent_client=RemoteAgentClient(), path="/tui")
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: pytest.fail("remote fallback must not read Gateway session state"),
    )

    await server.local_handlers["/tui"]["session.rebind_project"](
        object(),
        "req-remote-rebind",
        {"session_id": "sess-1", "project_dir": "/remote/user/project"},
        "sess-1",
    )

    assert server.responses[-1]["ok"] is False
    assert server.responses[-1]["code"] == "SERVICE_UNAVAILABLE"


class _DictOwnedCronController:
    """Mimics the real ``CronController.get_job`` which returns ``to_dict()``."""

    def __init__(self) -> None:
        self.job = {
            "id": "cron-dict-owner",
            "user_id": "user-owner",
            "description": "old",
            "work_mode": "work",
            "session_id": "sess-1",
        }
        self.update_calls: list[tuple[str, dict]] = []

    async def get_job(self, job_id):
        return dict(self.job) if job_id == self.job["id"] else None

    async def update_job(self, job_id, patch):
        self.update_calls.append((job_id, dict(patch)))
        return {"id": job_id, **self.job, **patch}


@pytest.mark.asyncio
async def test_cron_job_update_accepts_matching_owner_with_dict_job() -> None:
    """I-IP5-16: TUI cron.job.update must accept the creator when get_job returns a dict.

    Real CronController.get_job returns CronJob.to_dict().  Reading user_id
    via getattr(dict, ...) always yields "" and rejected every authenticated
    update with "job not found", even though create/get succeeded.
    """
    server = FakeGatewayServer()
    cron = _DictOwnedCronController()
    register_cli_handlers(
        CliHandlersBindParams(channel=server, cron_controller=cron, path="/tui")
    )

    await server.local_handlers["/tui"]["cron.job.update"](
        object(),
        "req-cron-update",
        {"id": "cron-dict-owner", "patch": {"description": "new"}},
        "sess-1",
        "user-owner",
    )

    assert server.responses[-1]["ok"] is True, server.responses[-1]
    assert cron.update_calls, "controller.update_job must be invoked"
    assert cron.update_calls[0][1]["description"] == "new"
    assert server.responses[-1]["payload"]["job"]["description"] == "new"


@pytest.mark.asyncio
async def test_cron_job_update_rejects_a_different_authenticated_owner() -> None:
    server = FakeGatewayServer()
    cron = _DictOwnedCronController()
    register_cli_handlers(
        CliHandlersBindParams(channel=server, cron_controller=cron, path="/tui")
    )

    await server.local_handlers["/tui"]["cron.job.update"](
        object(),
        "req-cron-owner",
        {"id": "cron-dict-owner", "patch": {"description": "stolen"}},
        "sess-1",
        "user-other",
    )

    assert cron.update_calls == []
    assert server.responses[-1]["ok"] is False
    assert server.responses[-1]["error"] == "job not found"
    assert server.responses[-1]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_cron_job_update_accepts_matching_owner_with_object_job() -> None:
    """Object-shaped jobs (tests / CronJob-like callers) must still pass owner check."""
    from types import SimpleNamespace

    class _ObjectOwnedCronController:
        def __init__(self) -> None:
            self.job = SimpleNamespace(id="cron-obj-owner", user_id="user-owner")
            self.update_calls = 0

        async def get_job(self, job_id):
            return self.job if job_id == self.job.id else None

        async def update_job(self, job_id, patch):
            self.update_calls += 1
            return {"id": job_id, **patch}

    server = FakeGatewayServer()
    cron = _ObjectOwnedCronController()
    register_cli_handlers(
        CliHandlersBindParams(channel=server, cron_controller=cron, path="/tui")
    )

    await server.local_handlers["/tui"]["cron.job.update"](
        object(),
        "req-cron-obj",
        {"id": "cron-obj-owner", "patch": {"description": "new"}},
        "sess-1",
        "user-owner",
    )

    assert cron.update_calls == 1
    assert server.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_agentos_cron_update_project_fields_with_dict_job(monkeypatch) -> None:
    """AgentOS update with project fields must work with dict-shaped jobs."""

    class _AgentOSClient:
        server_ready = True

    server = FakeGatewayServer()
    cron = _DictOwnedCronController()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server, agent_client=_AgentOSClient(), cron_controller=cron, path="/tui"
        )
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_agentos_routing_client",
        lambda _client: True,
    )

    async def _fake_resolve_agent_cron_project_binding(**kwargs):
        assert kwargs.get("session_id") == "sess-1"
        assert kwargs["params"].get("work_mode") == "work"
        return True, {"project_id": "user-proj-1", "work_mode": "code"}

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.resolve_agent_cron_project_binding",
        _fake_resolve_agent_cron_project_binding,
    )

    await server.local_handlers["/tui"]["cron.job.update"](
        object(),
        "req-cron-update-proj",
        {"id": "cron-dict-owner", "patch": {"project_id": "user-proj-1"}},
        "sess-1",
        "user-owner",
    )

    assert server.responses[-1]["ok"] is True, server.responses[-1]
    assert cron.update_calls, "controller.update_job must be invoked"
    patch = cron.update_calls[0][1]
    assert patch["project_id"] == "user-proj-1"
    assert patch["work_mode"] == "code"
    assert patch["_agentos_project_binding_verified"] is True
