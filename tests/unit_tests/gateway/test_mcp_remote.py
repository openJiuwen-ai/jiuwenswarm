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
    remote_enabled,
)
from gateway.server_store_bridge import _LocalMcpRegistryProvider, install
from jiuwenswarm.common import external_providers
from jiuwenswarm.common.external_providers import provider


class _FakeStore:
    """Sync mcp_registry double."""

    def __init__(self, completed: bool = True) -> None:
        self.completed = completed
        self.calls: list[dict[str, Any]] = []

    def complete_oauth_callback(self, query: dict[str, Any]) -> bool:
        self.calls.append(query)
        return self.completed


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
    assert fake_unary.captured["timeout_seconds"] <= 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        (False, {"error": "internal", "code": "MCP_INTERNAL"}),
        (True, {"completed": False}),
    ],
)
async def test_remote_callback_degrades_to_failure(fake_unary, result) -> None:
    fake_unary.result = result

    assert (
        await McpRegistryRemoteProvider().complete_oauth_callback_async({"code": "x"})
        is False
    )


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("", False), ("0", False), (None, False)],
)
def test_remote_enabled_env_parsing(monkeypatch: pytest.MonkeyPatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("JIUWENSWARM_MCP_REMOTE", raising=False)
    else:
        monkeypatch.setenv("JIUWENSWARM_MCP_REMOTE", value)
    assert remote_enabled() is expected


def test_local_adapter_sync_passthrough() -> None:
    store = _FakeStore(completed=True)

    assert _LocalMcpRegistryProvider(store).complete_oauth_callback({"code": "a"}) is True
    assert store.calls == [{"code": "a"}]


@pytest.mark.asyncio
async def test_local_adapter_async_entry() -> None:
    store = _FakeStore(completed=False)

    result = await _LocalMcpRegistryProvider(store).complete_oauth_callback_async(
        {"code": "b"}
    )

    assert result is False
    assert store.calls == [{"code": "b"}]


def test_local_adapter_delegates_the_full_module_interface() -> None:
    """适配器只增补异步入口，不得收窄域接口（__getattr__ 全量委托）。"""
    store = _FakeStore()
    store.resolve_package = lambda name: {"name": name}
    adapter = _LocalMcpRegistryProvider(store)

    assert adapter.resolve_package("acme") == {"name": "acme"}


@pytest.fixture(name="reset_bridge")
def _reset_bridge(monkeypatch: pytest.MonkeyPatch):
    """Isolate bridge install state and the provider registry around install()."""
    monkeypatch.setattr("gateway.server_store_bridge._INSTALLED", False)
    saved = dict(external_providers._providers)
    yield
    external_providers._providers.clear()
    external_providers._providers.update(saved)


def test_install_registers_local_domains_by_default(
    reset_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIUWENSWARM_MCP_REMOTE", raising=False)
    monkeypatch.delenv("JIUWENSWARM_SESSION_METADATA_REMOTE", raising=False)

    install()

    assert isinstance(provider("mcp_registry"), _LocalMcpRegistryProvider)
    # mcp_state / mcp_credential 在本地模式保持模块自注册（既有行为）
    assert provider("mcp_state") is not None
    assert provider("mcp_credential") is not None


def test_install_registers_remote_and_skips_server_modules_when_env_set(
    reset_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIUWENSWARM_MCP_REMOTE", "1")
    monkeypatch.delenv("JIUWENSWARM_SESSION_METADATA_REMOTE", raising=False)
    # 同进程早前用例可能因 import 副作用（import 即注册）残留过 server 模块
    # 注册；预清理后断言契约——远端模式的 install() 自身不新增这两域。
    external_providers._providers.pop("mcp_state", None)
    external_providers._providers.pop("mcp_credential", None)

    install()

    assert isinstance(provider("mcp_registry"), McpRegistryRemoteProvider)
    after = external_providers.registered_domains()
    # gateway 进程对这两域零消费：远端模式的 bridge 不加载、不注册它们
    assert "mcp_state" not in after
    assert "mcp_credential" not in after


@pytest.mark.asyncio
async def test_server_oauth_callback_branch_is_async(monkeypatch) -> None:
    """oauth_callback 经异步孪生执行（to_thread），不阻塞 AgentServer 事件循环。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.mcp import registry as mcp_registry

    called: list[dict] = {}

    async def _async_twin(query):
        called["query"] = query
        return True

    monkeypatch.setattr(mcp_registry, "complete_oauth_callback_async", _async_twin)
    srv = object.__new__(AgentWebSocketServer)

    # 非 gateway 通道拒绝（与同步 action 族同门控）
    denied = await srv._command_mcp_oauth_callback(
        SimpleNamespace(request_id="r1", channel_id="web", params={"query": {"code": "c"}})
    )
    assert denied.ok is False and denied.payload["code"] == "PERMISSION"
    assert called == {}

    ok_resp = await srv._command_mcp_oauth_callback(
        SimpleNamespace(request_id="r2", channel_id="gateway", params={"query": {"code": "c"}})
    )
    assert ok_resp.ok is True and ok_resp.payload == {"completed": True}
    assert called == {"query": {"code": "c"}}
