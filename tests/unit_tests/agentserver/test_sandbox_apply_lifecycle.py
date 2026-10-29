# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Automatic apply and final-owner teardown through real adapter/provider code."""
# Bare adapters avoid model execution while retaining the real lifecycle methods.
# pylint: disable=protected-access

from copy import deepcopy
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.sys_operation import OperationMode, SysOperation, SysOperationCard
from openjiuwen.core.sys_operation.config import (
    ContainerScope,
    PreDeployLauncherConfig,
    SandboxGatewayConfig,
    SandboxIsolationConfig,
)
from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxGateway
from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box
from jiuwenswarm.server import agent_ws_server as ws, sandbox_config_rpc
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep


@pytest.fixture
def runtime(monkeypatch):
    from jiuwenswarm.common import config

    mixin = box._JiuwenBoxProviderMixin
    for name in (
        "_shared_sandbox_ids",
        "_creation_locks",
        "_creation_failures",
        "_lifecycle_hooks",
        "_pending_deletions",
        "_delete_tasks",
        "_idle_timeout_cache",
        "_scope_releases",
    ):
        monkeypatch.setattr(mixin, name, {})
    monkeypatch.setattr(mixin, "_bulk_deletions", 0)
    monkeypatch.setattr(mixin, "_policy_updates", 0)
    monkeypatch.setattr(mixin, "_inflight_recreations", set())
    monkeypatch.setattr(deep, "_SYS_OPERATION_REFCOUNTS", {})
    monkeypatch.setattr(deep, "_SYS_OPERATION_RELEASE_TASKS", {})
    monkeypatch.setattr(SandboxGateway, "_instance", SandboxGateway())
    monkeypatch.delenv("JIUWENBOX_SANDBOX_ID", raising=False)
    monkeypatch.setattr(box, "_SANDBOX_DELETE_RETRY_SLEEP_SECONDS", 0)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(
        deep, "build_filesystem_policy", lambda files, **_: ({"files": deepcopy(files)}, [])
    )
    state = SimpleNamespace(
        active=set(), created=[], deleted=[], fail_create=False, operations={}, adapters={}
    )

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def live_sandbox_ids(self):
            return set(state.active)

        def create_sandbox(self, **kwargs):
            if state.fail_create:
                raise RuntimeError("replacement failed")
            sid = f"sandbox-{len(state.created) + 1}"
            state.created.append((sid, deepcopy(kwargs)))
            state.active.add(sid)
            return sid

        def delete_sandbox(self, sid):
            state.active.discard(sid)
            state.deleted.append(sid)

        def download_bytes(self, sid, _path):
            return sid.encode()

    monkeypatch.setattr(box, "_JiuwenBoxClient", Client)
    monkeypatch.setattr(
        deep.Runner,
        "resource_mgr",
        SimpleNamespace(
            get_sys_operation=state.operations.get,
            remove_sys_operation=lambda key: state.operations.pop(key, None),
        ),
    )
    state.service_update = AsyncMock(return_value=False)
    monkeypatch.setattr(
        sandbox_config_rpc, "apply_sandbox_policy_for_restart", state.service_update
    )
    state.policy = {"enabled": True, "files": [{"path": "new-policy"}]}
    monkeypatch.setattr(ws, "get_sandbox_runtime", lambda: state.policy)
    monkeypatch.setattr(ws, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    state.server = ws.AgentWebSocketServer.__new__(ws.AgentWebSocketServer)
    state.server._agent_manager = SimpleNamespace(agents={"web": state.adapters})
    state.server._resolve_adapter = lambda value: value

    def owner(name, shared=None):
        card = (
            shared._sys_operation_card
            if shared
            else SysOperationCard(
                mode=OperationMode.SANDBOX,
                gateway_config=SandboxGatewayConfig(
                    isolation=SandboxIsolationConfig(
                        container_scope=ContainerScope.CUSTOM, custom_id=name
                    ),
                    launcher_config=PreDeployLauncherConfig(
                        base_url="http://localhost:8321",
                        sandbox_type="jiuwenbox",
                        extra_params={"policy": {"old": True}},
                    ),
                ),
            )
        )
        op = state.operations.setdefault(card.id, SysOperation(card))
        adapter = SimpleNamespace(
            _sys_operation_card=card,
            _is_code_agent=False,
            _retained_sys_operation_ids=[],
            _resolve_project_dir_for_sandbox=lambda: None,
            _sys_operation_isolation_key=deep.JiuWenSwarmDeepAdapter._sys_operation_isolation_key,
        )
        for method in (
            "apply_sandbox_runtime_patch",
            "sandbox_scope_key",
            "_retain_sys_operation",
            "_release_sys_operations",
            "_cleanup_sys_operations",
        ):
            setattr(
                adapter, method, MethodType(getattr(deep.JiuWenSwarmDeepAdapter, method), adapter)
            )
        adapter._retain_sys_operation(str(op.id))
        state.adapters[name] = adapter
        return adapter, op

    state.owner = owner
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 20])
async def test_network_hot_apply_keeps_instances_and_skips_file_acl_rebuild(
    runtime, monkeypatch, count,
):
    for number in range(count):
        _, operation = runtime.owner(f"live-{number}")
        await operation.fs().read_file("/probe")
    previous_ids = set(runtime.active)
    hot_update = AsyncMock(return_value=True)
    monkeypatch.setattr(sandbox_config_rpc, "try_apply_sandbox_network_policy", hot_update)

    result = await runtime.server.apply_configured_sandboxes({"network_only": True})

    assert result["network_status"] == "hot_applied"
    assert result["restarted"] == 0
    assert runtime.active == previous_ids
    assert len(runtime.created) == count and not runtime.deleted
    assert (await operation.fs().read_file("/probe")).data.content == f"sandbox-{count}"
    runtime.service_update.assert_not_awaited()
    hot_update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_network_fast_path_falls_back_for_old_service_or_pending_rebuild(
    runtime, monkeypatch, pending,
):
    adapter, operation = runtime.owner("live")
    await operation.fs().read_file("/probe")
    if pending:
        extra = adapter._sys_operation_card.gateway_config.launcher_config.extra_params
        extra["_policy_apply_pending"] = True
    hot_update = AsyncMock(return_value=False)
    monkeypatch.setattr(sandbox_config_rpc, "try_apply_sandbox_network_policy", hot_update)

    result = await runtime.server.apply_configured_sandboxes({"network_only": True})

    assert result["restarted"] == 1
    assert runtime.active == {"sandbox-2"}
    assert hot_update.await_count == (0 if pending else 1)
    runtime.service_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_rebuilds_shared_live_scope_once_and_keeps_reaped_scopes_lazy(runtime):
    first, live = runtime.owner("live")
    runtime.owner("same-project-session", shared=first)
    reaped_adapter, reaped = runtime.owner("reaped")
    runtime.owner("never-used")
    await live.fs().read_file("/probe")
    await reaped.fs().read_file("/probe")
    runtime.active.remove("sandbox-2")

    result = await runtime.server.apply_configured_sandboxes({})

    assert result["restarted"] == 1
    assert len(runtime.created) == 3  # Two original instances, one replacement.
    assert runtime.active == {"sandbox-3"}
    extra = reaped_adapter._sys_operation_card.gateway_config.launcher_config.extra_params
    assert "sandbox_id" not in extra
    assert extra["policy"] == {"files": runtime.policy["files"]}
    assert (await reaped.fs().read_file("/probe")).code == 0
    assert len(runtime.created) == 4  # Reaped scope is created only on actual use.
    assert runtime.created[-1][1]["policy"] == {"files": runtime.policy["files"]}


