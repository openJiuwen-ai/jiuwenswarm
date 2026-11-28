# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit coverage for the remote-MCP per-call timeout patch.

End-to-end behaviour (long SSE / streamable-http calls under a stamped
``_jws_call_timeout``, SDK ``sse_read_timeout`` lifting, and the SSE teardown
guard) is verified against real delay servers in the vendored harness; these
tests pin the propagation contract that is observable without a live server.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

from jiuwenswarm.common.mcp_call_timeout_patch import (
    DEFAULT_CALL_TIMEOUT,
    apply_mcp_call_timeout_patch,
)


@pytest.fixture(autouse=True)
def _restore_patch_bindings(monkeypatch):
    from mcp.client import sse, streamable_http
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    # 成对恢复绑定与 guard；实际函数标记保证重复 apply 不会叠加包装。
    for cls in (SseClient, StreamableHttpClient, ToolMgr):
        for name in ("connect", "_do_connect", "disconnect", "call_tool",
                     "call_tool_with_meta", "list_tools", "_create_client"):
            if name in cls.__dict__:
                monkeypatch.setattr(cls, name, cls.__dict__[name])
    for module, name in ((sse, "sse_client"), (streamable_http, "streamablehttp_client")):
        monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(patch, "_PATCHED", False)
    monkeypatch.setattr(patch, "_wrapped_methods", set())
    monkeypatch.setattr(patch, "_wrapped_connects", set())
    monkeypatch.setattr(patch, "_wrapped_factories", set())
    monkeypatch.setattr(patch, "_patch_ability_manager_fail_after", lambda: None)


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
    client._session = _FakeSession()
    return client


class TestStampedTimeoutPropagation:
    @pytest.mark.asyncio
    async def test_stamped_timeout_reaches_inner_ceiling_with_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apply_mcp_call_timeout_patch()
        client = _make_sse_client()
        client._jws_call_timeout = 90.0

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


