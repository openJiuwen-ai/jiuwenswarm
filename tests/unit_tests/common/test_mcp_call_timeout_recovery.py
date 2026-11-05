# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP 超时 / Session terminated 后须能恢复（TC_MCP_CALL_014 与服务重启场景）。"""

from __future__ import annotations

from contextlib import AsyncExitStack, contextmanager
from types import SimpleNamespace
from typing import Iterator

import anyio
import pytest

import jiuwenswarm.common.mcp_call_timeout_patch as timeout_patch
from jiuwenswarm.common.mcp_call_timeout_patch import (
    force_invalidate_mcp_client,
    is_retryable_mcp_dead_session_error,
)


@contextmanager
def _restore_after_mcp_timeout_patch() -> Iterator[None]:
    """还原 apply_mcp_call_timeout_patch 可能改动的全局绑定（anyio / 原始 invoke）。

    假 transport 模块下主要还原真 SDK 的 ``ability_manager.anyio``；
    Tool 等类的 invoke 与子类钩子恢复为进入测试时的绑定，保留原有补丁。
    """
    am_mod = None
    orig_am_anyio = None
    try:
        import openjiuwen.core.single_agent.ability_manager as am_mod

        orig_am_anyio = am_mod.anyio
    except Exception:
        am_mod = None

    def _tool_classes_for_invoke_restore() -> list[type]:
        classes: list[type] = []
        try:
            from openjiuwen.core.foundation.tool.base import Tool
            from openjiuwen.core.foundation.tool.mcp.base import MCPTool

            classes.extend([Tool, MCPTool])
        except Exception:
            return classes
        try:
            from openjiuwen.core.foundation.tool.function.function import LocalFunction

            classes.append(LocalFunction)
        except Exception:
            pass
        try:
            from openjiuwen.core.foundation.tool.service_api.restful_api import (
                RestfulApi,
            )

            classes.append(RestfulApi)
        except Exception:
            pass
        try:
            from jiuwenswarm.common.mcp_config import RequestScopedOfficeClawMcpTool

            classes.append(RequestScopedOfficeClawMcpTool)
        except Exception:
            pass
        return classes

    # 可能已有 adapter 安装的 invoke 包装；退出时恢复进入测试时的精确绑定，
    # 而不是无条件退回原始方法，避免让其它测试失去已安装的 AM 超时桥。
    invoke_bindings = {
        cls: {name: cls.__dict__[name] for name in (
            "invoke", "_jws_orig_invoke", "__init_subclass__",
            "_jws_am_timeout_init_subclass_hooked",
        ) if name in cls.__dict__}
        for cls in _tool_classes_for_invoke_restore()
    }
    try:
        yield
    finally:
        if am_mod is not None and orig_am_anyio is not None:
            am_mod.anyio = orig_am_anyio
        for cls, bindings in invoke_bindings.items():
            for name in ("invoke", "_jws_orig_invoke", "__init_subclass__",
                         "_jws_am_timeout_init_subclass_hooked"):
                if name in bindings:
                    setattr(cls, name, bindings[name])
                elif name in cls.__dict__:
                    delattr(cls, name)
        # 单测会反复清 _PATCHED；收尾再清一次，避免泄漏到其它文件
        timeout_patch._PATCHED = False
        timeout_patch._wrapped_methods.clear()


@pytest.fixture
def restore_mcp_timeout_patch():
    """显式还原入口（与模块 autouse 等价；AM 桥接用例可点名依赖以表意）。"""
    with _restore_after_mcp_timeout_patch():
        yield


@pytest.mark.asyncio
async def test_force_invalidate_retains_resources_without_background_aclose() -> None:
    """同步作废只能标记，不能丢弃资源或在另一任务关闭上下文。"""
    import asyncio
    from unittest.mock import AsyncMock

    stack = AsyncExitStack()
    stack.aclose = AsyncMock()
    session = object()
    client = SimpleNamespace(_session=session, _exit_stack=stack, _is_disconnected=False)
    await asyncio.create_task(_mark_invalid(client))
    for _ in range(10):
        await asyncio.sleep(0)
    assert client._jws_needs_reconnect is True
    assert client._session is session
    assert client._exit_stack is stack
    stack.aclose.assert_not_awaited()


async def _mark_invalid(client):
    force_invalidate_mcp_client(client)


