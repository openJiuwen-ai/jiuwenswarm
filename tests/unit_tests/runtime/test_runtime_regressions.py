# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 4 small test modules)."""

from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
from openjiuwen.core.session.checkpointer import NoopCheckpointer
from jiuwenswarm.runtime import checkpointer
from jiuwenswarm.runtime.context_checkpointer import ContextCheckpointer
from importlib import import_module
import asyncio
from dataclasses import replace
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext
from contextlib import asynccontextmanager
from typing import AsyncIterator
from jiuwenswarm.enterprise.tenant.single_tenant_strategy import SingleTenantStrategy
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_manager import AgentManager


# --- from test_checkpoint_journal.py ---

@pytest.mark.asyncio
@pytest.mark.parametrize("config", [
    {"checkpoint": {"sqlite_journal_mode": "DELETE"}},
    {"workspace_sqlite": {"journal_mode": "DELETE"}},
])
async def test_checkpoint_journal_mode_reaches_core_provider(monkeypatch, tmp_path, config):
    monkeypatch.setattr(checkpointer, "_shared_checkpointer", None, raising=False)
    monkeypatch.setattr(checkpointer, "_PERSISTENT_CHECKPOINTER_READY", False)
    monkeypatch.setattr(checkpointer, "get_checkpoint_dir", lambda: tmp_path)
    monkeypatch.setattr(checkpointer, "get_config", lambda: config, raising=False)
    monkeypatch.setattr(checkpointer, "PersistenceCheckpointerProvider", lambda: None)
    checkpoint_store = NoopCheckpointer()
    create = AsyncMock(return_value=checkpoint_store)
    monkeypatch.setattr(checkpointer.CheckpointerFactory, "create", create)
    monkeypatch.setattr(checkpointer.CheckpointerFactory, "_default_checkpointer", None)

    await checkpointer.ensure_persistent_checkpointer()

    assert create.await_args.args[0].conf["db_journal_mode"] == "DELETE"
    registered = checkpointer.CheckpointerFactory.get_checkpointer()
    assert isinstance(registered, ContextCheckpointer)
    assert registered.delegate is checkpoint_store

@pytest.mark.asyncio
async def test_checkpoint_rejects_unknown_sqlite_journal_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(checkpointer, "_PERSISTENT_CHECKPOINTER_READY", False)
    monkeypatch.setattr(checkpointer, "get_checkpoint_dir", lambda: tmp_path)
    monkeypatch.setattr(checkpointer, "get_config", lambda: {
        "checkpoint": {"sqlite_journal_mode": "MEMORY"},
    }, raising=False)
    monkeypatch.setattr(checkpointer, "PersistenceCheckpointerProvider", lambda: None)
    with pytest.raises(RuntimeError, match="persistent checkpointer initialization failed"):
        await checkpointer.ensure_persistent_checkpointer()

# --- from test_cron_job_mutations.py ---

@pytest.mark.parametrize("package", ["agent_gateway.runtime.cron", "jiuwenswarm.runtime.cron"])
def test_cron_store_rejects_groups_on_create_and_update(package):
    mutations = import_module(f"{package}.cron_job_mutations")
    params = {
        "name": "single model task",
        "description": "report",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "targets": "web",
    }
    group = {"type": "model_group", "id": "group"}
    with pytest.raises(ValueError, match="only support a single model"):
        mutations.build_new_cron_job(**params, model_selection=group)
    job = mutations.build_new_cron_job(**params, model_name="configured-model")
    assert job.model_name == "configured-model"
    with pytest.raises(ValueError, match="only support a single model"):
        mutations.apply_cron_job_patch(job, {"model_selection": group})
    assert job.model_name == "configured-model"

# --- from test_run_context.py ---

