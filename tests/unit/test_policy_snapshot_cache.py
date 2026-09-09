# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""进程级 PolicySnapshotCache 单测: 单飞 / TTL / 失效 / 三级匹配等价性."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenclaw.infrastructure.module_importer import import_manager_ws_client_module


def _load_loader() -> Any:
    return import_manager_ws_client_module("core.enterprise_config.loader")


class _FakeDb:
    """替身 GatewayDb: 返回预置表数据并记录 list_records 调用."""

    tables: dict[str, list[dict[str, Any]]] = {}
    calls: list[str] = []

    @classmethod
    def current(cls) -> "_FakeDb":
        return cls

    @classmethod
    async def list_records(cls, table: str, filters: Any = None, order_by: Any = None) -> list[dict[str, Any]]:
        cls.calls.append(table)
        return cls.tables.get(table, [])


def _seed_tables() -> None:
    _FakeDb.tables = {
        "config_effective_service_policy": [
            {"policy_id": "sp1", "match_expr": "yes", "template_ref": {}, "priority": 10},
            {"policy_id": "sp2", "match_expr": "no", "template_ref": {}, "priority": 5},
        ],
        "config_effective_agent_policy": [
            {"service_policy_id": "sp1", "match_expr": "yes", "template_ref": {}},
            {"service_policy_id": "sp2", "match_expr": "yes", "template_ref": {}},
        ],
        "config_effective_global_policy": [
            {"id": 7, "template_ref": {}},
        ],
    }
    _FakeDb.calls = []


@pytest.fixture
def loader_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    mod = _load_loader()
    _seed_tables()
    monkeypatch.setattr(mod, "GatewayDb", _FakeDb)
    # 假匹配器: match_expr == ctx 即命中(与真实表达式引擎解耦)
    monkeypatch.setattr(mod.expressions, "evaluate_match_expr", lambda expr, ctx: bool(expr) and str(expr) == str(ctx))
    mod._policy_snapshot_cache.invalidate()
    mod._policy_snapshot_cache._snapshot = None
    return mod


@pytest.mark.unit
async def test_snapshot_fetched_once_and_reused(loader_mod: Any) -> None:
    """两次匹配只拉一次快照(3 次全表查询), 结果来自内存."""
    ctx_yes = "yes"
    m1 = await loader_mod._resolve_policy_match(ctx_yes)
    m2 = await loader_mod._resolve_policy_match(ctx_yes)

    assert _FakeDb.calls.count("config_effective_service_policy") == 1
    assert _FakeDb.calls.count("config_effective_agent_policy") == 1
    assert _FakeDb.calls.count("config_effective_global_policy") == 1
    assert m1.matched_service["policy_id"] == "sp1"
    assert m1.matched_agent["service_policy_id"] == "sp1"
    assert m1.matched_global["id"] == 7
    assert m2 is not None


@pytest.mark.unit
async def test_service_miss_falls_back_to_global(loader_mod: Any) -> None:
    """无 service 规则命中时 matched_agent 为空, 走 global refs."""
    m = await loader_mod._resolve_policy_match("nomatch")
    assert m.matched_service is None
    assert m.matched_agent is None
    assert m.matched_global["id"] == 7


@pytest.mark.unit
async def test_agent_rules_grouped_by_service_policy(loader_mod: Any) -> None:
    """agent 规则按 service_policy_id 分组: sp2 的规则不会被 sp1 的匹配选到."""
    # ctx="only2": service sp2 命中(match_expr 改造 via monkeypatch 不便, 直接用 no/yes 组合)
    # sp2 的 match_expr 是 "no", 改 ctx="no" 命中 sp2; agent 规则 sp2 的 match_expr 是 "yes" 不命中
    m = await loader_mod._resolve_policy_match("no")
    assert m.matched_service["policy_id"] == "sp2"
    assert m.matched_agent is None  # sp2 组内 match_expr="yes" != "no"


@pytest.mark.unit
async def test_invalidate_forces_refetch(loader_mod: Any) -> None:
    await loader_mod._resolve_policy_match("yes")
    loader_mod.invalidate_policy_snapshot()
    await loader_mod._resolve_policy_match("yes")
    assert _FakeDb.calls.count("config_effective_service_policy") == 2


@pytest.mark.unit
async def test_ttl_expiry_refetches(loader_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    await loader_mod._resolve_policy_match("yes")
    # 手动把快照拨到 TTL 之外(拨旧量 = ttl + 1, 避免踩 <= 边界)
    ttl = loader_mod._policy_snapshot_cache._ttl
    loader_mod._policy_snapshot_cache._snapshot.fetched_at = _time.monotonic() - (ttl + 1)
    await loader_mod._resolve_policy_match("yes")
    assert _FakeDb.calls.count("config_effective_service_policy") == 2


@pytest.mark.unit
async def test_concurrent_misses_singleflight(loader_mod: Any) -> None:
    """并发 10 个匹配同时 miss: 快照只拉一次."""

    class _SlowDb:
        @classmethod
        def current(cls) -> "_SlowDb":
            return cls

        @classmethod
        async def list_records(cls, table: str, filters: Any = None, order_by: Any = None) -> list[dict[str, Any]]:
            await asyncio.sleep(0.02)  # 模拟查库耗时, 让并发堆在单飞锁上
            _FakeDb.calls.append(table)
            return _FakeDb.tables.get(table, [])

    original = loader_mod.GatewayDb
    loader_mod.GatewayDb = _SlowDb  # noqa: SLF001 测试替换
    try:
        results = await asyncio.gather(
            *(loader_mod._resolve_policy_match("yes") for _ in range(10))
        )
        assert _FakeDb.calls.count("config_effective_service_policy") == 1
        assert all(r.matched_service["policy_id"] == "sp1" for r in results)
    finally:
        loader_mod.GatewayDb = original  # noqa: SLF001
