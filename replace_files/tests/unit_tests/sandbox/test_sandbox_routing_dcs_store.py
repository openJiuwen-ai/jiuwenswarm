"""Unit tests for SandboxRoutingDcsStore."""
from __future__ import annotations

from typing import Any

import pytest

from jiuwenclaw.dcs import DcsClusterConfig
from jiuwenclaw.sandbox.sandbox_routing_dcs_store import SandboxRoutingDcsStore


class InMemoryFakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._ttl: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: Any, nx: bool = False) -> bool:
        if nx and key in self._data:
            return False
        self._data[key] = str(value)
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        if key in self._data:
            self._ttl[key] = seconds
            return True
        return False

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                self._ttl.pop(k, None)
                deleted += 1
        return deleted

    async def aclose(self) -> None:
        return None

    async def scan_iter(self, *, match: str = "*", count: int = 10):
        prefix = match[:-1] if match.endswith("*") else match
        for key in self._data:
            if match.endswith("*"):
                if key.startswith(prefix):
                    yield key
            elif key == match:
                yield key


def _make_store() -> tuple[SandboxRoutingDcsStore, InMemoryFakeRedis]:
    cfg = DcsClusterConfig(host="fake", port=2881, password=None, ttl_seconds=3600)
    store = SandboxRoutingDcsStore(cfg)
    fake = InMemoryFakeRedis()
    store._dcs._client = fake
    return store, fake


@pytest.mark.asyncio
async def test_set_routing_nx_only_first_wins() -> None:
    store, fake = _make_store()
    key = "vibeskill:user:u1"
    assert await store.set_routing_nx(key, sandbox_id="sb-1", gateway_id="gw-1")
    assert await store.set_routing_nx(key, sandbox_id="sb-2", gateway_id="gw-2") is False
    record = await store.get_routing(key)
    assert record is not None
    assert record.sandbox_id == "sb-1"


@pytest.mark.asyncio
async def test_routing_default_ttl_matches_sandbox_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_DCS_TTL_SECONDS", raising=False)
    monkeypatch.delenv("SANDBOX_DURATION_SECONDS", raising=False)
    store, fake = _make_store()
    key = "vibeskill:session:s1"
    await store.save_routing(key, sandbox_id="sb-1", gateway_id="gw-1")
    redis_key = f"jiuwen:sandboxRouting:{key}"
    assert redis_key in fake._data
    assert fake._ttl[redis_key] == 3600


@pytest.mark.asyncio
async def test_routing_ttl_follows_sandbox_duration_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_DCS_TTL_SECONDS", raising=False)
    monkeypatch.setenv("SANDBOX_DURATION_SECONDS", "1800")
    store, fake = _make_store()
    key = "vibeskill:user:u2"
    await store.save_routing(key, sandbox_id="sb-1", gateway_id="gw-1")
    assert fake._ttl[f"jiuwen:sandboxRouting:{key}"] == 1800


@pytest.mark.asyncio
async def test_routing_ttl_from_sandbox_dcs_ttl_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_DCS_TTL_SECONDS", "7200")
    store, fake = _make_store()
    key = "vibeskill:user:u1"
    await store.save_routing(key, sandbox_id="sb-1", gateway_id="gw-1")
    assert fake._ttl[f"jiuwen:sandboxRouting:{key}"] == 7200


@pytest.mark.asyncio
async def test_delete_routing_removes_mapping() -> None:
    store, fake = _make_store()
    key = "vibeskill:user:u1"
    await store.save_routing(key, sandbox_id="sb-1", gateway_id="gw-1")
    await store.delete_routing(key)
    assert await store.get_routing(key) is None
    assert f"jiuwen:sandboxRouting:{key}" not in fake._data