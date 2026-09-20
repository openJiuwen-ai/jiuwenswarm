# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""进程级 TemplateEntityCache：命中 / 单飞 / TTL / 失效."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.server.runtime.enterprise_config import loader as loader_mod


@pytest.fixture
def template_cache(monkeypatch: pytest.MonkeyPatch):
    fetch_calls: list[tuple[str, tuple[str, ...]]] = []
    catalog = {
        "default_model": {
            "m1": {"template_id": "m1", "model_id": "model-1"},
            "m2": {"template_id": "m2", "model_id": "model-2"},
        },
        "vision_model": {
            "m1": {"template_id": "m1", "model_id": "model-1"},
        },
        "skill_prebuilt": {
            "w1": {"template_id": "w1", "skills": []},
        },
    }

    async def _fake_fetch(slot: str, template_ids: list[str]) -> list[dict[str, Any]]:
        fetch_calls.append((slot, tuple(template_ids)))
        rows = catalog.get(slot, {})
        return [rows[tid] for tid in template_ids if tid in rows]

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.enterprise_config.loader.db_queries.fetch_templates_by_slot",
        _fake_fetch,
    )
    loader_mod.invalidate_template_entity_cache()
    return loader_mod, fetch_calls


@pytest.mark.asyncio
async def test_second_fetch_hits_cache(template_cache) -> None:
    mod, fetch_calls = template_cache
    first = await mod._template_entity_cache.get_by_ids("default_model", ["m1"])
    second = await mod._template_entity_cache.get_by_ids("default_model", ["m1"])

    assert first[0]["model_id"] == "model-1"
    assert second[0]["model_id"] == "model-1"
    assert len(fetch_calls) == 1


@pytest.mark.asyncio
async def test_model_slots_share_table_cache(template_cache) -> None:
    """default_model / vision_model 共用 model_template 表缓存."""
    mod, fetch_calls = template_cache
    await mod._template_entity_cache.get_by_ids("default_model", ["m1"])
    by_id = await mod._template_entity_cache.get_by_ids("vision_model", ["m1"])

    assert by_id[0]["model_id"] == "model-1"
    assert len(fetch_calls) == 1


@pytest.mark.asyncio
async def test_only_missing_ids_are_fetched(template_cache) -> None:
    mod, fetch_calls = template_cache
    await mod._template_entity_cache.get_by_ids("default_model", ["m1"])
    rows = await mod._template_entity_cache.get_by_ids("default_model", ["m1", "m2"])

    assert {r["template_id"] for r in rows} == {"m1", "m2"}
    assert fetch_calls[-1] == ("default_model", ("m2",))


@pytest.mark.asyncio
async def test_invalidate_forces_refetch(template_cache) -> None:
    mod, fetch_calls = template_cache
    await mod._template_entity_cache.get_by_ids("default_model", ["m1"])
    mod.invalidate_template_entity_cache()
    await mod._template_entity_cache.get_by_ids("default_model", ["m1"])

    assert len(fetch_calls) == 2


@pytest.mark.asyncio
async def test_ttl_expiry_refetches(template_cache) -> None:
    mod, fetch_calls = template_cache
    cache = mod._template_entity_cache
    await cache.get_by_ids("default_model", ["m1"])
    entry = cache._entries[("model_template", "m1")]
    entry.fetched_at = time.monotonic() - (cache._ttl + 1)

    await cache.get_by_ids("default_model", ["m1"])
    assert len(fetch_calls) == 2
    # 过期读路径应丢掉旧条目，避免字典无限增长
    assert ("model_template", "m1") in cache._entries
    assert time.monotonic() - cache._entries[("model_template", "m1")].fetched_at < 1


@pytest.mark.asyncio
async def test_purge_expired_removes_stale_entries(template_cache) -> None:
    mod, _ = template_cache
    cache = mod._template_entity_cache
    await cache.get_by_ids("default_model", ["m1"])
    cache._entries[("model_template", "m1")].fetched_at = (
        time.monotonic() - (cache._ttl + 1)
    )
    cache._purge_expired()
    assert cache._entries == {}


@pytest.mark.asyncio
async def test_invalidate_clears_table_locks(template_cache) -> None:
    mod, _ = template_cache
    cache = mod._template_entity_cache
    await cache.get_by_ids("default_model", ["m1"])
    assert cache._table_locks
    mod.invalidate_template_entity_cache()
    assert cache._entries == {}
    assert cache._table_locks == {}