@pytest.mark.parametrize(
    "exc, expect",
    [
        (RuntimeError("Session terminated"), True),
        (Exception("server error 404"), True),
        (ConnectionError("Connection closed"), True),
        (Exception(""), True),  # 现场半死连接：error='' / 空消息
        (
            RuntimeError(
                "Attempted to exit cancel scope in a different task than it was entered in"
            ),
            True,
        ),
        (ValueError(""), False),  # 空业务错误不应当死会话重试
        (ValueError("invalid tool args"), False),
    ],
)
def test_is_retryable_mcp_dead_session_error(exc: BaseException, expect: bool) -> None:
    assert is_retryable_mcp_dead_session_error(exc) is expect


def _install_fake_transport_modules(monkeypatch: pytest.MonkeyPatch):
    """避免拉取 openjiuwen.tool 整包依赖；给超时补丁提供可包装的假客户端类。"""
    import sys
    from types import ModuleType

    async def _stub_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del self, tool_name, arguments, timeout
        raise RuntimeError("stub call_tool not replaced")

    async def _stub_list_tools(self, *, timeout=-1):
        del self, timeout
        return []

    async def _stub_connect(self, *, timeout=-1):
        del self, timeout
        return True

    async def _stub_disconnect(self, *, timeout=-1):
        del self, timeout
        return True

    class StreamableHttpClient:
        call_tool = _stub_call_tool
        list_tools = _stub_list_tools
        connect = _stub_connect
        disconnect = _stub_disconnect

    class SseClient:
        call_tool = _stub_call_tool
        list_tools = _stub_list_tools
        connect = _stub_connect
        disconnect = _stub_disconnect

    class ToolMgr:
        @staticmethod
        def _create_client(config):
            return object()

    http_mod = ModuleType(
        "openjiuwen.core.foundation.tool.mcp.client.streamable_http_client"
    )
    http_mod.StreamableHttpClient = StreamableHttpClient
    sse_mod = ModuleType("openjiuwen.core.foundation.tool.mcp.client.sse_client")
    sse_mod.SseClient = SseClient
    mgr_mod = ModuleType("openjiuwen.core.runner.resources_manager.tool_manager")
    mgr_mod.ToolMgr = ToolMgr

    monkeypatch.setitem(sys.modules, http_mod.__name__, http_mod)
    monkeypatch.setitem(sys.modules, sse_mod.__name__, sse_mod)
    monkeypatch.setitem(sys.modules, mgr_mod.__name__, mgr_mod)
    return StreamableHttpClient, SseClient, ToolMgr


def _install_fake_streamable_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_call_tool,
    fake_disconnect=None,
    fake_connect=None,
    default_timeout: float = 5.0,
):
    StreamableHttpClient, _, _ = _install_fake_transport_modules(monkeypatch)

    async def _default_list_tools(self, *, timeout=-1):
        del timeout
        if not self._session:
            raise RuntimeError("Not connected to streamable-http server")
        return []

    async def _default_disconnect(self, *, timeout=-1):
        del timeout
        return True

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


@pytest.fixture(autouse=True)
def _autouse_restore_mcp_timeout_patch(monkeypatch):
    """本文件凡 apply / 改 ability_manager.anyio 的用例结束后还原全局副作用。"""
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import StreamableHttpClient
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    # 本文件有真实 SSE 和假 transport 两种装配。绑定与 guard 必须成对还原，
    # 否则后续生命周期测试会在已有 wrapper 上再包一次，甚至叠加 HTTP owner。
    for cls in (SseClient, StreamableHttpClient, ToolMgr):
        for name in ("connect", "_do_connect", "disconnect", "call_tool", "list_tools", "_create_client"):
            if name in cls.__dict__:
                monkeypatch.setattr(cls, name, cls.__dict__[name])
    monkeypatch.setattr(timeout_patch, "_PATCHED", timeout_patch._PATCHED)
    for name in ("_wrapped_methods", "_wrapped_connects", "_wrapped_factories"):
        monkeypatch.setattr(timeout_patch, name, set(getattr(timeout_patch, name)))
    monkeypatch.setattr(timeout_patch, "_patch_sdk_read_timeouts", lambda: None)
    with _restore_after_mcp_timeout_patch():
        yield


