# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SSE / streamable-http support on the request-scoped MCP path.

Covers:
1. ``list_request_mcp_server_tools`` dispatches by transport — sse /
   streamable-http reuse ``SseClient`` / ``StreamableHttpClient`` and return
   params carrying ``_mcp_client_type``.
2. ``_run_mcp_worker`` dispatches by ``params["_mcp_client_type"]`` — the
   remote client's ``call_tool`` is used to drain the request queue, and the
   registered ``disconnect`` callback runs on worker exit.
3. stdio discovery keeps working and now tags params with ``_mcp_client_type``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_config import (
    _PooledMcpWorker,
    list_request_mcp_server_tools,
    _run_mcp_worker,
)


def _sse_config() -> dict:
    return {
        "name": "baidu-netdisk",
        "type": "sse",
        "url": "http://127.0.0.1:3001/sse",
        "auth_headers": {"Authorization": "Bearer test-token"},
    }


def _streamable_http_config() -> dict:
    return {
        "name": "remote-http",
        "type": "streamableHttp",
        "url": "http://127.0.0.1:3002/mcp",
        "auth_headers": {"Authorization": "Bearer http-token"},
    }


class _FakeTool:
    """Mimics the tool objects returned by openjiuwen's McpClient.list_tools."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.input_params = {"type": "object"}


class _FakeRemoteClient:
    """Stand-in for SseClient / StreamableHttpClient.

    Constructed as ``client_cls(rebuild_cfg)`` by both the discovery and the
    worker paths. Behavior is driven by class-level knobs (``tools``,
    ``connect_ok``, ``call_result``) so a single factory covers every test.
    """

    tools: list = []
    connect_ok: bool = True
    # SseClient/StreamableHttpClient.call_tool returns extract_mcp_tool_result_content
    # output — a bare string/value, NOT a CallToolResult. Tests must mimic this so
    # the _RemoteMcpCallAdapter path is exercised against the real return shape.
    call_result: object = "remote-tool-output"
    instances: list["_FakeRemoteClient"] = []

    def __init__(self, config) -> None:
        self.config = config
        self.connect = AsyncMock(return_value=_FakeRemoteClient.connect_ok)
        self.list_tools = AsyncMock(return_value=list(_FakeRemoteClient.tools))
        self.call_tool = AsyncMock(return_value=_FakeRemoteClient.call_result)
        self.disconnect = AsyncMock(return_value=True)
        _FakeRemoteClient.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.tools = []
        cls.connect_ok = True
        cls.call_result = "remote-tool-output"


def _patch_remote_client(monkeypatch: pytest.MonkeyPatch):
    """Point _remote_mcp_client_cls at _FakeRemoteClient for both transports."""

    monkeypatch.setattr(
        mcp_config, "_remote_mcp_client_cls", lambda client_type: _FakeRemoteClient
    )


@pytest.mark.asyncio
async def test_sse_discovery_dispatches_to_sse_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRemoteClient.reset()
    _FakeRemoteClient.tools = [_FakeTool("netdisk_list"), _FakeTool("netdisk_upload")]
    _patch_remote_client(monkeypatch)

    tool_defs, params = await list_request_mcp_server_tools(
        "baidu-netdisk", _sse_config()
    )

    assert [t["name"] for t in tool_defs] == ["netdisk_list", "netdisk_upload"]
    assert tool_defs[0]["input_params"] == {"type": "object"}
    # The dispatch marker _run_mcp_worker keys off of.
    assert params["_mcp_client_type"] == "sse"
    # Connection info must be carried for _run_mcp_worker to rebuild the client.
    assert params["server_path"] == "http://127.0.0.1:3001/sse"
    assert params["auth_headers"] == {"Authorization": "Bearer test-token"}
    assert params["server_name"] == "baidu-netdisk"

    assert len(_FakeRemoteClient.instances) == 1
    client = _FakeRemoteClient.instances[0]
    client.connect.assert_awaited_once()
    client.list_tools.assert_awaited_once()
    # Discovery closes the connection; the long-lived one is opened by the worker.
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_streamable_http_discovery_dispatches_to_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRemoteClient.reset()
    _FakeRemoteClient.tools = [_FakeTool("http_tool")]
    _patch_remote_client(monkeypatch)

    tool_defs, params = await list_request_mcp_server_tools(
        "remote-http", _streamable_http_config()
    )

    assert [t["name"] for t in tool_defs] == ["http_tool"]
    assert params["_mcp_client_type"] == "streamable-http"
    assert params["server_path"] == "http://127.0.0.1:3002/mcp"


@pytest.mark.asyncio
async def test_sse_discovery_connect_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRemoteClient.reset()
    _FakeRemoteClient.connect_ok = False
    _patch_remote_client(monkeypatch)

    tool_defs, params = await list_request_mcp_server_tools(
        "baidu-netdisk", _sse_config()
    )

    assert tool_defs == []
    assert params == {}
    client = _FakeRemoteClient.instances[0]
    client.disconnect.assert_not_awaited()  # connect returned False → no disconnect


@pytest.mark.asyncio
async def test_run_mcp_worker_dispatches_sse_and_drains_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE worker path must call the remote client's call_tool and honor
    the None sentinel (disconnect on exit). The bare-string return is wrapped by
    _RemoteMcpCallAdapter into a CallToolResult so the stdio invoke contract
    (result.content[-1].text) holds."""
    _FakeRemoteClient.reset()
    _FakeRemoteClient.tools = [_FakeTool("netdisk_list")]
    _FakeRemoteClient.call_result = "netdisk-list-payload"
    _patch_remote_client(monkeypatch)

    # Discovery only to obtain params shaped as the worker will receive them.
    _, params = await list_request_mcp_server_tools("baidu-netdisk", _sse_config())
    # The discovery client was the first instance; the worker builds its own.
    # Reset clears instances but also resets call_result — re-arm it so the
    # worker-built client returns the payload we assert on.
    _FakeRemoteClient.reset()
    _FakeRemoteClient.call_result = "netdisk-list-payload"

    worker = _PooledMcpWorker("baidu-netdisk")

    # Enqueue one call before the sentinel so the worker processes it.
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    req = SimpleNamespace(tool_name="netdisk_list", arguments={"q": "x"}, future=fut)
    worker.queue.put_nowait(req)
    worker.queue.put_nowait(None)  # sentinel → exit after draining

    await _run_mcp_worker(params, worker)

    assert fut.done()
    # The adapter wraps the bare string into a CallToolResult with .content.
    result = fut.result()
    assert getattr(result.content[-1], "text", None) == "netdisk-list-payload"
    assert len(_FakeRemoteClient.instances) == 1
    client = _FakeRemoteClient.instances[0]
    client.connect.assert_awaited_once()
    # Adapter forwards call_tool(name, arguments) positionally (matching the
    # SseClient/StreamableHttpClient signature where `arguments` is positional).
    client.call_tool.assert_awaited_once_with("netdisk_list", {"q": "x"})
    # Worker exit must disconnect the long-lived client.
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_call_result_adapts_to_stdio_invoke_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end-ish: a remote call_tool returning a bare string (the real
    SseClient/StreamableHttpClient shape) must surface to the invoke contract
    as result.content[-1].text — the bug that raised
    ''str' object has no attribute 'content' must stay fixed."""
    from jiuwenswarm.common.mcp_config import _RemoteMcpCallAdapter

    inner = MagicMock()
    inner.call_tool = AsyncMock(return_value="bare-string-from-remote")
    adapter = _RemoteMcpCallAdapter(inner)

    result = await adapter.call_tool("t", {"a": 1})

    # Mirror RequestScopedOfficeClawMcpTool.invoke:1803-1805 exactly.
    assert result.content, "content list must be non-empty so `if result.content` passes"
    assert getattr(result.content[-1], "text", None) == "bare-string-from-remote"