@pytest.mark.asyncio
async def test_failed_rebuild_blocks_tools_and_retries_without_live_instance(runtime):
    adapter, operation = runtime.owner("live")
    await operation.fs().read_file("/probe")
    runtime.fail_create = True
    with pytest.raises(RuntimeError, match="replacement failed"):
        await runtime.server.apply_configured_sandboxes({})
    assert not runtime.active
    extra = adapter._sys_operation_card.gateway_config.launcher_config.extra_params
    assert extra["_policy_rebuild_pending"] is True
    assert extra["_policy_apply_pending"] is True
    denied = await operation.fs().read_file("/probe")
    assert denied.code != 0 and "policy update is pending" in denied.message

    runtime.fail_create = False
    assert (await runtime.server.apply_configured_sandboxes({}))["restarted"] == 1
    assert "_policy_apply_pending" not in extra
    assert "_policy_rebuild_pending" not in extra
    assert (await operation.fs().read_file("/probe")).code == 0


@pytest.mark.asyncio
async def test_apply_snapshots_live_owners_before_service_retirement(runtime):
    adapter, operation = runtime.owner("live")
    await operation.fs().read_file("/probe")

    async def retire(_bootstrap):
        await box.retire_jiuwenbox_service("http://localhost:8321")
        runtime.active.clear()
        adapter._sys_operation_card.gateway_config.launcher_config.base_url = (
            "http://localhost:9000"
        )
        return True

    runtime.service_update.side_effect = retire
    assert (await runtime.server.apply_configured_sandboxes({}))["restarted"] == 1
    assert runtime.active == {"sandbox-2"}


