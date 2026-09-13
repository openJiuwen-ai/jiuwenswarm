# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP 超时 / Session terminated 后须能恢复（TC_MCP_CALL_014 与服务重启场景）。"""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace

import anyio
import pytest

import jiuwenswarm.common.mcp_call_timeout_patch as timeout_patch
from jiuwenswarm.common.mcp_call_timeout_patch import (
    force_invalidate_mcp_client,
    is_retryable_mcp_dead_session_error,
)


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


@pytest.mark.parametrize(
    "exc, expect",
    [
        (RuntimeError("Session terminated"), True),
        (Exception("server error 404"), True),
        (ConnectionError("Connection closed"), True),
        (ValueError("invalid tool args"), False),
    ],
)
def test_is_retryable_mcp_dead_session_error(exc: BaseException, expect: bool) -> None:
    assert is_retryable_mcp_dead_session_error(exc) is expect


def _install_fake_streamable_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_call_tool,
    fake_disconnect=None,
    fake_connect=None,
    default_timeout: float = 5.0,
):
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    async def _default_list_tools(self, *, timeout=-1):
        del timeout
        if not self._session:
            raise RuntimeError("Not connected to streamable-http server")
        return []

    async def _default_disconnect(self, *, timeout=-1):
        del timeout
        raise RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )

    connect_count = {"n": 0}

    async def _default_connect(self, *, timeout=-1):
        del timeout
        connect_count["n"] += 1
        self._session = object()
        self._is_disconnected = False
        return True

    monkeypatch.setattr(StreamableHttpClient, "call_tool", fake_call_tool, raising=False)
    monkeypatch.setattr(StreamableHttpClient, "list_tools", _default_list_tools, raising=False)
    monkeypatch.setattr(
        StreamableHttpClient,
        "disconnect",
        fake_disconnect or _default_disconnect,
        raising=False,
    )
    monkeypatch.setattr(
        StreamableHttpClient,
        "connect",
        fake_connect or _default_connect,
        raising=False,
    )

    timeout_patch._PATCHED = False
    timeout_patch._wrapped_methods.clear()
    timeout_patch.apply_mcp_call_timeout_patch(default_timeout=default_timeout)

    client = object.__new__(StreamableHttpClient)
    client._server_path = "http://192.168.1.96:18014/mcp"
    client._session = object()
    client._client = object()
    client._read = None
    client._write = None
    client._is_disconnected = False
    client._exit_stack = AsyncExitStack()
    client._auth_provider = None
    client._jws_call_timeout = default_timeout
    client._name = "qa-noauth-streamable-http"
    return client, connect_count, StreamableHttpClient


