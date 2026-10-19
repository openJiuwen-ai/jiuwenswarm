# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AGENT_RELOAD_CONFIG hook（agent.reload_config 生效前触发）的回合测试。

回合自 enterprise_dev（原 agentserver/agent_ws_server.py 实现）。检视后固化的契约：
- 触发时机：仅在确认执行 agent 侧 reload（reload_scopes 命中）且 officeclaw
  租户守卫通过后触发；scope 不命中 / 守卫拒绝的请求不触发扩展链。
- 改写语义：扩展整体替换 ctx.config / ctx.env，宿主 reload 使用修改值；
  config 原值可为 None（未下发，表示按本地配置 reload）。
- 异常语义：registry 未初始化时静默跳过（reload 照常）；trigger 抛错
  （如扩展 abort 重抛的 cause）向上传播，reload 请求失败（ok=False），
  不允许改写被静默丢弃后 reload 依旧成功。
"""

import asyncio
import json

import pytest

from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import AgentReloadConfigHookContext
from jiuwenswarm.extensions.registry import ExtensionRegistry

# 复用同目录 reload scope 测试的装配 harness（FakeWebSocket / RequestContext 装配）
from tests.unit_tests.agentserver.test_agent_reload_scope import (
    FakeWebSocket,
    _ctx_for_test,
)
from tests.unit_tests.conftest import patch_handler_name


class _RecordingCallbackFramework:
    """记录 hook 收到的 context 快照（改写前），可选拦截改写 / 抛错。"""

    def __init__(self, *, mutate: bool = True, error: Exception | None = None) -> None:
        self.received: list[tuple] = []
        self.calls: list[tuple] = []
        self._mutate = mutate
        self._error = error

    @staticmethod
    def register_sync(*_args, **_kwargs) -> None:
        return None

    async def trigger(self, *args, **kwargs) -> None:
        ctx = args[1] if len(args) > 1 else None
        if isinstance(ctx, AgentReloadConfigHookContext):
            self.received.append(
                (args[0], ctx.config, ctx.env, ctx.request_id, ctx.channel_id)
            )
            if self._error is not None:
                raise self._error
            if self._mutate:
                # 模拟扩展整体替换 reload 配置（不动 env）
                ctx.config = {"mutated": True}
        self.calls.append((args, kwargs))


def setup_function():
    ExtensionRegistry.reset_instance()


def teardown_function():
    ExtensionRegistry.reset_instance()


def _spy_registry(**framework_kwargs) -> _RecordingCallbackFramework:
    spy = _RecordingCallbackFramework(**framework_kwargs)
    ExtensionRegistry.create_instance(
        callback_framework=spy,
        config={},
        logger=object(),
    )
    return spy


def _patch_wire(monkeypatch):
    patch_handler_name(
        monkeypatch,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "ok": resp.ok,
            "payload": resp.payload,
        },
    )


async def _run_reload_handler(monkeypatch, request, *, error=None, mutate=True):
    """跑 handle_agent_reload_config，返回 (spy, reload_calls, ws)。"""
    from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
    from jiuwenswarm.server.handlers import ops as ops_handlers

    spy = _spy_registry(error=error, mutate=mutate)
    server = agent_ws_server_module.AgentWebSocketServer()
    reload_calls = []

    async def fake_reload(config, env, **kwargs):
        reload_calls.append((config, env))

    monkeypatch.setattr(server._agent_manager, "reload_agents_config", fake_reload)
    _patch_wire(monkeypatch)

    ws = FakeWebSocket()
    await ops_handlers.handle_agent_reload_config(
        _ctx_for_test(ws, request, asyncio.Lock(), server)
    )
    return spy, reload_calls, ws


def _make_request(params, *, request_id="reload-1", channel_id="cli", **extra):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    return AgentRequest(
        request_id=request_id,
        channel_id=channel_id,
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params=params,
        **extra,
    )


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
async def test_hook_receives_request_contract_and_mutation_applies(monkeypatch):
    """契约：hook 收到的正是请求载荷原值；扩展整体替换 config 后 reload 生效。"""
    request = _make_request(
        {"config": {"models": {"defaults": []}}, "env": {"K": "V"}}
    )

    spy, reload_calls, ws = await _run_reload_handler(monkeypatch, request)

    # 改写前快照：context 收到的就是请求里的 config / env / request_id / channel_id
    assert spy.received == [
        (
            AgentServerHookEvents.AGENT_RELOAD_CONFIG,
            {"models": {"defaults": []}},
            {"K": "V"},
            "reload-1",
            "cli",
        )
    ]
    # 扩展改写生效：reload 用替换后的 config，env 原样透传
    assert reload_calls == [({"mutated": True}, {"K": "V"})]
    assert json.loads(ws.sent[-1])["ok"] is True


@pytest.mark.asyncio
async def test_hook_not_fired_when_scope_excludes_agent_reload(monkeypatch):
    """reload_scopes 仅 web_ui：不执行 agent 侧 reload，也不触发扩展链。"""
    request = _make_request(
        {"config": {"a2ui": {"enabled": True}}, "env": {}, "reload_scopes": ["web_ui"]}
    )

    spy, reload_calls, ws = await _run_reload_handler(monkeypatch, request)

    assert spy.received == []
    assert spy.calls == []
    assert reload_calls == []
    assert json.loads(ws.sent[-1])["ok"] is True


@pytest.mark.asyncio
async def test_hook_not_fired_when_tenant_guard_rejects(monkeypatch):
    """officeclaw 命名租户不在目录：守卫先拒绝，扩展链不跑、reload 不执行。"""
    request = _make_request(
        {"config": {"models": {"defaults": []}}, "env": {}},
        channel_id="officeclaw",
        agent_id="ghost-tenant",
    )

    spy, reload_calls, _ws = await _run_reload_handler(monkeypatch, request)

    assert spy.received == []
    assert reload_calls == []


@pytest.mark.asyncio
async def test_trigger_error_fails_reload(monkeypatch):
    """扩展 abort（trigger 抛错）向上传播：reload 失败可见，不静默用原配置。"""
    request = _make_request({"config": {"models": {"defaults": []}}, "env": {}})

    spy, reload_calls, ws = await _run_reload_handler(
        monkeypatch, request, error=RuntimeError("quota exceeded")
    )

    # hook 确已触发（收到契约值），但错误向上传播
    assert spy.received[0][1] == {"models": {"defaults": []}}
    assert reload_calls == []
    assert json.loads(ws.sent[-1])["ok"] is False


@pytest.mark.asyncio
async def test_hook_none_config_replacement(monkeypatch):
    """只带 env 的 reload：hook 收到 config=None，扩展整体替换后生效。"""
    request = _make_request({"env": {}})

    spy, reload_calls, _ws = await _run_reload_handler(monkeypatch, request)

    assert spy.received == [
        (AgentServerHookEvents.AGENT_RELOAD_CONFIG, None, {}, "reload-1", "cli")
    ]
    assert reload_calls == [({"mutated": True}, {})]


@pytest.mark.asyncio
async def test_agent_reload_config_works_without_registry(monkeypatch):
    """ExtensionRegistry 未初始化时 reload 正常执行（hook 静默跳过）。"""
    from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
    from jiuwenswarm.server.handlers import ops as ops_handlers

    ExtensionRegistry.reset_instance()
    server = agent_ws_server_module.AgentWebSocketServer()
    reload_calls = []

    async def fake_reload(config, env, **kwargs):
        reload_calls.append((config, env))

    monkeypatch.setattr(server._agent_manager, "reload_agents_config", fake_reload)
    _patch_wire(monkeypatch)

    request = _make_request({"config": {"a2ui": {"enabled": True}}, "env": {}})

    ws = FakeWebSocket()
    await ops_handlers.handle_agent_reload_config(
        _ctx_for_test(ws, request, asyncio.Lock(), server)
    )

    assert reload_calls == [({"a2ui": {"enabled": True}}, {})]
