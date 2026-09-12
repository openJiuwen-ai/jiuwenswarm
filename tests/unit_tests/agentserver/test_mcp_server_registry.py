# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the process-local MCP server registry (mcp.server.* / mcp_server_list)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.common.mcp_config import (
    MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX,
    RequestScopedOfficeClawMcpTool,
    bind_active_office_claw_mcp_tools,
    ensure_request_scoped_mcp_tool_allowed,
    is_request_scoped_mcp_tool_id,
)
from jiuwenswarm.common.mcp_server_registry import (
    DisabledMcpServerError,
    McpRegistryChatError,
    McpRegistrySettings,
    McpServerRegistry,
    UnknownMcpServerError,
    extract_mcp_server_list,
    get_mcp_server_registry,
    redact_mcp_config,
    reset_mcp_server_registry_for_tests,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.handlers import mcp_servers as mcp_server_handlers
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    _FORWARD_NO_LOCAL_HANDLER_METHODS,
    _FORWARD_REQ_METHODS,
)


async def _wait_until(predicate, *, turns: int = 20) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("timed out waiting for worker task to start")


def _stdio_cfg(name: str = "chrome-devtools") -> dict:
    return {"name": name, "command": "node", "args": ["mcp.js"]}


def _remote_cfg(name: str = "qichacha") -> dict:
    return {
        "name": name,
        "type": "streamable-http",
        "url": "https://example.com/mcp",
        "auth_headers": {"Authorization": "Bearer secret"},
    }


async def _ok_discover(name, config):
    return (
        [{"name": f"{name}_tool", "description": "d", "input_params": {}}],
        {"_mcp_client_type": "stdio", "command": "node", "args": ["mcp.js"]},
    )


@pytest.fixture
def registry() -> McpServerRegistry:
    return McpServerRegistry(
        settings=McpRegistrySettings(
            scan_interval_s=600,
            scan_concurrency=3,
            worker_idle_ttl_s=600,
            scan_fail_threshold=3,
        )
    )


@pytest.mark.asyncio
async def test_add_writes_cache(registry: McpServerRegistry, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    results = await registry.add_servers([_stdio_cfg()])
    assert results == [
        {
            "name": "chrome-devtools",
            "ok": True,
            "tools": ["chrome-devtools_tool"],
            "tools_count": 1,
        }
    ]
    listed = await registry.list_servers()
    assert listed[0]["name"] == "chrome-devtools"
    assert listed[0]["version"] == 1
    got = await registry.get_server("chrome-devtools")
    assert got is not None
    assert got["tools"][0]["name"] == "chrome-devtools_tool"


@pytest.mark.asyncio
async def test_add_duplicate_and_invalid(registry: McpServerRegistry, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])
    again = await registry.add_servers([_stdio_cfg(), {"name": "bad", "command": "node", "args": ["-e", "1"]}])
    assert again[0]["ok"] is False
    assert again[0]["error"] == "already exists"
    assert again[1]["ok"] is False
    assert "安全拦截" in again[1]["error"] or "dangerous" in again[1]["error"].lower() or "-e" in again[1]["error"]


@pytest.mark.asyncio
async def test_add_rejects_private_remote_url_without_scanning(registry, monkeypatch) -> None:
    called: list[str] = []

    async def discover(name, config):
        called.append(name)
        return [], {}

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    results = await registry.add_servers(
        [{"name": "ssrf", "type": "streamable-http", "url": "http://10.0.0.1/mcp"}]
    )
    assert results[0]["ok"] is False
    assert "内网" in results[0]["error"] or "SSRF" in results[0]["error"]
    assert called == []
    assert await registry.list_servers() == []


@pytest.mark.asyncio
async def test_add_scan_failure_does_not_write(registry: McpServerRegistry, monkeypatch) -> None:
    async def fail_discover(name, config):
        return [], {}

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        fail_discover,
    )
    results = await registry.add_servers([_stdio_cfg()])
    assert results[0]["ok"] is False
    assert await registry.list_servers() == []