@pytest.mark.asyncio
async def test_timeout_then_same_session_call_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """对齐现场：disconnect 因 cancel scope 失败且未清 _session 时，下一轮须重连成功。"""
    call_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            await anyio.sleep(5)
            return {"unreachable": True}
        return {"tool": tool_name, "arguments": arguments, "ok": True}

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        default_timeout=0.05,
    )

    with pytest.raises(TimeoutError, match="timed out after"):
        await client.call_tool("qa_delay", {"seconds": 5})

    assert client._session is None
    assert client._is_disconnected is True

    result = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-59382e14cf32-after-timeout"},
    )
    assert result["ok"] is True
    assert result["tool"] == "qa_echo"
    assert connect_count["n"] >= 1
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_session_terminated_reconnects_and_retries_same_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP 服务重启后：进程级客户端遇 Session terminated 应重连并重试成功。

    新旧聊天会话共用 ToolMgr 里同一 MCP client，故一次恢复即可服务后续会话。
    """
    call_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Session terminated")
        return {
            "tool": tool_name,
            "text": arguments.get("text"),
            "marker": "SDS-DEV-MCP-OK",
            "ok": True,
        }

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        default_timeout=5.0,
    )

    # 会话 A：服务重启后第一次调用
    result_a = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-c839d94b0b7a-same-after-restart"},
    )
    assert result_a["ok"] is True
    assert result_a["text"] == "MCP-CALL-sds-c839d94b0b7a-same-after-restart"
    assert connect_count["n"] >= 1
    assert call_count["n"] == 2  # 失败一次 + 重试成功

    # 会话 B：同一 AS 进程级客户端，不应再 Session terminated
    result_b = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-c839d94b0b7a-fresh-after-restart"},
    )
    assert result_b["ok"] is True
    assert result_b["text"] == "MCP-CALL-sds-c839d94b0b7a-fresh-after-restart"
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_timeout_teardown_cancelled_error_still_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE 超时后 disconnect 抛 CancelledError 时，须报 TimeoutError 而非用户取消。

    现场：有 chat.tool_call 无 chat.tool_result，最终「工具调用被用户取消」。
    根因是 CancelledError 逃出 MCP 补丁，MCPTool.invoke 捕不到（非 Exception）。
    """
    import asyncio

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del self, tool_name, arguments, timeout
        await anyio.sleep(5)
        return {"unreachable": True}

    async def fake_disconnect(self, *, timeout=-1):
        del self, timeout
        raise asyncio.CancelledError()

    client, _, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        fake_disconnect=fake_disconnect,
        default_timeout=0.05,
    )

    with pytest.raises(TimeoutError, match="timed out after") as ei:
        await client.call_tool("qa_delay", {"seconds": 5})

    assert not isinstance(ei.value, asyncio.CancelledError)
    assert "user cancel" not in str(ei.value).lower()
    assert client._session is None
    assert client._is_disconnected is True


@pytest.mark.asyncio
async def test_spurious_cancelled_error_reconnects_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接中断冒出的 CancelledError（非外层取消）应重连重试，而非标成用户取消。"""
    import asyncio

    call_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise asyncio.CancelledError()
        return {"tool": tool_name, "arguments": arguments, "ok": True}

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        default_timeout=5.0,
    )

    result = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-90b35bbefb56-after-restart"},
    )
    assert result["ok"] is True
    assert call_count["n"] == 2
    assert connect_count["n"] >= 1


@pytest.mark.asyncio
async def test_sse_timeout_reports_timeout_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SseClient 超时同样须抛 TimeoutError（可被 MCPTool.invoke 捕获成工具错误）。"""
    import asyncio

    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del self, tool_name, arguments, timeout
        await anyio.sleep(5)
        return {"unreachable": True}

    async def fake_disconnect(self, *, timeout=-1):
        del self, timeout
        raise asyncio.CancelledError()

    async def fake_connect(self, *, timeout=-1):
        del timeout
        self._session = object()
        self._is_disconnected = False
        return True

    async def fake_list_tools(self, *, timeout=-1):
        del self, timeout
        return []

    monkeypatch.setattr(SseClient, "call_tool", fake_call_tool, raising=False)
    monkeypatch.setattr(SseClient, "list_tools", fake_list_tools, raising=False)
    monkeypatch.setattr(SseClient, "disconnect", fake_disconnect, raising=False)
    monkeypatch.setattr(SseClient, "connect", fake_connect, raising=False)

    timeout_patch._PATCHED = False
    timeout_patch._wrapped_methods.clear()
    timeout_patch.apply_mcp_call_timeout_patch(default_timeout=0.05)

    client = object.__new__(SseClient)
    client._server_path = "http://192.168.1.96:18013/sse"
    client._session = object()
    client._client = object()
    client._read = None
    client._write = None
    client._is_disconnected = False
    client._exit_stack = AsyncExitStack()
    client._auth_provider = None
    client._jws_call_timeout = 0.05
    client._name = "qa-noauth-sse"

    with pytest.raises(TimeoutError, match="timed out after"):
        await client.call_tool("qa_delay", {"seconds": 5})

    assert client._session is None
