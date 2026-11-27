# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenclaw.utils import get_checkpoint_dir, get_multi_tenant_user_workspace_dir


def test_get_checkpoint_dir_default_unchanged():
    expected = get_multi_tenant_user_workspace_dir("default", "default") / ".checkpoint"
    assert get_checkpoint_dir() == expected
    assert get_checkpoint_dir(None, None) == expected


def test_get_checkpoint_dir_tenant_scoped():
    service_id = "vibeskill_test_session"
    agent_id = "agent_default"
    expected = get_multi_tenant_user_workspace_dir(service_id, agent_id) / ".checkpoint"
    assert get_checkpoint_dir(service_id, agent_id) == expected
    assert "service_vibeskill_test_session" in str(expected)


@pytest.mark.asyncio
async def test_ensure_persistent_checkpointer_uses_tenant_path(tmp_path, monkeypatch):
    service_id = "tenant_svc"
    agent_id = "tenant_agent"
    tenant_ckpt = tmp_path / f"service_{service_id}" / f"agent_{agent_id}" / ".checkpoint"

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.checkpoint_setup.get_checkpoint_dir",
        lambda sid=None, aid=None: tenant_ckpt
        if sid == service_id and aid == agent_id
        else tmp_path / "default" / ".checkpoint",
    )

    mock_cp = object()
    with patch(
            "openjiuwen.core.session.checkpointer.CheckpointerFactory.create",
            new_callable=AsyncMock,
            return_value=mock_cp,
    ) as create_mock, patch(
        "openjiuwen.core.session.checkpointer.CheckpointerFactory.set_default_checkpointer",
    ) as set_default_mock, patch(
        "openjiuwen.core.session.checkpointer.persistence.PersistenceCheckpointerProvider",
    ):
        from jiuwenclaw.agentserver.checkpoint_setup import ensure_persistent_checkpointer

        await ensure_persistent_checkpointer(service_id, agent_id)

    create_mock.assert_awaited_once()
    conf = create_mock.await_args[0][0].conf
    assert conf["db_path"] == f"{tenant_ckpt}/checkpoint"
    set_default_mock.assert_called_once_with(mock_cp)
    assert tenant_ckpt.is_dir()


@pytest.mark.asyncio
async def test_ensure_persistent_checkpointer_reuses_cached_instance(tmp_path, monkeypatch):
    ckpt_dir = tmp_path / ".checkpoint"
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.checkpoint_setup.get_checkpoint_dir",
        lambda sid=None, aid=None: ckpt_dir,
    )

    mock_cp = object()
    with patch(
            "openjiuwen.core.session.checkpointer.CheckpointerFactory.create",
            new_callable=AsyncMock,
            return_value=mock_cp,
    ) as create_mock, patch(
        "openjiuwen.core.session.checkpointer.CheckpointerFactory.set_default_checkpointer",
    ) as set_default_mock, patch(
        "openjiuwen.core.session.checkpointer.persistence.PersistenceCheckpointerProvider",
    ):
        from jiuwenclaw.agentserver import checkpoint_setup

        await checkpoint_setup._checkpointers.clear()
        await checkpoint_setup.ensure_persistent_checkpointer("a", "b")
        await checkpoint_setup.ensure_persistent_checkpointer("a", "b")

    create_mock.assert_awaited_once()
    assert set_default_mock.call_count == 2


