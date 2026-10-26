# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内存 Ephemeral backend。"""

from __future__ import annotations

import asyncio


class MemoryEphemeralBackend:
    """namespace 隔离的进程内 KV + Hash 存储。"""

    def __init__(self, namespace: str) -> None:
        self._namespace = str(namespace or "default").strip() or "default"
        self._kv: dict[str, bytes] = {}
        self._hashes: dict[str, dict[str, bytes]] = {}
        self._lock = asyncio.Lock()

    @property
    def namespace(self) -> str:
        return self._namespace

    def _scoped(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def get(self, key: str) -> bytes | None:
        scoped = self._scoped(key)
        async with self._lock:
            return self._kv.get(scoped)

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        _ = ttl
        scoped = self._scoped(key)
        async with self._lock:
            self._kv[scoped] = bytes(value)

    async def delete(self, key: str) -> None:
        scoped = self._scoped(key)
        async with self._lock:
            self._kv.pop(scoped, None)

    async def hget(self, hash_key: str, field: str) -> bytes | None:
        scoped = self._scoped(hash_key)
        async with self._lock:
            bucket = self._hashes.get(scoped)
            if bucket is None:
                return None
            raw = bucket.get(field)
            return None if raw is None else bytes(raw)

    async def hset(self, hash_key: str, field: str, value: bytes) -> None:
        scoped = self._scoped(hash_key)
        async with self._lock:
            bucket = self._hashes.setdefault(scoped, {})
            bucket[field] = bytes(value)

    async def hdel(self, hash_key: str, field: str) -> None:
        scoped = self._scoped(hash_key)
        async with self._lock:
            bucket = self._hashes.get(scoped)
            if bucket is None:
                return
            bucket.pop(field, None)
            if not bucket:
                self._hashes.pop(scoped, None)

    async def hgetall(self, hash_key: str) -> dict[str, bytes]:
        scoped = self._scoped(hash_key)
        async with self._lock:
            bucket = self._hashes.get(scoped)
            if not bucket:
                return {}
            return {k: bytes(v) for k, v in bucket.items()}


__all__ = ["MemoryEphemeralBackend"]
