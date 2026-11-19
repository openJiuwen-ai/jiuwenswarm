# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import copy

import pytest
from e2a.agent_models import AgentResponse
from e2a.message import ReqMethod
from agent_gateway.common.config_panel import tui_models_handlers
from agent_gateway.channel_manager.tui import tui_connect as tui_connect_module
from agent_gateway.channel_manager.tui.tui_connect import (
    CliHandlersBindParams,
    CliRouteBindParams,
    build_cli_route_binding,
    register_cli_handlers,
)
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from agent_gateway.heartbeat import HeartbeatServiceUnavailableError

from tests.unit_tests.gateway.config_routing_harness import make_config_routing_client
from jiuwenswarm.common.config_panel import tui_models_handlers as _server_tui_models_handlers
from jiuwenswarm.common.opencode_go import (
    OPENCODE_GO_SESSION_HEADER,
    SCOPE_PROBE,
    derive_session_id,
)


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
        self, session_keys, *, stale_request_keys=None, delay_seconds=60.0, user_id=None, mode=None
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
        self, session_keys, *, stale_request_keys=None, delay_seconds=60.0, user_id=None, mode=None
    ):
        self.schedule_started.set()
        await self.release_schedule.wait()
        await super().schedule_cancel_agent_sessions_on_disconnect(
            session_keys,
            stale_request_keys=stale_request_keys,
            delay_seconds=delay_seconds,
            user_id=user_id,
            mode=mode,
        )


