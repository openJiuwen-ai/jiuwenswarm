# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.utils import resolve_gateway_cron_jobs_path
from tests.unit_tests.tenant_workspace_test_helpers import (
    patch_multi_tenant_workspace_dirs,
    tenant_workspace_key,
    tenant_workspace_root,
)
from jiuwenswarm.gateway.cron.tenant_registry import CronTenantRegistry


@pytest.fixture(autouse=True)
def _reset_registry():
    CronTenantRegistry.reset_instance()
    yield
    CronTenantRegistry.reset_instance()


def test_resolve_gateway_cron_jobs_path_per_tenant(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    path = resolve_gateway_cron_jobs_path("svc-a", "agent-b")
    assert path == (
        tmp_path
        / "gateway"
        / "cron"
        / "service_svc-a"
        / "agent_agent-b"
        / "cron_jobs.json"
    )


@pytest.mark.asyncio
async def test_registry_lazy_per_tenant_controller(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.start",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.stop",
        AsyncMock(),
    )

    registry = CronTenantRegistry.get_instance(
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )

    cc_a = await registry.get_controller("default", "office")
    cc_b = await registry.get_controller("default", "assistant")
    cc_a2 = await registry.get_controller("default", "office")

    assert cc_a is cc_a2
    assert cc_a is not cc_b
    assert cc_a._store.path == resolve_gateway_cron_jobs_path("default", "office")
    assert cc_b._store.path == resolve_gateway_cron_jobs_path("default", "assistant")


@pytest.mark.asyncio
async def test_get_controller_concurrent_same_tenant(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    start_mock = AsyncMock()
    stop_mock = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.start",
        start_mock,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.stop",
        stop_mock,
    )

    registry = CronTenantRegistry.get_instance(
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )
    results = await asyncio.gather(
        *[registry.get_controller("default", "office") for _ in range(20)]
    )

    assert all(c is results[0] for c in results)
    assert start_mock.await_count == 1
    assert stop_mock.await_count == 0


@pytest.mark.asyncio
async def test_web_create_mirrors_to_agent_home(tmp_path, monkeypatch) -> None:
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.start",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.reload",
        AsyncMock(),
    )
    # 本用例验证文件 store 镜像；企业 cron 需路由三元组，与此无关。
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.enterprise_store.enterprise_cron_enabled",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.controller.is_enterprise",
        lambda: False,
    )

    registry = CronTenantRegistry.get_instance(
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )
    job = await registry.web_create_job(
        {
            "name": "daily",
            "description": "daily reminder task",
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "targets": "web",
        },
        "default",
        "office",
    )

    gateway_path = resolve_gateway_cron_jobs_path("default", "office")
    wk = tenant_workspace_key("default", "office")
    agent_path = (
        tenant_workspace_root(tmp_path, workspace_key=wk) / "agent" / "home" / "cron_jobs.json"
    )
    assert gateway_path.exists()
    assert agent_path.exists(), f"missing agent mirror at {agent_path}"
    assert job["service_id"] == "default"
    assert job["agent_id"] == "office"


@pytest.mark.asyncio
async def test_handle_push_action_passes_routing_triple(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.start",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.tenant_registry.CronSchedulerService.stop",
        AsyncMock(),
    )

    registry = CronTenantRegistry.get_instance(
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )
    controller = MagicMock()
    controller.list_jobs = AsyncMock(return_value=[])
    controller.get_job = AsyncMock(return_value={"id": "job-1"})
    controller.update_job = AsyncMock(return_value={"id": "job-1", "name": "n2"})
    controller.delete_job = AsyncMock(return_value=True)
    controller.toggle_job = AsyncMock(return_value={"id": "job-1", "enabled": False})
    controller.preview_job = AsyncMock(return_value=[{"push_at": "x"}])
    controller.run_now = AsyncMock(return_value="run-1")
    registry.get_controller = AsyncMock(return_value=controller)

    identity = {"job_id": "job-1", "group_id": "g1", "bot_id": "b1", "user_id": "u1"}
    routing = {"group_id": "g1", "bot_id": "b1", "user_id": "u1"}

    await registry.handle_push_action(
        action="list", params=identity, service_id="default", agent_id="office"
    )
    controller.list_jobs.assert_awaited_once_with(routing)

    await registry.handle_push_action(
        action="get", params=identity, service_id="default", agent_id="office"
    )
    controller.get_job.assert_awaited_once_with("job-1", **routing)

    await registry.handle_push_action(
        action="update",
        params={**identity, "patch": {"name": "n2"}},
        service_id="default",
        agent_id="office",
    )
    controller.update_job.assert_awaited_once_with("job-1", {"name": "n2"}, **routing)

    deleted = await registry.handle_push_action(
        action="delete", params=identity, service_id="default", agent_id="office"
    )
    assert deleted == {"deleted": True}
    controller.delete_job.assert_awaited_once_with("job-1", **routing)

    await registry.handle_push_action(
        action="toggle",
        params={**identity, "enabled": False},
        service_id="default",
        agent_id="office",
    )
    controller.toggle_job.assert_awaited_once_with("job-1", False, **routing)

    await registry.handle_push_action(
        action="preview",
        params={**identity, "count": 3},
        service_id="default",
        agent_id="office",
    )
    controller.preview_job.assert_awaited_once_with("job-1", 3, **routing)

    ran = await registry.handle_push_action(
        action="run_now", params=identity, service_id="default", agent_id="office"
    )
    assert ran == {"run_id": "run-1"}
    controller.run_now.assert_awaited_once_with("job-1", **routing)


@pytest.mark.asyncio
async def test_handle_push_action_personal_delete_allows_empty_triple(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    registry = CronTenantRegistry.get_instance(
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )
    controller = MagicMock()
    controller.delete_job = AsyncMock(return_value=True)
    registry.get_controller = AsyncMock(return_value=controller)

    result = await registry.handle_push_action(
        action="delete",
        params={"job_id": "job-1"},
        service_id="default",
        agent_id="office",
    )
    assert result == {"deleted": True}
    controller.delete_job.assert_awaited_once_with(
        "job-1", group_id=None, bot_id=None, user_id=None
    )
