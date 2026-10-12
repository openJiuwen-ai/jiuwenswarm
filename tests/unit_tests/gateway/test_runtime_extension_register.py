# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""runtime 扩展只在环境变量写成 runtime 时注册。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_EXT_PARENT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "jiuwenclaw-ee"
    / "gateway"
    / "extensions"
)
if str(_EXT_PARENT) not in sys.path:
    sys.path.insert(0, str(_EXT_PARENT))

from runtime_management_extension.extension import (
    register_extensions,
    runtime_client_requested,
)


class _Registry:
    def __init__(self) -> None:
        self.client = None

    def register_agent_server_client(self, extension) -> None:
        self.client = extension


def test_runtime_client_requested_only_for_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_AGENT_CLIENT_MODE", raising=False)
    assert runtime_client_requested() is False
    assert runtime_client_requested({}) is False
    assert runtime_client_requested({"GATEWAY_AGENT_CLIENT_MODE": "websocket"}) is False
    assert runtime_client_requested({"GATEWAY_AGENT_CLIENT_MODE": "http"}) is False
    assert runtime_client_requested({"GATEWAY_AGENT_CLIENT_MODE": " runtime "}) is True


@pytest.mark.asyncio
async def test_register_skips_unless_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_AGENT_CLIENT_MODE", "websocket")
    registry = _Registry()
    assert await register_extensions(registry) == []
    assert registry.client is None


@pytest.mark.asyncio
async def test_register_uses_runtime_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_AGENT_CLIENT_MODE", "runtime")
    registry = _Registry()
    registered = await register_extensions(registry)
    assert len(registered) == 1
    assert registry.client is registered[0]
    assert type(registry.client.get_client()).__name__ == "RuntimeRoutedAgentClient"
