# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for AgentOS config updater client; uses fake etcd, no network."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.extensions.agentos.config_updater.client import ConfigUpdaterClient
from jiuwenswarm.common.etcd.client import (
    EtcdError,
    EtcdKv,
    EtcdRangeResult,
)

KEY = "/agentos/config/data-plane"


class FakeEtcd:
    """Minimal stand-in for EtcdJsonClient."""

    def __init__(self) -> None:
        self.kvs: dict[bytes, tuple[bytes, int]] = {}
        self.revision = 1
        self.watch_queue: asyncio.Queue[list[EtcdKv] | None] = asyncio.Queue()
        self.watch_started = asyncio.Event()
        self.watch_start_revisions: list[int | None] = []
        self.range_calls = 0
        self.closed = False
        self.range_error: Exception | None = None

    def _matches(self, key: bytes, range_end: bytes | None) -> list[bytes]:
        if range_end is None:
            return [key] if key in self.kvs else []
        return [item for item in self.kvs if key <= item < range_end]

    async def range(self, key: bytes, *, range_end: bytes | None = None) -> EtcdRangeResult:
        self.range_calls += 1
        if self.range_error is not None:
            raise self.range_error
        found: list[EtcdKv] = []
        for item in self._matches(key, range_end):
            value, mod_rev = self.kvs[item]
            found.append(EtcdKv(key=item, value=value, mod_revision=mod_rev))
        return EtcdRangeResult(kvs=found, revision=self.revision)

    async def watch_prefix(
        self,
        prefix: bytes,
        *,
        start_revision: int | None = None,
    ):
        self.watch_start_revisions.append(start_revision)
        self.watch_started.set()
        while True:
            batch = await self.watch_queue.get()
            if batch is None:
                return
            yield batch

    async def aclose(self) -> None:
        self.closed = True

    def set(self, key: str, value: str, mod_rev: int) -> None:
        self.kvs[key.encode()] = (value.encode(), mod_rev)
        self.revision = max(self.revision, mod_rev)

    def forget(self, key: str) -> None:
        self.kvs.pop(key.encode(), None)


def _client(fake: FakeEtcd, **kwargs) -> ConfigUpdaterClient:
    return ConfigUpdaterClient(
        etcd_endpoints=["http://etcd.test:2379"],
        etcd_client_factory=lambda endpoints, timeout=10.0: fake,
        **kwargs,
    )


# --------------------------------------------------------------------------
# enabled / factory
# --------------------------------------------------------------------------

def test_enabled_false_without_endpoints():
    client = ConfigUpdaterClient(etcd_endpoints=[])
    assert client.enabled is False


def test_enabled_true_with_endpoints():
    assert _client(FakeEtcd()).enabled is True


# --------------------------------------------------------------------------
# fetch_once
# --------------------------------------------------------------------------

async def test_fetch_once_returns_none_when_key_absent():
    fake = FakeEtcd()
    assert await _client(fake).fetch_once() is None


async def test_fetch_once_decodes_section_and_metadata():
    fake = FakeEtcd()
    fake.set(
        KEY,
        "gateway:\n  sandbox:\n    cpu: 2000\n_version: 1\n_revision: 3\n",
        7,
    )
    fetched = await _client(fake).fetch_once()

    assert fetched is not None
    assert fetched.section == {"sandbox": {"cpu": 2000}}
    assert fetched.metadata == {"_version": 1, "_revision": 3}
    assert fetched.mod_revision == 7


async def test_fetch_once_ignores_neighbouring_prefix_key():
    # A prefix-range read would also match this; the exact-key filter must not.
    fake = FakeEtcd()
    fake.set(KEY + "-backup", "gateway:\n  sandbox:\n    cpu: 1\n", 9)
    assert await _client(fake).fetch_once() is None


async def test_fetch_once_propagates_transport_error():
    fake = FakeEtcd()
    fake.range_error = EtcdError("boom")
    with pytest.raises(EtcdError):
        await _client(fake).fetch_once()


