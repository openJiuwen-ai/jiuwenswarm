# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 按环境变量选择南向客户端。不连接真正的 runtime 服务。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.agent_client_mode import (
    AgentClientModeError,
    read_agent_client_mode,
    resolve_agent_server_client,
)


class RuntimeRoutedAgentClient:
    """只借用这个类名。测试不加载扩展包。"""


class _OtherClient:
    pass


class _Extension:
    def __init__(self, client) -> None:
        self._client = client
        self.metadata = SimpleNamespace(name="probe")

    def get_client(self):
        return self._client


def test_read_agent_client_mode_strips_and_lowers() -> None:
    assert read_agent_client_mode({}) == ""
    assert read_agent_client_mode({"GATEWAY_AGENT_CLIENT_MODE": " WebSocket "}) == "websocket"


def test_unset_or_websocket_keeps_the_current_choice() -> None:
    created: list[str] = []

    def _factory():
        created.append("ws")
        return "websocket-client"

    assert resolve_agent_server_client(None, mode="", websocket_factory=_factory) == "websocket-client"
    assert resolve_agent_server_client(None, mode="foo", websocket_factory=_factory) == "websocket-client"
    other = _OtherClient()
    assert (
        resolve_agent_server_client(
            _Extension(other),
            mode="websocket",
            websocket_factory=_factory,
        )
        is other
    )
    assert created == ["ws", "ws"]


def test_runtime_requires_the_runtime_client() -> None:
    client = RuntimeRoutedAgentClient()
    assert (
        resolve_agent_server_client(
            _Extension(client),
            mode="runtime",
            websocket_factory=lambda: pytest.fail("runtime 不应退回 WebSocket"),
        )
        is client
    )


def test_runtime_without_the_extension_fails() -> None:
    with pytest.raises(AgentClientModeError, match="runtime 扩展包"):
        resolve_agent_server_client(
            None,
            mode="runtime",
            websocket_factory=lambda: pytest.fail("缺扩展时不应退回 WebSocket"),
        )


def test_runtime_rejects_a_different_extension_client() -> None:
    with pytest.raises(AgentClientModeError, match="runtime 扩展包"):
        resolve_agent_server_client(
            _Extension(_OtherClient()),
            mode="runtime",
            websocket_factory=lambda: "unused",
        )


def test_http_mode_fails() -> None:
    with pytest.raises(AgentClientModeError, match="没有单独的 HTTP 客户端"):
        resolve_agent_server_client(
            None,
            mode="http",
            websocket_factory=lambda: pytest.fail("http 不应创建 WebSocket 客户端"),
        )