@pytest.mark.asyncio
async def test_concurrent_misses_singleflight(template_cache) -> None:
    mod, fetch_calls = template_cache
    original = mod.db_queries.fetch_templates_by_slot

    async def _slow(slot: str, template_ids: list[str]):
        await asyncio.sleep(0.03)
        return await original(slot, template_ids)

    mod.db_queries.fetch_templates_by_slot = _slow  # type: ignore[method-assign]
    try:
        results = await asyncio.gather(
            *(
                mod._template_entity_cache.get_by_ids("default_model", ["m1"])
                for _ in range(10)
            )
        )
        assert len(fetch_calls) == 1
        assert all(r[0]["model_id"] == "model-1" for r in results)
    finally:
        mod.db_queries.fetch_templates_by_slot = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_invalidate_enterprise_config_caches_clears_template(
    template_cache,
) -> None:
    mod, _ = template_cache
    await mod._template_entity_cache.get_by_ids("skill_prebuilt", ["w1"])
    mod.invalidate_enterprise_config_caches()
    assert mod._template_entity_cache._entries == {}


@pytest.mark.asyncio
async def test_invalidate_during_fetch_does_not_write_stale(
    template_cache,
) -> None:
    """invalidate 发生在 DB 回填完成前时，旧结果不得写回缓存。"""
    mod, fetch_calls = template_cache
    started = asyncio.Event()
    resume = asyncio.Event()
    original = mod.db_queries.fetch_templates_by_slot

    async def _gated(slot: str, template_ids: list[str]):
        started.set()
        await resume.wait()
        return await original(slot, template_ids)

    mod.db_queries.fetch_templates_by_slot = _gated  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(
            mod._template_entity_cache.get_by_ids("default_model", ["m1"])
        )
        await started.wait()
        mod.invalidate_template_entity_cache()
        resume.set()
        rows = await task
        assert rows[0]["model_id"] == "model-1"
        # 旧 generation 的回填不得落盘；下一次仍应打 DB。
        assert mod._template_entity_cache._entries == {}
        fetch_before = len(fetch_calls)
        await mod._template_entity_cache.get_by_ids("default_model", ["m1"])
        assert len(fetch_calls) == fetch_before + 1
    finally:
        mod.db_queries.fetch_templates_by_slot = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_fetch_slot_entities_uses_cache_on_enterprise(
    template_cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, fetch_calls = template_cache
    monkeypatch.setattr(mod, "is_enterprise", lambda: True)
    await mod._fetch_slot_entities("default_model", ["m1"])
    await mod._fetch_slot_entities("default_model", ["m1"])
    assert len(fetch_calls) == 1


@pytest.mark.asyncio
async def test_fetch_slot_entities_skips_cache_when_not_enterprise(
    template_cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, fetch_calls = template_cache
    monkeypatch.setattr(mod, "is_enterprise", lambda: False)
    await mod._fetch_slot_entities("default_model", ["m1"])
    await mod._fetch_slot_entities("default_model", ["m1"])
    assert len(fetch_calls) == 2
    assert mod._template_entity_cache._entries == {}


@pytest.mark.asyncio
async def test_fetch_resource_row_skips_cache_when_not_enterprise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fake_list_records(table: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(str(filters.get("resource_id") or ""))
        return [{"resource_id": filters.get("resource_id"), "enabled": True}]

    monkeypatch.setattr(loader_mod, "is_enterprise", lambda: False)
    monkeypatch.setattr(loader_mod.db_queries, "list_records", _fake_list_records)
    loader_mod.invalidate_enterprise_config_caches()
    first = await loader_mod._fetch_instance_agent_resource("rid-1")
    second = await loader_mod._fetch_instance_agent_resource("rid-1")
    assert first == second
    assert calls == ["rid-1", "rid-1"]


@pytest.mark.asyncio
async def test_fetch_resource_row_uses_cache_on_enterprise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fake_list_records(table: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(str(filters.get("resource_id") or ""))
        return [{"resource_id": filters.get("resource_id"), "enabled": True}]

    monkeypatch.setattr(loader_mod, "is_enterprise", lambda: True)
    monkeypatch.setattr(loader_mod.db_queries, "list_records", _fake_list_records)
    loader_mod.invalidate_enterprise_config_caches()
    await loader_mod._fetch_instance_agent_resource("rid-1")
    await loader_mod._fetch_instance_agent_resource("rid-1")
    assert calls == ["rid-1"]
