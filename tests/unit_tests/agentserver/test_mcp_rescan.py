# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""mcp.server.rescan 与自动复扫：合并、失败保留缓存、变更通知、经活动连接复扫。"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from types import SimpleNamespace

import pytest

import jiuwenswarm.common.mcp_server_registry as registry_mod
from jiuwenswarm.common.mcp_server_registry import (
    CachedServerRecord,
    McpRegistrySettings,
    McpServerEntry,
    McpServerRegistry,
    reset_mcp_server_registry_for_tests,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.dispatch import HANDLERS
from jiuwenswarm.server.handlers import mcp_servers as mcp_server_handlers

_TOKEN = {"name": "X-Token", "via": "header"}
_GROWING_SERVER = textwrap.dedent(
    """
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.stdio import stdio_server

    server = Server("growing")
    names = ["grow"]

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=n, description="", inputSchema={"type": "object"}) for n in names]

    @server.call_tool()
    async def call_tool(name, arguments):
        names.append(f"extra_{len(names)}")
        await server.request_context.session.send_tool_list_changed()
        return [types.TextContent(type="text", text="ok")]

    async def main():
        options = server.create_initialization_options(NotificationOptions(tools_changed=True))
        async with stdio_server() as (read, write):
            await server.run(read, write, options)

    anyio.run(main)
    """
)


def _remote(name: str = "conn", **extra) -> dict:
    return {"name": name, "type": "streamable-http", "url": "https://example.com/mcp", **extra}


def _defs(*names: str) -> list[dict]:
    return [{"name": n, "description": "", "input_params": {}} for n in names]


@pytest.fixture
def source(monkeypatch) -> SimpleNamespace:
    state = SimpleNamespace(tools=["a"], fail=False, configs=[], gate=None)

    async def discover(name, config):
        state.configs.append(dict(config))
        if state.gate is not None:
            await state.gate.wait()
        if state.fail:
            raise RuntimeError("connection refused")
        return _defs(*state.tools), {"_mcp_client_type": "streamable-http", "auth_headers": dict(
            config.get("auth_headers") or config.get("headers") or {})}

    monkeypatch.setattr(registry_mod, "list_request_mcp_server_tools", discover)
    return state


async def _until(predicate, *, seconds: float = 10.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_rescan_reports_changes_and_keeps_cache_on_failure(source) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([_remote(runtime_params=[{**_TOKEN, "required": False}])])
    source.tools = ["a", "b"]
    (changed,) = await registry.rescan_servers(["conn"], {"conn": {"X-Token": "scan-secret"}})
    assert changed == {"name": "conn", "ok": True, "changed": True, "tools_count": 2, "version": 2}
    assert source.configs[-1]["auth_headers"] == {"X-Token": "scan-secret"}
    assert "scan-secret" not in repr(registry._cache["conn"])
    (same,) = await registry.rescan_servers(["conn"])
    assert (same["changed"], same["version"]) == (False, 2)
    source.fail = True
    failed, missing, undeclared = await registry.rescan_servers(
        ["conn", "missing", "conn"], {"conn": {"Other": "x"}},
    )
    assert failed["code"] == "INVALID_RUNTIME_PARAMS" and undeclared == failed
    assert missing == {"name": "missing", "ok": False, "error": "not found"}
    (down,) = await registry.rescan_servers(["conn"])
    assert (down["ok"], down["code"], down["error"]) == (False, "SCAN_FAILED", "connection refused")
    record = registry._cache["conn"]
    assert [t["name"] for t in record.tools] == ["a", "b"] and record.version == 2


@pytest.mark.asyncio
async def test_concurrent_rescans_share_one_scan(source) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([_remote()])
    source.configs.clear()
    source.tools, source.gate = ["a", "b"], asyncio.Event()
    first = asyncio.create_task(registry.rescan_servers(["conn"]))
    second = asyncio.create_task(registry.rescan_servers(["conn"]))
    await _until(lambda: source.configs)
    cancelled = asyncio.create_task(registry.rescan_servers(["conn"]))
    await asyncio.sleep(0)
    cancelled.cancel()
    source.gate.set()
    assert await first == await second == [
        {"name": "conn", "ok": True, "changed": True, "tools_count": 2, "version": 2},
    ]
    assert len(source.configs) == 1 and registry._rescans == {}


@pytest.mark.asyncio
async def test_periodic_scan_uses_an_active_connection_for_credentialed_servers(source, monkeypatch) -> None:
    async def fake_run(params, worker):
        while (req := await worker.queue.get()) is not None:
            req.future.set_result(_defs("a", "live") if req.list_tools else "ok")

    monkeypatch.setattr(registry_mod, "_run_mcp_worker", fake_run)
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([_remote(runtime_params=[_TOKEN], scan_params={"X-Token": "t"})])
    source.configs.clear()
    await registry.scan_once()
    assert source.configs == [] and registry._cache["conn"].version == 1
    await registry.acquire_worker("conn", via="header", values={"X-Token": "t"}, variant="v1")
    await registry.scan_once()
    record = registry._cache["conn"]
    assert source.configs == [] and [t["name"] for t in record.tools] == ["a", "live"]
    assert record.version == 2 and record.connect_params["auth_headers"] == {}
    await registry.worker_pool.close_all()


@pytest.mark.asyncio
async def test_stdio_list_changed_and_worker_rebuild_trigger_rescan(tmp_path) -> None:
    script = tmp_path / "growing_server.py"
    script.write_text(_GROWING_SERVER, encoding="utf-8")
    registry = reset_mcp_server_registry_for_tests()
    registry._registry["grow"] = McpServerEntry(name="grow", config={"command": sys.executable})
    registry._cache["grow"] = CachedServerRecord(
        name="grow", tools=_defs("grow"), version=1, last_scan_at=0.0, last_scan_ok=True,
        connect_params={"command": sys.executable, "args": [str(script)]},
    )
    try:
        worker = await registry.acquire_group_worker("grow", "")
        await worker.call_tool("grow", {}, reserved=True)
        await _until(lambda: registry._cache["grow"].version == 2)
        assert [t["name"] for t in registry._cache["grow"].tools] == ["grow", "extra_1"]
        fresh = await registry.acquire_group_worker("grow", "", replace=worker)
        fresh.inflight -= 1
        await _until(lambda: registry._cache["grow"].version == 3)
        assert [t["name"] for t in registry._cache["grow"].tools] == ["grow"]
    finally:
        await registry.stop_scanner()


@pytest.mark.asyncio
async def test_rescan_handler_validates_shape_and_returns_results(source, monkeypatch) -> None:
    assert HANDLERS[ReqMethod.MCP_SERVER_RESCAN].fn is mcp_server_handlers.handle_mcp_server_rescan
    assert ReqMethod.MCP_SERVER_RESCAN.value == "mcp.server.rescan"
    assert registry_mod.mcp_registry_capabilities()["rescan"] is True
    registry = reset_mcp_server_registry_for_tests()
    await registry.add_servers([_remote()])
    monkeypatch.setattr(
        mcp_server_handlers, "encode_agent_response_for_wire",
        lambda resp, response_id: {"ok": resp.ok, "payload": resp.payload},
    )
    sent: list[dict] = []

    async def send_wire(wire):
        sent.append(wire)

    def ctx(params):
        request = SimpleNamespace(request_id="r", channel_id="c", params=params)
        return SimpleNamespace(request=request, sink=SimpleNamespace(send_wire=send_wire), connection_id="ws")

    await mcp_server_handlers.handle_mcp_server_rescan(ctx({"names": "conn"}))
    await mcp_server_handlers.handle_mcp_server_rescan(ctx({"names": ["conn"], "scan_params": []}))
    await mcp_server_handlers.handle_mcp_server_rescan(ctx({"names": ["conn"]}))
    assert [s["ok"] for s in sent] == [False, False, True]
    assert sent[2]["payload"]["results"] == [
        {"name": "conn", "ok": True, "changed": False, "tools_count": 1, "version": 1},
    ]


@pytest.mark.asyncio
async def test_worker_rescan_loop_keeps_queued_rescans_after_a_failed_round(monkeypatch) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    calls = []

    async def flaky(name, worker, reason):
        calls.append(reason)
        if reason == "list_changed":
            registry._worker_rescan_again[name] = (worker, "rebuilt")
            raise RuntimeError("bad tool definition")
        return True

    monkeypatch.setattr(registry, "_rescan_via_worker", flaky)
    await registry._worker_rescan_loop("conn", object(), "list_changed")
    assert calls == ["list_changed", "rebuilt"] and registry._worker_rescan_again == {}


@pytest.mark.asyncio
async def test_remote_worker_rescan_caches_sdk_tool_cards_as_dicts(source, monkeypatch) -> None:
    from openjiuwen.core.foundation.tool.mcp.base import McpToolCard

    import jiuwenswarm.common.mcp_config as config_mod

    class CardClient:
        def __init__(self, _cfg) -> None:
            self._owner_task, self._session, self._client, self._is_disconnected = None, None, None, False

        async def connect(self, *, timeout=None) -> bool:
            return True

        async def list_tools(self) -> list:
            return [McpToolCard(name=n, server_name="conn", input_params={"type": "object"}) for n in ("a", "live")]

        async def disconnect(self, *, timeout=None) -> bool:
            self._is_disconnected = True
            return True

    monkeypatch.setattr(config_mod, "_remote_mcp_client_cls", lambda _client_type: CardClient)
    registry = McpServerRegistry(McpRegistrySettings())
    server = _remote(runtime_params=[_TOKEN], scan_params={"X-Token": "t"}, tool_filter=["a", "live"])
    assert (await registry.add_servers([server]))[0]["ok"] is True
    try:
        await registry.acquire_worker("conn", via="header", values={"X-Token": "t"}, variant="v1")
        await registry.scan_once()
        record = registry._cache["conn"]
        assert record.version == 2 and all(isinstance(t, dict) for t in record.tools)
        ((_, tools, _),) = await registry.snapshot_for_chat(["conn"])
        assert [(t["name"], t["input_params"]) for t in tools] == [("a", {"type": "object"}), ("live", {"type": "object"})]
    finally:
        await registry.worker_pool.close_all()
