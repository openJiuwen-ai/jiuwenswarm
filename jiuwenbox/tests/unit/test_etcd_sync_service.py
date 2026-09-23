# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for PolicySyncService -- no etcd, no real SandboxManager."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenbox.models.sandbox import PolicyMode
from jiuwenbox.server.etcd_sync import ETCD_CONFIG_KEY_ENV, ETCD_ENDPOINTS_ENV
from jiuwenbox.server.etcd_sync.client import FetchResult, PolicySyncClient
from jiuwenbox.server.etcd_sync.service import PolicySyncService


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.side_effect: Exception | None = None
        self.result: dict = {
            "updated": ["sb-1"],
            "skipped": [],
            "failed": [],
            "default_updated": True,
            "default_policy": {"name": "merged"},
        }

    async def update_all_policies(self, **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        self.calls.append(kwargs)
        return self.result


def _fetched(section: dict | None, mod_revision: int) -> FetchResult:
    return FetchResult(
        section=section if section is not None else {},
        metadata={"_version": 1},
        mod_revision=mod_revision,
    )


def _service(manager: _FakeManager, endpoints: list[str] | None = None) -> PolicySyncService:
    return PolicySyncService(
        manager,
        etcd_endpoints=endpoints or ["http://etcd.test:2379"],
    )


@pytest.mark.asyncio
async def test_apply_passes_fragment_with_override_and_both_flags():
    manager = _FakeManager()
    service = _service(manager)
    fragment = {"network": {"egress": {"default": "deny"}}}

    await service.apply(_fetched(fragment, 11))

    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call["policy_data"] == fragment
    assert call["policy_mode"] is PolicyMode.OVERRIDE
    assert call["update_default_policy"] is True
    assert call["update_existing_sandboxes"] is True
    assert service.last_applied_mod_revision == 11


@pytest.mark.asyncio
async def test_apply_skips_same_revision_across_reconnect():
    manager = _FakeManager()
    service = _service(manager)
    fragment = {"network": {"egress": {"default": "deny"}}}

    await service.apply(_fetched(fragment, 11))
    await service.apply(_fetched(fragment, 11))

    assert len(manager.calls) == 1
    assert service.last_applied_mod_revision == 11


@pytest.mark.asyncio
async def test_apply_older_revision_is_ignored():
    manager = _FakeManager()
    service = _service(manager)

    await service.apply(_fetched({"network": {}}, 11))
    await service.apply(_fetched({"network": {"egress": {"default": "allow"}}}, 9))

    assert len(manager.calls) == 1
    assert service.last_applied_mod_revision == 11


@pytest.mark.asyncio
async def test_empty_or_non_mapping_section_does_not_call_manager():
    manager = _FakeManager()
    service = _service(manager)

    await service.apply(_fetched({}, 3))
    await service.apply(FetchResult(section={}, metadata={}, mod_revision=4))

    assert manager.calls == []
    assert service.last_applied_mod_revision == 4


@pytest.mark.asyncio
async def test_manager_exception_does_not_advance_revision():
    manager = _FakeManager()
    manager.side_effect = ValueError("bad policy")
    service = _service(manager)
    fragment = {"network": {"egress": {"default": "bogus"}}}

    await service.apply(_fetched(fragment, 5))
    assert service.last_applied_mod_revision == 0

    manager.side_effect = None
    await service.apply(_fetched({"network": {"egress": {"default": "deny"}}}, 6))
    assert len(manager.calls) == 1
    assert service.last_applied_mod_revision == 6


@pytest.mark.asyncio
async def test_skipped_and_failed_are_not_treated_as_failure():
    manager = _FakeManager()
    manager.result = {
        "updated": [],
        "skipped": [{"sandbox_id": "host-sb", "reason": "network.mode is host"}],
        "failed": [{"sandbox_id": "dead", "error": "gone"}],
        "default_updated": True,
        "default_policy": {},
    }
    service = _service(manager)

    await service.apply(_fetched({"network": {"egress": {"default": "deny"}}}, 8))

    assert service.last_applied_mod_revision == 8
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_start_disabled_without_endpoints():
    manager = _FakeManager()
    service = PolicySyncService(manager, etcd_endpoints=[])
    assert service.enabled is False
    await service.start()
    assert service.is_running is False
    await service.stop()


@pytest.mark.asyncio
async def test_from_env_reads_endpoints_and_key(monkeypatch):
    monkeypatch.setenv(ETCD_ENDPOINTS_ENV, "http://10.0.0.1:32379,http://10.0.0.2:32379")
    monkeypatch.setenv(ETCD_CONFIG_KEY_ENV, "/custom/key")
    service = PolicySyncService.from_env(_FakeManager())
    assert service.enabled is True
    assert service.endpoints == ["http://10.0.0.1:32379", "http://10.0.0.2:32379"]
    assert service.key == "/custom/key"


@pytest.mark.asyncio
async def test_from_env_disabled_when_unset(monkeypatch):
    monkeypatch.delenv(ETCD_ENDPOINTS_ENV, raising=False)
    monkeypatch.delenv(ETCD_CONFIG_KEY_ENV, raising=False)
    service = PolicySyncService.from_env(_FakeManager())
    assert service.enabled is False


@pytest.mark.asyncio
async def test_start_stop_cancels_watch_task():
    manager = _FakeManager()
    started = asyncio.Event()

    class _HangClient(PolicySyncClient):
        def __init__(self) -> None:
            super().__init__(etcd_endpoints=["http://etcd.test:2379"])

        async def watch_loop(self, on_event):
            started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            return None

    service = PolicySyncService(
        manager,
        etcd_endpoints=["http://etcd.test:2379"],
        client=_HangClient(),
    )
    await service.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    assert service.is_running is True
    await service.stop()
    assert service.is_running is False


@pytest.mark.asyncio
async def test_watcher_survives_apply_exception_and_applies_next():
    """watch_loop + apply: a failing revision must not kill the loop."""
    manager = _FakeManager()
    manager.side_effect = RuntimeError("boom")
    service = _service(manager)

    await service.apply(_fetched({"network": {"egress": {"default": "deny"}}}, 1))
    assert service.last_applied_mod_revision == 0

    manager.side_effect = None
    await service.apply(_fetched({"network": {"egress": {"default": "allow"}}}, 2))
    assert service.last_applied_mod_revision == 2
    assert len(manager.calls) == 1