@pytest.mark.asyncio
async def test_timeout_then_same_session_call_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时在 owner 清理完成后，下一轮须重连成功。"""
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
async def test_timeout_then_cancel_scope_on_next_call_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC_MCP_CALL_014 第 3 轮：超时重连后首呼若冒 cancel scope，须同轮重试成功。

    现场：MCP 已 call completed，但返回路径抛 Attempted to exit a cancel scope，
    导致 after-timeout 的 qa_echo 失败；第 4 轮才恢复。补丁应对该噪声重连重试一次。
    """
    call_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            await anyio.sleep(5)
            return {"unreachable": True}
        if call_count["n"] == 2:
            # 模拟：工具其实已跑完，返回时被 cancel-scope 清理异常盖掉
            raise RuntimeError(
                "Attempted to exit cancel scope in a different task than it was entered in"
            )
        return {
            "tool": tool_name,
            "text": arguments.get("text"),
            "marker": "SDS-DEV-MCP-OK",
            "ok": True,
        }

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        default_timeout=0.05,
    )

    with pytest.raises(TimeoutError, match="timed out after"):
        await client.call_tool("qa_delay", {"seconds": 5})

    result = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-after-timeout-should-ok"},
    )
    assert result["ok"] is True
    assert result["marker"] == "SDS-DEV-MCP-OK"
    # 1 超时 + 1 cancel-scope 失败 + 1 重试成功
    assert call_count["n"] == 3
    assert connect_count["n"] >= 2


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
async def test_timeout_then_fresh_chat_session_on_shared_client_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时后新建聊天会话 B 与 A 共用进程级客户端，也须能 qa_echo。

    对齐 sds-037840350c2c：A 超时后 B 仅 echo 仍失败（error=''）。
    """
    call_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            await anyio.sleep(5)
            return {"unreachable": True}
        return {
            "tool": tool_name,
            "text": arguments.get("text"),
            "ok": True,
        }

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        default_timeout=0.05,
    )

    with pytest.raises(TimeoutError, match="timed out after"):
        await client.call_tool("qa_delay", {"seconds": 5})

    assert getattr(client, "_jws_needs_reconnect", False) is True

    # 会话 A 超时后 echo（对照 #4133）
    result_a = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-037840350c2c-after-timeout"},
    )
    assert result_a["ok"] is True
    assert result_a["text"] == "MCP-CALL-sds-037840350c2c-after-timeout"

    # 会话 B：同一 AS Pod / 同一 ToolMgr 客户端，仅 echo
    result_b = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-037840350c2c-fresh-after-timeout"},
    )
    assert result_b["ok"] is True
    assert result_b["text"] == "MCP-CALL-sds-037840350c2c-fresh-after-timeout"
    assert connect_count["n"] >= 1
    assert call_count["n"] == 3
    assert getattr(client, "_jws_needs_reconnect", True) is False


@pytest.mark.asyncio
async def test_empty_error_after_half_dead_session_reconnects_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """半死连接抛空消息 Exception 时须重连重试（现场 error=''）。"""
    call_count = {"n": 0}

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del timeout
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("")
        return {"tool": tool_name, "text": arguments.get("text"), "ok": True}

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch,
        fake_call_tool=fake_call_tool,
        default_timeout=5.0,
    )

    result = await client.call_tool(
        "qa_echo",
        {"text": "MCP-CALL-sds-037840350c2c-fresh-after-timeout"},
    )
    assert result["ok"] is True
    assert call_count["n"] == 2
    assert connect_count["n"] >= 1


@pytest.mark.asyncio
async def test_cleanup_failure_blocks_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """不能把失败的 disconnect 静默视为成功并在旧上下文上重连。"""
    healthy = False

    async def call(self, *args, **kwargs):
        if not healthy:
            raise RuntimeError("Session terminated")
        return {"ok": True}

    async def disconnect(self, **kwargs):
        return healthy

    client, connect_count, _ = _install_fake_streamable_client(
        monkeypatch, fake_call_tool=call, fake_disconnect=disconnect,
    )
    session, stack = client._session, client._exit_stack
    with pytest.raises(RuntimeError, match="disconnect returned false"):
        await client.call_tool("echo", {})
    assert connect_count["n"] == 0
    assert client._session is session
    assert client._exit_stack is stack
    assert client._jws_needs_reconnect is True

    # 清理恢复后同一个 client 应可重连，不能永久卡在 needs_reconnect。
    healthy = True
    assert await client.call_tool("echo", {}) == {"ok": True}
    assert connect_count["n"] == 1
    assert client._session is not session
    assert client._jws_needs_reconnect is False


@pytest.mark.asyncio
async def test_cleanup_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理中的取消也必须传播，不可伪装为完整断开。"""
    import asyncio

    async def call(self, *args, **kwargs):
        await asyncio.Event().wait()

    async def disconnect(self, **kwargs):
        raise asyncio.CancelledError()

    client, _, _ = _install_fake_streamable_client(
        monkeypatch, fake_call_tool=call, fake_disconnect=disconnect, default_timeout=0.01,
    )
    with pytest.raises(asyncio.CancelledError):
        await client.call_tool("echo", {})
    assert client._session is not None
    assert client._jws_needs_reconnect is True