@pytest.mark.asyncio
async def test_cache_eviction_triggers_dispose_on_put(tmp_path, monkeypatch):
    """当缓存容量满时，put 新条目应淘汰旧条目并触发 dispose."""
    from jiuwenclaw.agentserver import checkpoint_setup
    from jiuwenclaw.utils import AsyncLRUCache

    def _get_ckpt_dir(sid=None, aid=None):
        # 不同租户返回不同路径，确保 key 不同
        svc = sid or "default"
        agt = aid or "default"
        return tmp_path / f"service_{svc}" / f"agent_{agt}" / ".checkpoint"

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.checkpoint_setup.get_checkpoint_dir",
        _get_ckpt_dir,
    )

    dispose_mock = AsyncMock()

    async def _mock_dispose(_key: str, checkpointer: object) -> None:
        await dispose_mock(_key, checkpointer)

    # 触发淘汰
    original_cache = checkpoint_setup._checkpointers
    checkpoint_setup._checkpointers = AsyncLRUCache(
        max_size=1,
        ttl_seconds=3600,
        on_evict=_mock_dispose,
    )

    old_cp = MagicMock()
    new_cp = MagicMock()

    try:
        with patch(
                "openjiuwen.core.session.checkpointer.CheckpointerFactory.create",
                new_callable=AsyncMock,
                side_effect=[old_cp, new_cp],
        ) as create_mock, patch(
            "openjiuwen.core.session.checkpointer.CheckpointerFactory.set_default_checkpointer",
        ) as set_default_mock, patch(
            "openjiuwen.core.session.checkpointer.persistence.PersistenceCheckpointerProvider",
        ):
            # 第一次调用：key1 -> old_cp
            await checkpoint_setup.ensure_persistent_checkpointer("svc1", "agent1")
            # 第二次调用：key2 -> new_cp，缓存满触发 key1 被淘汰
            await checkpoint_setup.ensure_persistent_checkpointer("svc2", "agent2")

        assert create_mock.await_count == 2
        # 第一次创建的 checkpointer 被淘汰时应触发 dispose
        dispose_mock.assert_awaited_once()
        assert dispose_mock.await_args[0][1] is old_cp
        set_default_mock.assert_called_with(new_cp)
    finally:
        checkpoint_setup._checkpointers = original_cache


@pytest.mark.asyncio
async def test_cache_cover_triggers_dispose():
    """AsyncLRUCache put 同一个 key 覆盖旧值时，应触发旧值的 dispose."""
    from jiuwenclaw.utils import AsyncLRUCache

    dispose_mock = AsyncMock()

    async def _mock_dispose(_key: str, checkpointer: object) -> None:
        await dispose_mock(_key, checkpointer)

    cache = AsyncLRUCache(
        max_size=10,
        ttl_seconds=3600,
        on_evict=_mock_dispose,
    )

    old_val = MagicMock()
    new_val = MagicMock()

    await cache.put("key1", old_val)
    await cache.put("key1", new_val)

    dispose_mock.assert_awaited_once()
    assert dispose_mock.await_args[0][1] is old_val
    # 新值应在缓存中
    assert await cache.get("key1") is new_val


@pytest.mark.asyncio
async def test_cleanup_expired_removes_ttl_expired_entries_and_triggers_evict():
    """cleanup_expired 应清理所有 TTL 过期条目并触发 on_evict，返回清理计数."""
    from jiuwenclaw.utils import AsyncLRUCache

    dispose_mock = AsyncMock()

    async def _mock_dispose(_key: str, value: object) -> None:
        await dispose_mock(_key, value)

    # 极短 TTL + 短暂等待，构造可靠过期
    cache = AsyncLRUCache(
        max_size=10,
        ttl_seconds=1,
        on_evict=_mock_dispose,
    )

    v1, v2, v3 = MagicMock(), MagicMock(), MagicMock()
    await cache.put("k1", v1)
    await cache.put("k2", v2)
    await cache.put("k3", v3)
    await asyncio.sleep(1)  # 等待 TTL 过期

    cleaned = await cache.cleanup_expired()

    assert cleaned == 3
    assert dispose_mock.await_count == 3
    # 过期条目应全部清除
    assert await cache.keys() == []


@pytest.mark.asyncio
async def test_cleanup_expired_keeps_active_entries():
    """cleanup_expired 不应清理未过期的活跃条目."""
    from jiuwenclaw.utils import AsyncLRUCache

    dispose_mock = AsyncMock()

    async def _mock_dispose(_key: str, value: object) -> None:
        await dispose_mock(_key, value)

    cache = AsyncLRUCache(
        max_size=10,
        ttl_seconds=3600,
        on_evict=_mock_dispose,
    )

    v1, v2 = MagicMock(), MagicMock()
    await cache.put("k1", v1)
    await cache.put("k2", v2)

    cleaned = await cache.cleanup_expired()

    assert cleaned == 0
    dispose_mock.assert_not_awaited()
    assert set(await cache.keys()) == {"k1", "k2"}


