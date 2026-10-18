# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""带作用域的 Provider、物理路径映射和安全边界回归。"""

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.common import path_provider as providers
from jiuwenswarm.common import utils
from jiuwenswarm.common.workspace.service import WorkspaceError, WorkspaceService
from jiuwenswarm.server.runtime.tenant_context import (
    bind_tenant_workspace_dirs,
    reset_tenant_workspace_dirs,
)


@pytest.fixture
def install_provider():
    previous = providers.get_path_provider()
    calls = []

    def install(resolve):
        class Provider(providers.PathProvider):
            name = "scoped-workspace"

            def resolve_path(self, category, ctx, **kwargs):
                if category == providers.PathCategory.WORKSPACE:
                    calls.append(ctx)
                    return resolve(ctx)
                return None

        providers.register_path_provider(Provider())
        return calls

    yield install
    providers.reset_path_provider()
    if previous is not None:
        providers.register_path_provider(previous)


@pytest.mark.parametrize("outcome", ["none", "exception"])
def test_scoped_fallback_does_not_use_another_tenants_bound_workspace(
    tmp_path, install_provider, outcome,
):
    def resolve(ctx):
        if outcome == "exception":
            raise RuntimeError("provider unavailable")

    calls = install_provider(resolve)
    token = bind_tenant_workspace_dirs(
        jiuwenclaw_workspace=str(tmp_path / "tenant-a" / "custom-workspace"),
        agent_root=str(tmp_path / "tenant-a" / "agent"),
        tenant_root=str(tmp_path / "tenant-a"),
    )
    try:
        before = providers.current_path_context()
        root_b = tmp_path / "tenant-b" / "agent"
        assert utils.get_agent_workspace_dir(
            agent_root=root_b, workspace_key="tenant-b", service_id="svc-b", agent_id="bot-b",
        ) == root_b / "jiuwenclaw_workspace"
        assert calls[-1].bound_agent_root == str(root_b)
        assert calls[-1].bound_workspace is None
        assert (calls[-1].service_id, calls[-1].agent_id, calls[-1].workspace_key) == (
            "svc-b", "bot-b", "tenant-b",
        )
        assert providers.current_path_context() == before
        assert utils.get_agent_workspace_dir() == tmp_path / "tenant-a" / "custom-workspace"
    finally:
        reset_tenant_workspace_dirs(token)


@pytest.mark.asyncio
async def test_concurrent_scoped_resolution_does_not_mix_tenants(tmp_path, install_provider):
    calls = install_provider(lambda ctx: tmp_path / ctx.workspace_key / ctx.agent_id)

    async def resolve(key):
        root = tmp_path / key / "agent"
        token = bind_tenant_workspace_dirs(
            jiuwenclaw_workspace=str(root / "bound"), agent_root=str(root),
            tenant_root=str(root.parent),
        )
        try:
            await asyncio.sleep(0)
            return utils.get_agent_workspace_dir(
                agent_root=root, workspace_key=key, service_id=f"svc-{key}", agent_id=f"bot-{key}",
            )
        finally:
            reset_tenant_workspace_dirs(token)

    results = await asyncio.gather(resolve("a"), resolve("b"))
    assert results == [tmp_path / "a" / "bot-a", tmp_path / "b" / "bot-b"]
    assert {ctx.workspace_key for ctx in calls} == {"a", "b"}
    assert providers.current_path_context().bound_workspace is None


def test_scope_only_getter_supplies_correct_root(tmp_path, monkeypatch, install_provider):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    calls = install_provider(lambda ctx: Path(ctx.bound_agent_root) / "custom")
    expected = tmp_path / "workspace_other" / "agent" / "custom"
    assert utils.get_agent_workspace_dir(workspace_key="other") == expected
    assert calls[-1].bound_agent_root == str(expected.parent)


def test_explicit_final_paths_still_skip_provider(tmp_path, install_provider):
    calls = install_provider(lambda ctx: tmp_path / "plugin")
    assert utils._dispatch_path(providers.PathCategory.WORKSPACE, explicit=tmp_path / "explicit") is None
    assert calls == []


