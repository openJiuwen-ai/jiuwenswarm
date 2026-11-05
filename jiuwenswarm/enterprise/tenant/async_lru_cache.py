# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async LRU cache with optional capacity and TTL."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any


class AsyncLRUCache:
    """带可选过期时间与容量上限的 LRU 缓存（异步并发安全）.

    ``max_size=None`` / ``ttl_seconds=None`` 表示不启用对应限制。
    """

    def __init__(
        self,
        max_size: int | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._cache: OrderedDict[Hashable, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    def _is_expired(self, timestamp: float) -> bool:
        if self._ttl is None:
            return False
        return time.time() - timestamp > self._ttl

    async def get(self, key: Hashable) -> Any | None:
        """获取缓存值，如果不存在或已过期则返回 None.

        命中时会刷新访问时间（滑动过期：自最后一次 get/put 起算 ttl）。
        """
        async with self._lock:
            if key not in self._cache:
                return None

            value, timestamp = self._cache[key]
            if self._is_expired(timestamp):
                self._cache.pop(key, None)
                return None

            # 刷新访问时间并移动到末尾（最近使用）
            self._cache[key] = (value, time.time())
            self._cache.move_to_end(key)
            return value

    async def put(self, key: Hashable, value: Any) -> None:
        """存入缓存值，如果超过容量则淘汰最久未使用的."""
        async with self._lock:
            if key in self._cache:
                self._cache.pop(key)
            elif self._max_size is not None and len(self._cache) >= self._max_size:
                # 淘汰最久未使用的（头部）
                self._cache.popitem(last=False)

            self._cache[key] = (value, time.time())

    async def touch_if_same(self, key: Hashable, value: Any) -> bool:
        """若 key 存在且缓存值与 value 为同一对象，则刷新访问时间.

        用于请求结束时续约 TTL，避免无条件 put 用旧实例覆盖并发创建的新实例。
        同一对象仍挂在 cache 上时，即使时间戳已过期也会续期（执行期间无 get 触达）。
        """
        async with self._lock:
            if key not in self._cache:
                return False

            cached_value, _timestamp = self._cache[key]
            if cached_value is not value:
                return False

            self._cache[key] = (value, time.time())
            self._cache.move_to_end(key)
            return True

    async def remove(self, key: Hashable) -> None:
        """删除缓存项."""
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        """清空缓存."""
        async with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def snapshot_values_nowait(self) -> list[Any]:
        """Return cached values for sync callers (best-effort, no async lock).

        Entries are stored as ``(value, timestamp)``; malformed entries are skipped.
        """
        values: list[Any] = []
        for entry in self._cache.values():
            if not isinstance(entry, tuple) or len(entry) < 1:
                continue
            values.append(entry[0])
        return values

    def get_nowait(self, key: Hashable) -> Any | None:
        """Sync read for callers that cannot await (best-effort, no async lock)."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if not isinstance(entry, tuple) or len(entry) < 2:
            return None
        value, timestamp = entry
        if self._is_expired(timestamp):
            return None
        return value

    def put_nowait(self, key: Hashable, value: Any) -> None:
        """Sync write for ``__init__`` / early boot (best-effort, no async lock)."""
        if key in self._cache:
            self._cache.pop(key)
        elif self._max_size is not None and len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = (value, time.time())

    async def keys(self) -> list[Hashable]:
        async with self._lock:
            if self._ttl is not None:
                expired_keys = [
                    key
                    for key, (_, timestamp) in self._cache.items()
                    if self._is_expired(timestamp)
                ]
                for key in expired_keys:
                    del self._cache[key]
            return list(self._cache.keys())
