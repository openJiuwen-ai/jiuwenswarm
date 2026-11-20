# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the mcp_registry WS remote provider and the bridge adapter."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from agent_gateway.routing import mcp_remote
from agent_gateway.routing.mcp_remote import (
    McpRegistryRemoteProvider,
    bind_agent_client,
    install,
)
from agent_gateway.common import external_providers
from agent_gateway.common.external_providers import provider


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


async def _async_false(query):
    return False


@pytest.fixture(name="reset_registry")
def _reset_registry():
    """Isolate the provider registry around install()."""
    saved = dict(external_providers._providers)
    yield
    external_providers._providers.clear()
    external_providers._providers.update(saved)


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