@pytest.mark.asyncio
async def test_remove_and_update(registry: McpServerRegistry, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])
    removed = await registry.remove_servers(["chrome-devtools", "missing"])
    assert removed[0]["ok"] is True
    assert removed[1]["error"] == "not found"

    await registry.add_servers([_stdio_cfg()])
    same = await registry.update_servers([_stdio_cfg()])
    assert same[0]["ok"] is True
    assert same[0].get("skipped") is True

    async def new_discover(name, config):
        return (
            [{"name": "new_tool", "description": "", "input_params": {}}],
            {"_mcp_client_type": "stdio", "command": "node", "args": ["other.js"]},
        )

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        new_discover,
    )
    changed = await registry.update_servers(
        [{"name": "chrome-devtools", "command": "node", "args": ["other.js"]}]
    )
    assert changed[0]["ok"] is True
    got = await registry.get_server("chrome-devtools")
    assert got is not None
    assert got["version"] == 2
    assert got["tools"][0]["name"] == "new_tool"


@pytest.mark.asyncio
async def test_update_scan_fail_keeps_old(registry: McpServerRegistry, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])

    async def fail_discover(name, config):
        return [], {}

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        fail_discover,
    )
    updated = await registry.update_servers(
        [{"name": "chrome-devtools", "command": "node", "args": ["other.js"]}]
    )
    assert updated[0]["ok"] is False
    got = await registry.get_server("chrome-devtools")
    assert got is not None
    assert got["version"] == 1
    assert got["tools"][0]["name"] == "chrome-devtools_tool"


@pytest.mark.asyncio
async def test_scanner_skips_stdio_and_updates_remote(
    registry: McpServerRegistry, monkeypatch
) -> None:
    calls: list[str] = []

    async def discover(name, config):
        calls.append(name)
        tools = [{"name": f"{name}_v{len(calls)}", "description": "", "input_params": {}}]
        return tools, {"_mcp_client_type": "streamable-http", "server_path": "https://example.com/mcp"}

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    await registry.add_servers([_stdio_cfg(), _remote_cfg()])
    calls.clear()
    await registry.scan_once()
    assert calls == ["qichacha"]
    first = await registry.get_server("qichacha")
    assert first is not None
    version_after_add = first["version"]
    await registry.scan_once()
    second = await registry.get_server("qichacha")
    assert second is not None
    assert second["version"] == version_after_add + 1
    stdio = await registry.get_server("chrome-devtools")
    assert stdio is not None
    assert stdio["version"] == 1


@pytest.mark.asyncio
async def test_scanner_failure_keeps_tools(registry: McpServerRegistry, monkeypatch) -> None:
    async def discover(name, config):
        return (
            [{"name": "live", "description": "", "input_params": {}}],
            {"_mcp_client_type": "streamable-http"},
        )

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    await registry.add_servers([_remote_cfg()])

    async def fail(name, config):
        return [], {}

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        fail,
    )
    await registry.scan_once()
    got = await registry.get_server("qichacha")
    assert got is not None
    assert got["last_scan_ok"] is False
    assert got["tools"][0]["name"] == "live"


def test_redact_auth_headers() -> None:
    masked = redact_mcp_config(_remote_cfg())
    assert masked["auth_headers"] == {"Authorization": "***"}
    assert masked["url"] == "https://example.com/mcp"


def test_extract_mcp_server_list() -> None:
    assert extract_mcp_server_list({}) is None
    assert extract_mcp_server_list({"query": "hi"}) is None
    assert extract_mcp_server_list({"mcp_server_list": []}) == []
    assert extract_mcp_server_list({"mcp_server_list": None}) == []
    assert extract_mcp_server_list({"mcp_server_list": ["a", "a", " b "]}) == ["a", "b"]
    with pytest.raises(McpRegistryChatError):
        extract_mcp_server_list({"mcp_server_list": "chrome"})


@pytest.mark.asyncio
async def test_snapshot_unknown_and_empty(registry: McpServerRegistry, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])
    snap = await registry.snapshot_for_chat(["chrome-devtools"])
    assert snap[0][0] == "chrome-devtools"
    with pytest.raises(UnknownMcpServerError):
        await registry.snapshot_for_chat(["missing"])