@pytest.mark.asyncio
async def test_apply_does_not_resurrect_owner_released_after_snapshot(runtime, monkeypatch):
    adapter, operation = runtime.owner("disconnecting")
    await operation.fs().read_file("/probe")

    async def disconnect(_bootstrap):
        await adapter._cleanup_sys_operations()
        return False

    runtime.service_update.side_effect = disconnect
    result = await runtime.server.apply_configured_sandboxes({})
    assert result["restarted"] == 0
    assert result["skipped"] == 1
    assert not runtime.active
    assert len(runtime.created) == 1
    assert not runtime.server._has_pending_sandbox_rebuild()
    hot_update = AsyncMock(return_value=True)
    monkeypatch.setattr(sandbox_config_rpc, "try_apply_sandbox_network_policy", hot_update)
    result = await runtime.server.apply_configured_sandboxes({"network_only": True})
    assert result["network_status"] == "hot_applied"
    hot_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_scope_fails_preflight_before_marking_or_changing_other_owners(runtime):
    healthy, live = runtime.owner("healthy")
    invalid, invalid_op = runtime.owner("invalid")
    await live.fs().read_file("/probe")
    await invalid_op.fs().read_file("/probe")
    invalid._sys_operation_isolation_key = lambda _card: None

    with pytest.raises(RuntimeError, match="scope validation failed.*isolation key is unavailable"):
        await runtime.server.apply_configured_sandboxes({})

    runtime.service_update.assert_not_awaited()
    assert runtime.active == {"sandbox-1", "sandbox-2"}
    assert not runtime.deleted
    for adapter in (healthy, invalid):
        extra = adapter._sys_operation_card.gateway_config.launcher_config.extra_params
        assert "_policy_apply_pending" not in extra
        assert "_policy_rebuild_pending" not in extra


@pytest.mark.asyncio
async def test_scope_recheck_error_reports_partial_failure_and_processes_other_owners(runtime):
    invalid, invalid_op = runtime.owner("invalid")
    _, healthy_op = runtime.owner("healthy")
    await invalid_op.fs().read_file("/probe")
    await healthy_op.fs().read_file("/probe")
    original_scope = invalid.sandbox_scope_key
    calls = 0

    def resolve_scope():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("scope became invalid")
        return original_scope()

    invalid.sandbox_scope_key = resolve_scope
    with pytest.raises(RuntimeError, match="restarted=1, failed=1.*scope became invalid"):
        await runtime.server.apply_configured_sandboxes({})
    assert runtime.active == {"sandbox-3"}
    assert runtime.server._has_pending_sandbox_rebuild()


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [False, True])
async def test_false_rebuild_result_only_clears_pending_for_released_owner(runtime, released):
    adapter, operation = runtime.owner("live")
    await operation.fs().read_file("/probe")
    original_patch = adapter.apply_sandbox_runtime_patch

    async def patch(policy, **kwargs):
        if kwargs.get("prepare_only"):
            return await original_patch(policy, **kwargs)
        if released:
            await adapter._cleanup_sys_operations()
        return False

    adapter.apply_sandbox_runtime_patch = patch
    if released:
        result = await runtime.server.apply_configured_sandboxes({})
        assert result["skipped"] == 1 and result["restarted"] == 0
        assert not runtime.server._has_pending_sandbox_rebuild()
    else:
        with pytest.raises(RuntimeError, match="rebuild did not complete for retained owner"):
            await runtime.server.apply_configured_sandboxes({})
        assert runtime.server._has_pending_sandbox_rebuild()


@pytest.mark.asyncio
async def test_missing_launcher_reports_strict_configuration_error_without_attribute_error(runtime):
    adapter, operation = runtime.owner("missing-launcher")
    await operation.fs().read_file("/probe")
    scope = adapter.sandbox_scope_key()
    adapter.sandbox_scope_key = lambda: scope
    adapter._sys_operation_card.gateway_config.launcher_config = None

    with pytest.raises(RuntimeError, match="missing sandbox launcher_config"):
        await runtime.server.apply_configured_sandboxes({})


@pytest.mark.asyncio
async def test_deleted_session_batch_does_not_accumulate_apply_rebuild_targets(runtime):
    for number in range(20):
        name = f"project-{number}"
        adapter, operation = runtime.owner(name)
        assert (await operation.fs().read_file("/probe")).code == 0
        await adapter._cleanup_sys_operations()
        del runtime.adapters[name]
    assert not runtime.active
    assert not runtime.operations
    assert not deep._SYS_OPERATION_REFCOUNTS
    assert not SandboxGateway.get_instance()._provider_cache
    assert (await runtime.server.apply_configured_sandboxes({}))["restarted"] == 0
    assert len(runtime.created) == 20