@pytest.mark.asyncio
async def test_cleanup_expired_returns_zero_on_empty_cache():
    """空缓存调用 cleanup_expired 应返回 0，不触发任何回调."""
    from jiuwenclaw.utils import AsyncLRUCache

    dispose_mock = AsyncMock()

    async def _mock_dispose(_key: str, value: object) -> None:
        await dispose_mock(_key, value)

    cache = AsyncLRUCache(
        max_size=10,
        ttl_seconds=3600,
        on_evict=_mock_dispose,
    )

    cleaned = await cache.cleanup_expired()

    assert cleaned == 0
    dispose_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_persistent_checkpointer_cleans_expired_on_miss(tmp_path, monkeypatch):
    """缓存未命中（新建 checkpointer）时应顺带清理 TTL 过期条目."""
    from jiuwenclaw.agentserver import checkpoint_setup
    from jiuwenclaw.utils import AsyncLRUCache

    ckpt_dir = tmp_path / ".checkpoint"
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.checkpoint_setup.get_checkpoint_dir",
        lambda sid=None, aid=None: ckpt_dir,
    )

    # 预置一个已过期的条目，验证它在新租户接入时被清理
    expired_key = "/tmp/expired_tenant/.checkpoint"
    expired_cp = MagicMock()
    dispose_mock = AsyncMock()

    async def _mock_dispose(_key: str, cp: object) -> None:
        await dispose_mock(_key, cp)

    original_cache = checkpoint_setup._checkpointers
    checkpoint_setup._checkpointers = AsyncLRUCache(
        max_size=10,
        ttl_seconds=0,  # 立即过期
        on_evict=_mock_dispose,
    )

    try:
        # 预置过期条目（ttl=0，put 后立即过期）
        await checkpoint_setup._checkpointers.put(expired_key, expired_cp)
        assert len(checkpoint_setup._checkpointers) == 1

        mock_cp = object()
        with patch(
                "openjiuwen.core.session.checkpointer.CheckpointerFactory.create",
                new_callable=AsyncMock,
                return_value=mock_cp,
        ) as create_mock, patch(
            "openjiuwen.core.session.checkpointer.CheckpointerFactory.set_default_checkpointer",
        ), patch(
            "openjiuwen.core.session.checkpointer.persistence.PersistenceCheckpointerProvider",
        ):
            await checkpoint_setup.ensure_persistent_checkpointer("svc", "agent")

        create_mock.assert_awaited_once()
        # 过期条目应被清理（dispose 被触发）
        dispose_mock.assert_awaited_once()
        assert dispose_mock.await_args[0][1] is expired_cp
    finally:
        checkpoint_setup._checkpointers = original_cache


@pytest.mark.asyncio
async def test_evict_source_logged_for_each_path():
    """不同淘汰路径应调用 _safe_evict 并传入对应 source（直接验证行为，不依赖异步日志时序）."""
    from jiuwenclaw.utils import AsyncLRUCache

    sources: list[str] = []

    async def _noop(_key, _value):
        return None

    async def _capture(self, _key, _value, *, source=""):
        sources.append(source)

    def _wrap(cache):
        cache._on_evict = None
        cache._safe_evict = lambda k, v, *, source="": _capture(cache, k, v, source=source)

    # --- get_expired: TTL 过期 ---
    cache = AsyncLRUCache(max_size=10, ttl_seconds=1, on_evict=_noop)
    _wrap(cache)
    await cache.put("g1", MagicMock())
    await asyncio.sleep(1)
    await cache.get("g1")  # 命中过期 → get_expired
    assert "get_expired" in sources

    sources.clear()
    # --- put_cover ---
    cache = AsyncLRUCache(max_size=10, ttl_seconds=3600, on_evict=_noop)
    _wrap(cache)
    await cache.put("p1", MagicMock())
    await cache.put("p1", MagicMock())  # 覆盖 → put_cover
    assert "put_cover" in sources

    sources.clear()
    # --- put_lru ---
    cache = AsyncLRUCache(max_size=1, ttl_seconds=3600, on_evict=_noop)
    _wrap(cache)
    await cache.put("l1", MagicMock())
    await cache.put("l2", MagicMock())  # 挤掉 l1 → put_lru
    assert "put_lru" in sources

    sources.clear()
    # --- remove ---
    cache = AsyncLRUCache(max_size=10, ttl_seconds=3600, on_evict=_noop)
    _wrap(cache)
    await cache.put("r1", MagicMock())
    await cache.remove("r1")
    assert "remove" in sources

    sources.clear()
    # --- clear ---
    cache = AsyncLRUCache(max_size=10, ttl_seconds=3600, on_evict=_noop)
    _wrap(cache)
    await cache.put("c1", MagicMock())
    await cache.clear()
    assert "clear" in sources

    sources.clear()
    # --- cleanup_expired ---
    cache = AsyncLRUCache(max_size=10, ttl_seconds=1, on_evict=_noop)
    _wrap(cache)
    await cache.put("e1", MagicMock())
    await asyncio.sleep(1)
    await cache.cleanup_expired()
    assert "cleanup_expired" in sources


