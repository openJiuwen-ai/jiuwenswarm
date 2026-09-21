# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for etcd_sync.client -- fake etcd, no network."""

from __future__ import annotations

import asyncio

import pytest
import yaml

from jiuwenbox.server.etcd_sync import COMPONENT_NAME, CONFIG_SYNC_KEY
from jiuwenbox.server.etcd_sync.client import (
    EtcdKv,
    EtcdRangeResult,
    FetchResult,
    PolicySyncClient,
    extract_section,
    parse_etcd_endpoints,
)


def test_extract_section_extracts_component_and_metadata():
    doc = {
        "_version": 1,
        "_revision": 7,
        "_updated_at": "2026-09-04T10:00:00+08:00",
        "gateway": {"sandbox": {"cpu": 2000}},
        "jiuwenbox": {"network": {"egress": {"default": "deny"}}},
    }
    section, metadata = extract_section(doc)

    assert section == {"network": {"egress": {"default": "deny"}}}
    assert "gateway" not in section
    assert metadata == {
        "_version": 1,
        "_revision": 7,
        "_updated_at": "2026-09-04T10:00:00+08:00",
    }


def test_extract_section_missing_component_returns_empty():
    section, metadata = extract_section({"_version": 1, "gateway": {}})
    assert section == {}
    assert metadata == {"_version": 1}


def test_extract_section_non_mapping_section_returns_empty():
    section, _ = extract_section({"jiuwenbox": ["not", "a", "dict"]})
    assert section == {}


def test_extract_section_non_mapping_document_returns_empty():
    assert extract_section(None) == ({}, {})
    assert extract_section("string") == ({}, {})


def test_parse_etcd_endpoints_csv_and_list():
    assert parse_etcd_endpoints("http://a:2379, http://b:2379") == [
        "http://a:2379",
        "http://b:2379",
    ]
    assert parse_etcd_endpoints(["http://a:2379", "ftp://x", ""]) == [
        "http://a:2379",
    ]
    assert parse_etcd_endpoints("") == []
    assert parse_etcd_endpoints("127.0.0.1:2379") == ["http://127.0.0.1:2379"]


def test_decode_extracts_jiuwenbox_and_ignores_gateway():
    payload = yaml.safe_dump(
        {
            "_version": 1,
            "gateway": {"sandbox": {"cpu": 2000}},
            "jiuwenbox": {"network": {"egress": {"default": "deny"}}},
        }
    )
    kv = EtcdKv(
        key=CONFIG_SYNC_KEY.encode("utf-8"),
        value=payload.encode("utf-8"),
        mod_revision=9,
    )
    fetched = PolicySyncClient._decode(kv)
    assert fetched.mod_revision == 9
    assert fetched.section == {"network": {"egress": {"default": "deny"}}}
    assert fetched.metadata["_version"] == 1
    assert COMPONENT_NAME not in fetched.metadata


def test_decode_invalid_yaml_yields_empty_section():
    kv = EtcdKv(key=b"/k", value=b"::::not yaml", mod_revision=3)
    fetched = PolicySyncClient._decode(kv)
    assert fetched.section == {}
    assert fetched.mod_revision == 3


class _FakeEtcd:
    def __init__(self) -> None:
        self.kvs: list[EtcdKv] = []
        self.revision = 7
        self.watch_batches: list[list[EtcdKv]] = []
        self.watch_start_revision: int | None = None
        self.closed = False

    async def range(self, key: bytes) -> EtcdRangeResult:
        matches = [kv for kv in self.kvs if kv.key == key]
        return EtcdRangeResult(kvs=matches, revision=self.revision)

    async def watch(self, key: bytes, *, start_revision: int | None = None):
        self.watch_start_revision = start_revision
        for batch in self.watch_batches:
            yield batch

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_watch_loop_applies_snapshot_then_watch_event():
    fake = _FakeEtcd()
    key = CONFIG_SYNC_KEY.encode("utf-8")
    first = yaml.safe_dump({"jiuwenbox": {"network": {"egress": {"default": "deny"}}}})
    second = yaml.safe_dump({"jiuwenbox": {"network": {"egress": {"default": "allow"}}}})
    fake.kvs = [EtcdKv(key=key, value=first.encode("utf-8"), mod_revision=4)]
    fake.watch_batches = [
        [EtcdKv(key=key, value=second.encode("utf-8"), mod_revision=5)],
    ]

    events: list[FetchResult] = []

    async def on_event(fetched: FetchResult) -> None:
        events.append(fetched)
        if len(events) >= 2:
            raise asyncio.CancelledError

    client = PolicySyncClient(
        etcd_endpoints=["http://etcd.test:2379"],
        etcd_client_factory=lambda endpoints, timeout=10.0: fake,
    )
    with pytest.raises(asyncio.CancelledError):
        await client.watch_loop(on_event)

    assert [item.mod_revision for item in events] == [4, 5]
    assert events[0].section["network"]["egress"]["default"] == "deny"
    assert events[1].section["network"]["egress"]["default"] == "allow"
    assert fake.watch_start_revision == 8  # snapshot revision 7 + 1