@pytest.mark.asyncio
async def test_chat_prefers_mcp_server_list(monkeypatch) -> None:
    reset_mcp_server_registry_for_tests()
    registry = reset_mcp_server_registry_for_tests()
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=SimpleNamespace())
    adapter._active_office_claw_mcp = None

    seen: dict[str, object] = {}

    async def fake_registry(self, request, names):
        seen["names"] = names
        return None

    async def fake_legacy(*args, **kwargs):
        raise AssertionError("legacy path must not run")

    monkeypatch.setattr(JiuWenSwarmDeepAdapter, "_register_mcp_from_registry", fake_registry)
    request = AgentRequest(
        request_id="r1",
        channel_id="officeclaw",
        session_id="s1",
        params={
            "query": "hi",
            "mcp_server_list": ["chrome-devtools"],
            "office_claw_mcp": {"command": "node", "args": ["x.js"], "cwd": "/tmp"},
        },
    )
    result = await adapter.register_request_scoped_office_claw_mcp(request)
    assert result is None
    assert seen["names"] == ["chrome-devtools"]


@pytest.mark.asyncio
async def test_chat_empty_list_skips_legacy(monkeypatch) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace()
    request = AgentRequest(
        request_id="r1",
        channel_id="officeclaw",
        session_id="s1",
        params={
            "mcp_server_list": [],
            "office_claw_mcp": {"command": "node", "args": ["x.js"], "cwd": "/tmp"},
        },
    )
    result = await adapter.register_request_scoped_office_claw_mcp(request)
    assert result is None


@pytest.mark.asyncio
async def test_chat_unknown_server_raises(monkeypatch) -> None:
    reset_mcp_server_registry_for_tests()
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=SimpleNamespace())
    adapter._active_office_claw_mcp = None
    request = AgentRequest(
        request_id="r1",
        channel_id="officeclaw",
        session_id="s1",
        params={"mcp_server_list": ["no-such"]},
    )
    with pytest.raises(UnknownMcpServerError):
        await adapter.register_request_scoped_office_claw_mcp(request)


@pytest.mark.asyncio
async def test_global_pool_reuses_worker() -> None:
    registry = McpServerRegistry()
    params = {"_mcp_client_type": "stdio", "command": "node", "args": ["x.js"]}

    created = []

    async def fake_run(params, worker):
        created.append(worker)
        await worker.queue.get()

    with patch("jiuwenswarm.common.mcp_server_registry._run_mcp_worker", fake_run):
        w1 = await registry.worker_pool.acquire("chrome-devtools", params)
        w2 = await registry.worker_pool.acquire("chrome-devtools", params)
        assert w1 is w2
        await registry.worker_pool.close_all()


@pytest.mark.asyncio
async def test_global_pool_ttl_reap() -> None:
    registry = McpServerRegistry(
        settings=McpRegistrySettings(worker_idle_ttl_s=1, scan_interval_s=600, scan_concurrency=1, scan_fail_threshold=3)
    )
    params = {"_mcp_client_type": "stdio", "command": "node", "args": ["x.js"]}

    async def fake_run(params, worker):
        await worker.queue.get()

    with patch("jiuwenswarm.common.mcp_server_registry._run_mcp_worker", fake_run):
        worker = await registry.worker_pool.acquire("chrome-devtools", params)
        worker.last_used = 0.0
        await registry.worker_pool.reap_idle(1)
        assert registry.worker_pool._workers == {}


@pytest.mark.asyncio
async def test_invoke_after_remove_does_not_rebuild_worker(monkeypatch) -> None:
    """remove 关掉 worker 后，已分发工具不得凭旧 params 再拉起进程。"""

    registry = reset_mcp_server_registry_for_tests()
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])
    card = ToolCard(
        id=f"{MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX}abc.chrome-devtools.t",
        name="t",
        description="",
        input_params={},
    )
    tool = RequestScopedOfficeClawMcpTool(
        card,
        {"_mcp_client_type": "stdio", "command": "node", "args": ["mcp.js"]},
        "req",
        "chrome-devtools",
        use_global_pool=True,
    )
    started: list[dict] = []

    async def fake_run(params, worker):
        started.append(dict(params))
        await worker.queue.get()

    with patch("jiuwenswarm.common.mcp_server_registry._run_mcp_worker", fake_run):
        first = await tool._acquire_mcp_session()
        assert first is not None
        await _wait_until(lambda: len(started) == 1)
        await registry.remove_servers(["chrome-devtools"])
        assert registry.worker_pool._workers == {}
        with pytest.raises(UnknownMcpServerError):
            await tool._acquire_mcp_session()
        assert registry.worker_pool._workers == {}
        assert len(started) == 1


