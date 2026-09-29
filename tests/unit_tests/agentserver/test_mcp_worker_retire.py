# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retire-not-cancel semantics for the global MCP worker pool.

Covers the config-update race: while an MCP call is in flight, an
``mcp.server.update`` (e.g. IAM re-signature) must let the in-flight call
finish and only affect subsequent calls.
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.mcp_config import (
    McpWorkerRetiringError,
    McpWorkerStoppedError,
)
from jiuwenswarm.common.mcp_server_registry import (
    GlobalMcpWorkerPool,
    reset_mcp_server_registry_for_tests,
)


async def _wait_until(predicate, *, turns: int = 200) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("timed out waiting for worker state")


async def _drain_retire_tasks() -> None:
    from jiuwenswarm.common import mcp_config

    for _ in range(200):
        if not mcp_config._retire_tasks:
            return
        await asyncio.gather(*list(mcp_config._retire_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_retire_server_lets_inflight_call_finish(monkeypatch) -> None:
    import jiuwenswarm.common.mcp_server_registry as registry_mod

    started: list[str] = []
    gate = asyncio.Event()

    async def fake_run(params, worker):
        while True:
            req = await worker.queue.get()
            if req is None:
                return
            started.append(req.tool_name)
            await gate.wait()
            if not req.future.done():
                req.future.set_result(("ok", req.tool_name))

    monkeypatch.setattr(registry_mod, "_run_mcp_worker", fake_run)
    pool = GlobalMcpWorkerPool()
    params = {"_mcp_client_type": "streamable-http", "url": "https://example.com/mcp"}
    worker = await pool.acquire("s", params)
    call = asyncio.create_task(worker.call_tool("invoke", {}))
    await _wait_until(lambda: started == ["invoke"])

    await pool.retire_server("s", reason="config_update")
    assert worker.retiring is True
    refreshed = await pool.acquire("s", params)
    assert refreshed is not worker

    gate.set()
    assert await call == ("ok", "invoke")
    await _drain_retire_tasks()
    await pool.close_all()


@pytest.mark.asyncio
async def test_retiring_worker_rejects_new_calls(monkeypatch) -> None:
    import jiuwenswarm.common.mcp_server_registry as registry_mod

    async def fake_run(params, worker):
        while True:
            req = await worker.queue.get()
            if req is None:
                return

    monkeypatch.setattr(registry_mod, "_run_mcp_worker", fake_run)
    pool = GlobalMcpWorkerPool()
    worker = await pool.acquire("s", {"url": "https://example.com/mcp"})
    worker.retiring = True
    with pytest.raises(McpWorkerRetiringError):
        await worker.call_tool("invoke", {})
    await pool.close_all()


@pytest.mark.asyncio
async def test_reap_idle_skips_busy_worker(monkeypatch) -> None:
    import jiuwenswarm.common.mcp_server_registry as registry_mod

    async def fake_run(params, worker):
        while True:
            req = await worker.queue.get()
            if req is None:
                return

    monkeypatch.setattr(registry_mod, "_run_mcp_worker", fake_run)
    pool = GlobalMcpWorkerPool()
    worker = await pool.acquire("s", {"url": "https://example.com/mcp"})
    worker.last_used = 0.0
    worker.inflight = 1
    await pool.reap_idle(1)
    assert "s" in pool._workers

    worker.inflight = 0
    await pool.reap_idle(1)
    assert pool._workers == {}


@pytest.mark.asyncio
async def test_retire_timeout_maps_to_worker_stopped_error(monkeypatch) -> None:
    from jiuwenswarm.common import mcp_config

    class _BlockingSession:
        async def call_tool(self, name, arguments):
            await asyncio.sleep(3600)

    async def fake_enter(stack, params, client_type):
        return _BlockingSession()

    monkeypatch.setattr(mcp_config, "_enter_remote_mcp_session", fake_enter)
    monkeypatch.setenv("MCP_REGISTRY_WORKER_RETIRE_GRACE_S", "0.05")

    pool = GlobalMcpWorkerPool()
    params = {"_mcp_client_type": "streamable-http", "url": "https://example.com/mcp"}
    worker = await pool.acquire("s", params)
    call = asyncio.create_task(worker.call_tool("invoke", {}))
    await _wait_until(lambda: worker.inflight == 1)

    await pool.retire_server("s", reason="config_update")
    with pytest.raises(McpWorkerStoppedError):
        await asyncio.wait_for(call, timeout=5)
    await _drain_retire_tasks()


@pytest.mark.asyncio
async def test_repro_update_during_inflight_call(monkeypatch) -> None:
    """Repro of the ticket: mcp.server.update mid-call must not cancel the call.

    Drives the real registry update path (auth_headers change) while a call is
    in flight and asserts the call still returns its result. Without the
    retire-not-cancel fix this call is cancelled and this test fails.
    """

    from jiuwenswarm.common import mcp_config

    import jiuwenswarm.common.mcp_server_registry as registry_mod

    started = asyncio.Event()
    release = asyncio.Event()

    class _Session:
        async def call_tool(self, name, arguments):
            started.set()
            await release.wait()
            return "RESULT"

    async def fake_enter(stack, params, client_type):
        return _Session()

    async def discover(name, config):
        return (
            [{"name": "invoke", "description": "", "input_params": {}}],
            {"_mcp_client_type": "streamable-http", "url": config.get("url")},
        )

    monkeypatch.setattr(mcp_config, "_enter_remote_mcp_session", fake_enter)
    monkeypatch.setattr(
        registry_mod, "list_request_mcp_server_tools", discover
    )

    registry = reset_mcp_server_registry_for_tests()
    await registry.add_servers(
        [
            {
                "name": "s",
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "auth_headers": {
                    "Authorization": "sig-1",
                    "X-Sdk-Date": "20260928T013020Z",
                },
            }
        ]
    )

    worker = await registry.acquire_worker("s")
    call = asyncio.create_task(worker.call_tool("invoke", {}))
    await asyncio.wait_for(started.wait(), timeout=2)

    await registry.update_servers(
        [
            {
                "name": "s",
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "auth_headers": {
                    "Authorization": "sig-2",
                    "X-Sdk-Date": "20260928T013032Z",
                },
            }
        ]
    )

    release.set()
    assert await asyncio.wait_for(call, timeout=2) == "RESULT"
    await _drain_retire_tasks()


@pytest.mark.asyncio
async def test_blaster_repeated_updates_do_not_cancel_inflight(monkeypatch) -> None:
    """Hammer update_servers with rotating signatures while a call is in flight.

    Makes the intermittent production race deterministic: the in-flight call
    must still return its result no matter how many config updates land.
    """

    from jiuwenswarm.common import mcp_config

    import jiuwenswarm.common.mcp_server_registry as registry_mod

    started = asyncio.Event()
    release = asyncio.Event()

    class _Session:
        async def call_tool(self, name, arguments):
            started.set()
            await release.wait()
            return "RESULT"

    async def fake_enter(stack, params, client_type):
        return _Session()

    async def discover(name, config):
        return (
            [{"name": "invoke", "description": "", "input_params": {}}],
            {"_mcp_client_type": "streamable-http", "url": config.get("url")},
        )

    monkeypatch.setattr(mcp_config, "_enter_remote_mcp_session", fake_enter)
    monkeypatch.setattr(
        registry_mod, "list_request_mcp_server_tools", discover
    )

    def cfg(sig: int) -> dict:
        return {
            "name": "s",
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "auth_headers": {
                "Authorization": f"sig-{sig}",
                "X-Sdk-Date": f"20260928T0130{sig:02d}Z",
            },
        }

    registry = reset_mcp_server_registry_for_tests()
    await registry.add_servers([cfg(0)])

    worker = await registry.acquire_worker("s")
    call = asyncio.create_task(worker.call_tool("invoke", {}))
    await asyncio.wait_for(started.wait(), timeout=2)

    updates = 0
    stop = asyncio.Event()

    async def blaster() -> None:
        nonlocal updates
        sig = 1
        while not stop.is_set():
            await registry.update_servers([cfg(sig)])
            await registry.acquire_worker("s")
            updates += 1
            sig += 1
            await asyncio.sleep(0.005)

    blast = asyncio.create_task(blaster())
    await asyncio.sleep(0.05)
    stop.set()
    await blast

    release.set()
    assert await asyncio.wait_for(call, timeout=2) == "RESULT"
    assert updates >= 1
    await registry.worker_pool.close_all()
    await _drain_retire_tasks()


