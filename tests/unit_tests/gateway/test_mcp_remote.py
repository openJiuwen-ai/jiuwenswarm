# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the mcp_registry WS remote provider and the bridge adapter."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.routing import mcp_remote
from gateway.routing.mcp_remote import (
    McpRegistryRemoteProvider,
    bind_agent_client,
    install,
)
from gateway.common import external_providers
from gateway.common.external_providers import provider


class _FakeUnary:
    """Captures fetch_agent_unary kwargs and replays a canned (ok, payload)."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.result: tuple[bool, dict[str, Any]] = (True, {"completed": True})

    async def __call__(self, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
        self.captured.update(kwargs)
        return self.result


@pytest.fixture(autouse=True)
def _reset_agent_client(monkeypatch: pytest.MonkeyPatch):
    """bind_agent_client 写的是模块级全局，跨用例必须复位防状态泄漏。"""
    monkeypatch.setattr(mcp_remote, "_agent_client", None)


@pytest.fixture(name="fake_unary")
def _fake_unary(monkeypatch: pytest.MonkeyPatch) -> _FakeUnary:
    fake = _FakeUnary()
    monkeypatch.setattr(mcp_remote, "fetch_agent_unary", fake)
    return fake


@pytest.mark.asyncio
async def test_remote_callback_returns_result_and_shapes_request(fake_unary) -> None:
    bind_agent_client(SimpleNamespace(name="agent-client"))
    fake_unary.result = (True, {"completed": True})

    result = await McpRegistryRemoteProvider().complete_oauth_callback_async(
        {"code": "abc", "state": "s1"}
    )

    assert result is True
    assert fake_unary.captured["req_method"].value == "command.mcp"
    assert fake_unary.captured["params"] == {
        "action": "oauth_callback",
        "query": {"code": "abc", "state": "s1"},
    }
    # 内部通道标识：server 侧按 channel_id == "gateway" 门控原始数据 action 族
    assert fake_unary.captured["channel_id"] == "gateway"
    # 换码含 IdP 20s 外部往返：超时须覆盖服务端满额再加余量（P2-2）
    assert fake_unary.captured["timeout_seconds"] >= 25.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        (False, {"error": "internal", "code": "MCP_INTERNAL"}),
    ],
)
async def test_remote_callback_raises_on_failure(fake_unary, result) -> None:
    """服务端失败文案经 McpOAuthError 透传——回调页据此给 400 落地而非假成功。"""
    from gateway.common.mcp_oauth import McpOAuthError

    fake_unary.result = result

    with pytest.raises(McpOAuthError, match="internal"):
        await McpRegistryRemoteProvider().complete_oauth_callback_async({"code": "x"})


@pytest.mark.asyncio
async def test_remote_callback_completed_false_returns_false(fake_unary) -> None:
    """壳对 ok-but-not-completed 返回 False；抛错在上层 mcp_oauth（见下）。"""
    fake_unary.result = (True, {"completed": False})

    assert (
        await McpRegistryRemoteProvider().complete_oauth_callback_async({"code": "x"})
        is False
    )


@pytest.mark.asyncio
async def test_mcp_oauth_layer_raises_on_uncompleted(monkeypatch) -> None:
    """complete_oauth_flow_async 对 False 结果抛 McpOAuthError——handler 不检查返回值。"""
    from types import SimpleNamespace as NS

    from gateway.common import external_providers, mcp_oauth

    saved = dict(external_providers._providers)
    external_providers._providers["mcp_registry"] = NS(
        complete_oauth_callback_async=_async_false
    )
    try:
        with pytest.raises(mcp_oauth.McpOAuthError):
            await mcp_oauth.complete_oauth_flow_async({"code": "x"})
    finally:
        external_providers._providers.clear()
        external_providers._providers.update(saved)


async def _async_false(query):
    return False


def test_remote_sync_entry_degrades_to_false() -> None:
    """同步入口降级返回 False 并告警（远端模式下没有本地桥可回落）。"""
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    module_logger = logging.getLogger("gateway.routing.mcp_remote")
    handler = _Capture()
    module_logger.addHandler(handler)
    try:
        result = McpRegistryRemoteProvider().complete_oauth_callback({"code": "x"})
    finally:
        module_logger.removeHandler(handler)

    assert result is False
    assert any("sync entry degraded" in message for message in records)


@pytest.fixture(name="reset_registry")
def _reset_registry():
    """Isolate the provider registry around install()."""
    saved = dict(external_providers._providers)
    yield
    external_providers._providers.clear()
    external_providers._providers.update(saved)


def test_install_registers_only_the_registry_domain(reset_registry) -> None:
    """bridge 退役后 install 只注册 mcp_registry（其余 MCP 域 gateway 零消费）。"""
    # 同进程早前用例可能因 import 副作用（import 即注册）残留 server 模块
    # 注册；预清理后断言 install() 自身不新增这两域
    external_providers._providers.pop("mcp_state", None)
    external_providers._providers.pop("mcp_credential", None)

    install()

    assert isinstance(provider("mcp_registry"), McpRegistryRemoteProvider)
    after = external_providers.registered_domains()
    assert "mcp_state" not in after
    assert "mcp_credential" not in after



    install()

    assert isinstance(provider("mcp_registry"), McpRegistryRemoteProvider)
    after = external_providers.registered_domains()
    # gateway 进程对这两域零消费：远端模式的 bridge 不加载、不注册它们
    assert "mcp_state" not in after
    assert "mcp_credential" not in after


@pytest.mark.asyncio
async def test_server_oauth_callback_branch_is_async(monkeypatch) -> None:
    """oauth_callback 经异步孪生执行（to_thread），门控为连接级共享密钥。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.mcp import registry as mcp_registry

    called: list[dict] = {}

    async def _async_twin(query):
        called["query"] = query
        return True

    monkeypatch.setattr(mcp_registry, "complete_oauth_callback_async", _async_twin)
    monkeypatch.setenv("JIUWENSWARM_GATEWAY_INTERNAL_TOKEN", "secret-1")
    srv = object.__new__(AgentWebSocketServer)

    def _ws(token: str) -> SimpleNamespace:
        headers = {"X-Jiuwen-Gateway-Internal": token} if token else {}
        return SimpleNamespace(request_headers=headers)

    # 连接级令牌缺失/不匹配：拒绝——载荷 channel_id 可伪造，不作判据
    for bad_ws in (_ws(""), _ws("wrong")):
        denied = await srv._command_mcp_oauth_callback(
            bad_ws,
            SimpleNamespace(request_id="r1", channel_id="gateway", params={"query": {"code": "c"}}),
        )
        assert denied.ok is False and denied.payload["code"] == "PERMISSION"
        assert called == {}

    # 密钥未配置：fail-closed
    monkeypatch.delenv("JIUWENSWARM_GATEWAY_INTERNAL_TOKEN")
    unconfigured = await srv._command_mcp_oauth_callback(
        _ws("whatever"),
        SimpleNamespace(request_id="r3", channel_id="gateway", params={}),
    )
    assert unconfigured.ok is False and unconfigured.payload["code"] == "PERMISSION"

    # 密钥匹配：经异步孪生执行（与载荷 channel_id 无关）
    monkeypatch.setenv("JIUWENSWARM_GATEWAY_INTERNAL_TOKEN", "secret-1")
    ok_resp = await srv._command_mcp_oauth_callback(
        _ws("secret-1"),
        SimpleNamespace(request_id="r2", channel_id="anything", params={"query": {"code": "c"}}),
    )
    assert ok_resp.ok is True and ok_resp.payload == {"completed": True}
    assert called == {"query": {"code": "c"}}
