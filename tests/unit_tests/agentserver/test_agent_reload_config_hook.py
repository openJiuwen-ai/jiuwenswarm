# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AGENT_RELOAD_CONFIG hook（agent.reload_config 生效前触发）的回合测试。

回合自 enterprise_dev（原 agentserver/agent_ws_server.py 实现）：
- 触发：handle_agent_reload_config 在 reload 生效前触发 AGENT_RELOAD_CONFIG
- 语义：扩展可原地改写 config/env，宿主在 hook 返回后使用修改值执行 reload；
  ExtensionRegistry 未初始化时静默跳过（与 enterprise_dev 语义一致）
"""

import asyncio

import pytest

from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import AgentReloadConfigHookContext
from jiuwenswarm.extensions.registry import ExtensionRegistry

from tests.unit_tests.conftest import patch_handler_name


class _RecordingCallbackFramework:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    @staticmethod
    def register_sync(*_args, **_kwargs) -> None:
        return None

    async def trigger(self, *args, **kwargs) -> None:
        ctx = args[1] if len(args) > 1 else None
        if isinstance(ctx, AgentReloadConfigHookContext):
            # 模拟扩展改写 reload 配置
            ctx.config = {"mutated": True}
        self.calls.append((args, kwargs))


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def setup_function():
    ExtensionRegistry.reset_instance()


def teardown_function():
    ExtensionRegistry.reset_instance()


def _spy_registry() -> _RecordingCallbackFramework:
    spy = _RecordingCallbackFramework()
    ExtensionRegistry.create_instance(
        callback_framework=spy,
        config={},
        logger=object(),
    )
    return spy


def test_agent_reload_config_context_to_dict() -> None:
    reload_ctx = AgentReloadConfigHookContext(
        request_id="r1", channel_id="cli", config={"a": 1}, env={"K": "V"}
    )
    assert reload_ctx.to_dict() == {
        "request_id": "r1",
        "channel_id": "cli",
        "config": {"a": 1},
        "env": {"K": "V"},
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_agent_reload_config_triggers_hook_and_applies_mutation(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
    from jiuwenswarm.server.handlers import ops as ops_handlers

    spy = _spy_registry()
    server = agent_ws_server_module.AgentWebSocketServer()
    calls = []

    async def fake_reload(config, env, **kwargs):
        calls.append((config, env))

    monkeypatch.setattr(server._agent_manager, "reload_agents_config", fake_reload)
    patch_handler_name(
        monkeypatch,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {"response_id": response_id, "ok": resp.ok},
    )

    request = AgentRequest(
        request_id="reload-1",
        channel_id="cli",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params={"config": {"models": {"defaults": []}}, "env": {}},
    )

    ws = FakeWebSocket()
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    ctx = RequestContext(
        request=request,
        sink=WSSink(ws, asyncio.Lock()),
        connection_id=str(id(ws)),
        services=AgentServerServices(server),
    )
    await ops_handlers.handle_agent_reload_config(ctx)

    # hook 触发且扩展改写的 config 生效
    assert len(spy.calls) == 1
    assert spy.calls[0][0][0] == AgentServerHookEvents.AGENT_RELOAD_CONFIG
    assert calls == [({"mutated": True}, {})]


@pytest.mark.asyncio
async def test_agent_reload_config_works_without_registry(monkeypatch):
    """ExtensionRegistry 未初始化时 reload 正常执行（hook 静默跳过）。"""
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
    from jiuwenswarm.server.handlers import ops as ops_handlers

    ExtensionRegistry.reset_instance()
    server = agent_ws_server_module.AgentWebSocketServer()
    calls = []

    async def fake_reload(config, env, **kwargs):
        calls.append((config, env))

    monkeypatch.setattr(server._agent_manager, "reload_agents_config", fake_reload)
    patch_handler_name(
        monkeypatch,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {"response_id": response_id, "ok": resp.ok},
    )

    request = AgentRequest(
        request_id="reload-2",
        channel_id="cli",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params={"config": {"a2ui": {"enabled": True}}, "env": {}},
    )

    ws = FakeWebSocket()
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    ctx = RequestContext(
        request=request,
        sink=WSSink(ws, asyncio.Lock()),
        connection_id=str(id(ws)),
        services=AgentServerServices(server),
    )
    await ops_handlers.handle_agent_reload_config(ctx)

    assert calls == [({"a2ui": {"enabled": True}}, {})]