@pytest.mark.asyncio
async def test_async_lru_cache_max_size_zero_or_negative():
    """max_size <= 0 时应被 clamp 到 1，空缓存 put 不崩溃."""
    from jiuwenclaw.utils import AsyncLRUCache

    for bad_size in (0, -1, -5):
        cache = AsyncLRUCache(max_size=bad_size, ttl_seconds=3600)
        assert cache._max_size == 1, f"expected 1 for max_size={bad_size}"
        # 空缓存首次 put 不应抛 KeyError
        await cache.put("k1", MagicMock())
        assert await cache.get("k1") is not None


@pytest.mark.asyncio
async def test_async_lru_cache_get_or_create_reuses_existing():
    """get_or_create 对已存在的未过期 key 应直接复用，不调用 factory."""
    from jiuwenclaw.utils import AsyncLRUCache

    factory_mock = AsyncMock(return_value="new_value")
    cache = AsyncLRUCache(max_size=10, ttl_seconds=3600)

    await cache.put("key1", "old_value")
    val, is_new = await cache.get_or_create("key1", factory_mock)

    assert val == "old_value"
    assert is_new is False
    factory_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_lru_cache_get_or_create_concurrent_same_key():
    """并发请求同一 key 时 factory 应只执行一次，消除 TOCTOU."""
    from jiuwenclaw.utils import AsyncLRUCache

    call_count = 0

    async def slow_factory(key: str) -> str:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # 故意放大竞态窗口
        return f"created-{key}"

    cache = AsyncLRUCache(max_size=10, ttl_seconds=3600)

    async def task():
        val, _ = await cache.get_or_create("shared", slow_factory)
        return val

    results = await asyncio.gather(task(), task(), task())
    assert call_count == 1, f"factory called {call_count} times, expected 1"
    assert all(r == "created-shared" for r in results)


@pytest.mark.asyncio
async def test_async_lru_cache_get_or_create_evicts_on_full():
    """get_or_create 在缓存满时应触发 LRU 淘汰."""
    from jiuwenclaw.utils import AsyncLRUCache

    evicted = []

    async def _on_evict(key: str, value: object) -> None:
        evicted.append((key, value))

    cache = AsyncLRUCache(max_size=2, ttl_seconds=3600, on_evict=_on_evict)

    async def factory(key: str) -> str:
        return f"val-{key}"

    await cache.get_or_create("a", factory)
    await cache.get_or_create("b", factory)
    await cache.get_or_create("c", factory)  # 应淘汰 a

    assert ("a", "val-a") in evicted
    assert await cache.get("a") is None
    assert await cache.get("b") == "val-b"
    assert await cache.get("c") == "val-c"


@pytest.mark.asyncio
async def test_ensure_persistent_checkpointer_concurrent_same_tenant(
        tmp_path, monkeypatch
):
    """同一 tenant 并发接入时，checkpointer 应只创建一次且 engine 不会被提前 dispose."""
    ckpt_dir = tmp_path / ".checkpoint"
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.checkpoint_setup.get_checkpoint_dir",
        lambda sid=None, aid=None: ckpt_dir,
    )

    mock_cp = MagicMock()
    create_mock = AsyncMock(return_value=mock_cp)

    with patch(
            "openjiuwen.core.session.checkpointer.CheckpointerFactory.create",
            new=create_mock,
    ), patch(
        "openjiuwen.core.session.checkpointer.CheckpointerFactory.set_default_checkpointer",
    ) as set_default_mock, patch(
        "openjiuwen.core.session.checkpointer.persistence.PersistenceCheckpointerProvider",
    ):
        from jiuwenclaw.agentserver import checkpoint_setup

        await checkpoint_setup._checkpointers.clear()

        async def task():
            await checkpoint_setup.ensure_persistent_checkpointer("svc", "agent")

        await asyncio.gather(task(), task(), task())

    # create 只应被调用一次（get_or_create 的原子性保证）
    assert create_mock.await_count == 1
    # set_default_checkpointer 被调用 3 次，但都是同一个实例
    assert set_default_mock.call_count == 3
    assert all(call.args[0] is mock_cp for call in set_default_mock.call_args_list)
