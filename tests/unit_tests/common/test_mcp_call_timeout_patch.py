# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit coverage for the remote-MCP per-call timeout patch.

End-to-end behaviour (long SSE / streamable-http calls under a stamped
``_jws_call_timeout``, SDK ``sse_read_timeout`` lifting, and the SSE teardown
guard) is verified against real delay servers in the vendored harness; these
tests pin the propagation contract that is observable without a live server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.common.mcp_call_timeout_patch import (
    DEFAULT_CALL_TIMEOUT,
    apply_mcp_call_timeout_patch,
)
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return None


def _make_sse_client() -> SseClient:
    config = McpServerConfig(
        server_id="timeout-test",
        server_name="timeout-test",
        server_path="http://127.0.0.1:9/sse",
        client_type="sse",
    )
    client = SseClient(config)
    client._session = _FakeSession()  # noqa: SLF001 - test seam
    return client


class TestStampedTimeoutPropagation:
    @pytest.mark.asyncio
    async def test_stamped_timeout_reaches_inner_ceiling_with_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apply_mcp_call_timeout_patch()
        client = _make_sse_client()
        setattr(client, "_jws_call_timeout", 90.0)

        captured: dict[str, float] = {}
        real_wait_for = asyncio.wait_for

        async def spy_wait_for(coro, timeout=None):  # type: ignore[no-untyped-def]
            captured["timeout"] = float(timeout)  # type: ignore[arg-type]
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

        result = await client.call_tool("delay_remote", {"seconds": 0})

        assert result is None
        assert client._session.calls == [("delay_remote", {"seconds": 0})]  # type: ignore[attr-defined]
        # The stamped 90s must propagate past SseClient's 60s default ceiling,
        # with the +5s backstop margin so the pooled worker's outer deadline
        # (exactly timeout_s) always fires first.
        assert captured["timeout"] == pytest.approx(95.0)

    @pytest.mark.asyncio
    async def test_unstamped_client_falls_back_to_default_plus_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apply_mcp_call_timeout_patch()
        client = _make_sse_client()
        assert not hasattr(client, "_jws_call_timeout")

        captured: dict[str, float] = {}
        real_wait_for = asyncio.wait_for

        async def spy_wait_for(coro, timeout=None):  # type: ignore[no-untyped-def]
            captured["timeout"] = float(timeout)  # type: ignore[arg-type]
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

        await client.call_tool("delay_remote", {"seconds": 0})

        assert captured["timeout"] == pytest.approx(DEFAULT_CALL_TIMEOUT + 5.0)
