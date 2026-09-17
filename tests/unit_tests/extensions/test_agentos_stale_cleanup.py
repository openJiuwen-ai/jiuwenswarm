# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentManager
from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo, AgentStatus
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    InstanceRecord,
    instance_service_id,
)
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.stale_cleanup import cleanup_stale_sandboxes
from tests.unit_tests.extensions.test_agentos_router import (
    FakeRegistryClient,
    FakeYuanRongClient,
    _router_client,
)


class _CleanupRegistry:
    enabled = True
    node = "192.168.0.12"

    def __init__(self, records: list[InstanceRecord]) -> None:
        self.records = list(records)
        self.unregistered: list[str] = []
        self.list_calls = 0

    async def list_instances(self, **kwargs: Any) -> list[InstanceRecord]:
        self.list_calls += 1
        assert kwargs.get("include_unhealthy") is True
        return list(self.records)

    async def unregister_instance(self, service_id: str) -> dict[str, Any]:
        self.unregistered.append(service_id)
        return {"service_id": service_id, "deleted": True}

    async def close(self) -> None:
        return None


def _record(*, user: str, framework: str, instance_id: str) -> InstanceRecord:
    return InstanceRecord(
        service_id=instance_service_id(user, framework),
        framework=framework,
        instance_id=instance_id,
        user=user,
        status="运行",
    )


@pytest.mark.asyncio
async def test_cleanup_deletes_all_listed_sandboxes_and_unregisters() -> None:
    yuanrong = FakeYuanRongClient()
    records = [
        _record(user="u1", framework="jiuwenswarm", instance_id="sbx-old-1"),
        _record(user="u2", framework="opencode", instance_id="sbx-old-2"),
    ]
    registry = _CleanupRegistry(records)
    processed = await cleanup_stale_sandboxes(
        yuanrong=yuanrong,
        registry=registry,  # type: ignore[arg-type]
        agent_manager=AgentManager(),
    )
    assert processed == 2
    assert yuanrong.delete_calls == ["sbx-old-1", "sbx-old-2"]
    assert registry.unregistered == [row.service_id for row in records]


@pytest.mark.asyncio
async def test_cleanup_continues_after_one_record_fails() -> None:
    yuanrong = FakeYuanRongClient()
    records = [
        _record(user="u1", framework="jiuwenswarm", instance_id="sbx-boom"),
        _record(user="u2", framework="opencode", instance_id="sbx-ok"),
    ]
    registry = _CleanupRegistry(records)

    class _FlakyManager(AgentManager):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_all_agents(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("snapshot boom")
            return await super().list_all_agents()

    processed = await cleanup_stale_sandboxes(
        yuanrong=yuanrong,
        registry=registry,  # type: ignore[arg-type]
        agent_manager=_FlakyManager(),
    )
    assert processed == 2
    assert yuanrong.delete_calls == ["sbx-ok"]
    assert registry.unregistered == [instance_service_id("u2", "opencode")]


@pytest.mark.asyncio
async def test_cleanup_skips_live_sandbox_and_service() -> None:
    yuanrong = FakeYuanRongClient()
    manager = AgentManager()
    live = AgentInfo(
        user_id="u1",
        agent_type="jiuwenswarm",
        sandbox_id="sbx-live",
        status=AgentStatus.READY,
    )

    async def creator(info: AgentInfo) -> AgentInfo:
        info.sandbox_id = "sbx-live"
        info.status = AgentStatus.READY
        return info

    await manager.get_or_create_agent(
        live.user_id,
        live.agent_type,
        creator=creator,
        metadata={},
    )

    records = [
        _record(user="u1", framework="jiuwenswarm", instance_id="sbx-live"),
        _record(user="u2", framework="opencode", instance_id="sbx-stale"),
    ]
    registry = _CleanupRegistry(records)
    await cleanup_stale_sandboxes(
        yuanrong=yuanrong,
        registry=registry,  # type: ignore[arg-type]
        agent_manager=manager,
    )
    assert yuanrong.delete_calls == ["sbx-stale"]
    assert registry.unregistered == [instance_service_id("u2", "opencode")]


@pytest.mark.asyncio
async def test_connect_lists_and_clears_sandboxes_in_background() -> None:
    yuanrong = FakeYuanRongClient()
    records = [_record(user="u1", framework="opencode", instance_id="sbx-left")]
    registry = _CleanupRegistry(records)
    client = _router_client(yuanrong, registry, AgentManager())  # type: ignore[arg-type]
    await client.connect("http://yuanrong.test")
    task = client._stale_cleanup_task
    assert task is not None
    await task
    assert yuanrong.delete_calls == ["sbx-left"]
    assert registry.unregistered == [records[0].service_id]
    await client.disconnect()
    assert client._stale_cleanup_task is None


@pytest.mark.asyncio
async def test_connect_skips_cleanup_when_registry_disabled() -> None:
    yuanrong = FakeYuanRongClient()
    client = _router_client(yuanrong, FakeRegistryClient(), AgentManager())
    await client.connect("http://yuanrong.test")
    assert client._stale_cleanup_task is None
    assert yuanrong.delete_calls == []
    await client.disconnect()