@pytest.mark.parametrize("external", [False, True])
def test_logical_tree_preview_download_delete_and_protected_zones(
    tmp_path, install_provider, external,
):
    tenant = tmp_path / "tenant"
    workspace = tmp_path / "external" if external else tenant / "agent" / "custom"
    project = workspace / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "report.md").write_text("report", encoding="utf-8")
    (workspace / "AGENT.md").write_text("role", encoding="utf-8")
    (workspace / "memory").mkdir()
    (workspace / "memory" / "keep.txt").write_text("keep", encoding="utf-8")
    install_provider(lambda ctx: workspace)
    service = WorkspaceService.for_tenant(tenant)
    prefix = "agent/jiuwenclaw_workspace"

    assert {entry["name"] for entry in service.list_tree()["entries"]} == {"agent"}
    assert "jiuwenclaw_workspace" in {
        entry["name"] for entry in service.list_tree("agent")["entries"]
    }
    entries = service.list_tree(prefix)["entries"]
    role = next(entry for entry in entries if entry["name"] == "AGENT.md")
    assert role["zone"] == "role_md" and role["deletable"] is False
    relative = f"{prefix}/projects/demo/report.md"
    assert service.preview_file(relative)["content"] == "report"
    assert service.download_payload(relative)["content"] == b"report"
    denied = service.delete_entries([f"{prefix}/AGENT.md", f"{prefix}/memory"])["results"]
    assert all(item["error"] == "forbidden_zone" for item in denied)
    assert service.delete_entries([f"{prefix}/projects/demo"])["results"][0]["ok"] is True
    assert not project.exists()
    assert (workspace / "memory" / "keep.txt").is_file()
    assert (workspace / "AGENT.md").is_file()


@pytest.mark.parametrize("path", ["../escape", "agent/jiuwenclaw_workspace/../escape", "/absolute", "C:/escape"])
def test_mapped_workspace_rejects_unsafe_paths(tmp_path, install_provider, path):
    install_provider(lambda ctx: tmp_path / "external")
    service = WorkspaceService.for_tenant(tmp_path / "tenant")
    with pytest.raises(WorkspaceError, match="invalid relative_path"):
        service.resolve(path)
    assert service.delete_entries([path])["results"][0]["error"] == "bad_path"


def test_mapped_symlink_cannot_escape_or_bypass_protected_zone(tmp_path, install_provider):
    workspace = tmp_path / "external"
    projects = workspace / "projects"
    memory = workspace / "memory"
    projects.mkdir(parents=True)
    memory.mkdir()
    victim = memory / "keep.txt"
    victim.write_text("keep", encoding="utf-8")
    link = projects / "link"
    try:
        link.symlink_to(memory, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")
    install_provider(lambda ctx: workspace)
    service = WorkspaceService.for_tenant(tmp_path / "tenant")
    nested = service.delete_entries(["agent/jiuwenclaw_workspace/projects/link/keep.txt"])
    assert nested["results"][0]["error"] == "forbidden_zone"
    deleted = service.delete_entries(["agent/jiuwenclaw_workspace/projects/link"])
    assert deleted["results"][0]["ok"] is True
    assert victim.read_text(encoding="utf-8") == "keep"
    outside = tmp_path / "outside"
    outside.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="escapes"):
        service.resolve("agent/jiuwenclaw_workspace/projects/link/secret")


def test_provider_seed_is_idempotent_and_does_not_move_default_data(
    tmp_path, monkeypatch, install_provider,
):
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(utils, "_resolve_preferred_language", lambda *_: "en")
    workspace = tmp_path / "plugin"
    install_provider(lambda ctx: workspace)
    utils.prepare_provider_workspace()
    assert (workspace / "AGENT.md").is_file()
    (workspace / "AGENT.md").write_text("user content", encoding="utf-8")
    utils.prepare_provider_workspace()
    assert (workspace / "AGENT.md").read_text(encoding="utf-8") == "user content"
    assert not (tmp_path / "data" / "agent" / "jiuwenclaw_workspace").exists()


