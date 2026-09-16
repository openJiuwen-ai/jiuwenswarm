from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.gateway.cron.etcd_client import (
    EtcdCasError,
    EtcdError,
    EtcdJsonClient,
    EtcdKv,
    EtcdRangeResult,
    prefix_range_end,
)
from jiuwenswarm.gateway.cron.etcd_store import EtcdCronJobStore


class FakeEtcdJsonClient:
    def __init__(self) -> None:
        self.kvs: dict[bytes, tuple[bytes, int]] = {}
        self.revision = 1
        self.watch_queue: asyncio.Queue[list[EtcdKv] | None] = asyncio.Queue()
        self.watch_started = asyncio.Event()

    @property
    def endpoints(self) -> list[str]:
        return ["http://etcd.test:2379"]

    async def aclose(self) -> None:
        return None

    def _matches(self, key: bytes, range_end: bytes | None) -> list[bytes]:
        if range_end is None:
            return [key] if key in self.kvs else []
        return [item for item in self.kvs if key <= item < range_end]

    async def range(self, key: bytes, *, range_end: bytes | None = None) -> EtcdRangeResult:
        found: list[EtcdKv] = []
        for item in self._matches(key, range_end):
            value, mod_rev = self.kvs[item]
            found.append(EtcdKv(key=item, value=value, mod_revision=mod_rev))
        return EtcdRangeResult(kvs=found, revision=self.revision)

    async def put(self, key: bytes, value: bytes) -> int:
        self.revision += 1
        self.kvs[key] = (value, self.revision)
        return self.revision

    async def delete(self, key: bytes) -> int:
        self.kvs.pop(key, None)
        self.revision += 1
        return self.revision

    async def put_if_mod_revision(
        self, key: bytes, value: bytes, *, mod_revision: int
    ) -> int:
        current = self.kvs.get(key)
        current_rev = current[1] if current else 0
        if current_rev != int(mod_revision):
            raise EtcdCasError(f"cas failed key={key!r} mod_revision={mod_revision}")
        return await self.put(key, value)

    async def watch_prefix(self, prefix: bytes):
        self.watch_started.set()
        while True:
            batch = await self.watch_queue.get()
            if batch is None:
                return
            yield batch


def _store(client: FakeEtcdJsonClient | None = None) -> tuple[EtcdCronJobStore, FakeEtcdJsonClient]:
    fake = client or FakeEtcdJsonClient()
    store = EtcdCronJobStore(
        endpoints=["http://etcd.test:2379"],
        client=fake,  # type: ignore[arg-type]
    )
    return store, fake


@pytest.mark.asyncio
async def test_etcd_put_get_list_delete():
    store, _fake = _store()
    created = await store.create_job(
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="ping",
        targets="web",
        user_id="u1",
    )
    fetched = await store.get_job(created.id)
    assert fetched is not None
    assert fetched.name == "daily"
    assert fetched.user_id == "u1"
    listed = await store.list_jobs()
    assert [job.id for job in listed] == [created.id]
    assert await store.delete_job(created.id) is True
    assert await store.get_job(created.id) is None
    assert await store.list_jobs() == []


@pytest.mark.asyncio
async def test_etcd_cas_conflict_retries_once():
    store, fake = _store()
    job = await store.create_job(
        name="cas",
        cron_expr="0 9 * * *",
        timezone="UTC",
        description="x",
        targets="web",
    )
    key = f"/jiuwenswarm/cron/jobs/{job.id}".encode("utf-8")
    original_get = store._get_job_with_rev
    calls = {"n": 0}

    async def _flaky_get(job_id: str):
        existing, rev = await original_get(job_id)
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent writer bumping ModRevision after our read.
            fake.revision += 1
            value, _old = fake.kvs[key]
            fake.kvs[key] = (value, fake.revision)
            return existing, rev
        return existing, rev

    store._get_job_with_rev = _flaky_get  # type: ignore[method-assign]
    updated = await store.update_job(job.id, {"description": "after-cas"})
    assert updated.description == "after-cas"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_etcd_cas_conflict_without_retry_raises():
    store, fake = _store()
    job = await store.create_job(
        name="cas-fail",
        cron_expr="0 9 * * *",
        timezone="UTC",
        description="x",
        targets="web",
    )

    async def _always_cas(*_a, **_k):
        raise EtcdCasError("conflict")

    fake.put_if_mod_revision = _always_cas  # type: ignore[method-assign]
    with pytest.raises(EtcdError):
        await store.update_job(job.id, {"description": "nope"})


@pytest.mark.asyncio
async def test_etcd_prefix_list_isolates_other_keys():
    store, fake = _store()
    await fake.put(b"/jiuwenswarm/cron/other", b"{}")
    job = await store.create_job(
        name="keep",
        cron_expr="0 9 * * *",
        timezone="UTC",
        description="x",
        targets="web",
    )
    jobs = await store.list_jobs()
    assert [item.id for item in jobs] == [job.id]


@pytest.mark.asyncio
async def test_etcd_watch_invokes_callback():
    store, fake = _store()
    hits: list[int] = []

    async def _cb() -> None:
        hits.append(1)

    task = asyncio.create_task(store.watch(_cb))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)
    # First successful connect already reloads once.
    assert hits == [1]
    await fake.watch_queue.put([EtcdKv(key=b"k", value=b"v", mod_revision=2)])
    await asyncio.sleep(0.05)
    assert hits == [1, 1]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_prefix_range_end_increments_last_byte():
    assert prefix_range_end(b"/jiuwenswarm/cron/jobs/") == b"/jiuwenswarm/cron/jobs0"


def test_normalize_endpoint_adds_scheme():
    client = EtcdJsonClient(["127.0.0.1:2379"])
    assert client.endpoints == ["http://127.0.0.1:2379"]


@pytest.mark.asyncio
async def test_watch_triggers_scheduler_reload():
    from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService

    class _StubAgent:
        async def send_request(self, *_a, **_k):
            raise AssertionError("not used")

    class _StubHandler:
        async def publish_robot_messages(self, _msg):
            return None

    store, fake = _store()
    svc = CronSchedulerService(
        store=store,
        agent_client=_StubAgent(),  # type: ignore[arg-type]
        message_handler=_StubHandler(),  # type: ignore[arg-type]
    )
    await svc.start()
    try:
        await asyncio.wait_for(fake.watch_started.wait(), timeout=2)
        assert svc._jobs == {}
        job = await store.create_job(
            name="watched",
            cron_expr="0 0 9 * * ? *",
            timezone="UTC",
            description="x",
            targets="web",
        )
        await fake.watch_queue.put(
            [EtcdKv(key=store._job_key(job.id), value=b"{}", mod_revision=2)]
        )
        for _ in range(20):
            if job.id in svc._jobs:
                break
            await asyncio.sleep(0.05)
        assert job.id in svc._jobs
        assert svc._jobs[job.id].name == "watched"
    finally:
        await svc.stop()