@pytest.mark.asyncio
async def test_internal_cancel_reconnects_once_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部取消在原任务清理后重连一次，不取消业务调用者。"""
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

    assert (await client.call_tool("qa_echo", {}))["ok"] is True
    assert call_count["n"] == 2
    assert connect_count["n"] == 1
    assert client._session is not None


@pytest.mark.asyncio
async def test_sse_timeout_reports_timeout_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SseClient 超时同样须抛 TimeoutError（可被 MCPTool.invoke 捕获成工具错误）。"""
    import asyncio

    _, SseClient, _ = _install_fake_transport_modules(monkeypatch)

    async def fake_call_tool(self, tool_name: str, arguments: dict, *, timeout=-1):
        del self, tool_name, arguments, timeout
        await anyio.sleep(5)
        return {"unreachable": True}

    async def fake_disconnect(self, *, timeout=-1):
        del self, timeout
        return True

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

    with pytest.raises(TimeoutError, match="timed out after") as ei:
        await client.call_tool("qa_delay", {"seconds": 5})

    assert not isinstance(ei.value, asyncio.CancelledError)
    assert client._session is None
    assert client._is_disconnected is True


@pytest.mark.asyncio
async def test_am_fail_after_bridge_does_not_mutate_global_anyio(
    monkeypatch: pytest.MonkeyPatch,
    restore_mcp_timeout_patch,
) -> None:
    """AbilityManager 桥接不得改写共享 anyio.fail_after（否则 ProgressiveRail 丢 cancel_called）。"""
    import anyio

    del restore_mcp_timeout_patch  # fixture 副作用：结束后还原 am_mod.anyio
    _install_fake_transport_modules(monkeypatch)
    timeout_patch._PATCHED = False
    timeout_patch._wrapped_methods.clear()

    # 预加载真 AbilityManager（若环境无 openjiuwen 则跳过）
    try:
        import openjiuwen.core.single_agent.ability_manager as am_mod
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"openjiuwen AbilityManager unavailable: {exc}")

    real_fail_after = anyio.fail_after
    timeout_patch.apply_mcp_call_timeout_patch(default_timeout=1.0)

    # 全局 anyio.fail_after 必须仍是原对象
    assert anyio.fail_after is real_fail_after
    # AbilityManager 模块拿到的是代理
    assert am_mod.anyio is not anyio
    assert am_mod.anyio.fail_after is not real_fail_after

    # ProgressiveRail 依赖的 cancel_called 仍在
    with anyio.fail_after(1.0) as scope:
        assert hasattr(scope, "cancel_called")
        assert scope.cancel_called is False


@pytest.mark.asyncio
async def test_am_invoke_wrap_honors_contextvar_timeout() -> None:
    """AbilityManager 桥：contextvar 截止时间经 invoke 包装走 wait_for。"""
    import asyncio

    class _SlowTool:
        async def invoke(self, *args, **kwargs):
            del args, kwargs
            await asyncio.sleep(1.0)
            return "done"

    # 清 idempotency，避免与其它用例抢同一 (cls, key)
    key = (_SlowTool, "am_timeout:invoke")
    timeout_patch._wrapped_methods.discard(key)
    timeout_patch._wrap_invoke_with_am_timeout(_SlowTool)

    tool = _SlowTool()
    token = timeout_patch._am_call_timeout.set(0.05)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await tool.invoke({})
    finally:
        timeout_patch._am_call_timeout.reset(token)
        orig = getattr(_SlowTool, "_jws_orig_invoke", None)
        if orig is not None:
            setattr(_SlowTool, "invoke", orig)
            delattr(_SlowTool, "_jws_orig_invoke")
        timeout_patch._wrapped_methods.discard(key)


