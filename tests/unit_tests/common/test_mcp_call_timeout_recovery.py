# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TC_MCP_CALL_014：工具超时后同会话后续调用须能恢复。"""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace

import anyio
import pytest

import jiuwenswarm.common.mcp_call_timeout_patch as timeout_patch
from jiuwenswarm.common.mcp_call_timeout_patch import force_invalidate_mcp_client


def test_force_invalidate_clears_session_and_replaces_exit_stack() -> None:
    old_stack = AsyncExitStack()
    client = SimpleNamespace(
        _session=object(),
        _client=object(),
        _read=object(),
        _write=object(),
        _is_disconnected=False,
        _exit_stack=old_stack,
        _auth_provider=object(),
    )
    force_invalidate_mcp_client(client)
    assert client._session is None
    assert client._client is None
    assert client._read is None
    assert client._write is None
    assert client._is_disconnected is True
    assert client._auth_provider is None
    assert client._exit_stack is not old_stack
    assert isinstance(client._exit_stack, AsyncExitStack)


@pytest.mark.asyncio
async def test_timeout_then_same_session_call_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """对齐现场：disconnect 因 cancel scope 失败且未清 _session 时，下一轮须重连成功。"""
    from openjiuwen.core.foundation.tool.mcp.client import streamable_http_client as shc_mod
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    call_count = {"n": 0}
    connect_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 模拟慢工具；由 fail_after 取消
            await anyio.sleep(5)
            return {"unreachable": True}
        return {"tool": tool_name, "arguments": arguments, "ok": True}

    async def fake_list_tools(self, *, timeout=-1):
        del timeout
        if not self._session:
            raise RuntimeError("Not connected to streamable-http server")
        return []

    async def fake_disconnect(self, *, timeout=-1):
        del timeout
        # 模拟 SDK：aclose 抛 cancel-scope，且不清理 _session（现场根因）
        raise RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )

    async def fake_connect(self, *, timeout=-1):
        del timeout
        connect_count["n"] += 1
        self._session = object()
        self._is_disconnected = False
        return True

    # 撤掉可能已套上的旧 wrap，装入可控假实现后再重新打补丁
    monkeypatch.setattr(StreamableHttpClient, "call_tool", fake_call_tool, raising=False)
    monkeypatch.setattr(StreamableHttpClient, "list_tools", fake_list_tools, raising=False)
    monkeypatch.setattr(StreamableHttpClient, "disconnect", fake_disconnect, raising=False)
    monkeypatch.setattr(StreamableHttpClient, "connect", fake_connect, raising=False)

    timeout_patch._PATCHED = False
    timeout_patch._wrapped_methods.clear()
    timeout_patch.apply_mcp_call_timeout_patch(default_timeout=0.05)

    client = object.__new__(StreamableHttpClient)
    client._server_path = "http://192.168.1.96:18014/mcp"
    client._session = object()
    client._client = object()
    client._read = None
    client._write = None
    client._is_disconnected = False
    client._exit_stack = AsyncExitStack()
    client._auth_provider = None
    client._jws_call_timeout = 0.05
    client._name = "qa-noauth-streamable-http"

    with pytest.raises(TimeoutError, match="timed out after"):
        await client.call_tool("qa_delay", {"seconds": 5})

    # disconnect 失败也必须清掉半死会话
    assert client._session is None
    assert client._is_disconnected is True

    # 同会话下一轮普通工具：应自动重连并成功（不再 execute invoke failed）
    result = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-59382e14cf32-after-timeout"},
    )
    assert result["ok"] is True
    assert result["tool"] == "qa_echo"
    assert connect_count["n"] >= 1
    assert call_count["n"] == 2
