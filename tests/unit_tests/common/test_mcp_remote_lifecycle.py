# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""离线验证真实 SDK 客户端 + AnyIO TaskGroup 的生命周期任务归属。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
import pytest_asyncio
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
    StreamableHttpClient,
)

from jiuwenswarm.common import mcp_call_timeout_patch as patch
from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_server_registry import (
    McpRegistrySettings,
    McpServerRegistry,
)


@pytest_asyncio.fixture
async def transport(monkeypatch):
    import mcp
    from mcp.client import sse, streamable_http
    from openjiuwen.core.runner import Runner

    state = SimpleNamespace(
        phase="normal",
        hold_close=False,
        closing=asyncio.Event(),
        release_close=asyncio.Event(),
        close_cancellations=0,
        entered=asyncio.Event(),
        calls=asyncio.Event(),
        owners=[],
        exits=[],
        children=[],
        sessions=[],
        connects=0,
        callbacks=0,
        release=asyncio.Event(),
        initial_tasks=set(asyncio.all_tasks()),
    )
    loop = asyncio.get_running_loop()
    original_soon = loop.call_soon

    def call_soon(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_deliver_cancellation":
            state.callbacks += 1
        return original_soon(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon", call_soon)

    async def receive(stream):
        state.children.append(asyncio.current_task())
        async with stream:
            async for _ in stream:
                pass

    @asynccontextmanager
    async def memory_transport(*args, **kwargs):
        state.owners.append(asyncio.current_task())
        send, recv = anyio.create_memory_object_stream(1)
        async with anyio.create_task_group() as group:
            group.start_soon(receive, recv)
            try:
                yield recv, send, None
            finally:
                state.exits.append(asyncio.current_task())
                group.cancel_scope.cancel()
                await send.aclose()

    @asynccontextmanager
    async def sse_transport(*args, **kwargs):
        async with memory_transport() as (read, write, _):
            yield read, write

    class Session:
        def __init__(self, *args, **kwargs):
            self.group = anyio.create_task_group()
            self.owner = None
            self.exited = False
            state.sessions.append(self)

        async def __aenter__(self):
            self.owner = asyncio.current_task()
            await self.group.__aenter__()
            return self

        async def __aexit__(self, *args):
            assert not self.exited, "session closed twice"
            assert asyncio.current_task() is self.owner
            try:
                if state.hold_close:
                    state.closing.set()
                    await state.release_close.wait()
            except asyncio.CancelledError:
                state.close_cancellations += 1
                raise
            finally:
                self.exited = True
                suppressed = await self.group.__aexit__(*args)
            return suppressed

        async def initialize(self):
            state.connects += 1
            state.entered.set()
            if state.phase == "connect-error":
                raise RuntimeError("initialize failed")
            if state.phase == "connect-hang":
                await asyncio.Event().wait()

        async def list_tools(self):
            state.calls.set()
            if state.phase == "list-hang":
                await asyncio.Event().wait()
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="echo", description="test", inputSchema={"type": "object"}
                    )
                ]
            )

        async def call_tool(self, name, arguments):
            from mcp.types import CallToolResult, TextContent

            state.calls.set()
            if state.phase == "call-hang":
                await state.release.wait()
            if state.phase == "dead":
                state.phase = "normal"
                raise RuntimeError("Session terminated")
            return CallToolResult(content=[TextContent(type="text", text="ok")])

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", memory_transport)
    monkeypatch.setattr(sse, "sse_client", sse_transport)
    monkeypatch.setattr(
        Runner.callback_framework, "trigger", AsyncMock(return_value=[])
    )
    monkeypatch.setenv("JIUWENSWARM_ALLOW_LOOPBACK_MCP", "1")
    monkeypatch.setattr(mcp_config, "_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mcp_config, "_MCP_CALL_TOOL_TIMEOUT_S", 0.05)
    return state


@pytest.fixture
def patched(monkeypatch):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    # 全量 UT 可能已通过 adapter 安装补丁；清空 guard 再包会递归争用同一 I/O 锁。
    if patch._PATCHED:
        return

    # 仅还原此测试装配点，不把 patch 泄漏给别的 MCP 测试。
    for cls, names in (
        (StreamableHttpClient, ("connect", "disconnect", "call_tool", "list_tools")),
        (
            SseClient,
            ("connect", "_do_connect", "disconnect", "call_tool", "list_tools"),
        ),
        (ToolMgr, ("_create_client",)),
    ):
        for name in names:
            if name in cls.__dict__:
                monkeypatch.setattr(cls, name, cls.__dict__[name])
    monkeypatch.setattr(patch, "_PATCHED", False)
    monkeypatch.setattr(patch, "_wrapped_methods", set())
    monkeypatch.setattr(patch, "_wrapped_connects", set())
    monkeypatch.setattr(patch, "_wrapped_factories", set())
    monkeypatch.setattr(patch, "_patch_ability_manager_fail_after", lambda: None)
    patch.apply_mcp_call_timeout_patch(default_timeout=0.05)


def config(kind):
    return {"type": kind, "url": "http://127.0.0.1:9/mcp"}


async def settled(state):
    for _ in range(20):
        await asyncio.sleep(0)
    assert Counter(state.owners) == Counter(state.exits)
    assert all(s.exited for s in state.sessions)
    assert all(t.done() for t in state.children)
    assert not (asyncio.all_tasks() - state.initial_tasks - {asyncio.current_task()})
    count = state.callbacks
    for _ in range(20):
        await asyncio.sleep(0)
    assert state.callbacks == count, "cleanup left a cancellation callback loop"


def test_foreign_task_exit_reproduces_anyio_error():
    # 故意破坏作用域的测试放子进程，不能污染 pytest 的事件循环。
    code = """
import asyncio, anyio, os
async def main():
    ready = asyncio.Event()
    group = anyio.create_task_group()
    async def owner():
        await group.__aenter__()
        ready.set()
        await asyncio.Event().wait()
    asyncio.create_task(owner())
    await ready.wait()
    try:
        await group.__aexit__(None, None, None)
    except RuntimeError as exc:
        assert 'different task' in str(exc), str(exc)
        os._exit(0)
    os._exit(1)
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code], timeout=5, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
@pytest.mark.parametrize(
    "phase", ["normal", "connect-error", "connect-hang", "list-hang"]
)
async def test_discovery_lifecycle(transport, patched, kind, phase):
    transport.phase = phase
    tools, params = await asyncio.wait_for(
        mcp_config.list_request_mcp_server_tools("test", config(kind)), 2
    )
    assert bool(params) == (phase == "normal")
    assert bool(tools) == (phase == "normal")
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
@pytest.mark.parametrize("phase", ["connect-hang", "list-hang"])
async def test_discovery_external_cancel(transport, patched, monkeypatch, kind, phase):
    monkeypatch.setattr(mcp_config, "_MCP_CONNECTOR_DISCOVERY_TIMEOUT_S", 30.0)
    transport.phase = phase
    task = asyncio.create_task(
        mcp_config.list_request_mcp_server_tools("test", config(kind))
    )
    await asyncio.wait_for(
        transport.entered.wait() if phase == "connect-hang" else transport.calls.wait(),
        1,
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_worker_reconnect_and_shutdown(transport, patched, kind):
    params = {
        "_mcp_client_type": kind,
        "server_name": "test",
        "server_path": config(kind)["url"],
    }
    worker = await mcp_config.acquire_request_scoped_mcp_session(
        "lifecycle", kind, params
    )
    try:
        assert (await asyncio.wait_for(worker.call_tool("echo", {}), 1)).content[
            0
        ].text == "ok"
        transport.phase = "dead"
        assert (await asyncio.wait_for(worker.call_tool("echo", {}), 1)).content[
            0
        ].text == "ok"
        transport.phase = "call-hang"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.call_tool("echo", {}), 1)
        await asyncio.wait_for(worker.task, 1)
        transport.phase = "normal"
        rebuilt = await mcp_config.acquire_request_scoped_mcp_session(
            "lifecycle", kind, params
        )
        assert rebuilt is not worker
        assert (await asyncio.wait_for(rebuilt.call_tool("echo", {}), 1)).content[
            0
        ].text == "ok"
    finally:
        await mcp_config.release_request_scoped_mcp_sessions("lifecycle")
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_worker_cancel_during_connect_unblocks_callers(transport, patched, kind):
    transport.phase = "connect-hang"
    worker = mcp_config._PooledMcpWorker("test")
    params = {
        "_mcp_client_type": kind,
        "server_name": "test",
        "server_path": config(kind)["url"],
    }
    worker.task = asyncio.create_task(mcp_config._run_mcp_worker(params, worker))
    caller = asyncio.create_task(worker.call_tool("echo", {}))
    await asyncio.wait_for(transport.entered.wait(), 1)
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker.task, 2)
    with pytest.raises(mcp_config.McpWorkerStoppedError):
        await asyncio.wait_for(caller, 2)
    await settled(transport)


@pytest.mark.asyncio
async def test_toolmgr_owner_reconnect_and_cross_task_disconnect(transport, patched):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type="streamable-http",
            server_path=config("streamable-http")["url"],
            params={"timeout_s": 0.05},
        )
    )
    assert await asyncio.wait_for(client.connect(), 1)
    transport.phase = "dead"
    assert await asyncio.wait_for(client.call_tool("echo", {}), 1) == "ok"
    transport.phase = "call-hang"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.call_tool("echo", {}), 1)
    transport.phase = "normal"
    assert await asyncio.wait_for(client.call_tool("echo", {}), 1) == "ok"
    assert await asyncio.wait_for(client.disconnect(), 1)
    assert client._jws_http_owner.task.done()
    await settled(transport)


@pytest.mark.asyncio
async def test_discovery_disconnect_false_is_failure(monkeypatch):
    client = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        list_tools=AsyncMock(return_value=[]),
        disconnect=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        mcp_config, "_remote_mcp_client_cls", lambda _: lambda cfg: client
    )
    cfg = McpServerConfig(
        server_id="test", server_name="test", server_path="http://example/mcp"
    )
    assert await mcp_config._list_remote_mcp_connector_tools(
        "test", cfg, "streamable-http"
    ) == ([], {})
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_discovery_without_startup_patch(transport, kind):
    tools, params = await mcp_config.list_request_mcp_server_tools("test", config(kind))
    assert tools and params
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect-hang", "call-hang"])
async def test_toolmgr_owner_external_cancel(transport, patched, phase):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type="streamable-http",
            server_path=config("streamable-http")["url"],
        )
    )
    if phase == "call-hang":
        assert await asyncio.wait_for(client.connect(), 1)
    transport.phase = phase
    task = asyncio.create_task(
        client.connect() if phase == "connect-hang" else client.call_tool("echo", {})
    )
    event = transport.entered if phase == "connect-hang" else transport.calls
    await asyncio.wait_for(event.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert client._jws_http_owner.task.done()
    await settled(transport)


@pytest.mark.asyncio
async def test_toolmgr_partial_connect_timeout_and_reconnect(transport, patched):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type="streamable-http",
            server_path=config("streamable-http")["url"],
        )
    )
    transport.phase = "connect-hang"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.connect(timeout=0.01), 1)
    assert client._jws_http_owner.task.done()
    await settled(transport)
    transport.phase = "normal"
    assert await asyncio.wait_for(client.connect(), 1)
    assert await asyncio.wait_for(client.disconnect(), 1)
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_registry_scan_timeout_and_shutdown(
    transport, patched, monkeypatch, kind
):
    from jiuwenswarm.common import mcp_server_registry as registry_module

    registry = McpServerRegistry(settings=McpRegistrySettings(scan_interval_s=60))
    assert (await registry.add_servers([{"name": "test", **config(kind)}]))[0]["ok"]
    worker = await registry.acquire_worker("test")
    await asyncio.wait_for(worker.call_tool("echo", {}), 1)
    transport.phase = "list-hang"
    monkeypatch.setattr(registry_module, "_discover_timeout_s", lambda cfg: 0.01)
    await asyncio.wait_for(registry.scan_once(), 1)
    assert registry._cache["test"].last_scan_ok is False
    assert registry._cache["test"].tools  # 失败扫描保留旧缓存
    transport.calls.clear()
    registry.start_scanner()
    scanner = registry._scanner_task
    await asyncio.wait_for(transport.calls.wait(), 1)
    await asyncio.wait_for(registry.stop_scanner(), 1)
    assert scanner.done() and worker.task.done()
    await settled(transport)


@pytest.mark.asyncio
async def test_toolmgr_cancel_queued_caller_does_not_cancel_active_call(
    transport, patched
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type="streamable-http",
            server_path=config("streamable-http")["url"],
            params={"timeout_s": 1.0},
        )
    )
    assert await asyncio.wait_for(client.connect(), 1)
    transport.phase = "call-hang"
    first = asyncio.create_task(client.call_tool("echo", {}))
    await asyncio.wait_for(transport.calls.wait(), 1)
    queued = asyncio.create_task(client.call_tool("echo", {}))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert not first.done()
    transport.release.set()
    assert await asyncio.wait_for(first, 1) == "ok"
    assert await asyncio.wait_for(client.disconnect(), 1)
    await settled(transport)


@pytest.mark.asyncio
async def test_registry_shutdown_without_scanner_closes_workers(transport, patched):
    registry = McpServerRegistry()
    assert (
        await registry.add_servers([{"name": "test", **config("streamable-http")}])
    )[0]["ok"]
    worker = await registry.acquire_worker("test")
    await asyncio.wait_for(worker.call_tool("echo", {}), 1)
    await asyncio.wait_for(registry.stop_scanner(), 1)
    assert worker.task.done()
    await settled(transport)


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_external_cancel(monkeypatch):
    entered = asyncio.Event()

    async def list_tools(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    client = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        list_tools=list_tools,
        disconnect=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        mcp_config, "_remote_mcp_client_cls", lambda _: lambda cfg: client
    )
    cfg = McpServerConfig(
        server_id="test", server_name="test", server_path="http://example/mcp"
    )
    task = asyncio.create_task(
        mcp_config._list_remote_mcp_connector_tools("test", cfg, "streamable-http")
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert client._jws_needs_reconnect
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_toolmgr_does_not_move_failed_cleanup_to_new_owner():
    client = SimpleNamespace(
        _session=None,
        _client=None,
        _is_disconnected=False,
        connect=AsyncMock(return_value=True),
        disconnect=AsyncMock(side_effect=RuntimeError("disconnect returned false")),
    )
    patch._install_streamable_http_owner(client)
    assert await client.connect()
    client._session = object()
    # 模拟 disconnect wrapper 把 False 变成错误后的保留状态。
    with pytest.raises(RuntimeError, match="disconnect returned false"):
        await client.disconnect()
    owner = client._jws_http_owner.task
    assert owner.done()
    with pytest.raises(RuntimeError, match="previous owner cleanup incomplete"):
        await client.connect()
    assert client._jws_http_owner.task is owner


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_idle_worker_cancel_drains_newly_queued_request(transport, patched, kind):
    worker = mcp_config._PooledMcpWorker("test")
    params = {
        "_mcp_client_type": kind,
        "server_name": "test",
        "server_path": config(kind)["url"],
    }
    worker.task = asyncio.create_task(mcp_config._run_mcp_worker(params, worker))
    await asyncio.wait_for(worker.call_tool("echo", {}), 1)
    request = mcp_config._McpCallRequest("echo", {})
    worker.queue.put_nowait(request)
    worker.task.cancel()  # 已唤醒的 queue.get 在获取 request 之前收到取消
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker.task, 1)
    with pytest.raises(mcp_config.McpWorkerStoppedError):
        await asyncio.wait_for(request.future, 1)
    await settled(transport)


@pytest.mark.asyncio
async def test_registry_stop_cancellation_waits_for_scanner_cleanup():
    registry = McpServerRegistry()
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def scanner():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    registry._scanner_task = asyncio.create_task(scanner())
    registry.worker_pool.close_all = AsyncMock()
    await started.wait()
    stop = asyncio.create_task(registry.stop_scanner())
    await asyncio.wait_for(cleaning.wait(), 1)
    stop.cancel()
    await asyncio.sleep(0)
    assert not stop.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stop, 1)
    registry.worker_pool.close_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_sse_failed_owner_is_not_cleaned_from_new_task():
    owner = asyncio.create_task(asyncio.sleep(0))
    await owner
    client = SimpleNamespace(
        _session=object(),
        _client=object(),
        _owner_task=owner,
        _is_disconnected=False,
        disconnect=AsyncMock(return_value=True),
    )
    with pytest.raises(RuntimeError, match="SSE previous owner cleanup incomplete"):
        await patch.disconnect_remote_mcp_client(client)
    client.disconnect.assert_not_awaited()
    assert client._jws_needs_reconnect


@pytest.mark.asyncio
async def test_sse_disconnect_cancel_waits_for_owner_exit(transport, patched):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type="sse",
            server_path=config("sse")["url"],
        )
    )
    assert await asyncio.wait_for(client.connect(), 1)
    transport.hold_close = True
    disconnect = asyncio.create_task(client.disconnect())
    await asyncio.wait_for(transport.closing.wait(), 1)
    disconnect.cancel()
    await asyncio.sleep(0)
    disconnect.cancel()
    await asyncio.sleep(0)
    transport.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(disconnect, 1)
    owner = client._owner_task
    # 失败时也主动结束空闲 SDK owner，避免本回归把 pytest 循环弄脏。
    for _ in range(20):
        await asyncio.sleep(0)
    leaked = owner is not None and not owner.done()
    if leaked:
        owner.cancel()
        await owner
    assert not leaked, "cancelled disconnect left an idle SSE owner"
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
@pytest.mark.parametrize("phase", ["connect-hang", "call-hang"])
async def test_repeated_caller_cancel_does_not_interrupt_owner_cleanup(
    transport, patched, kind, phase
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type=kind,
            server_path=config(kind)["url"],
            params={"timeout_s": 1.0},
        )
    )
    if phase == "call-hang":
        assert await asyncio.wait_for(client.connect(), 1)
    transport.phase = phase
    transport.hold_close = True
    call = asyncio.create_task(
        client.connect() if phase == "connect-hang" else client.call_tool("echo", {})
    )
    ready = transport.entered if phase == "connect-hang" else transport.calls
    await asyncio.wait_for(ready.wait(), 1)
    call.cancel()
    await asyncio.wait_for(transport.closing.wait(), 1)
    call.cancel()
    await asyncio.sleep(0)
    transport.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 1)
    assert transport.close_cancellations == 0
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_anyio_caller_scope_cancel_allows_owner_cleanup(transport, patched, kind):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(
        McpServerConfig(
            server_id="test",
            server_name="test",
            client_type=kind,
            server_path=config(kind)["url"],
            params={"timeout_s": 1.0},
        )
    )
    assert await asyncio.wait_for(client.connect(), 1)
    transport.phase = "call-hang"
    transport.hold_close = True

    async def release_cleanup():
        await transport.closing.wait()
        for _ in range(20):
            await asyncio.sleep(0)
        transport.release_close.set()

    release = asyncio.create_task(release_cleanup())
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.01):
            await client.call_tool("echo", {})
    await asyncio.wait_for(release, 1)
    assert transport.close_cancellations == 0
    await settled(transport)


@pytest.mark.asyncio
async def test_worker_init_internal_cancel_is_not_caller_cancel(monkeypatch):
    """worker 初始化的内部取消不能伪装成所有排队请求的用户取消。"""
    monkeypatch.setattr(
        mcp_config, "_enter_remote_mcp_session",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    worker = mcp_config._PooledMcpWorker("test")
    requests = [mcp_config._McpCallRequest("echo", {}) for _ in range(2)]
    for request in requests:
        worker.queue.put_nowait(request)
    worker.task = asyncio.create_task(mcp_config._run_mcp_worker(
        {"_mcp_client_type": "sse"}, worker,
    ))
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    results = await asyncio.gather(*(request.future for request in requests), return_exceptions=True)
    assert all(isinstance(result, mcp_config.McpWorkerStoppedError) for result in results)
    await asyncio.wait_for(worker.queue.join(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sse", "streamable-http"])
@pytest.mark.parametrize("phase", ["connect", "list_tools"])
async def test_discovery_internal_cancel_skips_only_connector(monkeypatch, kind, phase):
    """SDK 内部 CancelledError 只让当前连接器发现失败。"""
    client = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        list_tools=AsyncMock(return_value=[]),
        disconnect=AsyncMock(return_value=True),
    )
    getattr(client, phase).side_effect = asyncio.CancelledError()
    monkeypatch.setattr(mcp_config, "_remote_mcp_client_cls", lambda _: lambda cfg: client)
    cfg = SimpleNamespace(server_name="test", server_path="http://localhost/mcp", params={})
    assert await mcp_config._list_remote_mcp_connector_tools("test", cfg, kind) == ([], {})
    client.disconnect.assert_awaited_once()


def sse_cleanup_client(disconnect):
    client = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        disconnect=disconnect,
        _owner_loop=AsyncMock(),
        _do_disconnect=AsyncMock(return_value=True),
    )
    patch.manage_sse_owner_cleanup(client)
    return client


@pytest.mark.asyncio
async def test_sse_disconnect_internal_cancel_is_cleanup_failure():
    client = sse_cleanup_client(AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(RuntimeError, match="cancelled internally"):
        await client.disconnect()
    assert client._jws_needs_reconnect is True
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
async def test_sse_cancelled_disconnect_has_bounded_cleanup(monkeypatch):
    """没有 owner 回执时仍须在预算内结束取消流程，不无限 shield。"""
    monkeypatch.setattr(patch, "_MCP_CLEANUP_TIMEOUT_S", 0.02)
    monkeypatch.setattr(patch, "_MCP_CANCEL_DRAIN_TIMEOUT_S", 0.02)
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def disconnect():
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    client = sse_cleanup_client(disconnect)
    caller = asyncio.create_task(client.disconnect())
    await started.wait()
    caller.cancel()
    done, _ = await asyncio.wait({caller}, timeout=0.2)
    try:
        assert done, "取消后的 SSE disconnect 超过清理预算仍未返回"
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert stopped.is_set()
        assert client._jws_needs_reconnect is True
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_finish_cleanup_budget_includes_cancel_resistant_task(monkeypatch):
    """目标忽略 cancel 时收尾仍有上限，资源由原任务持有，测试最终显式释放。"""
    monkeypatch.setattr(patch, "_MCP_CLEANUP_TIMEOUT_S", 0.02)
    monkeypatch.setattr(patch, "_MCP_CANCEL_DRAIN_TIMEOUT_S", 0.02)
    started, release = asyncio.Event(), asyncio.Event()

    async def resistant():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    target = asyncio.create_task(resistant())
    await started.wait()
    waiter = asyncio.create_task(patch._finish_mcp_task_after_cancel(target))
    done, _ = await asyncio.wait({waiter}, timeout=0.2)
    try:
        assert done, "目标忽略取消导致清理等待无上限"
        with pytest.raises(TimeoutError, match="cleanup"):
            await waiter
        assert target.cancelling() > 0
    finally:
        release.set()
        await asyncio.gather(target, waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
@pytest.mark.parametrize("method", ["call_tool", "list_tools"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_internal_io_cancel_retries_once_without_cancelling_caller(
    transport, patched, monkeypatch, kind, method, persistent,
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(McpServerConfig(
        server_id="test", server_name="test", client_type=kind,
        server_path=config(kind)["url"],
    ))
    assert await client.connect()
    session_cls = type(transport.sessions[-1])
    original = getattr(session_cls, method)
    calls = 0

    async def internally_cancelled(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if persistent or calls == 1:
            raise asyncio.CancelledError("internal transport cancellation")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(session_cls, method, internally_cancelled)
    caller = asyncio.create_task(
        client.call_tool("echo", {}) if method == "call_tool" else client.list_tools()
    )
    try:
        if persistent:
            with pytest.raises(RuntimeError, match="cancelled internally"):
                await asyncio.wait_for(caller, 1)
        else:
            assert await asyncio.wait_for(caller, 1)
        assert not caller.cancelled() and caller.cancelling() == 0
        assert calls == 2
        assert transport.connects == 2
    finally:
        await client.disconnect()
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_worker_internal_call_cancel_is_tool_error(
    transport, patched, monkeypatch, kind,
):
    worker = mcp_config._PooledMcpWorker("test")
    params = {
        "_mcp_client_type": kind, "server_name": "test",
        "server_path": config(kind)["url"],
    }
    worker.task = asyncio.create_task(mcp_config._run_mcp_worker(params, worker))
    try:
        await asyncio.wait_for(worker.call_tool("echo", {}), 1)
        session_cls = type(transport.sessions[-1])
        original = session_cls.call_tool
        monkeypatch.setattr(session_cls, "call_tool", AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(RuntimeError, match="cancelled internally"):
            await asyncio.wait_for(worker.call_tool("echo", {}), 1)
        assert not worker.task.done()
        monkeypatch.setattr(session_cls, "call_tool", original)
        assert (await asyncio.wait_for(worker.call_tool("echo", {}), 1)).content[0].text == "ok"
    finally:
        await mcp_config.shutdown_pooled_mcp_worker(worker)
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamable-http", "sse"])
async def test_external_cancel_during_internal_cancel_cleanup(
    transport, patched, monkeypatch, kind,
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(McpServerConfig(
        server_id="test", server_name="test", client_type=kind,
        server_path=config(kind)["url"],
    ))
    assert await client.connect()
    monkeypatch.setattr(
        transport.sessions[-1], "call_tool", AsyncMock(side_effect=asyncio.CancelledError()),
    )
    transport.hold_close = True
    caller = asyncio.create_task(client.call_tool("echo", {}))
    try:
        await asyncio.wait_for(transport.closing.wait(), 1)
        caller.cancel()
        await asyncio.sleep(0)
        transport.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
        assert transport.connects == 1
    finally:
        transport.release_close.set()
        await asyncio.gather(caller, return_exceptions=True)
        await client.disconnect()
    await settled(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mode", ["none", "once", "repeated", "anyio"])
async def test_connect_timeout_cleanup_survives_caller_cancel_and_reconnects(
    transport, patched, cancel_mode,
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    client = ToolMgr._create_client(McpServerConfig(
        server_id="test", server_name="test", client_type="streamable-http",
        server_path=config("streamable-http")["url"],
    ))
    transport.phase = "connect-hang"
    transport.hold_close = True
    scope = None
    cancellation_seen = False

    async def connect():
        nonlocal scope, cancellation_seen
        with anyio.CancelScope() as scope:
            try:
                return await client.connect(timeout=0.01)
            except asyncio.CancelledError:
                cancellation_seen = True
                raise

    caller = asyncio.create_task(connect())
    try:
        # owner 先发布建连超时，再进入清理；此时调用者正在等待 owner 退出。
        await asyncio.wait_for(transport.closing.wait(), 1)
        if cancel_mode == "anyio":
            scope.cancel()
        elif cancel_mode != "none":
            caller.cancel()
        await asyncio.sleep(0)
        if cancel_mode == "repeated":
            caller.cancel()
            await asyncio.sleep(0)
        transport.release_close.set()
        if cancel_mode == "none":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(caller, 1)
        elif cancel_mode == "anyio":
            await asyncio.wait_for(caller, 1)
            assert cancellation_seen
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
        assert transport.close_cancellations == 0
        assert client._jws_http_owner.task.done()
        assert client._session is None
        assert client._client is None
        await settled(transport)
        transport.phase = "normal"
        transport.hold_close = False
        assert await client.connect()
        assert await client.call_tool("echo", {}) == "ok"
        assert await client.disconnect()
        await settled(transport)
    finally:
        transport.release_close.set()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_connect_failure_cleanup_has_deadline(transport, patched, monkeypatch):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    monkeypatch.setattr(patch, "_MCP_CLEANUP_TIMEOUT_S", 0.02)
    monkeypatch.setattr(patch, "_MCP_CANCEL_DRAIN_TIMEOUT_S", 0.02)
    client = ToolMgr._create_client(McpServerConfig(
        server_id="test", server_name="test", client_type="streamable-http",
        server_path=config("streamable-http")["url"],
    ))
    transport.phase = "connect-hang"
    transport.hold_close = True
    caller = asyncio.create_task(client.connect(timeout=0.01))
    try:
        await asyncio.wait_for(transport.closing.wait(), 1)
        caller.cancel()
        # 不释放清理事件，必须由清理预算耗尽结束等待，不能无限 shield。
        done, _ = await asyncio.wait({caller}, timeout=1)
        assert done, "取消后的建连失败清理超过预算仍未返回"
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert client._jws_needs_reconnect
        assert client._jws_http_owner.task.done()
        assert transport.close_cancellations == 1  # 只有预算耗尽才取消 owner。
        with pytest.raises(RuntimeError, match="previous owner cleanup incomplete"):
            await client.connect()
    finally:
        transport.release_close.set()
        await asyncio.gather(caller, return_exceptions=True)
    await settled(transport)


@pytest.mark.asyncio
async def test_failed_connect_owner_cancel_preserves_operation_error():
    client = SimpleNamespace(
        connect=AsyncMock(side_effect=ValueError("connect failed")),
        disconnect=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    patch._install_streamable_http_owner(client)
    caller = asyncio.create_task(client.connect())
    with pytest.raises(ValueError, match="connect failed"):
        await asyncio.wait_for(caller, 1)
    assert not caller.cancelled() and caller.cancelling() == 0
    assert client._jws_http_owner.task.done()
    assert client._jws_needs_reconnect
