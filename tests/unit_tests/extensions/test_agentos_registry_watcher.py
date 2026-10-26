# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the registry watcher; uses fake etcd, no network."""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.extensions.agentos.registry_watcher import (
    NAMESPACE_ENV,
    registry_prefix,
)
from gateway.extensions.agentos.registry_watcher.client import (
    RegistryWatcherClient,
)
from gateway.extensions.agentos.registry_watcher.decode import (
    decode_record,
    web_svc_port,
)
from jiuwenswarm.common.etcd.client import EtcdKv, EtcdRangeResult

# Registry contract: ``{namespace}/images/image_<sid>`` (no leading slash,
# ``access_mode`` nested under ``data``).
PREFIX = "a2x-registry/images/"


def _key(name: str) -> str:
    return f"{PREFIX}image_{name}"


def _record(name: str, *, web_svc=None, web=None) -> str:
    rows = []
    if web is not None:
        rows.append({"name": "web", "port": str(web)})
    if web_svc is not None:
        rows.append({"name": "web_svc_port", "port": str(web_svc)})
    return json.dumps(
        {
            "service_id": f"image_{name}",
            "name": name,
            "framework": name,
            "version": "v1",
            "data": {"access_mode": rows},
        }
    )


class FakeEtcd:
    """Minimal stand-in for EtcdJsonClient (prefix range + watch)."""

    def __init__(self) -> None:
        self.kvs: dict[bytes, bytes] = {}
        self.revision = 1
        self.watch_queue: asyncio.Queue[list[EtcdKv] | None] = asyncio.Queue()
        self.watch_started = asyncio.Event()
        self.watch_start_revisions: list[int | None] = []
        self.closed = False

    def _matches(self, key: bytes, range_end: bytes | None) -> list[bytes]:
        if range_end is None:
            return [key] if key in self.kvs else []
        return sorted(item for item in self.kvs if key <= item < range_end)

    async def range(self, key: bytes, *, range_end: bytes | None = None) -> EtcdRangeResult:
        found = [
            EtcdKv(key=item, value=self.kvs[item], mod_revision=self.revision)
            for item in self._matches(key, range_end)
        ]
        return EtcdRangeResult(kvs=found, revision=self.revision)

    async def watch_prefix(self, prefix: bytes, *, start_revision: int | None = None):
        self.watch_start_revisions.append(start_revision)
        self.watch_started.set()
        while True:
            batch = await self.watch_queue.get()
            if batch is None:
                return
            yield batch

    async def aclose(self) -> None:
        self.closed = True

    def set(self, key: str, value: str) -> None:
        self.kvs[key.encode()] = value.encode()

    def forget(self, key: str) -> None:
        self.kvs.pop(key.encode(), None)


def _client(fake: FakeEtcd, **kwargs) -> RegistryWatcherClient:
    return RegistryWatcherClient(
        etcd_endpoints=["http://etcd.test:2379"],
        etcd_client_factory=lambda endpoints, timeout=10.0: fake,
        **kwargs,
    )


# --------------------------------------------------------------------------
# prefix / decode (pure)
# --------------------------------------------------------------------------

def test_registry_prefix_default_and_env(monkeypatch):
    monkeypatch.delenv(NAMESPACE_ENV, raising=False)
    assert registry_prefix() == PREFIX
    assert not PREFIX.startswith("/")

    monkeypatch.setenv(NAMESPACE_ENV, "team-x")
    assert registry_prefix() == "team-x/images/"
    # explicit arg wins over env
    assert registry_prefix("other") == "other/images/"


def test_decode_record_reads_web_svc_port_from_data():
    value = _record("dsh", web=2222, web_svc=19101).encode()
    assert decode_record(value, _key("dsh")) == ("dsh", 19101)


def test_decode_record_name_comes_from_value_not_key():
    # The opaque ``image_<sid>`` key is not a name fallback.
    value = json.dumps(
        {"data": {"access_mode": [{"name": "web_svc_port", "port": "19107"}]}}
    ).encode()
    assert decode_record(value, _key("abc")) is None


def test_decode_record_none_without_web_svc_row():
    # Only a ``web`` (container) row -> no Gateway northbound port.
    value = _record("dsh", web=2222).encode()
    assert decode_record(value, _key("dsh")) is None


def test_decode_record_rejects_bad_json_and_empty():
    assert decode_record(b"", _key("dsh")) is None
    assert decode_record(b"{not json", _key("dsh")) is None
    assert decode_record(b"[1,2,3]", _key("dsh")) is None


