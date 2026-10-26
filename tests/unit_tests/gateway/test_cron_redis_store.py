# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Cron Redis store unit tests (mock Redis client)."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.redis.redis_keys import cron_jobs_hash_rel
from jiuwenswarm.gateway.cron.redis_store import RedisCronJobStore


class _MockRedisClient:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    async def hdel(self, key: str, field: str) -> None:
        bucket = self._hashes.get(key)
        if bucket is not None:
            bucket.pop(field, None)

    def hash_contains(self, key: str) -> bool:
        return key in self._hashes

    def hash_field_contains(self, key: str, field: str) -> bool:
        return field in self._hashes.get(key, {})


@pytest.mark.asyncio
async def test_redis_store_create_list_delete() -> None:
    client = _MockRedisClient()
    store = RedisCronJobStore(client, gateway_instance_id="gw-a")  # type: ignore[arg-type]

    job = await store.create_job(
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="test",
        targets="web:default",
    )
    assert job.id
    jobs_hash = cron_jobs_hash_rel("gw-a")
    assert client.hash_contains(jobs_hash)
    assert client.hash_field_contains(jobs_hash, job.id)
    assert await store.get_revision() == 0

    jobs = await store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == job.id

    updated = await store.update_job(job.id, {"name": "daily-updated"})
    assert updated.name == "daily-updated"

    assert await store.delete_job(job.id) is True
    assert await store.list_jobs() == []


@pytest.mark.asyncio
async def test_redis_store_isolated_by_gateway_instance_id() -> None:
    client = _MockRedisClient()
    store_a = RedisCronJobStore(client, gateway_instance_id="gw-a")  # type: ignore[arg-type]
    store_b = RedisCronJobStore(client, gateway_instance_id="gw-b")  # type: ignore[arg-type]

    job_a = await store_a.create_job(
        name="a",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="a",
        targets="web",
    )
    await store_b.create_job(
        name="b",
        cron_expr="0 10 * * *",
        timezone="Asia/Shanghai",
        description="b",
        targets="web",
    )

    assert len(await store_a.list_jobs()) == 1
    assert (await store_a.list_jobs())[0].id == job_a.id
    assert await store_a.get_job(job_a.id) is not None
    assert await store_a.get_job((await store_b.list_jobs())[0].id) is None

    assert len(await store_b.list_jobs()) == 1
    assert (await store_b.list_jobs())[0].name == "b"


@pytest.mark.asyncio
async def test_factory_standalone_returns_file_store(tmp_path, monkeypatch) -> None:
    from jiuwenswarm.extensions.redis import init_gateway_redis_from_config, shutdown_gateway_redis
    from jiuwenswarm.gateway.cron.factory import create_gateway_cron_store
    from jiuwenswarm.gateway.cron.store import FileCronJobStore

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    await init_gateway_redis_from_config({"gateway": {"deployment_mode": "standalone"}})
    try:
        monkeypatch.setattr(
            "jiuwenswarm.gateway.cron.factory.get_cron_jobs_path",
            lambda: tmp_path / "cron_jobs.json",
        )
        store = await create_gateway_cron_store()
        assert isinstance(store, FileCronJobStore)
        assert store.path == tmp_path / "cron_jobs.json"
    finally:
        await shutdown_gateway_redis()


@pytest.mark.asyncio
async def test_factory_active_standby_without_redis_raises(monkeypatch) -> None:
    from jiuwenswarm.gateway.cron.factory import create_gateway_cron_store

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_declared_deployment_mode",
        lambda: "active-standby",
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_effective_distributed_redis_active",
        lambda: False,
    )
    with pytest.raises(RuntimeError, match="requires Redis"):
        await create_gateway_cron_store()


@pytest.mark.asyncio
async def test_factory_active_standby_requires_instance_id(monkeypatch) -> None:
    from jiuwenswarm.gateway.cron.factory import create_gateway_cron_store

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_declared_deployment_mode",
        lambda: "active-standby",
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_effective_distributed_redis_active",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_gateway_redis_client",
        lambda: _MockRedisClient(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_gateway_instance_id",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="gateway.instance_id is required"):
        await create_gateway_cron_store()


@pytest.mark.asyncio
async def test_factory_without_agent_runtime_uses_file(tmp_path, monkeypatch) -> None:
    from jiuwenswarm.gateway.cron.factory import create_gateway_cron_store
    from jiuwenswarm.gateway.cron.store import FileCronJobStore

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_declared_deployment_mode",
        lambda: "active-standby",
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.factory.get_cron_jobs_path",
        lambda: tmp_path / "cron_jobs.json",
    )
    store = await create_gateway_cron_store()
    assert isinstance(store, FileCronJobStore)
