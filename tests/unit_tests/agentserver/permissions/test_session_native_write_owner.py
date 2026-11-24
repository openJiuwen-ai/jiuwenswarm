# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""F02 owner follows the existing Work and Code session permission recipe."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.sys_operation.sys_operation import OperationMode

from jiuwenswarm.agents.harness.common.rails.permissions.artifact_path_provenance import SessionArtifactPaths
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    TOOL_INVOCATION_CONTEXT_ATTRIBUTE,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from openjiuwen.harness.tools.base_tool import ToolOutput
from jiuwenswarm.agents.harness.common.rails.permissions.native_path_context import (
    NATIVE_PATH_ACCESS, NativePathAccess, native_arguments_json,
)
from tests.unit_tests.agentserver.permissions.test_code_smart_f01 import _create, _verify, code_cold
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold


@pytest.fixture(autouse=True)
def native_context():
    token = NATIVE_PATH_ACCESS.set(None)
    yield
    NATIVE_PATH_ACCESS.reset(token)


def _proof(tool, args):
    NATIVE_PATH_ACCESS.set(NativePathAccess(
        tool, native_arguments_json(args), args["file_path"], "read" if tool == "read_file" else "write",
    ))


def _history(path):
    return ({"path": path, "native_write_completed": True},)




def _key(session_id: str, call_id: str) -> ToolInvocationKeyV1:
    return ToolInvocationKeyV1(
        invocation_id=f"invocation-{call_id}", root_session_id=session_id,
        request_id="f02-owner-request", executor_kind="agent",
        execution_session_id=session_id, tool_call_id=call_id,
    )


def _file_facts(tool: str, path, root):
    args = {"file_path": str(path)}
    if tool == "write_file":
        args["content"] = "accepted output"
    _proof(tool, args)
    return args, build_tool_decision_facts(
        tool, args, workspace_root=root, original_args_were_valid_object=True,
    )


def _seed(owner: SessionArtifactPaths, root, session_id: str):
    path = root / "f02-owner-output.txt"
    path.write_text("accepted output", encoding="utf-8")
    args, facts = _file_facts("write_file", path, root)
    assert facts.write_paths == (str(path),)
    key = _key(session_id, "completed-write")
    assert owner.stage(key, "write_file", args, facts) == 1
    assert owner.complete(key, "write_file", args, ToolOutput(success=True, data={"file_path": str(path)})) == 1
    return path, args, facts


@pytest.mark.parametrize("failure", ["native_failure", "other_path", "changed_args", "other_call", "child", "cancel"])
async def test_incomplete_or_mismatched_write_cannot_produce_history(tmp_path, failure):
    import asyncio
    from dataclasses import replace

    root = tmp_path.resolve()
    owner = SessionArtifactPaths("root")
    owner.configure(root, host_visible=True)
    path = root / "existing.txt"
    path.write_text("existing")
    args, facts = _file_facts("write_file", path, root)
    key = _key("root", "write")
    if failure == "child":
        key = replace(key, execution_session_id="child")
        assert owner.stage(key, "write_file", args, facts) == 0
    elif failure == "cancel":
        async def stage_write():
            assert owner.stage(key, "write_file", args, facts) == 1
            await asyncio.get_running_loop().create_future()

        task = asyncio.create_task(stage_write())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._pending
    else:
        assert owner.stage(key, "write_file", args, facts) == 1
        result = ToolOutput(
            success=failure != "native_failure",
            data={"file_path": str(root / "other.txt") if failure == "other_path" else str(path)},
        )
        if failure == "changed_args":
            args = {**args, "content": "different"}
        if failure == "other_call":
            key = _key("root", "different")
        assert owner.complete(key, "write_file", args, result) == 0
    _, read = _file_facts("read_file", path, root)
    assert owner.evidence(read, "root") == ()


def _assert_installed(adapter) -> SessionArtifactPaths:
    owner = adapter._session_artifact_paths
    assert isinstance(owner, SessionArtifactPaths)
    assert owner.root_session_id == adapter._parent_session_id
    assert isinstance(adapter._permission_rail, AutoPermissionInterruptRail)
    assert adapter._permission_rail.session_artifact_paths is owner
    assert adapter._permission_rail._owner_agent is adapter._instance
    assert adapter._permission_rail.workspace_root == adapter._permission_workspace_root
    assert adapter._instance.is_registered_rail(adapter._permission_rail)
    return owner


