# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentManager 池限额解析与自动淘汰边界。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.utils import AsyncLRUCache
from jiuwenswarm.server.runtime.tenant_agent_pool import (
    TenantAgentPool,
    resolve_agent_pool_cache_limits,
)


def test_resolve_limits_personal_stays_unlimited():
    assert resolve_agent_pool_cache_limits(enterprise=False) == (None, None)


def test_resolve_limits_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv("AGENT_POOL_MAX_SIZE", "50")
    monkeypatch.setenv("AGENT_POOL_TTL", "30")
    assert resolve_agent_pool_cache_limits(
        cache_max_size=7,
        cache_ttl=9,
        enterprise=True,
    ) == (7, 9)


def test_resolve_limits_enterprise_env_boundaries(monkeypatch):
    monkeypatch.delenv("AGENT_POOL_MAX_SIZE", raising=False)
    monkeypatch.delenv("AGENT_POOL_TTL", raising=False)
    assert resolve_agent_pool_cache_limits(enterprise=True) == (100, 600)

    monkeypatch.setenv("AGENT_POOL_MAX_SIZE", "")
    monkeypatch.setenv("AGENT_POOL_TTL", "0")
    assert resolve_agent_pool_cache_limits(enterprise=True) == (None, None)

    monkeypatch.setenv("AGENT_POOL_MAX_SIZE", "abc")
    monkeypatch.setenv("AGENT_POOL_TTL", "-1")
    assert resolve_agent_pool_cache_limits(enterprise=True) == (100, None)


@pytest.mark.asyncio
async def test_put_if_absent_does_not_replace_or_evict():
    replaced = []

    async def on_evict(key, value):
        replaced.append((key, value))

    cache = AsyncLRUCache(max_size=1, on_evict=on_evict)
    await cache.put("a", 1)
    assert await cache.put_if_absent("a", 2) is False
    assert await cache.get("a") == 1
    assert replaced == []
    assert await cache.put_if_absent("b", 3) is True
    assert await cache.get("b") == 3


@pytest.mark.asyncio
async def test_keys_includes_entry_restored_by_on_evict():
    cache = AsyncLRUCache(max_size=None, ttl_seconds=1)

    async def restore(key, value):
        await cache.put_if_absent(key, value)

    cache._on_evict = restore
    await cache.put("busy", "manager")
    cache._cache["busy"] = ("manager", 0.0)
    assert await cache.keys() == ["busy"]


def _pool() -> TenantAgentPool:
    return TenantAgentPool(cache_max_size=10, cache_ttl=60)


def _patch_side_pools(monkeypatch):
    calls = {"rail": None, "cron": 0, "research": None}

    def rail_remove(service_id, agent_id, workspace_key="default"):
        calls["rail"] = (service_id, agent_id, workspace_key)
        return True

    async def research_remove(service_id, agent_id, workspace_key="default"):
        calls["research"] = (service_id, agent_id, workspace_key)
        return True

    async def cron_remove(service_id, agent_id):
        calls["cron"] += 1
        calls["cron_ids"] = (service_id, agent_id)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.plugins.rail_manager.RailManagerPool.remove",
        rail_remove,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager."
        "DeepResearchTaskManagerPool.remove",
        research_remove,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.cron_local_runtime.AgentCronRegistry.remove",
        cron_remove,
    )
    return calls


@pytest.mark.asyncio
async def test_auto_evict_idle_skips_cron_and_passes_workspace(monkeypatch):
    calls = _patch_side_pools(monkeypatch)
    pool = _pool()
    key = ("agent-1", "service-1", "workspace-1")
    lock = asyncio.Lock()
    pool._locks[key] = lock
    manager = SimpleNamespace(is_working=lambda: False, cleanup=AsyncMock())

    await pool._on_agent_wrapper_evicted(key, manager)

    manager.cleanup.assert_awaited()
    assert calls["cron"] == 0
    assert calls["rail"] == ("service-1", "agent-1", "workspace-1")
    assert calls["research"] == ("service-1", "agent-1", "workspace-1")
    assert pool._locks[key] is lock


@pytest.mark.asyncio
async def test_auto_evict_keeps_working_manager(monkeypatch):
    calls = _patch_side_pools(monkeypatch)
    pool = _pool()
    key = ("agent-1", "service-1", "workspace-1")
    manager = SimpleNamespace(is_working=lambda: True, cleanup=AsyncMock())

    await pool._on_agent_wrapper_evicted(key, manager)

    manager.cleanup.assert_not_awaited()
    assert calls["cron"] == 0
    assert calls["rail"] is None
    assert await pool._agent_wrappers.get(key) is manager


@pytest.mark.asyncio
async def test_catalog_evict_stops_cron_and_drops_lock(monkeypatch):
    calls = _patch_side_pools(monkeypatch)
    pool = _pool()
    key = ("agent-1", "service-1", "workspace-1")
    manager = SimpleNamespace(is_working=lambda: False, cleanup=AsyncMock())
    await pool._agent_wrappers.put(key, manager)
    pool._locks[key] = asyncio.Lock()

    await pool._evict_manager_cache("agent-1", "service-1", "workspace-1")

    manager.cleanup.assert_awaited()
    assert calls["cron"] == 1
    assert calls["cron_ids"] == ("service-1", "agent-1")
    assert key not in pool._locks
    assert await pool._agent_wrappers.get(key) is None


@pytest.mark.asyncio
async def test_drop_stale_locks_keeps_locked_entry():
    pool = _pool()
    held = asyncio.Lock()
    await held.acquire()
    idle = asyncio.Lock()
    pool._locks["held"] = held
    pool._locks["idle"] = idle

    pool._drop_stale_locks(set())

    assert pool._locks["held"] is held
    assert "idle" not in pool._locks
    held.release()