@pytest.mark.asyncio
async def test_same_request_id_in_different_sessions_does_not_share_credentials():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    async def run(session_id):
        credential = {"private": "not-for-logs", "owner": session_id}
        request = AgentRequest(request_id="same-id", channel_id="web", session_id=session_id, params={})
        context = RunContext(request_id=request.request_id, session_id=session_id, credential_context=credential)
        token = CURRENT_RUN_CONTEXT.set(context)
        try:
            await asyncio.sleep(0)
            assert JiuWenSwarmDeepAdapter._scoped_login_auth(request) is credential
            assert JiuWenSwarmDeepAdapter._scoped_login_auth(replace(request, session_id="foreign")) is None
            assert "not-for-logs" not in repr(context)
        finally:
            CURRENT_RUN_CONTEXT.reset(token)

    await asyncio.gather(run("session-a"), run("session-b"))
    assert CURRENT_RUN_CONTEXT.get() is None

# --- from test_tenant_request_scope_concurrency.py ---

class _NamedManager:
    def __init__(self, name: str) -> None:
        self.name = name

    async def cleanup(self) -> None:
        return None

class _FakeMultiStrategy:
    """测试用：按 metadata.tenant_key 分槽，不依赖真实 Pool."""

    def __init__(self, default: _NamedManager, by_key: dict[str, _NamedManager]) -> None:
        self._default = default
        self._by_key = by_key

    def extract_tenant_key(self, request: AgentRequest | None) -> str:
        if request is None:
            return "default"
        meta = request.metadata if isinstance(request.metadata, dict) else {}
        return str(meta.get("tenant_key") or "default")

    async def resolve_manager(self, request: AgentRequest | None = None):
        if request is None:
            return self._default
        return self._by_key[self.extract_tenant_key(request)]

    def get_default_manager(self):
        return self._default

    def adopt_default_manager(self, manager):
        return self._default

    @asynccontextmanager
    async def bind_request(self, request, manager) -> AsyncIterator[None]:
        yield

    def iter_managers_for_host_event(self):
        return [self._default, *self._by_key.values()]

    async def cleanup(self) -> None:
        return None

    @asynccontextmanager
    async def scoped_manager(self, request: AgentRequest) -> AsyncIterator[_NamedManager]:
        manager = await self.resolve_manager(request)
        async with self.bind_request(request, manager):
            yield manager

@pytest.mark.asyncio
async def test_concurrent_tenant_scopes_yield_isolated_managers() -> None:
    default = _NamedManager("default")
    manager_a = _NamedManager("tenant-a")
    manager_b = _NamedManager("tenant-b")
    strategy = _FakeMultiStrategy(
        default,
        {"tenant-a": manager_a, "tenant-b": manager_b},
    )

    runtime = AgentRuntime(
        agent_manager=default,  # type: ignore[arg-type]
        tenant_strategy=strategy,  # type: ignore[arg-type]
        initializer=AsyncMock(),
    )
    seen: dict[str, list[str]] = {"a": [], "b": []}

    async def _run(label: str, tenant_key: str, expected: _NamedManager) -> None:
        request = AgentRequest(
            request_id=f"req-{label}",
            channel_id="web",
            session_id=f"sess-{label}",
            metadata={"tenant_key": tenant_key},
        )
        async with runtime.tenant_strategy.scoped_manager(request) as manager:
            assert manager is expected
            # 默认槽属性始终指向默认 Manager，不被并发请求改写。
            assert runtime.agent_manager is default
            await asyncio.sleep(0)
            seen[label].append(manager.name)
            assert manager is expected
            assert runtime.agent_manager is default

    await asyncio.gather(
        _run("a", "tenant-a", manager_a),
        _run("b", "tenant-b", manager_b),
    )

    assert seen["a"] == ["tenant-a"]
    assert seen["b"] == ["tenant-b"]
    assert runtime.agent_manager is default

@pytest.mark.asyncio
async def test_personal_runtime_uses_single_strategy_without_pool(
    monkeypatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    from jiuwenswarm.enterprise import reset_enterprise_plane

    reset_enterprise_plane()
    manager = AgentManager()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    assert isinstance(runtime.tenant_strategy, SingleTenantStrategy)
    assert runtime.agent_manager is manager
    with pytest.raises(Exception):
        _ = runtime.tenant_pool