@pytest.mark.asyncio
async def test_am_wrap_does_not_replace_subclass_invoke_with_parent_orig() -> None:
    """回归：先包父类再包子类时，不得用父类 _jws_orig_invoke 覆盖子类 invoke。"""

    class _BaseTool:
        async def invoke(self, *args, **kwargs):
            del args, kwargs
            return "base"

    class _ChildTool(_BaseTool):
        async def invoke(self, *args, **kwargs):
            del args, kwargs
            return "child"

    timeout_patch._wrap_invoke_with_am_timeout(_BaseTool)
    timeout_patch._wrap_invoke_with_am_timeout(_ChildTool)
    try:
        assert await _ChildTool().invoke({}) == "child"
        assert "_jws_orig_invoke" in _ChildTool.__dict__
        assert _ChildTool.__dict__["_jws_orig_invoke"] is not _BaseTool.__dict__.get(
            "_jws_orig_invoke"
        )
    finally:
        for cls in (_BaseTool, _ChildTool):
            orig = cls.__dict__.get("_jws_orig_invoke")
            if orig is not None:
                setattr(cls, "invoke", orig)
                delattr(cls, "_jws_orig_invoke")
            timeout_patch._wrapped_methods.discard((cls, "am_timeout:invoke"))


def test_init_subclass_hook_must_be_classmethod() -> None:
    """回归 !6630：钩子若是普通函数，创建 Tool 子类会 TypeError 缺 cls。"""

    class _BaseTool:
        pass

    # 允许对本假基类重复安装（勿污染真实 Tool 的 hooked 标记语义）
    if hasattr(_BaseTool, "_jws_am_timeout_init_subclass_hooked"):
        delattr(_BaseTool, "_jws_am_timeout_init_subclass_hooked")

    timeout_patch._install_tool_init_subclass_am_timeout_hook(_BaseTool)

    assert isinstance(_BaseTool.__dict__["__init_subclass__"], classmethod)

    # 创建子类不得再抛 TypeError: __init_subclass__() missing 1 required positional argument: 'cls'
    class _ChildTool(_BaseTool):
        async def invoke(self, *args, **kwargs):
            del args, kwargs
            return "child"

    assert "invoke" in _ChildTool.__dict__
    assert "_jws_orig_invoke" in _ChildTool.__dict__
    # 清理包装，避免泄漏到其它用例的 idempotency 集合语义
    orig = _ChildTool.__dict__.get("_jws_orig_invoke")
    if orig is not None:
        setattr(_ChildTool, "invoke", orig)
        delattr(_ChildTool, "_jws_orig_invoke")
    timeout_patch._wrapped_methods.discard((_ChildTool, "am_timeout:invoke"))


@pytest.mark.asyncio
async def test_disconnect_wrap_retains_resources_when_aclose_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理失败不丢弃仍待 owner 关闭的资源，不假报成功。"""
    StreamableHttpClient, _, _ = _install_fake_transport_modules(monkeypatch)

    async def boom_disconnect(self, *, timeout=-1):
        del self, timeout
        raise RuntimeError("aclose failed")

    monkeypatch.setattr(StreamableHttpClient, "disconnect", boom_disconnect, raising=False)
    timeout_patch._PATCHED = False
    timeout_patch._wrapped_methods.clear()
    timeout_patch.apply_mcp_call_timeout_patch(default_timeout=1.0)

    client = object.__new__(StreamableHttpClient)
    client._session = object()
    client._client = object()
    client._read = object()
    client._write = object()
    client._is_disconnected = False
    client._exit_stack = AsyncExitStack()
    client._auth_provider = object()
    client._server_path = "http://example/mcp"

    with pytest.raises(RuntimeError, match="aclose failed"):
        await client.disconnect()

    assert client._session is not None
    assert client._client is not None
    assert client._is_disconnected is False
    assert client._jws_needs_reconnect is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_cancelled", [False, True])
async def test_internal_cancel_cleanup_failure_does_not_reconnect(monkeypatch, cleanup_cancelled):
    import asyncio

    async def call(self, *args, **kwargs):
        raise asyncio.CancelledError("internal call cancellation")

    async def disconnect(self, **kwargs):
        if cleanup_cancelled:
            raise asyncio.CancelledError("internal cleanup cancellation")
        return False

    client, connects, _ = _install_fake_streamable_client(
        monkeypatch, fake_call_tool=call, fake_disconnect=disconnect,
    )
    session = client._session
    with pytest.raises(RuntimeError, match="internal cancellation cleanup failed"):
        await client.call_tool("echo", {})
    assert connects["n"] == 0
    assert client._session is session
    assert client._jws_needs_reconnect is True
