# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""header / env 方式的执行时参数：连接区分、随请求关闭、扫描参数、委托判定。"""

from __future__ import annotations

import asyncio

import pytest

import jiuwenswarm.common.mcp_server_registry as registry_mod
from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_server_registry import (
    McpRegistrySettings,
    McpRuntimeParam,
    McpServerDeclaration,
    McpServerRegistry,
    apply_runtime_params,
    runtime_delivery,
)

_TOKEN = {"name": "X-Token", "via": "header"}
_HEADER = McpServerDeclaration(runtime_params=(McpRuntimeParam("X-Token", "header"),))
_REQUEST = McpServerDeclaration(runtime_params=_HEADER.runtime_params, connection_scope="request")


def _remote(name: str = "conn", **extra) -> dict:
    return {"name": name, "type": "streamable-http", "url": "https://example.com/mcp", **extra}


@pytest.fixture
def seen(monkeypatch) -> list[dict]:
    configs: list[dict] = []

    async def discover(name, config):
        configs.append(dict(config))
        params = {"_mcp_client_type": "stdio" if "command" in config else "streamable-http"}
        # 与真实发现一致：headers 是 auth_headers 的别名，后者优先。
        headers = config.get("auth_headers") or config.get("headers")
        if headers is not None:
            params["auth_headers"] = dict(headers)
        if "env" in config:
            params["env"] = dict(config["env"])
        return [{"name": f"{name}_tool", "description": "", "input_params": {}}], params

    monkeypatch.setattr(registry_mod, "list_request_mcp_server_tools", discover)
    return configs


@pytest.fixture
def started(monkeypatch) -> list[dict]:
    params_seen: list[dict] = []

    async def fake_run(params, worker):
        params_seen.append(dict(params))
        while await worker.queue.get() is not None:
            pass

    monkeypatch.setattr(registry_mod, "_run_mcp_worker", fake_run)
    return params_seen


async def _settle() -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if not mcp_config._retire_tasks:
            return
        await asyncio.gather(*list(mcp_config._retire_tasks), return_exceptions=True)


def test_delivery_keys_connections_by_value_fingerprint() -> None:
    via, key, owned = runtime_delivery(_HEADER, {"X-Token": "t1"}, "s:r1")
    assert (via, owned) == ("header", False) and key.startswith("v") and "t1" not in key
    assert runtime_delivery(_HEADER, {"X-Token": "t1"}, "s:r2") == (via, key, False)
    assert runtime_delivery(_HEADER, {"X-Token": "t2"}, "s:r1")[1] != key
    _, first, owned = runtime_delivery(_REQUEST, {"X-Token": "t1"}, "s:r1")
    assert owned and first.startswith("r")
    assert runtime_delivery(_REQUEST, {"X-Token": "t1"}, "s:r2")[1] != first


def test_runtime_values_override_static_config_without_mutating_it() -> None:
    params = {"auth_headers": {"x-token": "static", "Accept": "a"}, "env": {"A": "1", "B": "2"}}
    merged = apply_runtime_params(params, "header", {"X-Token": "live"})
    assert merged["auth_headers"] == {"Accept": "a", "X-Token": "live"}
    assert apply_runtime_params(params, "env", {"B": "live"})["env"] == {"A": "1", "B": "live"}
    assert apply_runtime_params(params, "meta", {"B": "live"}) == params
    assert params == {"auth_headers": {"x-token": "static", "Accept": "a"}, "env": {"A": "1", "B": "2"}}


@pytest.mark.asyncio
async def test_header_values_cannot_inject_extra_headers(seen) -> None:
    for bad in ("t\r\nX-Evil: 1", "t\nx", "t\0"):
        assert registry_mod.resolve_runtime_params(_HEADER, {"X-Token": bad})[1].startswith("invalid value")
    result = await McpServerRegistry(McpRegistrySettings()).add_servers([
        _remote(runtime_params=[_TOKEN], scan_params={"X-Token": "t\r\nX-Evil: 1"}),
    ])
    assert result[0]["code"] == "INVALID_RUNTIME_PARAMS" and seen == []