@pytest.mark.asyncio
async def test_invoke_disabled_server_refuses(monkeypatch) -> None:
    registry = reset_mcp_server_registry_for_tests()
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    await registry.add_servers([_stdio_cfg()])
    registry._registry["chrome-devtools"].enabled = False
    with pytest.raises(DisabledMcpServerError):
        await registry.acquire_worker("chrome-devtools")
    assert registry.worker_pool._workers == {}


@pytest.mark.asyncio
async def test_invoke_after_update_uses_new_connect_params(monkeypatch) -> None:
    """update 关掉旧 worker 后，invoke 必须用新缓存连接参数，不能连回旧端点。"""

    registry = reset_mcp_server_registry_for_tests()

    async def discover(name, config):
        args = list(config.get("args") or [])
        return (
            [{"name": "t", "description": "", "input_params": {}}],
            {"_mcp_client_type": "stdio", "command": "node", "args": args},
        )

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    await registry.add_servers([_stdio_cfg()])
    card = ToolCard(
        id=f"{MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX}abc.chrome-devtools.t",
        name="t",
        description="",
        input_params={},
    )
    tool = RequestScopedOfficeClawMcpTool(
        card,
        {"_mcp_client_type": "stdio", "command": "node", "args": ["mcp.js"]},
        "req",
        "chrome-devtools",
        use_global_pool=True,
    )
    seen_args: list[list] = []

    async def fake_run(params, worker):
        seen_args.append(list(params.get("args") or []))
        await worker.queue.get()

    with patch("jiuwenswarm.common.mcp_server_registry._run_mcp_worker", fake_run):
        await tool._acquire_mcp_session()
        await _wait_until(lambda: seen_args == [["mcp.js"]])
        await registry.update_servers(
            [{"name": "chrome-devtools", "command": "node", "args": ["other.js"]}]
        )
        await tool._acquire_mcp_session()
        await _wait_until(lambda: seen_args == [["mcp.js"], ["other.js"]])
        await registry.worker_pool.close_all()


def test_tool_global_pool_flag() -> None:
    card = ToolCard(id="office-claw-request-abc.chrome.t", name="t", description="", input_params={})
    tool = RequestScopedOfficeClawMcpTool(card, {"command": "node"}, "req", "chrome", use_global_pool=True)
    assert tool._use_global_pool is True


def _playwright_cfg(name: str = "pw") -> dict:
    return {
        "name": name,
        "type": "playwright",
        "url": "http://127.0.0.1:3003/sse",
    }


def _sse_cfg(name: str = "remote-sse") -> dict:
    return {"name": name, "type": "sse", "url": "https://example.com/sse"}


class _AbilityManager:
    def __init__(self) -> None:
        self.cards: dict[str, object] = {}

    def get(self, name: str) -> object | None:
        return self.cards.get(name)

    def add(self, card: object) -> SimpleNamespace:
        self.cards[str(getattr(card, "name"))] = card
        return SimpleNamespace(added=True, reason="added_tool")

    def remove(self, name: str) -> None:
        self.cards.pop(name, None)


class _ResourceManager:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.removed: list[str] = []

    def add_tool(self, tool: object, *, tag: str) -> None:
        assert tag == "office-claw"
        card = getattr(tool, "card")
        self.tools[str(card.id)] = tool

    def remove_tool(self, tool_id: str) -> None:
        self.removed.append(tool_id)
        self.tools.pop(tool_id, None)


def _bare_session_adapter() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager())
    adapter._active_office_claw_mcp = None
    adapter._active_office_claw_tool_instances = ()
    adapter._progressive_tool_rail = None
    return adapter


def _handler_ctx(params: dict, sent: list) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(
            params=params,
            request_id="req-1",
            channel_id="web",
        ),
        sink=SimpleNamespace(send_wire=AsyncMock(side_effect=lambda wire: sent.append(wire))),
    )


@pytest.mark.asyncio
async def test_scanner_skips_playwright_and_scans_sse(
    registry: McpServerRegistry, monkeypatch
) -> None:
    calls: list[str] = []

    async def discover(name, config):
        calls.append(name)
        return (
            [{"name": f"{name}_tool", "description": "", "input_params": {}}],
            {"_mcp_client_type": str(config.get("type") or "stdio")},
        )

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    await registry.add_servers([_playwright_cfg(), _sse_cfg(), _stdio_cfg()])
    calls.clear()
    await registry.scan_once()
    assert calls == ["remote-sse"]