class TestMetaVariantCoverage:
    """``call_tool`` 与 ``call_tool_with_meta`` 两层都必须被同一套保护覆盖。

    agent-core 的 ``call_tool`` 委托给 ``call_tool_with_meta``；remote 适配器与
    ``MCPTool.invoke`` 优先走 meta 版本。local venv 里的 openjiuwen 可能还没有
    meta 版本，这里用等价 stub 注入，保证包装与“内层不重入锁”契约不依赖版本。
    """

    @pytest.mark.asyncio
    async def test_meta_variant_is_wrapped_and_keeps_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jiuwenswarm.common import mcp_call_timeout_patch as patch

        async def call_tool_with_meta(self, tool_name, arguments, *, timeout=None):
            return "mcp error payload", True

        monkeypatch.setattr(SseClient, "call_tool_with_meta", call_tool_with_meta, raising=False)
        apply_mcp_call_timeout_patch()

        assert getattr(SseClient.call_tool_with_meta, "_jws_mcp_wrapper", None) == "call-timeout"
        assert (SseClient, "call_tool_with_meta") in patch._wrapped_methods

        client = _make_sse_client()
        content, is_error = await asyncio.wait_for(client.call_tool_with_meta("echo", {}), 1)
        assert (content, is_error) == ("mcp error payload", True)

    @pytest.mark.asyncio
    async def test_nested_call_tool_enters_io_section_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """agent-core 的 call_tool → call_tool_with_meta 不能重入同一个 IO 锁。"""
        from jiuwenswarm.common import mcp_call_timeout_patch as patch

        async def call_tool_with_meta(self, tool_name, arguments, *, timeout=None):
            self._session.calls.append((tool_name, arguments))
            return None, False

        async def call_tool(self, tool_name, arguments, *, timeout=None):
            content, _ = await self.call_tool_with_meta(
                tool_name, arguments, timeout=timeout
            )
            return content

        monkeypatch.setattr(SseClient, "call_tool_with_meta", call_tool_with_meta, raising=False)
        monkeypatch.setattr(SseClient, "call_tool", call_tool, raising=False)
        apply_mcp_call_timeout_patch()

        seen: list[Any] = []
        real_lock = patch._client_io_lock

        def counting_lock(target: Any) -> Any:
            seen.append(target)
            return real_lock(target)

        monkeypatch.setattr(patch, "_client_io_lock", counting_lock)

        client = _make_sse_client()
        assert await asyncio.wait_for(client.call_tool("echo", {}), 1) is None
        assert client._session.calls == [("echo", {})]  # type: ignore[attr-defined]
        # 只允许进入一次 IO 临界区；否则 asyncio.Lock（不可重入）会死锁。
        assert seen == [client]

    @pytest.mark.asyncio
    async def test_streamable_owner_routes_meta_variant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
            StreamableHttpClient,
        )

        from jiuwenswarm.common import mcp_call_timeout_patch as patch

        async def call_tool_with_meta(self, tool_name, arguments, *, timeout=None):
            return None, False

        monkeypatch.setattr(
            StreamableHttpClient, "call_tool_with_meta", call_tool_with_meta, raising=False
        )
        apply_mcp_call_timeout_patch()
        client = StreamableHttpClient(McpServerConfig(
            server_id="meta-route", server_name="meta-route",
            server_path="http://127.0.0.1:9/mcp", client_type="streamable-http",
        ))
        patch._install_streamable_http_owner(client)
        # owner 必须把 meta 版本也挂到同一个串行队列上，不能走未串行的类方法。
        assert "call_tool_with_meta" in client.__dict__
        assert client.call_tool_with_meta is not StreamableHttpClient.call_tool_with_meta

    @pytest.mark.asyncio
    async def test_owner_routed_nested_meta_does_not_starve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner 路由 + ``call_tool`` 委托 ``call_tool_with_meta`` 不能饿死。

        外层调用经 owner 队列执行，``_invoke_once`` 的 ``wait_for`` 子任务里
        agent-core 的 ``call_tool`` 会委托 ``self.call_tool_with_meta``，而该属性
        是 owner 挂的实例级 routed。若 ``submit`` 只认 owner 任务，内层请求入队后
        永远等不到出队，只能空转到 timeout 再断连（merge-base 无此问题）。
        """
        from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
            StreamableHttpClient,
        )

        from jiuwenswarm.common import mcp_call_timeout_patch as patch

        calls: list[str] = []

        async def call_tool_with_meta(self, tool_name, arguments, *, timeout=None):
            calls.append(tool_name)
            return "payload", True

        async def call_tool(self, tool_name, arguments, *, timeout=None):
            content, _ = await self.call_tool_with_meta(
                tool_name, arguments, timeout=timeout
            )
            return content

        monkeypatch.setattr(
            StreamableHttpClient, "call_tool_with_meta", call_tool_with_meta, raising=False
        )
        monkeypatch.setattr(StreamableHttpClient, "call_tool", call_tool, raising=False)
        apply_mcp_call_timeout_patch()

        client = StreamableHttpClient(McpServerConfig(
            server_id="owner-nested", server_name="owner-nested",
            server_path="http://127.0.0.1:9/mcp", client_type="streamable-http",
        ))
        # 已有会话即跳过 _ensure_connected 的真实连接。
        client._session = object()
        patch._install_streamable_http_owner(client)
        monkeypatch.setattr(patch, "disconnect_remote_mcp_client", AsyncMock())

        owner = client._jws_http_owner
        try:
            result = await asyncio.wait_for(client.call_tool("echo", {}), 1)
        finally:
            # owner 任务在第一次 submit 时才创建。
            owner_task = owner.task
            if owner_task is not None:
                owner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await owner_task

        assert result == "payload"
        # 底层只执行一次：内层委托必须内联，不能再排一次队。
        assert calls == ["echo"]


@pytest.mark.asyncio
async def test_reapply_after_guard_reset_keeps_single_wrapper(monkeypatch):
    from mcp.client import sse, streamable_http
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    monkeypatch.setattr(patch, "_patch_ability_manager_fail_after", lambda: None)
    # 记录 guard 的初始对象，不能把本测试的 reset 泄漏给已预装补丁的进程。
    for name in ("_PATCHED", "_wrapped_methods", "_wrapped_connects", "_wrapped_factories"):
        monkeypatch.setattr(patch, name, getattr(patch, name))
    apply_mcp_call_timeout_patch()
    bindings = [
        (cls, name, getattr(cls, name))
        for cls in (SseClient, StreamableHttpClient)
        for name in ("connect", "disconnect", "call_tool", "list_tools")
    ] + [
        (SseClient, "_do_connect", SseClient._do_connect),
        (ToolMgr, "_create_client", ToolMgr._create_client),
        (sse, "sse_client", sse.sse_client),
        (streamable_http, "streamablehttp_client", streamable_http.streamablehttp_client),
    ]
    for _ in range(10):
        patch._PATCHED = False
        patch._wrapped_methods = set()
        patch._wrapped_connects = set()
        patch._wrapped_factories = set()
        apply_mcp_call_timeout_patch()
        assert all(getattr(target, name) is method for target, name, method in bindings)
    client = _make_sse_client()
    assert await asyncio.wait_for(client.call_tool("echo", {}), 1) is None
    assert client._session.calls == [("echo", {})]


@pytest.mark.asyncio
async def test_legacy_connect_cannot_swallow_timeout_cancellation(monkeypatch):
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    apply_mcp_call_timeout_patch()

    async def legacy_connect(self, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return False

    monkeypatch.setattr(StreamableHttpClient, "connect", legacy_connect)
    monkeypatch.setattr(patch, "_mcp_ssl_contexts", {patch._mcp_ssl_context_key(): object()})
    # 方法被替换而注册集合未变，必须以实际函数为准重新包装。
    patch._patch_sdk_read_timeouts()
    client = StreamableHttpClient(McpServerConfig(
        server_id="test", server_name="test", server_path="http://127.0.0.1:9/mcp",
        client_type="streamable-http",
    ))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await client.connect()


def _make_streamable_client():
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    return StreamableHttpClient(McpServerConfig(
        server_id="ssl-test", server_name="ssl-test",
        server_path="http://127.0.0.1:9/mcp", client_type="streamable-http",
    ))


def _ssl_context_of(client: Any) -> Any:
    return client._transport._pool._ssl_context


@pytest.fixture
def _record_transport(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from mcp.client import streamable_http
    from openjiuwen.core.runner import Runner

    seen: list[Any] = []

    @asynccontextmanager
    async def recording_transport(url, **kwargs):
        factory = kwargs.get("httpx_client_factory")
        client = factory() if factory is not None else None
        seen.append((factory, client))
        if client is not None:
            await client.aclose()
        raise RuntimeError("stop after factory")
        yield  # pragma: no cover

    monkeypatch.setattr(streamable_http, "streamablehttp_client", recording_transport)
    monkeypatch.setattr(Runner.callback_framework, "trigger", AsyncMock(return_value=[]))
    return seen


@pytest.mark.asyncio
async def test_shared_ssl_context_built_once_off_loop_and_reused(monkeypatch, _record_transport):
    import threading

    import httpx

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    monkeypatch.setattr(patch, "_mcp_ssl_contexts", {})
    builds: list[int] = []
    real_build = httpx.create_ssl_context

    def recording_build(*args, **kwargs):
        builds.append(threading.get_ident())
        return real_build(*args, **kwargs)

    monkeypatch.setattr(httpx, "create_ssl_context", recording_build)
    apply_mcp_call_timeout_patch()

    for _ in range(3):
        assert await _make_streamable_client().connect() is False

    assert len(builds) == 1, "SSL 上下文只应构建一次"
    assert builds[0] != threading.get_ident(), "SSL 上下文必须在工作线程里构建"
    shared = patch._cached_mcp_ssl_context()
    assert shared is not None and len(_record_transport) == 3
    for factory, client in _record_transport:
        assert getattr(factory, "_jws_mcp_shared_ssl", False)
        assert _ssl_context_of(client) is shared


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,timeout,with_auth",
    [(None, None, False), ({"x-officeclaw-pptx-capability": "cap"}, 12.5, True)],
)
async def test_shared_ssl_factory_matches_sdk_default(headers, timeout, with_auth):
    import httpx
    from mcp.shared._httpx_utils import create_mcp_http_client

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    ssl_context = httpx.create_ssl_context()
    httpx_timeout = httpx.Timeout(timeout) if timeout is not None else None
    auth = httpx.BasicAuth("u", "p") if with_auth else None
    ours = patch._mcp_http_client_factory(ssl_context)(headers=headers, timeout=httpx_timeout, auth=auth)
    sdk = create_mcp_http_client(headers=headers, timeout=httpx_timeout, auth=auth)
    try:
        assert ours.timeout == sdk.timeout
        assert dict(ours.headers) == dict(sdk.headers)
        assert ours.follow_redirects == sdk.follow_redirects
        assert ours.trust_env == sdk.trust_env
        assert type(ours.auth) is type(sdk.auth)
        assert _ssl_context_of(ours) is ssl_context
    finally:
        await ours.aclose()
        await sdk.aclose()


@pytest.mark.asyncio
async def test_shared_ssl_falls_back_to_sdk_default(monkeypatch, _record_transport):
    import httpx
    from mcp.client import streamable_http

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    monkeypatch.setattr(patch, "_mcp_ssl_contexts", {})

    def broken_build(*args, **kwargs):
        raise OSError("bad SSL_CERT_FILE")

    monkeypatch.setattr(httpx, "create_ssl_context", broken_build)
    apply_mcp_call_timeout_patch()

    assert await _make_streamable_client().connect() is False
    assert patch._mcp_ssl_contexts == {}
    assert _record_transport[-1] == (None, None), "构建失败时不得注入工厂"

    sentinel = object()
    monkeypatch.setattr(patch, "_mcp_ssl_contexts", {patch._mcp_ssl_context_key(): sentinel})
    mine = object()
    captured: dict[str, Any] = {}

    def fake_orig(url, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(streamable_http, "streamablehttp_client", fake_orig)
    patch._patch_sdk_read_timeouts()
    assert streamable_http.streamablehttp_client("http://x", httpx_client_factory=mine) == "ok"
    assert captured["httpx_client_factory"] is mine


def test_shared_ssl_cache_key_follows_cert_env(monkeypatch):
    import certifi

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(patch, "_mcp_ssl_contexts", {("", ""): "default"})
    assert patch._cached_mcp_ssl_context() == "default"
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    assert patch._cached_mcp_ssl_context() is None, "证书源变了不得复用旧上下文"