async def test_fetch_once_parse_error_yields_empty_section():
    fake = FakeEtcd()
    fake.set(KEY, "gateway: [unclosed\n", 2)
    fetched = await _client(fake).fetch_once()

    assert fetched is not None
    assert fetched.section == {}
    assert fetched.mod_revision == 2


# --------------------------------------------------------------------------
# watch_loop
# --------------------------------------------------------------------------

async def test_watch_loop_pulls_once_before_watching():
    fake = FakeEtcd()
    fake.set(KEY, "gateway:\n  sandbox:\n    cpu: 2000\n", 5)
    seen: list[int] = []

    async def on_event(fetched):
        seen.append(fetched.mod_revision)

    client = _client(fake)
    task = asyncio.create_task(client.watch_loop(on_event))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)

    assert seen == [5]  # initial full pull
    assert fake.watch_start_revisions == [fake.revision + 1]

    fake.set(KEY, "gateway:\n  sandbox:\n    cpu: 4000\n", 6)
    await fake.watch_queue.put(
        [EtcdKv(key=KEY.encode(), value=fake.kvs[KEY.encode()][0], mod_revision=6)]
    )
    await asyncio.sleep(0.05)

    assert seen == [5, 6]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_loop_filters_neighbour_key_events():
    fake = FakeEtcd()
    seen: list[int] = []

    async def on_event(fetched):
        seen.append(fetched.mod_revision)

    client = _client(fake)
    task = asyncio.create_task(client.watch_loop(on_event))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)

    # Only a neighbouring key changed -> must be ignored.
    await fake.watch_queue.put(
        [
            EtcdKv(
                key=(KEY + "-backup").encode(),
                value=b"gateway:\n  sandbox:\n    cpu: 1\n",
                mod_revision=9,
            )
        ]
    )
    await asyncio.sleep(0.05)

    assert seen == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_loop_repulls_after_stream_ends(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.extensions.agentos.config_updater.client._CONNECT_INITIAL_DELAY",
        0.01,
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.agentos.config_updater.client._CONNECT_MAX_DELAY",
        0.02,
    )
    fake = FakeEtcd()
    fake.set(KEY, "gateway:\n  sandbox:\n    cpu: 2000\n", 5)
    seen: list[int] = []

    async def on_event(fetched):
        seen.append(fetched.mod_revision)

    client = _client(fake)
    task = asyncio.create_task(client.watch_loop(on_event))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)
    assert seen == [5]

    # A change made while the stream is down is also picked up by the fresh
    # full pull performed before the replacement watch is established.
    fake.set(KEY, "gateway:\n  sandbox:\n    cpu: 8000\n", 8)
    await fake.watch_queue.put(None)  # end the stream -> loop reconnects

    await asyncio.sleep(0.1)
    assert 8 in seen

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_loop_catches_change_between_range_and_watch():
    fake = FakeEtcd()
    fake.revision = 5
    fake.set(KEY, "gateway:\n  sandbox:\n    cpu: 2000\n", 5)
    seen: list[int] = []

    async def on_event(fetched):
        seen.append(fetched.mod_revision)
        if fetched.mod_revision == 5:
            fake.revision = 6
            fake.set(KEY, "gateway:\n  sandbox:\n    cpu: 4000\n", 6)
            await fake.watch_queue.put(
                [EtcdKv(key=KEY.encode(), value=fake.kvs[KEY.encode()][0], mod_revision=6)]
            )

    client = _client(fake)
    task = asyncio.create_task(client.watch_loop(on_event))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)
    await asyncio.sleep(0.05)

    assert fake.watch_start_revisions == [6]
    assert seen == [5, 6]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_loop_uses_latest_exact_event_in_batch():
    fake = FakeEtcd()
    seen: list[int] = []

    async def on_event(fetched):
        seen.append(fetched.mod_revision)

    task = asyncio.create_task(_client(fake).watch_loop(on_event))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)
    await fake.watch_queue.put(
        [
            EtcdKv(key=KEY.encode(), value=b"gateway: {}\n", mod_revision=2),
            EtcdKv(key=KEY.encode(), value=b"gateway: {}\n", mod_revision=3),
        ]
    )
    await asyncio.sleep(0.05)

    assert seen == [3]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


