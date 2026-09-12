# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for gateway -> AgentServer connect retry logic.

Regression: the TCP pre-probe in `_connect_with_retry` used to hard-code
127.0.0.1, so a remote AgentServer (deployed on another machine) could never
pass the probe and the gateway always raised ConnectionRefusedError.
"""

from __future__ import annotations

import socket
from contextlib import nullcontext

import pytest

from jiuwenswarm.gateway.app_gateway import _connect_with_retry


class _FakeClient:
    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect(self, uri: str) -> None:
        self.connected.append(uri)


async def test_tcp_probe_uses_remote_host(monkeypatch) -> None:
    """Probe must connect to the URI host, not 127.0.0.1 (remote deploy)."""
    captured: list[tuple] = []

    def fake_create_connection(addr, timeout=None):  # noqa: ANN001
        captured.append(addr)
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    client = _FakeClient()
    with pytest.raises(ConnectionRefusedError):
        await _connect_with_retry(
            client, "ws://remote-host:18092/ws", max_retries=1, interval=0.01
        )
    assert captured, "probe should have been attempted"
    assert captured[0][0] == "remote-host"
    assert captured[0][1] == 18092


async def test_localhost_probe_unchanged(monkeypatch) -> None:
    """Local deployments still probe 127.0.0.1 (hostname == localhost)."""
    captured: list[tuple] = []

    def fake_create_connection(addr, timeout=None):  # noqa: ANN001
        captured.append(addr)
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    client = _FakeClient()
    with pytest.raises(ConnectionRefusedError):
        await _connect_with_retry(
            client, "ws://127.0.0.1:18092/ws", max_retries=1, interval=0.01
        )
    assert captured[0][0] == "127.0.0.1"


async def test_probe_success_then_connect(monkeypatch) -> None:
    """When the probe passes, the websocket client connects to the URI."""
    monkeypatch.setattr(
        socket, "create_connection", lambda addr, timeout=None: nullcontext()
    )
    client = _FakeClient()
    await _connect_with_retry(
        client, "ws://remote-host:18092/ws", max_retries=1, interval=0.01
    )
    assert client.connected == ["ws://remote-host:18092/ws"]