@pytest.mark.asyncio
async def test_scan_params_are_used_once_and_never_cached(seen) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    results = await registry.add_servers([
        _remote(headers={"x-token": "static", "Accept": "a"}, runtime_params=[_TOKEN],
                scan_params={"X-Token": "scan-secret"}),
        {"name": "local", "command": "node", "args": ["s.js"], "env": {"A": "1"},
         "runtime_params": [{"name": "AK", "via": "env"}], "scan_params": {"AK": "scan-secret"}},
    ])
    assert [r["ok"] for r in results] == [True, True]
    assert seen[0]["auth_headers"] == {"Accept": "a", "X-Token": "scan-secret"}
    assert seen[1]["env"] == {"A": "1", "AK": "scan-secret"}
    assert registry._cache["conn"].connect_params["auth_headers"] == {"Accept": "a", "x-token": "static"}
    assert registry._cache["local"].connect_params["env"] == {"A": "1"}
    stored = repr([registry._registry, registry._cache, await registry.list_servers(),
                   await registry.get_server("conn"), await registry.get_server("local")])
    assert "scan-secret" not in stored
    updated = await registry.update_servers([
        _remote(version="2", runtime_params=[_TOKEN], scan_params={"X-Token": "scan-secret"}),
    ])
    assert updated[0]["ok"] and seen[2]["auth_headers"] == {"X-Token": "scan-secret"}
    assert registry._cache["conn"].connect_params["auth_headers"] == {}


@pytest.mark.asyncio
async def test_workers_are_separated_by_runtime_values(seen, started) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([_remote(headers={"Accept": "a"}, runtime_params=[_TOKEN])])

    async def acquire(token: str, request: str = "s:r1", declaration=_HEADER):
        values = {"X-Token": token}
        via, variant, _ = runtime_delivery(declaration, values, request)
        return variant, await registry.acquire_worker("conn", via=via, values=values, variant=variant)

    key1, first = await acquire("t1")
    _, again = await acquire("t1", "s:r2")
    key2, second = await acquire("t2")
    owned_key, owned = await acquire("t1", "s:r3", _REQUEST)
    await asyncio.sleep(0)
    assert again is first and len({first, second, owned}) == 3
    assert [p["auth_headers"] for p in started] == [
        {"Accept": "a", "X-Token": "t1"}, {"Accept": "a", "X-Token": "t2"},
        {"Accept": "a", "X-Token": "t1"},
    ]
    assert registry._cache["conn"].connect_params["auth_headers"] == {"Accept": "a"}
    await registry.worker_pool.retire_variant("conn", owned_key, reason="request_end")
    assert owned.retiring and owned.close_reason == "request_end"
    assert sorted(registry.worker_pool._workers) == sorted([f"conn#{key1}", f"conn#{key2}"])
    await registry.remove_servers(["conn"])
    assert registry.worker_pool._workers == {} and first.retiring and second.retiring
    await _settle()


@pytest.mark.asyncio
async def test_env_values_start_separate_processes(seen, started) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    declaration = McpServerDeclaration(runtime_params=(McpRuntimeParam("AK", "env"),))
    await registry.add_servers([
        {"name": "local", "command": "node", "args": ["s.js"], "env": {"A": "1"},
         "runtime_params": [{"name": "AK", "via": "env"}]},
    ])
    workers = []
    for ak in ("k1", "k2", "k1"):
        via, variant, _ = runtime_delivery(declaration, {"AK": ak})
        workers.append(await registry.acquire_worker("local", via=via, values={"AK": ak}, variant=variant))
    await asyncio.sleep(0)
    assert workers[0] is workers[2] and workers[0] is not workers[1]
    assert [p["env"] for p in started] == [{"A": "1", "AK": "k1"}, {"A": "1", "AK": "k2"}]
    await registry.worker_pool.close_all()


@pytest.mark.asyncio
async def test_periodic_scan_skips_servers_without_fixed_credentials(seen) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([
        _remote("needs", runtime_params=[_TOKEN]),
        _remote("optional", runtime_params=[{**_TOKEN, "required": False}]),
        _remote("plain"),
    ])
    seen.clear()
    await registry.scan_once()
    assert sorted(c["url"] for c in seen) == ["https://example.com/mcp"] * 2
    assert registry._cache["needs"].scan_fail_count == 0


@pytest.mark.asyncio
async def test_deepresearch_snapshot_counts_declared_headers(seen) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([
        _remote(headers={"Accept": "a"}, runtime_params=[_TOKEN]), _remote("plain", headers={"Accept": "a"}),
    ])
    _, _, params = await registry.snapshot_for_deepresearch("conn")
    assert params["auth_headers"] == {"Accept": "a", "X-Token": "***"}
    assert registry_mod.deepresearch_connect_params(
        registry._cache["conn"].connect_params, _HEADER, {"X-Token": "live"},
    )["auth_headers"] == {"Accept": "a", "X-Token": "live"}
    assert (await registry.snapshot_for_deepresearch("plain"))[2]["auth_headers"] == {"Accept": "a"}
    assert registry._cache["conn"].connect_params["auth_headers"] == {"Accept": "a"}