def test_request_scoped_allowlist_covers_registry_prefix() -> None:
    tool_id = f"{MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX}abc.chrome.t"
    assert is_request_scoped_mcp_tool_id(tool_id)
    with pytest.raises(RuntimeError, match="without an active request binding"):
        ensure_request_scoped_mcp_tool_allowed(tool_id)
    with bind_active_office_claw_mcp_tools([tool_id]):
        ensure_request_scoped_mcp_tool_allowed(tool_id)


def test_web_gateway_forwards_mcp_server_methods() -> None:
    for method in (
        "mcp.server.add",
        "mcp.server.remove",
        "mcp.server.update",
        "mcp.server.list",
        "mcp.server.get",
    ):
        assert method in _FORWARD_REQ_METHODS
        assert method in _FORWARD_NO_LOCAL_HANDLER_METHODS


@pytest.mark.asyncio
async def test_crud_handlers_batch_partial_failure(monkeypatch) -> None:
    reset_mcp_server_registry_for_tests()
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        _ok_discover,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.mcp_servers.encode_agent_response_for_wire",
        lambda resp, response_id: {"ok": resp.ok, "payload": resp.payload},
    )

    sent: list = []
    await mcp_server_handlers.handle_mcp_server_add(
        _handler_ctx(
            {
                "servers": [
                    _stdio_cfg(),
                    _stdio_cfg(),
                    {"name": "bad", "command": "node", "args": ["-e", "1"]},
                ]
            },
            sent,
        )
    )
    results = sent[0]["payload"]["results"]
    assert sent[0]["ok"] is True
    assert results[0]["ok"] is True
    assert results[1]["error"] == "already exists"
    assert results[2]["ok"] is False

    sent.clear()
    await mcp_server_handlers.handle_mcp_server_list(_handler_ctx({}, sent))
    assert sent[0]["payload"]["servers"][0]["name"] == "chrome-devtools"

    sent.clear()
    await mcp_server_handlers.handle_mcp_server_get(
        _handler_ctx({"name": "chrome-devtools"}, sent)
    )
    assert sent[0]["payload"]["server"]["tools"][0]["name"] == "chrome-devtools_tool"

    sent.clear()
    await mcp_server_handlers.handle_mcp_server_remove(
        _handler_ctx({"names": ["chrome-devtools", "missing"]}, sent)
    )
    assert sent[0]["payload"]["results"][0]["ok"] is True
    assert sent[0]["payload"]["results"][1]["error"] == "not found"


@pytest.mark.asyncio
async def test_chat_registry_path_skips_discovery_and_keeps_global_worker(
    monkeypatch,
) -> None:
    from jiuwenswarm.common.mcp_config import _clear_live_office_claw_allowlists_for_tests

    registry = reset_mcp_server_registry_for_tests()
    discover_calls: list[str] = []

    async def discover(name, config):
        discover_calls.append(name)
        return (
            [{"name": "page_snapshot", "description": "snap", "input_params": {}}],
            {"_mcp_client_type": "stdio", "command": "node", "args": ["mcp.js"]},
        )

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    await registry.add_servers([_stdio_cfg()])
    assert discover_calls == ["chrome-devtools"]
    discover_calls.clear()

    async def boom(*_args, **_kwargs):
        raise AssertionError("chat.send must not call list_tools")

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        boom,
    )
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(side_effect=AssertionError("legacy office-claw discovery must not run")),
    )
    monkeypatch.setattr(
        interface_deep,
        "list_request_mcp_server_tools",
        AsyncMock(side_effect=AssertionError("legacy connector discovery must not run")),
    )

    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    _clear_live_office_claw_allowlists_for_tests()
    adapter = _bare_session_adapter()
    request = AgentRequest(
        request_id="r-chat",
        channel_id="officeclaw",
        session_id="s-chat",
        params={
            "query": "hi",
            "mcp_server_list": ["chrome-devtools"],
            "office_claw_mcp": {"command": "node", "args": ["x.js"], "cwd": "/tmp"},
        },
    )
    registration = await adapter.register_request_scoped_office_claw_mcp(request)
    assert registration is not None
    assert discover_calls == []
    assert registration.tool_names == ("page_snapshot",)
    assert registration.tool_ids[0].startswith(MCP_REGISTRY_REQUEST_TOOL_ID_PREFIX)
    tool = resource_manager.tools[registration.tool_ids[0]]
    assert tool._use_global_pool is True

    async def fake_run(params, worker):
        await worker.queue.get()

    with patch("jiuwenswarm.common.mcp_server_registry._run_mcp_worker", fake_run):
        worker = await tool._acquire_mcp_session()
        await adapter.cleanup_request_scoped_office_claw_mcp(registration)
        pool = get_mcp_server_registry().worker_pool
        reused = await pool.acquire(tool._server_name, tool._params)
        assert reused is worker
        await pool.close_all()
    _clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_chat_without_mcp_server_list_uses_legacy(monkeypatch) -> None:
    """A13: omitting mcp_server_list still runs the old office_claw_mcp path."""

    adapter = _bare_session_adapter()
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    legacy = AsyncMock(
        return_value=[
            {
                "name": "office_claw_post_message",
                "description": "post",
                "input_params": {},
            }
        ]
    )
    monkeypatch.setattr(interface_deep, "list_office_claw_mcp_tools", legacy)
    monkeypatch.setattr(
        interface_deep,
        "validate_office_claw_mcp_config",
        lambda config, environ=None: {
            "command": "node",
            "args": ["mcp.js"],
            "cwd": "/tmp",
            "env": {},
        },
    )
    request = AgentRequest(
        request_id="r-legacy",
        channel_id="officeclaw",
        session_id="s-legacy",
        params={
            "query": "hi",
            "office_claw_mcp": {"command": "node", "args": ["mcp.js"], "cwd": "/tmp"},
        },
    )
    registration = await adapter.register_request_scoped_office_claw_mcp(request)
    assert registration is not None
    assert legacy.await_count == 1
    assert "office_claw_post_message" in registration.tool_names