@pytest.mark.asyncio
async def test_remote_call_result_none_adapts_to_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A None (empty) remote result must become an empty content list, matching
    stdio's empty CallToolResult — invoke's `if result.content:` then yields None."""
    from jiuwenswarm.common.mcp_config import _RemoteMcpCallAdapter

    inner = MagicMock()
    inner.call_tool = AsyncMock(return_value=None)
    adapter = _RemoteMcpCallAdapter(inner)

    result = await adapter.call_tool("t", {})

    assert result.content == []
    assert not result.content  # invoke's `if result.content:` → False → None


@pytest.mark.asyncio
async def test_remote_call_result_dict_serialized_as_json() -> None:
    """A dict result (extract_mcp_tool_result_content's model_dump branch) must
    become JSON text, not Python repr — ``str(dict)`` would emit {'k': 'v'}
    which LLMs parse poorly. Regression for the str() fallback."""
    import json as _json

    from jiuwenswarm.common.mcp_config import _RemoteMcpCallAdapter

    payload = {"key": "值", "nested": {"n": 1}}
    inner = MagicMock()
    inner.call_tool = AsyncMock(return_value=payload)
    adapter = _RemoteMcpCallAdapter(inner)

    result = await adapter.call_tool("t", {})

    text = getattr(result.content[-1], "text", None)
    # Round-trips through JSON and preserves unicode (no \uXXXX escapes).
    assert _json.loads(text) == payload
    assert "值" in text


@pytest.mark.asyncio
async def test_remote_call_tool_honors_connector_timeout_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """params["timeout_s"]（前端下发的单连接器超时）必须覆盖 30s 默认值：
    未超 30s 但超连接器 timeout_s 的调用要被打断；反之连接器 timeout_s
    大于默认值时调用要在 30s 后仍存活。"""
    _FakeRemoteClient.reset()
    _FakeRemoteClient.tools = [_FakeTool("netdisk_list")]
    _patch_remote_client(monkeypatch)

    # 连接器超时 0.3s（远小于 30s 默认）：挂在 10s 的调用应在 ~0.3s 失败。
    _, params = await list_request_mcp_server_tools(
        "baidu-netdisk", {**_sse_config(), "timeout_s": 0.3}
    )
    assert params["timeout_s"] == 0.3

    async def _hang(*a, **kw):
        await asyncio.sleep(10)

    inner = MagicMock()
    inner.call_tool = AsyncMock(side_effect=_hang)
    inner.connect = AsyncMock(return_value=True)
    inner.disconnect = AsyncMock(return_value=True)
    inner.list_tools = AsyncMock(return_value=[])
    monkeypatch.setattr(
        mcp_config, "_remote_mcp_client_cls", lambda client_type: lambda cfg: inner
    )

    worker = _PooledMcpWorker("baidu-netdisk")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    worker.queue.put_nowait(
        SimpleNamespace(tool_name="netdisk_list", arguments={}, future=fut)
    )
    worker.queue.put_nowait(None)

    await asyncio.wait_for(_run_mcp_worker(params, worker), timeout=10)

    # 若仍按 30s 默认，wait_for(10) 会先超时；走到这里即证明用了 0.3s 连接器超时。
    assert fut.done() and isinstance(fut.exception(), TimeoutError)


@pytest.mark.asyncio
async def test_remote_call_tool_invalid_timeout_s_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 timeout_s（0/负数/非数值）必须回落到 _MCP_CALL_TOOL_TIMEOUT_S。"""
    for bad in (0, -5, "120", True):
        _FakeRemoteClient.reset()
        _FakeRemoteClient.tools = [_FakeTool("netdisk_list")]
        _patch_remote_client(monkeypatch)

        _, params = await list_request_mcp_server_tools(
            "baidu-netdisk", {**_sse_config(), "timeout_s": bad}
        )
        # 非法值被 create_mcp_tool 丢弃，params 不带 timeout_s。
        assert "timeout_s" not in params


@pytest.mark.asyncio
async def test_remote_call_tool_no_timeout_s_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未下发 timeout_s 时沿用 _MCP_CALL_TOOL_TIMEOUT_S 默认 30s。"""
    _FakeRemoteClient.reset()
    _FakeRemoteClient.tools = [_FakeTool("netdisk_list")]
    _patch_remote_client(monkeypatch)

    _, params = await list_request_mcp_server_tools("baidu-netdisk", _sse_config())
    assert "timeout_s" not in params


@pytest.mark.asyncio
async def test_remote_call_tool_timeout_breaks_worker_and_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out remote call_tool must break the worker (not continue) and
    fail already-queued callers, so the stale SseClient owner-task command does
    not pile up behind the timed-out one. The next acquire rebuilds."""
    _FakeRemoteClient.reset()
    _FakeRemoteClient.tools = [_FakeTool("netdisk_list")]
    _patch_remote_client(monkeypatch)

    _, params = await list_request_mcp_server_tools("baidu-netdisk", _sse_config())
    _FakeRemoteClient.reset()

    # Shrink the call timeout so the test doesn't wait 30s.
    monkeypatch.setattr(mcp_config, "_MCP_CALL_TOOL_TIMEOUT_S", 0.2)

    # Make the worker-built client's call_tool hang past the worker timeout.
    async def _hang(*a, **kw):
        await asyncio.sleep(60)  # far beyond the shrunken timeout

    inner = MagicMock()
    inner.call_tool = AsyncMock(side_effect=_hang)
    inner.connect = AsyncMock(return_value=True)
    inner.disconnect = AsyncMock(return_value=True)
    inner.list_tools = AsyncMock(return_value=[])
    # Bypass _FakeRemoteClient so the worker builds a stub that hangs.
    monkeypatch.setattr(
        mcp_config, "_remote_mcp_client_cls", lambda client_type: lambda cfg: inner
    )

    worker = _PooledMcpWorker("baidu-netdisk")
    fut1: asyncio.Future = asyncio.get_running_loop().create_future()
    fut2: asyncio.Future = asyncio.get_running_loop().create_future()
    worker.queue.put_nowait(
        SimpleNamespace(tool_name="netdisk_list", arguments={}, future=fut1)
    )
    worker.queue.put_nowait(
        SimpleNamespace(tool_name="netdisk_list", arguments={}, future=fut2)
    )
    worker.queue.put_nowait(None)  # would exit only after draining

    await _run_mcp_worker(params, worker)

    # First caller failed with TimeoutError; second drained (not stranded).
    assert fut1.done() and isinstance(fut1.exception(), TimeoutError)
    assert fut2.done() and isinstance(fut2.exception(), TimeoutError)
    # Worker broke out of its loop (not continue): _drain_queue_with_error ate
    # the sentinel too, so the queue is empty, and the AsyncExitStack teardown
    # ran disconnect — the stale SseClient owner-task command is torn down so
    # the next acquire rebuilds a fresh worker instead of piling up behind it.
    assert worker.queue.empty()
    inner.disconnect.assert_awaited_once()




@pytest.mark.asyncio
async def test_run_mcp_worker_init_failure_drains_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the remote client fails to connect, already-queued callers must be
    failed (not stranded), so the next invoke rebuilds the worker."""
    _FakeRemoteClient.reset()
    _FakeRemoteClient.connect_ok = False
    _patch_remote_client(monkeypatch)

    params = {
        "_mcp_client_type": "sse",
        "server_name": "baidu-netdisk",
        "server_id": "baidu-netdisk",
        "server_path": "http://127.0.0.1:3001/sse",
        "auth_headers": {},
        "auth_query_params": {},
        "params": {},
    }
    worker = _PooledMcpWorker("baidu-netdisk")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    req = SimpleNamespace(tool_name="netdisk_list", arguments={}, future=fut)
    worker.queue.put_nowait(req)
    worker.queue.put_nowait(None)

    await _run_mcp_worker(params, worker)

    assert fut.done()
    assert isinstance(fut.exception(), Exception)


@pytest.mark.asyncio
async def test_stdio_discovery_still_marks_client_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stdio discovery path must keep working and now tag params with
    _mcp_client_type='stdio' so the worker dispatch is explicit."""
    # Loopback must be allowed for the 127.0.0.1-style commands we simulate.
    monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(
        return_value=SimpleNamespace(tools=[_FakeTool("stdio_tool")])
    )

    # mcp.client.stdio.stdio_client is an async context manager; patch it to
    # return dummy read/write streams so no real process is spawned.
    class _StdioCtx:
        async def __aenter__(self):
            return MagicMock(), MagicMock()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("mcp.client.stdio.stdio_client", lambda _p: _StdioCtx())
    # stdio_server_parameters is evaluated before stdio_client; stub it so no
    # StdioServerParameters (which needs cwd/env) is constructed.
    monkeypatch.setattr(mcp_config, "_stdio_server_parameters", lambda params: None)

    # ClientSession is imported lazily inside the function; patch at the source.
    import mcp as _mcp_mod

    class _SessionCtx:
        def __init__(self, session) -> None:
            self._session = session

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_mcp_mod, "ClientSession", lambda *a, **k: _SessionCtx(fake_session))

    config = {
        "name": "local-stdio",
        "type": "stdio",
        "command": "node",
        "args": ["server.js"],
    }
    tool_defs, params = await list_request_mcp_server_tools("local-stdio", config)

    assert [t["name"] for t in tool_defs] == ["stdio_tool"]
    assert params["_mcp_client_type"] == "stdio"