@pytest.mark.parametrize("external", [True, False])
def test_external_workspace_is_counted_by_quota_reconciliation(
    tmp_path, monkeypatch, install_provider, external,
):
    from jiuwenswarm.common.workspace import quota
    from jiuwenswarm.common.workspace import service as service_module

    def no_du(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(service_module.subprocess, "run", no_du)

    tenant = tmp_path / "tenant"
    tenant.mkdir()
    (tenant / "metadata.txt").write_bytes(b"abc")
    workspace = tmp_path / "external" if external else tenant / "custom"
    workspace.mkdir()
    (workspace / "report.txt").write_bytes(b"12345")
    install_provider(lambda ctx: workspace)
    service = WorkspaceService.for_tenant(tenant)
    assert service.measure_used_bytes() == 8
    quota.clear_used_bytes_cache()
    try:
        quota.mark_usage_active(tenant, workspace_root=workspace, user_id="u")
        assert quota.measure_and_cache(tenant) == 8
        assert quota.get_cached_used(tenant) == 8
        entry = quota.list_usage_reconcile_targets(min_interval_seconds=0, now=float("inf"))[0]
        assert entry.workspace_root == str(workspace.resolve())
        if external:
            assert quota.measure_and_cache(tenant, workspace_root=None) == 3
    finally:
        quota.clear_used_bytes_cache()


def test_http_paths_and_generated_manifest_remain_logical(tmp_path, install_provider):
    from jiuwenswarm.gateway.channel_manager.web import file_http

    project_root = tmp_path / "tenant"
    workspace = tmp_path / "external"
    workspace.mkdir()
    (workspace / "hello.md").write_text("hello", encoding="utf-8")
    install_provider(lambda ctx: workspace)
    roots = file_http.FileHttpRoots(
        project_root=project_root, workspace_root=project_root / "agent",
        agent_teams_root=project_root / "teams", logs_root=project_root / "logs",
        auto_harness_root=project_root / "harness",
    )
    prefix = "agent/jiuwenclaw_workspace"
    status, data = file_http.list_files(roots, prefix)
    assert status == 200
    assert data["files"][0]["path"].replace("\\", "/") == f"{prefix}/hello.md"
    assert file_http.read_file_text(roots, f"{prefix}/hello.md")[2] == b"hello"
    assert file_http.write_markdown_content(roots, f"{prefix}/hello.md", "updated")[0] == 200
    assert (workspace / "hello.md").read_text(encoding="utf-8") == "updated"
    for alias in ("agent/workspace/agent-data.json", f"{prefix}/agent-data.json"):
        status, _, content, _ = file_http.read_file_text(roots, alias)
        assert status == 200
        assert b"agent/jiuwenclaw_workspace/hello.md" in content
        assert str(workspace).encode() not in content
    assert file_http.resolve_raw_file_path(roots, f"{prefix}/hello.md")[2] == workspace / "hello.md"
    assert file_http.list_files(roots, "../outside")[0] == 403


def test_runtime_binding_keeps_roots_independent_from_external_workspace(
    tmp_path, install_provider,
):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.tenant_context import (
        get_bound_agent_root,
        get_bound_tenant_root,
    )

    install_provider(lambda ctx: tmp_path / "external" / ctx.workspace_key)
    obj = object.__new__(JiuWenSwarm)
    obj._user_workspace_dir = tmp_path / "tenant"
    obj._workspace_key = "wk"
    obj._env_service_id = "svc"
    obj._env_agent_id = "bot"
    before = providers.current_path_context()
    token, memory_token = obj._bind_tenant_request_context()
    try:
        assert get_bound_agent_root() == tmp_path / "tenant" / "agent"
        assert get_bound_tenant_root() == tmp_path / "tenant"
        assert providers.current_path_context().bound_workspace == str(tmp_path / "external" / "wk")
    finally:
        obj._reset_tenant_request_context(token, memory_token)
    assert providers.current_path_context() == before


@pytest.mark.asyncio
async def test_skill_manager_and_permission_root_use_the_same_provider_workspace(
    tmp_path, monkeypatch, install_provider,
):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    from jiuwenswarm.server.runtime.agent_manager import AgentManager
    from jiuwenswarm.server.runtime.skill import skill_manager

    workspace = tmp_path / "external" / "tenant"
    calls = install_provider(lambda ctx: workspace)
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    class Manager:
        def __init__(self, **kwargs):
            self.workspace_dir = kwargs["workspace_dir"]

    monkeypatch.setattr(skill_manager, "SkillManager", Manager)
    manager = object.__new__(AgentManager)
    manager._skill_manager = None
    manager._skill_manager_lock = asyncio.Lock()
    manager._user_workspace_dir = tmp_path / "tenant"
    manager._workspace_key = "tenant"
    manager._env_service_id = "svc"
    manager._env_agent_id = "bot"
    manager.service_id = "svc"
    manager.agent_id = "runtime-bot"
    result = await manager._get_or_create_skill_manager()
    assert Path(result.workspace_dir) == workspace
    assert JiuWenSwarmDeepAdapter._permission_workspace_dir(manager) == workspace
    assert calls[-1].workspace_key == "tenant"
    assert calls[-1].agent_id == "bot"


@pytest.mark.parametrize("workspace_name", ["custom", "workspace"])
def test_nested_workspace_collapse_respects_provider_root(tmp_path, install_provider, workspace_name):
    workspace = tmp_path / "jiuwenclaw_workspace" / workspace_name
    install_provider(lambda ctx: workspace)
    assert utils.collapse_nested_agent_workspace_dir(workspace) == workspace
    assert utils.collapse_nested_agent_workspace_dir(workspace / "workspace") == workspace


def test_mapped_delete_preserves_depth_and_entry_budgets(tmp_path, install_provider):
    workspace = tmp_path / "external"
    target = workspace / "projects" / "demo"
    (target / "child").mkdir(parents=True)
    (target / "one.txt").write_text("1", encoding="utf-8")
    (target / "two.txt").write_text("2", encoding="utf-8")
    install_provider(lambda ctx: workspace)
    relative = "agent/jiuwenclaw_workspace/projects/demo"
    shallow = WorkspaceService(tmp_path / "tenant", max_delete_depth=1, agent_workspace=workspace)
    limited = WorkspaceService(tmp_path / "tenant", max_delete_entries=1, agent_workspace=workspace)
    assert shallow.delete_entries([relative])["results"][0]["error"] == "delete_too_deep"
    assert limited.delete_entries([relative])["results"][0]["error"] == "delete_too_many"
    assert (target / "one.txt").is_file()


@pytest.mark.parametrize("override", [True, False])
def test_http_and_agent_use_the_same_runtime_root_for_root_based_provider(
    tmp_path, monkeypatch, install_provider, override,
):
    from jiuwenswarm.gateway.channel_manager.web import file_http

    data_root = tmp_path / "data"
    tenant = data_root / "service_default" / "agent_default"
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: data_root)
    install_provider(lambda ctx: Path(ctx.bound_agent_root) / "custom" if override else None)
    workspace = utils.get_agent_workspace_dir(agent_root=tenant / "agent")
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("report", encoding="utf-8")
    roots = file_http.FileHttpRoots(
        project_root=data_root, workspace_root=data_root / "agent",
        agent_teams_root=data_root / "teams", logs_root=data_root / "logs",
        auto_harness_root=data_root / "harness",
    )
    logical = "agent/jiuwenclaw_workspace/report.md"
    actual = file_http.resolve_under_project(roots, logical)
    if override:
        assert actual == workspace / "report.md"
        assert file_http.read_file_text(roots, logical)[2] == b"report"
    else:
        assert actual == data_root / "agent" / "jiuwenclaw_workspace" / "report.md"