class FailedOnceScheduledDisconnectMessageHandler(FakeMessageHandler):
    def __init__(self):
        super().__init__()
        self.schedule_attempts = 0

    async def schedule_cancel_agent_sessions_on_disconnect(
        self, session_keys, *, stale_request_keys=None, delay_seconds=60.0, user_id=None, mode=None
    ):
        self.schedule_attempts += 1
        if self.schedule_attempts == 1:
            raise RuntimeError("cleanup scheduling failed")
        await super().schedule_cancel_agent_sessions_on_disconnect(
            session_keys,
            stale_request_keys=stale_request_keys,
            delay_seconds=delay_seconds,
            user_id=user_id,
            mode=mode,
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
        "agent_gateway.routing.e2a_proxy.proxy_unary_request",
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
async def test_harmonyos_dev_init_remote_client_error_without_fallback(monkeypatch):
    """远程/AgentOS client 传输失败时不回退本地，直接报错（不做部署目录 fallback）。"""
    server = FakeGatewayServer()
    _install_failing_dev_init_send(monkeypatch)
    monkeypatch.setattr(
        "agent_gateway.routing.e2a_proxy.is_legacy_shared_directory_client",
        lambda client: False,
    )

    local_calls = []

    async def fake_local_init(params):
        local_calls.append(dict(params))
        return {"ok": True}

    monkeypatch.setattr(
        "agent_gateway.channel_manager.tui.harmonyos.harmonyos_dev.run_harmonyos_dev_init",
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
async def test_config_validate_model_handler_uses_local_probe(monkeypatch):
    server = FakeGatewayServer()

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=make_config_routing_client(),
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

    monkeypatch.setattr("jiuwenswarm.common.config_panel.tui_models_handlers.Model", FakeModel)

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
async def test_config_validate_model_forwards_opencode_go_vendor(monkeypatch):
    """TUI 探测与 Web 同口径：草稿 vendor_key 进入探测并携带 probe 会话头。"""
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=make_config_routing_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    captured = {}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def invoke(self, *args, **kwargs):
            return {"content": "hello"}

    monkeypatch.setattr("jiuwenswarm.common.config_panel.tui_models_handlers.Model", FakeModel)

    await server.local_handlers["/tui"]["config.validate_model"](
        object(),
        "req-validate-opencode",
        {
            "model_provider": "OpenAI",
            "model": "deepseek-v4.1-flash",
            "api_base": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-test-not-a-real-key",
            "vendor_key": "opencode-go",
        },
        "sess-1",
    )

    mcc = captured["model_client_config"]
    assert mcc.vendor_key == "opencode-go"
    assert mcc.custom_headers[OPENCODE_GO_SESSION_HEADER] == derive_session_id(
        SCOPE_PROBE
    )
    assert server.responses[-1]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["exception", "empty"])
async def test_config_validate_model_retries_failed_probe_with_more_tokens(
    monkeypatch, first_failure
):
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=make_config_routing_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    max_tokens_calls = []

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        async def invoke(self, *args, **kwargs):
            max_tokens_calls.append(kwargs["max_tokens"])
            if len(max_tokens_calls) == 1:
                if first_failure == "exception":
                    raise RuntimeError("token budget too small")
                return {"content": "", "reasoning_content": ""}
            return {"content": "hello"}

    monkeypatch.setattr(_server_tui_models_handlers, "Model", FakeModel)

    await server.local_handlers["/tui"]["config.validate_model"](
        object(),
        "req-validate-retry",
        {
            "model_provider": "openai",
            "model": "gpt-4.1",
            "api_base": "https://api.openai.com/v1",
            "api_key": "secret",
        },
        "sess-1",
    )

    assert max_tokens_calls == [3, 16]
    assert server.responses[-1]["ok"] is True
    assert server.responses[-1]["payload"]["response"] == "hello"


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
    monkeypatch.setattr(tui_models_handlers, "update_config", fake_update_config)
    monkeypatch.setattr(
        tui_models_handlers,
        "get_config_raw",
        lambda: {"models": {"defaults": current_defaults}},
    )
    monkeypatch.setattr(
        tui_models_handlers, "get_config", lambda: {"models": {"defaults": current_defaults}}
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            force_local_config=True,
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
    monkeypatch.setattr(tui_models_handlers, "update_config", fake_update_config)
    monkeypatch.setattr(
        tui_models_handlers,
        "get_config_raw",
        lambda: {"models": {"defaults": current_defaults}},
    )
    monkeypatch.setattr(
        tui_models_handlers, "get_config", lambda: {"models": {"defaults": current_defaults}}
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            force_local_config=True,
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
    monkeypatch.setattr(tui_models_handlers, "get_model_names", lambda: [])
    monkeypatch.setattr(
        tui_models_handlers,
        "get_config_raw",
        lambda: {"models": {"defaults": [], "agentos": agentos_models}},
    )

    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=_offline_local_client(),
            force_local_config=True,
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


_OPENCODE_GO_MCC = {
    "api_key": "sk-test-not-a-real-key",
    "api_base": "https://opencode.ai/zen/go/v1",
    "model_name": "oc-model",
    "client_provider": "OpenAI",
    "vendor_key": "opencode-go",
}


def _command_model_store(monkeypatch, defaults):
    """Local AgentServer-side handler with a transactional in-memory config store."""
    server = FakeGatewayServer()
    store = {"data": {"models": {"defaults": [dict(d) for d in defaults]}}}

    def fake_update_config(mutator, **kwargs):
        # 与事务语义一致：只有 mutator 正常返回才落库，异常时丢弃候选数据。
        candidate = copy.deepcopy(store["data"])
        store["data"] = mutator(candidate)
        return store["data"]

    monkeypatch.setattr(_server_tui_models_handlers, "update_config", fake_update_config)
    monkeypatch.setattr(_server_tui_models_handlers, "get_config", lambda: store["data"])
    monkeypatch.setattr(_server_tui_models_handlers, "get_model_names", lambda: [])
    return server, store


async def _call_command_model(server, req_id, params):
    await _server_tui_models_handlers.command_model_handler(
        server,
        object(),
        req_id,
        params,
        "tui_session_1",
        agent_client=_offline_local_client(),
    )


@pytest.mark.asyncio
async def test_command_model_list_emits_opencode_go_vendor_key(monkeypatch):
    server = FakeGatewayServer()
    defaults = [
        {
            "alias": "opencode-go",
            "model_client_config": dict(_OPENCODE_GO_MCC),
            "model_config_obj": {},
        }
    ]
    monkeypatch.setattr(
        _server_tui_models_handlers, "get_model_names", lambda: ["opencode-go"]
    )
    monkeypatch.setattr(
        _server_tui_models_handlers,
        "get_config_raw",
        lambda: {"models": {"defaults": defaults}},
    )

    await _call_command_model(server, "req-model-list", {})

    meta = server.responses[-1]["payload"]["models"][0]
    assert meta["vendor_key"] == "opencode-go"
    assert meta["model_provider"] == "OpenAI"


@pytest.mark.asyncio
async def test_command_model_add_persists_opencode_go_vendor_key(monkeypatch):
    server, store = _command_model_store(monkeypatch, [])

    await _call_command_model(
        server,
        "req-add-opencode",
        {
            "action": "add_model",
            "target": "oc",
            "config": {
                "provider": "OpenAI",
                "vendor_key": "opencode-go",
                "api_base": "https://opencode.ai/zen/go/v1",
                "api_key": "sk-test-not-a-real-key",
                "model_name": "oc-model",
            },
        },
    )

    assert server.responses[-1]["ok"] is True
    persisted = store["data"]["models"]["defaults"][-1]["model_client_config"]
    assert persisted["vendor_key"] == "opencode-go"
    assert persisted["client_provider"] == "OpenAI"


@pytest.mark.asyncio
async def test_command_model_add_accepts_lowercase_openai_for_opencode_go(monkeypatch):
    server, store = _command_model_store(monkeypatch, [])

    await _call_command_model(
        server,
        "req-add-opencode-lower",
        {
            "action": "add_model",
            "target": "oc",
            "config": {
                "provider": "openai",
                "vendor_key": "opencode-go",
                "api_base": "https://opencode.ai/zen/go/v1",
                "api_key": "sk-test-not-a-real-key",
                "model_name": "oc-model",
            },
        },
    )

    assert server.responses[-1]["ok"] is True
    persisted = store["data"]["models"]["defaults"][-1]["model_client_config"]
    assert persisted["client_provider"] == "openai"
    assert persisted["vendor_key"] == "opencode-go"


@pytest.mark.asyncio
async def test_command_model_add_rejects_opencode_go_with_other_provider(monkeypatch):
    server, store = _command_model_store(monkeypatch, [])

    await _call_command_model(
        server,
        "req-add-opencode-bad",
        {
            "action": "add_model",
            "target": "oc",
            "config": {
                "provider": "DeepSeek",
                "vendor_key": "opencode-go",
                "api_base": "https://opencode.ai/zen/go/v1",
                "api_key": "sk-test-not-a-real-key",
                "model_name": "oc-model",
            },
        },
    )

    assert server.responses[-1]["ok"] is False
    assert "OpenAI" in server.responses[-1]["error"]
    assert store["data"]["models"]["defaults"] == []


@pytest.mark.asyncio
async def test_command_model_update_rejects_opencode_go_with_other_provider(monkeypatch):
    defaults = [
        {
            "alias": "oc",
            "model_client_config": dict(_OPENCODE_GO_MCC),
            "model_config_obj": {},
        }
    ]
    server, store = _command_model_store(monkeypatch, defaults)

    await _call_command_model(
        server,
        "req-update-opencode-bad",
        {
            "action": "update_model",
            "index": 0,
            "config": {"provider": "DeepSeek"},
        },
    )

    assert server.responses[-1]["ok"] is False
    assert "OpenAI" in server.responses[-1]["error"]
    persisted = store["data"]["models"]["defaults"][0]["model_client_config"]
    assert persisted["client_provider"] == "OpenAI"
    assert persisted["vendor_key"] == "opencode-go"


@pytest.mark.asyncio
async def test_command_model_update_clears_vendor_key_when_switching_provider(monkeypatch):
    defaults = [
        {
            "alias": "oc",
            "model_client_config": dict(_OPENCODE_GO_MCC),
            "model_config_obj": {},
        }
    ]
    server, store = _command_model_store(monkeypatch, defaults)

    await _call_command_model(
        server,
        "req-update-provider",
        {
            "action": "update_model",
            "index": 0,
            "config": {"provider": "DeepSeek", "vendor_key": ""},
        },
    )

    assert server.responses[-1]["ok"] is True
    persisted = store["data"]["models"]["defaults"][0]["model_client_config"]
    assert persisted["client_provider"] == "DeepSeek"
    assert "vendor_key" not in persisted


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
# Front 控制面 RPC（session.list/color_set/preview）离线时不得由 Gateway 顶替。

def _offline_local_client() -> WebSocketAgentServerClient:
    """server_ready=False 的本地 WebSocketAgentServerClient（共享目录单用户）。"""
    return WebSocketAgentServerClient()  # server_ready defaults to False


def _assert_control_method_unavailable(server: FakeGatewayServer) -> None:
    resp = server.responses[-1]
    assert resp["ok"] is False
    assert resp["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_session_color_set_unavailable_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session.color_set is Front Control; Gateway must not run a local adapter."""
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

    _assert_control_method_unavailable(server)


@pytest.mark.asyncio
async def test_session_list_unavailable_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session.list is Front Control; Gateway must not run a local adapter."""
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

    _assert_control_method_unavailable(server)


@pytest.mark.asyncio
async def test_session_preview_unavailable_when_agent_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session.preview is Front Control; Gateway must not run a local adapter."""
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

    _assert_control_method_unavailable(server)


@pytest.mark.asyncio
async def test_session_delete_unavailable_when_agent_offline() -> None:
    """session.delete is execution-plane; Gateway must not run a local adapter."""
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

    await server.local_handlers["/tui"]["session.delete"](
        object(), "req-del", {"session_id": "sess-del"}, "sess-del"
    )

    _assert_control_method_unavailable(server)


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

    async def get_job(self, job_id, **_kwargs):
        return dict(self.job) if job_id == self.job["id"] else None

    async def update_job(self, job_id, patch, **_kwargs):
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
async def test_models_list_builds_the_list_off_the_event_loop(monkeypatch):
    server = FakeGatewayServer()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=server,
            agent_client=make_config_routing_client(),
            message_handler=None,
            on_config_saved=None,
            path="/tui",
        )
    )
    seen = {}

    def fake_available(_config=None):
        try:
            asyncio.get_running_loop()
            seen["on_event_loop"] = True
        except RuntimeError:
            seen["on_event_loop"] = False
        return [{"model_client_config": {"model_name": "m", "api_key": "k"}}]

    monkeypatch.setattr(_server_tui_models_handlers, "get_config", lambda: {})
    monkeypatch.setattr(_server_tui_models_handlers, "get_available_models", fake_available)

    await server.local_handlers["/tui"]["models.list"](object(), "req-models", {}, "sess-1")

    assert server.responses[-1]["ok"] is True
    assert seen["on_event_loop"] is False, "get_available_models 不能在事件循环线程上执行"
