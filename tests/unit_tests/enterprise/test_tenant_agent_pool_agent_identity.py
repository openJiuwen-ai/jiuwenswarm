# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TenantAgentPool writes the workspace agent identity on scope entry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.enterprise.tenant import agent_identity_store
from jiuwenswarm.enterprise.tenant.agent_identity_store import agent_identity_path
from jiuwenswarm.enterprise.tenant.multi_tenant_strategy import MultiTenantStrategy
from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool

_AGENT_ID = "EyMFedy4mZ0b8btF1gxJFjsq4FOl7Pj6m2EYP3Ecmu7"
_IDENTITY = {
    "agent_name": "agent_" + _AGENT_ID,
    "agent_id": _AGENT_ID,
    "runtime_template_id": "rt-1",
    "agent_template_id": "tpl-1",
}


@pytest.fixture()
def pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TenantAgentPool:
    instance = TenantAgentPool()
    instance._agent_wrappers.put_nowait("tk", SimpleNamespace(name="manager"))
    monkeypatch.setattr(
        TenantAgentPool,
        "extract_tenant_key",
        classmethod(lambda cls, request: "tk"),
    )
    monkeypatch.setattr(
        MultiTenantStrategy,
        "extract_tenant_key",
        lambda self, request: "tk",
    )
    monkeypatch.setattr(
        agent_identity_store,
        "get_multi_tenant_user_workspace_dir",
        lambda tenant_key: tmp_path / f"workspace_{tenant_key}",
    )
    return instance


def _request(agent_identity: dict[str, str] | None) -> SimpleNamespace:
    metadata: dict[str, object] = {"routing": {"group_id": "g", "bot_id": "b"}}
    if agent_identity is not None:
        metadata["agent_identity"] = agent_identity
    return SimpleNamespace(metadata=metadata)


def test_tenant_scope_persists_agent_identity(pool: TenantAgentPool) -> None:
    async def _run() -> None:
        async with pool.tenant_scope(_request(_IDENTITY)) as manager:
            assert manager.name == "manager"

    asyncio.run(_run())

    state = agent_identity_store.read_agent_identity_file("tk")
    assert state["current_agent_id"] == _AGENT_ID


def test_tenant_scope_skips_write_without_identity(pool: TenantAgentPool) -> None:
    async def _run() -> None:
        async with pool.tenant_scope(_request(None)):
            pass

    asyncio.run(_run())

    assert not agent_identity_path("tk").exists()


def test_bind_request_persists_agent_identity(pool: TenantAgentPool) -> None:
    """HTTP 入口走 bind_request，不经过 tenant_scope，同样要落盘."""
    strategy = MultiTenantStrategy(pool)
    manager = SimpleNamespace(name="manager")

    async def _run() -> None:
        async with strategy.bind_request(_request(_IDENTITY), manager):
            pass

    asyncio.run(_run())

    state = agent_identity_store.read_agent_identity_file("tk")
    assert state["current_agent_id"] == _AGENT_ID
    assert "agent_name" not in state["current"]