@pytest.mark.asyncio
async def test_scanner_start_stop_lifecycle() -> None:
    """A18: scanner task starts with AgentServer runtime and stops cleanly."""

    registry = McpServerRegistry(
        settings=McpRegistrySettings(scan_interval_s=60, scan_concurrency=1, worker_idle_ttl_s=60)
    )
    registry.start_scanner()
    assert registry._scanner_task is not None
    assert not registry._scanner_task.done()
    await registry.stop_scanner()
    assert registry._scanner_task is None


def test_cached_tools_equal_ignores_order() -> None:
    from jiuwenswarm.common.mcp_server_registry import cached_tools_equal

    a = [
        {"name": "beta", "description": "b", "input_params": {}},
        {"name": "alpha", "description": "a", "input_params": {"x": 1}},
    ]
    b = [
        {"name": "alpha", "description": "a", "input_params": {"x": 1}},
        {"name": "beta", "description": "b", "input_params": {}},
    ]
    assert cached_tools_equal(a, b) is True
    c = [
        {"name": "alpha", "description": "changed", "input_params": {"x": 1}},
        {"name": "beta", "description": "b", "input_params": {}},
    ]
    assert cached_tools_equal(a, c) is False


@pytest.mark.asyncio
async def test_scan_does_not_bump_version_when_only_tool_order_changes(
    registry: McpServerRegistry, monkeypatch
) -> None:
    calls = {"n": 0}

    async def discover(name, config):
        calls["n"] += 1
        if calls["n"] == 1:
            tools = [
                {"name": "a", "description": "", "input_params": {}},
                {"name": "b", "description": "", "input_params": {}},
            ]
        else:
            tools = [
                {"name": "b", "description": "", "input_params": {}},
                {"name": "a", "description": "", "input_params": {}},
            ]
        return tools, {"_mcp_client_type": "streamable-http", "server_path": "https://example.com/mcp"}

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_server_registry.list_request_mcp_server_tools",
        discover,
    )
    await registry.add_servers([_remote_cfg()])
    first = await registry.get_server("qichacha")
    assert first is not None
    version_after_add = first["version"]
    await registry.scan_once()
    second = await registry.get_server("qichacha")
    assert second is not None
    assert second["version"] == version_after_add


def test_registry_scan_timeout_stays_30s() -> None:
    from jiuwenswarm.common.mcp_server_registry import _MCP_SCAN_TIMEOUT_S
    from jiuwenswarm.common import mcp_config

    assert _MCP_SCAN_TIMEOUT_S == 30.0
    assert mcp_config._MCP_CALL_TOOL_TIMEOUT_S == 300.0
    assert mcp_config._MCP_CONNECTOR_DISCOVERY_TIMEOUT_S == 300.0
