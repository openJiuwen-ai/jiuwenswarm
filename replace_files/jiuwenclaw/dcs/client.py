from __future__ import annotations

from redis.asyncio.cluster import RedisCluster

from jiuwenclaw.dcs.config import (
    DCS_SOCKET_CONNECT_TIMEOUT_SECONDS,
    DcsClusterConfig,
)


class DcsClusterClient:
    """Lazy Redis Cluster connection wrapper shared by domain DCS stores."""

    def __init__(self, config: DcsClusterConfig) -> None:
        self._config = config
        self._client: RedisCluster | None = None

    @property
    def config(self) -> DcsClusterConfig:
        return self._config

    @property
    def host(self) -> str:
        return self._config.host

    @property
    def ttl_seconds(self) -> int:
        return self._config.ttl_seconds

    def _create_redis_client(self) -> RedisCluster:
        return RedisCluster(
            host=self._config.host,
            port=self._config.port,
            password=self._config.password,
            decode_responses=True,
            require_full_coverage=False,
            socket_connect_timeout=DCS_SOCKET_CONNECT_TIMEOUT_SECONDS,
        )

    async def ensure_connected(self) -> RedisCluster:
        if self._client is None:
            self._client = self._create_redis_client()
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, key: str) -> str | None:
        client = await self.ensure_connected()
        return await client.get(key)

    async def set(self, key: str, value: str) -> None:
        client = await self.ensure_connected()
        await client.set(key, value)

    def _resolve_ttl_seconds(self, ttl_seconds: int | None) -> int:
        if ttl_seconds is not None:
            return max(0, int(ttl_seconds))
        return max(0, int(self._config.ttl_seconds))

    async def expire(self, key: str, *, ttl_seconds: int | None = None) -> bool:
        seconds = self._resolve_ttl_seconds(ttl_seconds)
        if seconds <= 0:
            return False
        client = await self.ensure_connected()
        return bool(await client.expire(key, seconds))

    async def set_with_ttl(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        client = await self.ensure_connected()
        await client.set(key, value)
        await self.expire(key, ttl_seconds=ttl_seconds)

    async def set_nx_with_ttl(self, key: str, value: str, *, ttl_seconds: int | None = None) -> bool:
        """SET key value NX; on success apply EXPIRE. Returns True if the key was set."""
        client = await self.ensure_connected()
        claimed = await client.set(key, value, nx=True)
        if not claimed:
            return False
        await self.expire(key, ttl_seconds=ttl_seconds)
        return True

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        client = await self.ensure_connected()
        return await client.delete(*keys)
