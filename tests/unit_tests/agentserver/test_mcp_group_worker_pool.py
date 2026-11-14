# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""meta 方式的分组 worker 池：分发规则、占位归还、_meta 透传。"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest
from openjiuwen.core.foundation.tool import ToolCard

import jiuwenswarm.common.mcp_server_registry as registry_mod
from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_config import RequestScopedOfficeClawMcpTool
from jiuwenswarm.common.mcp_server_registry import (
    CachedServerRecord,
    GlobalMcpWorkerPool,
    McpRuntimeParam,
    McpServerDeclaration,
    McpServerEntry,
    UnknownMcpServerError,
    reset_mcp_server_registry_for_tests,
    runtime_delivery,
)

_PARAMS = {"command": "node", "args": ["server.js"], "env": {"A": "1"}}
_ECHO_SERVER = textwrap.dedent(
    """
    import json, os
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("echo")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name="echo", description="", inputSchema={"type": "object"})]

    @server.call_tool()
    async def call_tool(name, arguments):
        meta = server.request_context.meta
        data = meta.model_dump(exclude_none=True) if meta else {}
        await anyio.sleep(float(arguments.get("delay") or 0))
        return [types.TextContent(type="text", text=json.dumps({"meta": data, "pid": os.getpid()}))]

    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
    """
)


@pytest.fixture
def calls(monkeypatch) -> list:
    seen: list = []

    async def fake_run(params, worker):
        while True:
            req = await worker.queue.get()
            if req is None:
                return
            seen.append((worker, req.tool_name, req.meta))
            req.future.set_result("ok")

    monkeypatch.setattr(registry_mod, "_run_mcp_worker", fake_run)
    return seen


async def _settle() -> None:
    for _ in range(200):
        if not mcp_config._retire_tasks:
            return
        await asyncio.gather(*list(mcp_config._retire_tasks), return_exceptions=True)


def _register(registry, name: str, params: dict) -> None:
    registry._registry[name] = McpServerEntry(name=name, config={"command": params["command"]})
    registry._cache[name] = CachedServerRecord(
        name=name, tools=[], version=1, last_scan_at=0.0, last_scan_ok=True, connect_params=dict(params),
    )


def test_runtime_delivery_groups_by_value_digest() -> None:
    declaration = McpServerDeclaration(runtime_params=(
        McpRuntimeParam("CALL", "meta"), McpRuntimeParam("USER", "meta", group_key=True),
    ))
    via, group, owned = runtime_delivery(declaration, {"CALL": "c1", "USER": "u1"})
    assert via == "meta" and len(group) == 16 and "u1" not in group and not owned
    assert runtime_delivery(declaration, {"CALL": "c2", "USER": "u1"}) == (via, group, False)
    assert runtime_delivery(declaration, {"CALL": "c1", "USER": "u2"})[1] != group
    ungrouped = McpServerDeclaration(runtime_params=(McpRuntimeParam("CALL", "meta"),))
    assert runtime_delivery(ungrouped, {"CALL": "c1"}) == ("meta", "", False)
    assert runtime_delivery(McpServerDeclaration(), {}) == ("", "", False)


@pytest.mark.asyncio
async def test_group_fills_idle_workers_then_queues_on_least_busy(calls) -> None:
    pool = GlobalMcpWorkerPool()
    first = await pool.acquire_grouped("srv", _PARAMS, "g1", 2)
    second = await pool.acquire_grouped("srv", _PARAMS, "g1", 2)
    third = await pool.acquire_grouped("srv", _PARAMS, "g1", 2)
    other = await pool.acquire_grouped("srv", _PARAMS, "g2", 2)
    assert first is not second and third is first and other not in (first, second)
    assert (first.inflight, second.inflight, other.inflight) == (2, 1, 1)
    assert sorted(pool._workers) == ["srv#gg1#0", "srv#gg1#1", "srv#gg2#0"]
    second.inflight -= 1
    assert await pool.acquire_grouped("srv", _PARAMS, "g1", 2) is second
    await pool.close_all()