@pytest.mark.parametrize("project", [True, False])
async def test_work_cold_create_automatically_installs_bound_artifact_owner(cold, monkeypatch, project):
    h = cold
    # The cold fixture replaces the operation builder. Supply the matching card
    # that the product builder normally retains, without changing the recipe.
    h.adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.LOCAL)
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(h.root / "tasks"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(h.root / "registry"))
    await h.adapter.create_instance(
        {"channel_id": "web", "project_dir": str(h.root) if project else None}, mode="agent",
    )
    owner = _assert_installed(h.adapter)
    assert h.helper_calls[0]["session_artifact_paths"] is owner
    root = h.adapter._permission_workspace_root
    path, _, _ = _seed(owner, root, h.adapter._parent_session_id)
    _, read = _file_facts("read_file", path, root)
    assert owner.evidence(read, h.adapter._parent_session_id) == _history(path.name)
    assert h.adapter._permission_runtime_paths.is_projectless is not project


@pytest.mark.parametrize("project,sub_mode", [(True, "normal"), (True, "plan"), (False, "normal"), (False, "plan")])
async def test_code_spec_automatically_borrows_shared_artifact_owner(code_cold, project, sub_mode):
    h = code_cold
    await _create(h, project=project, sub_mode=sub_mode)
    owner = _assert_installed(h.adapter)
    _verify(h.adapter)
    assert h.adapter._permission_rail in h.adapter._code_spec_rails
    root = h.adapter._permission_workspace_root
    path, _, _ = _seed(owner, root, h.adapter._parent_session_id)
    _, read = _file_facts("read_file", path, root)
    assert owner.evidence(read, h.adapter._parent_session_id) == _history(path.name)
    assert h.adapter._permission_runtime_paths.is_projectless is not project


async def _assert_reload_and_dispose(h, monkeypatch, *, code: bool) -> None:
    adapter = h.adapter
    owner = _assert_installed(adapter)
    sid, root = adapter._parent_session_id, adapter._permission_workspace_root
    path, args, facts = _seed(owner, root, sid)
    _, read = _file_facts("read_file", path, root)
    pending = _key(sid, "pending-before-reload")
    _proof("write_file", args)
    assert owner.stage(pending, "write_file", args, facts) == 1
    _, read = _file_facts("read_file", path, root)
    old = adapter._permission_rail
    h.change()
    await adapter.reload_agent_config(h.raw, reload_scopes={"permissions"})
    assert adapter._permission_rail is not old
    assert old._owner_agent is None
    assert _assert_installed(adapter) is owner
    assert owner.complete(pending, "write_file", args, {"success": True}) == 0
    assert owner.evidence(read, sid) == _history(path.name)
    assert owner.evidence(read, "different-root-session") == ()
    if code:
        _verify(adapter)
        assert old not in adapter._code_spec_rails

    # Manual mode removes the consumer, but does not dispose the session owner.
    h.raw["permissions"]["mode"] = "manual"
    h.change()
    await adapter.reload_agent_config(h.raw, reload_scopes={"permissions"})
    assert not isinstance(adapter._permission_rail, AutoPermissionInterruptRail)
    assert adapter._session_artifact_paths is owner
    assert owner.evidence(read, sid) == ()
    if code:
        _verify(adapter, smart=False)
    h.raw["permissions"]["mode"] = "auto"
    h.change()
    await adapter.reload_agent_config(h.raw, reload_scopes={"permissions"})
    assert _assert_installed(adapter) is owner
    assert owner.evidence(read, sid) == ()
    if code:
        _verify(adapter)

    other = JiuWenSwarmDeepAdapter()
    other.mark_as_session_scoped("f02-isolated-session")
    assert other._session_artifact_paths is not owner
    other._session_artifact_paths.configure(root, host_visible=True)
    assert other._session_artifact_paths.evidence(read, "f02-isolated-session") == ()
    other._session_artifact_paths.dispose()

    # Keep actual adapter cleanup and artifact disposal; unrelated session
    # finalizers do not need a model or any external-memory connection here.
    monkeypatch.setattr(adapter, "_finalize_external_memory_session", AsyncMock())
    monkeypatch.setattr(adapter, "_sync_personal_context_rail", AsyncMock())
    await adapter.cleanup()
    assert owner.evidence(read, sid) == ()
    assert owner.stage(_key(sid, "after-dispose"), "write_file", args, facts) == 0


async def test_work_permission_replacement_retains_evidence_and_disposes_with_session(cold, monkeypatch):
    h = cold
    h.adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.LOCAL)
    await h.create()
    await _assert_reload_and_dispose(h, monkeypatch, code=False)


