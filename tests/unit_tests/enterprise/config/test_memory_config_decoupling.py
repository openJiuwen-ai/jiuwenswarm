# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway persistence and execution-owned memory configuration refresh."""

from __future__ import annotations

import builtins
import copy
from typing import Any
from unittest.mock import Mock

import pytest

from agent_gateway.config.enterprise.repository import EnterpriseRecordRepository
from agent_gateway.config.memory.repository import MemoryConfigRepository
from agent_gateway.config.section import DbBodySectionCodec
from agent_gateway.enterprise.config.manager_config.core.application_config import (
    memory_config as memory_service,
    task_memory_config as task_service,
)
from agent_gateway.enterprise.config.manager_config.routers import application_config_routers as routes
from agent_gateway.enterprise.config.manager_config.routers.deps import SyncContext
from jiuwenswarm.agents.harness.common.memory import config as memory
from jiuwenswarm.enterprise.config.config_provider import AgentConfigContext, DefaultConfigProvider
from jiuwenswarm.enterprise.config.effective import db_queries


class RecordStore:
    """In-memory persistence boundary shared by the real repositories/read facade."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get(self, name, key):
        return copy.deepcopy(self.rows.get(name))

    async def create(self, name, record):
        self.rows[name] = copy.deepcopy(record)
        return copy.deepcopy(record)

    async def update(self, name, key, updates):
        self.rows[name].update(copy.deepcopy(updates))
        return copy.deepcopy(self.rows[name])

    async def delete(self, name, key):
        return self.rows.pop(name, None) is not None

    async def list(self, name, **kwargs):
        return await self.list_records(name)

    async def list_records(self, name, **kwargs):
        row = self.rows.get(name)
        return [copy.deepcopy(row)] if row is not None else []


@pytest.fixture
def memory_environment(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    for attr in (
        "_config_cache", "_memory_config_db_cache", "_memory_config_cache_source",
        "_task_memory_config_db_cache", "_embed_config_db_cache",
    ):
        monkeypatch.setattr(memory, attr, None)
    monkeypatch.setattr(memory, "_load_config", lambda: {
        "memory": {"mode": "local", "nested": {"yaml": True}},
        "task_memory": {"enabled": False, "llm_model": "yaml-model"},
    })
    store = RecordStore()
    memory_repo = MemoryConfigRepository(store, DbBodySectionCodec())
    task_repo = EnterpriseRecordRepository(store, "task_memory_config")
    monkeypatch.setattr(memory_service, "require_memory_repository", lambda: memory_repo)
    monkeypatch.setattr(task_service, "require_enterprise_repository", lambda name: task_repo)
    monkeypatch.setattr(db_queries, "list_records", store.list_records)
    return store


@pytest.mark.asyncio
async def test_receiver_writes_without_importing_execution_memory(memory_environment, monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("jiuwenswarm.agents.harness.common.memory"):
            raise AssertionError("Gateway must not import execution memory")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    saved = await memory_service.MemoryConfigService().upsert(body={"mode": "cloud"})
    assert saved["body"] == {"mode": "cloud"}
    assert saved["revision"] == 1
    saved = await memory_service.MemoryConfigService().upsert(body={"enabled": False})
    assert saved["revision"] == 2
    assert saved["body"] == {"enabled": False}
    await task_service.TaskMemoryConfigService().upsert({
        "enabled": True, "llm_model": "enterprise-model",
    })
    assert memory_environment.rows["task_memory_config"]["llm_model"] == "enterprise-model"
    assert memory.get_memory_config_overlay() is None
    assert memory.get_task_memory_config()["enabled"] is False
    await memory_service.MemoryConfigService().delete()
    await task_service.TaskMemoryConfigService().delete()
    assert memory_environment.rows == {}


@pytest.mark.asyncio
async def test_execution_loads_updates_and_deletes_from_persistence(memory_environment):
    provider = DefaultConfigProvider()
    base = {"memory": {"mode": "local", "nested": {"yaml": True}}}
    await memory_service.MemoryConfigService().upsert(body={
        "mode": "cloud", "nested": {"enterprise": True},
    })
    await task_service.TaskMemoryConfigService().upsert({
        "enabled": True, "llm_model": "enterprise-model",
    })
    # No Gateway apply or explicit refresh call: first execution must see DB state.
    result = await provider.load_agent_config(AgentConfigContext(), base=base)
    assert result.config["memory"] == {
        "mode": "cloud", "nested": {"yaml": True, "enterprise": True},
    }
    assert base["memory"]["mode"] == "local"
    assert memory.get_task_memory_config()["llm_model"] == "enterprise-model"

    await memory_service.MemoryConfigService().upsert(body={"enabled": False})
    result = await provider.load_agent_config(AgentConfigContext(), base=base)
    assert result.config["memory"] == {
        "mode": "local", "enabled": False, "nested": {"yaml": True},
    }
    await memory_service.MemoryConfigService().delete()
    await task_service.TaskMemoryConfigService().delete()
    await provider.refresh()
    result = await provider.load_agent_config(AgentConfigContext(), base=base)
    assert result.config["memory"] == base["memory"]
    assert memory.get_task_memory_config()["llm_model"] == "yaml-model"


@pytest.mark.asyncio
async def test_execution_read_failure_preserves_last_applied_values(memory_environment, monkeypatch):
    memory.apply_memory_config_payload({"body": {"mode": "cloud"}})
    memory.apply_task_memory_config_payload({"body": {"enabled": True}})

    async def unavailable(*args, **kwargs):
        raise ConnectionError("test database unavailable")

    monkeypatch.setattr(db_queries, "list_records", unavailable)
    result = await DefaultConfigProvider().load_agent_config(AgentConfigContext(), base={})
    assert result.config["memory"]["mode"] == "cloud"
    assert memory.get_task_memory_config()["enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source,edition", [("yaml", "enterprise"), ("enterprise", "community")])
async def test_yaml_and_community_do_not_read_enterprise_memory(memory_environment, monkeypatch, source, edition):
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", source)
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)

    read_calls = []

    async def record_read(*args, **kwargs):
        read_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(db_queries, "list_records", record_read)
    result = await DefaultConfigProvider().load_agent_config(AgentConfigContext(), base={})
    assert read_calls == []
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "task_memory"])
async def test_routes_notify_after_save_and_delete(memory_environment, monkeypatch, kind):
    snapshots = []
    monkeypatch.setattr(routes, "trigger_runtime_config_update", lambda: snapshots.append(
        copy.deepcopy(memory_environment.rows),
    ))
    payload = {"body": {"enabled": False}} if kind == "memory" else {"enabled": True}
    put = getattr(routes, f"upsert_{kind}")
    delete = getattr(routes, f"delete_{kind}")
    response = await put(SyncContext(business=payload, method="PUT"))
    assert response.code == 200
    assert f"{kind}_config" in snapshots[0]
    response = await delete(SyncContext(business={}, method="DELETE"))
    assert response.code == 200
    assert snapshots[1] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "task_memory"])
async def test_failed_write_does_not_notify_runtime(memory_environment, monkeypatch, kind):
    notify = Mock()
    monkeypatch.setattr(routes, "trigger_runtime_config_update", notify)

    async def failed_write(*args, **kwargs):
        raise RuntimeError("test write failed")

    monkeypatch.setattr(memory_environment, "create", failed_write)
    with pytest.raises(RuntimeError, match="test write failed"):
        put = getattr(routes, f"upsert_{kind}")
        payload = {"body": {}} if kind == "memory" else {"enabled": True}
        await put(SyncContext(business=payload, method="PUT"))
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, TypeError])
async def test_unexpected_memory_refresh_failure_does_not_abort_default_config(
    memory_environment, monkeypatch, error_type,
):
    from jiuwenswarm.enterprise.config import config_provider

    memory.apply_memory_config_payload({"body": {"mode": "cloud"}})
    memory.apply_task_memory_config_payload({"body": {"enabled": True}})
    provider = DefaultConfigProvider()
    warning = Mock()
    monkeypatch.setattr(config_provider.logger, "warning", warning)
    monkeypatch.setattr(config_provider, "_provider", None)
    monkeypatch.setattr(config_provider, "_DEFAULT_PROVIDER", provider)

    async def failed_refresh():
        raise error_type("test unexpected refresh failure")

    monkeypatch.setattr(provider, "_refresh_memory_configs", failed_refresh)
    base = {"memory": {"mode": "local", "enabled": False}}
    result = await config_provider.resolve_agent_config(base)
    assert result.config["memory"] == {"mode": "cloud", "enabled": False}
    assert memory.get_task_memory_config()["enabled"] is True
    assert base["memory"]["mode"] == "local"
    warning.assert_called_once()
    assert "memory configuration refresh failed" in warning.call_args.args[0]