@pytest.mark.asyncio
async def test_reserved_call_forwards_meta_and_returns_the_slot(calls) -> None:
    pool = GlobalMcpWorkerPool()
    worker = await pool.acquire_grouped("srv", _PARAMS, "", 2)
    assert await worker.call_tool("echo", {}, meta={"CALL": "c1"}, reserved=True) == "ok"
    assert calls == [(worker, "echo", {"CALL": "c1"})] and worker.inflight == 0
    await mcp_config.retire_pooled_mcp_worker(worker, reason="test")
    reserved = await pool.acquire_grouped("srv", _PARAMS, "", 2, replace=worker)
    assert reserved is not worker and reserved.inflight == 1
    worker.inflight += 1
    with pytest.raises(mcp_config.McpWorkerRetiringError):
        await worker.call_tool("echo", {}, reserved=True)
    assert worker.inflight == 0
    await _settle()
    await pool.close_all()


@pytest.mark.asyncio
async def test_replace_and_fingerprint_change_retire_old_workers(calls) -> None:
    pool = GlobalMcpWorkerPool()
    failed = await pool.acquire_grouped("srv", _PARAMS, "g1", 2)
    failed.inflight -= 1
    fresh = await pool.acquire_grouped("srv", _PARAMS, "g1", 2, replace=failed)
    assert fresh is not failed and failed.retiring and failed.close_reason == "worker_replaced"
    fresh.inflight -= 1
    changed = await pool.acquire_grouped("srv", {**_PARAMS, "env": {"A": "2"}}, "g1", 2)
    assert changed is not fresh and fresh.retiring and list(pool._workers) == ["srv#gg1#0"]
    await _settle()
    await pool.close_all()


@pytest.mark.asyncio
async def test_registry_group_workers_follow_entry_lifecycle(calls) -> None:
    registry = reset_mcp_server_registry_for_tests()
    with pytest.raises(UnknownMcpServerError):
        await registry.acquire_group_worker("srv", "g1")
    _register(registry, "srv", _PARAMS)
    worker = await registry.acquire_group_worker("srv", "g1")
    assert worker.inflight == 1
    await registry.remove_servers(["srv"])
    assert registry.worker_pool._workers == {} and worker.retiring
    await _settle()


@pytest.mark.asyncio
async def test_stdio_server_receives_each_calls_own_meta(tmp_path) -> None:
    script = tmp_path / "echo_server.py"
    script.write_text(_ECHO_SERVER, encoding="utf-8")
    registry = reset_mcp_server_registry_for_tests()
    _register(registry, "echo", {"command": sys.executable, "args": [str(script)]})

    def tool(call: str, group: str) -> RequestScopedOfficeClawMcpTool:
        card = ToolCard(id=f"mcp-registry-request-{call}.echo.echo", name="echo", description="")
        return RequestScopedOfficeClawMcpTool(
            card, {}, call, "echo", use_global_pool=True,
            runtime_params={"CALL": call}, runtime_via="meta", runtime_group=group,
        )

    async def call(name: str, group: str) -> dict:
        result = await tool(name, group)._call_through_group_pool("echo", {"delay": 0.3})
        return json.loads(result.content[0].text)

    try:
        a1, a2, a3, b1 = await asyncio.gather(
            call("a1", "ga"), call("a2", "ga"), call("a3", "ga"), call("b1", "gb"),
        )
        assert [r["meta"] for r in (a1, a2, a3, b1)] == [
            {"CALL": "a1"}, {"CALL": "a2"}, {"CALL": "a3"}, {"CALL": "b1"},
        ]
        assert len({a1["pid"], a2["pid"], a3["pid"]}) == 2
        assert b1["pid"] not in {a1["pid"], a2["pid"]}
        assert all(w.inflight == 0 for w in registry.worker_pool._workers.values())
    finally:
        await registry.worker_pool.close_all()