def test_decode_record_enforces_port_range():
    # Below / above the default northbound range -> ignored.
    assert decode_record(_record("dsh", web_svc=2222).encode(), _key("dsh")) is None
    assert decode_record(_record("dsh", web_svc=19201).encode(), _key("dsh")) is None
    # Boundaries are inclusive.
    assert decode_record(_record("dsh", web_svc=19101).encode(), _key("dsh")) == ("dsh", 19101)
    assert decode_record(_record("dsh", web_svc=19200).encode(), _key("dsh")) == ("dsh", 19200)


def test_web_svc_port_reads_data_and_tolerates_top_level():
    assert web_svc_port({"data": {"access_mode": [{"name": "WEB_SVC_PORT", "port": "19101"}]}}) == 19101
    assert web_svc_port({"access_mode": [{"name": "web_svc_port", "port": "19102"}]}) == 19102
    assert web_svc_port({"data": {"access_mode": [{"name": "web_svc_port", "port": "bad"}]}}) is None


# --------------------------------------------------------------------------
# enabled
# --------------------------------------------------------------------------

def test_enabled_flags():
    assert RegistryWatcherClient(etcd_endpoints=[]).enabled is False
    assert _client(FakeEtcd()).enabled is True


# --------------------------------------------------------------------------
# fetch_once
# --------------------------------------------------------------------------

async def test_fetch_once_builds_map_skipping_records_without_port():
    fake = FakeEtcd()
    fake.set(_key("dsh"), _record("dsh", web=2222, web_svc=19101))
    fake.set(_key("claude"), _record("claude", web=2222, web_svc=19102))
    fake.set(_key("noweb"), _record("noweb", web=2222))  # no web_svc_port

    assert await _client(fake).fetch_once() == {"dsh": 19101, "claude": 19102}


async def test_fetch_once_ignores_unrelated_keys():
    fake = FakeEtcd()
    fake.set("other/images/image_x", _record("x", web_svc=19101))

    assert await _client(fake).fetch_once() == {}


# --------------------------------------------------------------------------
# watch_loop (re-ranges on every event; keys are opaque)
# --------------------------------------------------------------------------

async def test_watch_loop_emits_snapshot_then_add_and_delete():
    fake = FakeEtcd()
    fake.set(_key("dsh"), _record("dsh", web_svc=19101))
    snaps: list[dict[str, int]] = []

    async def on_change(desired):
        snaps.append(dict(desired))

    client = _client(fake)
    task = asyncio.create_task(client.watch_loop(on_change))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)

    assert snaps[-1] == {"dsh": 19101}
    assert fake.watch_start_revisions == [fake.revision + 1]

    # PUT a new agent: store it, then signal a change.
    fake.set(_key("claude"), _record("claude", web_svc=19102))
    await fake.watch_queue.put(
        [EtcdKv(key=_key("claude").encode(), value=_record("claude", web_svc=19102).encode())]
    )
    await asyncio.sleep(0.05)
    assert snaps[-1] == {"dsh": 19101, "claude": 19102}

    # DELETE: remove the key, then signal; the re-range drops it.
    fake.forget(_key("dsh"))
    await fake.watch_queue.put([EtcdKv(key=_key("dsh").encode(), value=b"")])
    await asyncio.sleep(0.05)
    assert snaps[-1] == {"claude": 19102}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_loop_repulls_after_stream_ends(monkeypatch):
    monkeypatch.setattr(
        "gateway.extensions.agentos.registry_watcher.client._CONNECT_INITIAL_DELAY",
        0.01,
    )
    monkeypatch.setattr(
        "gateway.extensions.agentos.registry_watcher.client._CONNECT_MAX_DELAY",
        0.02,
    )
    fake = FakeEtcd()
    fake.set(_key("dsh"), _record("dsh", web_svc=19101))
    snaps: list[dict[str, int]] = []

    async def on_change(desired):
        snaps.append(dict(desired))

    task = asyncio.create_task(_client(fake).watch_loop(on_change))
    await asyncio.wait_for(fake.watch_started.wait(), timeout=2)
    assert snaps[-1] == {"dsh": 19101}

    # A change made while the stream is down is picked up by the fresh full pull.
    fake.set(_key("claude"), _record("claude", web_svc=19102))
    await fake.watch_queue.put(None)  # end stream -> reconnect
    await asyncio.sleep(0.1)

    assert snaps[-1] == {"dsh": 19101, "claude": 19102}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
