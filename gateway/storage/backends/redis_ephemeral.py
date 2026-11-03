# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Redis Ephemeral backend。client 由装配层注入。"""

from __future__ import annotations

from typing import Any


def _to_bytes(raw: Any) -> bytes | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw
    return str(raw).encode("utf-8")


class RedisEphemeralBackend:
    """Redis 实现的 Ephemeral 存储；未注入 client 时 ``available`` 为 False。"""

    def __init__(
        self,
        namespace: str,
        *,
        client: Any | None = None,
        key_prefix: str = "",
    ) -> None:
        self._namespace = str(namespace or "default").strip() or "default"
        self._redis = client
        self._key_prefix = str(key_prefix or "")

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def available(self) -> bool:
        return self._redis is not None

    def _scoped(self, key: str) -> str:
        if self._key_prefix:
            return f"{self._key_prefix}{self._namespace}:{key}"
        return f"{self._namespace}:{key}"

    async def get(self, key: str) -> bytes | None:
        if self._redis is None:
            return None
        return _to_bytes(await self._redis.get(self._scoped(key)))

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        if self._redis is None:
            return
        await self._redis.set(
            self._scoped(key),
            value.decode("utf-8") if isinstance(value, bytes) else value,
            ttl_seconds=ttl,
        )

    async def delete(self, key: str) -> None:
        if self._redis is None:
            return
        await self._redis.delete(self._scoped(key))

    async def hget(self, hash_key: str, field: str) -> bytes | None:
        if self._redis is None:
            return None
        return _to_bytes(await self._redis.hget(self._scoped(hash_key), field))

    async def hset(self, hash_key: str, field: str, value: bytes) -> None:
        if self._redis is None:
            return
        await self._redis.hset(
            self._scoped(hash_key),
            field,
            value.decode("utf-8") if isinstance(value, bytes) else value,
        )

    async def hdel(self, hash_key: str, field: str) -> None:
        if self._redis is None:
            return
        hdel = getattr(self._redis, "hdel", None)
        if callable(hdel):
            await hdel(self._scoped(hash_key), field)

    async def hgetall(self, hash_key: str) -> dict[str, bytes]:
        if self._redis is None:
            return {}
        raw = await self._redis.hgetall(self._scoped(hash_key))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, bytes] = {}
        for k, v in raw.items():
            b = _to_bytes(v)
            if b is not None:
                out[str(k)] = b
        return out


__all__ = ["RedisEphemeralBackend"]