@pytest.mark.parametrize("success", [True, False])
async def test_native_write_after_hook_captures_mapping_result(cold, success):
    h = cold
    h.adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.LOCAL)
    await h.create()
    owner = _assert_installed(h.adapter)
    sid, root = h.adapter._parent_session_id, h.adapter._permission_workspace_root
    path = root / "mapping-output.txt"
    path.write_text("accepted output", encoding="utf-8")
    args, facts = _file_facts("write_file", path, root)
    key = _key(sid, "mapping-write")
    assert owner.stage(key, "write_file", args, facts) == 1
    ctx = SimpleNamespace(
        inputs={
            "tool_name": "write_file",
            "tool_call": SimpleNamespace(id=key.tool_call_id, name="write_file", arguments=args),
            "tool_args": args,
            "tool_result": ToolOutput(success=success, data={"file_path": str(path)}),
        },
        session=SimpleNamespace(get_session_id=lambda: sid),
        extra={},
    )
    setattr(ctx, TOOL_INVOCATION_CONTEXT_ATTRIBUTE, key)
    await h.adapter._permission_rail.after_tool_call(ctx)
    _, read = _file_facts("read_file", path, root)
    assert owner.evidence(read, sid) == (_history(path.name) if success else ())
    assert not owner._pending


async def test_failed_permission_group_prepare_preserves_active_write_history(cold, monkeypatch):
    h = cold
    adapter = h.adapter
    adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.LOCAL)
    await h.create()
    owner = _assert_installed(adapter)
    sid, root = adapter._parent_session_id, adapter._permission_workspace_root
    path, _, _ = _seed(owner, root, sid)
    _, read = _file_facts("read_file", path, root)
    old_rail = adapter._permission_rail

    def fail_preparation(*args, **kwargs):
        raise RuntimeError("test_permission_prepare_failed")

    monkeypatch.setattr(adapter, "_prepare_general_purpose_permission_update", fail_preparation)
    h.raw["permissions"]["mode"] = "manual"
    h.change()
    with pytest.raises(RuntimeError, match="test_permission_prepare_failed"):
        await adapter._replace_permission_group(h.raw)
    assert adapter._permission_rail is old_rail
    assert _assert_installed(adapter) is owner
    assert owner.evidence(read, sid) == _history(path.name)


async def test_code_permission_reload_retains_evidence_and_spec_owner_contract(code_cold, monkeypatch):
    h = code_cold
    await _create(h)
    await _assert_reload_and_dispose(h, monkeypatch, code=True)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("mount_case,expected", [
    ("identity", True), ("covering_parent", True), ("remapped", False),
    ("missing", False), ("file", False), ("unrelated", False), ("relative", False),
])
def test_host_visibility_requires_real_identity_directory_mount(tmp_path, nested, mount_case, expected):
    root = tmp_path / "project"
    root.mkdir()
    source = root
    target = str(root)
    if mount_case == "covering_parent":
        source = tmp_path
        target = str(tmp_path)
    elif mount_case == "remapped":
        target = "/different/sandbox/root"
    elif mount_case == "missing":
        source = root / "missing"
        target = str(source)
    elif mount_case == "file":
        source = root / "file.txt"
        source.write_text("not a directory", encoding="utf-8")
        target = str(source)
    elif mount_case == "unrelated":
        source = tmp_path / "unrelated"
        source.mkdir()
        target = str(source)
    elif mount_case == "relative":
        source = "relative"
        target = "relative"
    policy = {"filesystem_policy": {"bind_mounts": [{"host_path": str(source), "sandbox_path": target}]}}
    if nested:
        policy = {"conch": policy}
    adapter = JiuWenSwarmDeepAdapter()
    adapter._sys_operation_card = SimpleNamespace(
        mode=OperationMode.SANDBOX,
        gateway_config=SimpleNamespace(launcher_config=SimpleNamespace(base_url="http://127.0.0.1:9999", extra_params={"policy": policy})),
    )
    assert adapter._artifact_workspace_host_visible(root) is expected


@pytest.mark.parametrize("card,expected", [
    (SimpleNamespace(mode=OperationMode.LOCAL), True),
    (SimpleNamespace(mode=OperationMode.SANDBOX), False),
    (SimpleNamespace(mode=None), False), (None, False),
])
def test_host_visibility_local_or_missing_card(tmp_path, card, expected):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._sys_operation_card = card
    assert adapter._artifact_workspace_host_visible(tmp_path) is expected


@pytest.mark.parametrize("endpoint", ["https://sandbox.invalid", "", "http://localhost.attacker.invalid", "http://[bad"])
def test_remote_or_unknown_gateway_does_not_prove_host_visibility(tmp_path, endpoint):
    policy = {"filesystem_policy": {"bind_mounts": [{"host_path": str(tmp_path), "sandbox_path": str(tmp_path)}]}}
    adapter = JiuWenSwarmDeepAdapter()
    adapter._sys_operation_card = SimpleNamespace(
        mode=OperationMode.SANDBOX,
        gateway_config=SimpleNamespace(launcher_config=SimpleNamespace(base_url=endpoint, extra_params={"policy": policy})),
    )
    assert not adapter._artifact_workspace_host_visible(tmp_path)


async def test_null_auto_options_keep_shared_recipe_compatible(cold):
    h = cold
    h.raw['permissions']['auto'] = None
    h.adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.LOCAL)
    await h.create()
    _assert_installed(h.adapter)
