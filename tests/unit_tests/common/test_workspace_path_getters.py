# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作区路径收口后的布局、租户隔离和 Provider 边界回归。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import path_provider as providers
from jiuwenswarm.common import utils
from jiuwenswarm.common.local_env_config import bind_agent_env_ns, reset_agent_env_ns
from jiuwenswarm.server.runtime.tenant_context import (
    bind_tenant_workspace_dirs,
    bind_workspace_key,
    reset_tenant_workspace_dirs,
    reset_workspace_key,
)


@pytest.fixture
def no_path_provider():
    previous = providers.get_path_provider()
    providers.reset_path_provider()
    yield
    if previous is not None:
        providers.register_path_provider(previous)


@pytest.fixture
def path_provider(tmp_path):
    calls = []

    class Provider(providers.PathProvider):
        name = "workspace-path-regression"

        def resolve_path(self, category, ctx, **kwargs):
            calls.append(category)
            return tmp_path / "provider" / category.value

    previous = providers.get_path_provider()
    providers.register_path_provider(Provider())
    yield tmp_path / "provider", calls
    providers.reset_path_provider()
    if previous is not None:
        providers.register_path_provider(previous)


@pytest.mark.parametrize("as_string", [False, True])
def test_explicit_root_keeps_default_layout_without_creating_directories(
    tmp_path, as_string, path_provider,
):
    provider_root, calls = path_provider
    agent_root = tmp_path / "tenant" / "agent"
    token = bind_tenant_workspace_dirs(
        jiuwenclaw_workspace=str(tmp_path / "bound-workspace"),
        agent_root=str(tmp_path / "bound-agent"),
        tenant_root=str(tmp_path / "bound-tenant"),
    )
    try:
        actual = utils.get_agent_workspace_dir(
            agent_root=str(agent_root) if as_string else agent_root,
        )
        assert actual == provider_root / "workspace"
        assert not agent_root.exists()
        assert calls == [providers.PathCategory.WORKSPACE]
        assert utils.get_agent_workspace_dir(
            agent_root=agent_root, use_provider=False,
        ) == agent_root / "jiuwenclaw_workspace"
        assert utils.get_agent_workspace_dir() == provider_root / "workspace"
        assert calls == [providers.PathCategory.WORKSPACE] * 2
    finally:
        reset_tenant_workspace_dirs(token)


def test_relative_layout_keeps_relative_path_and_ignores_provider(path_provider):
    _, calls = path_provider
    assert utils.get_agent_workspace_relative_dir() == Path("agent/jiuwenclaw_workspace")
    assert utils.get_agent_workspace_dir(
        agent_root=Path("tenant") / ".." / "agent", use_provider=False,
    ) == Path("tenant/../agent/jiuwenclaw_workspace")
    assert calls == []


@pytest.mark.parametrize("edition", ["personal", "enterprise"])
@pytest.mark.parametrize("workspace_key", [None, "explicit"])
def test_tenant_getters_preserve_existing_scope_resolution(
    tmp_path, monkeypatch, no_path_provider, edition, workspace_key,
):
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    env_token = bind_agent_env_ns("bound-service", "bound-agent")
    key_token = bind_workspace_key("bound-key")
    try:
        key = workspace_key or "bound-key"
        tenant_root = (
            tmp_path / f"workspace_{key}"
            if edition == "enterprise"
            else tmp_path / "service_bound-service" / "agent_bound-agent"
        )
        expected = tenant_root / "agent" / "jiuwenclaw_workspace"
        assert utils.get_tenant_agent_workspace_dir(workspace_key) == expected
        assert utils.resolve_tenant_agent_workspace_dir(workspace_key) == expected

        explicit_root = (
            tmp_path / f"workspace_{key}"
            if edition == "enterprise"
            else tmp_path / "service_explicit-service" / "agent_explicit-agent"
        )
        assert utils.resolve_tenant_agent_workspace_dir(
            workspace_key,
            service_id="explicit-service",
            agent_id="explicit-agent",
        ) == explicit_root / "agent" / "jiuwenclaw_workspace"
    finally:
        reset_workspace_key(key_token)
        reset_agent_env_ns(env_token)


def test_tenant_resolver_keeps_bound_root_without_using_bound_workspace(
    tmp_path, no_path_provider,
):
    agent_root = tmp_path / "tenant" / "custom-agent-root"
    token = bind_tenant_workspace_dirs(
        jiuwenclaw_workspace=str(tmp_path / "bound-workspace"),
        agent_root=str(agent_root),
        tenant_root=str(tmp_path / "tenant"),
    )
    try:
        assert utils.resolve_tenant_agent_workspace_dir() == (
            agent_root / "jiuwenclaw_workspace"
        )
    finally:
        reset_tenant_workspace_dirs(token)


@pytest.mark.parametrize("edition", ["personal", "enterprise"])
def test_gateway_keeps_default_key_when_only_request_key_is_bound(
    tmp_path, monkeypatch, no_path_provider, edition,
):
    from jiuwenswarm.gateway.tenant_paths import resolve_channel_agent_workspace

    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    token = bind_workspace_key("request-key")
    try:
        for key in (None, "explicit"):
            tenant_root = (
                tmp_path / f"workspace_{key or 'default'}"
                if edition == "enterprise"
                else tmp_path / "service_default" / "agent_default"
            )
            assert resolve_channel_agent_workspace(key) == (
                tenant_root / "agent" / "jiuwenclaw_workspace"
            )
    finally:
        reset_workspace_key(token)


@pytest.mark.parametrize("mode", ["work", "code"])
def test_project_defaults_use_workspace_provider(
    path_provider, mode,
):
    from jiuwenswarm.server.runtime.session.project_store import (
        resolve_default_project_dir,
    )

    provider_root, calls = path_provider
    assert resolve_default_project_dir("demo", mode) == str(
        provider_root / "workspace" / mode / "demo"
    )
    assert calls == [providers.PathCategory.AGENT_ROOT, providers.PathCategory.WORKSPACE]


def test_adapter_workspace_keeps_explicit_tenant_root(tmp_path, path_provider):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    calls_before = list(path_provider[1])
    obj = SimpleNamespace(_user_workspace_dir=tmp_path / "tenant")
    assert JiuWenSwarm._resolve_workspace_dir(obj) == str(
        (path_provider[0] / "workspace").resolve()
    )
    assert path_provider[1] == calls_before + [providers.PathCategory.WORKSPACE]


@pytest.mark.parametrize("edition", ["personal", "enterprise"])
def test_rail_extensions_keep_existing_edition_behavior(
    tmp_path, monkeypatch, path_provider, edition,
):
    from jiuwenswarm.agents.harness.common.plugins.rail_manager import RailManager

    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    obj = SimpleNamespace(workspace_key="tenant", service_id="svc", agent_id="agent")
    actual = RailManager._resolve_extensions_dir(obj)

    assert actual == path_provider[0] / "workspace" / "extensions"
    assert path_provider[1] == [providers.PathCategory.WORKSPACE]


def test_external_memory_keeps_explicit_and_bound_tenant_paths(
    tmp_path, monkeypatch, no_path_provider,
):
    from jiuwenswarm.agents.harness.common.memory import external_memory_config

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    token = bind_workspace_key("bound-key")
    try:
        for key in (None, "explicit"):
            actual = external_memory_config._resolve_ltm_dir(workspace_key=key)
            assert actual == (
                tmp_path / f"workspace_{key or 'bound-key'}" / "agent"
                / "jiuwenclaw_workspace" / "memory" / "ltm"
            )
            assert actual.is_dir()
    finally:
        reset_workspace_key(token)


def test_legacy_migration_keeps_local_target_with_provider_registered(
    tmp_path, path_provider,
):
    from jiuwenswarm.server.runtime.session.session_metadata import (
        _remap_legacy_workspace_prefix,
    )

    old_workspace = tmp_path / "workspace_tenant" / "agent" / "workspace"
    old_workspace.mkdir(parents=True)
    (old_workspace / "user.txt").write_text("preserve", encoding="utf-8")

    utils._migrate_workspace_to_jiuwenclaw_workspace(tmp_path)

    new_workspace = old_workspace.parent / "jiuwenclaw_workspace"
    assert (new_workspace / "user.txt").read_text(encoding="utf-8") == "preserve"
    assert not old_workspace.exists()
    assert _remap_legacy_workspace_prefix(str(old_workspace / "work" / "demo")) == str(
        new_workspace / "work" / "demo"
    )
    assert path_provider[1] == []


def test_cached_workspace_resolution_keeps_default_tenant_with_provider(
    tmp_path, monkeypatch, path_provider,
):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    (tmp_path / "config").mkdir()
    for attr in ("_initialized", "_config_dir", "_workspace_dir", "_root_dir"):
        monkeypatch.setattr(utils, attr, getattr(utils, attr))

    utils._resolve_paths(force=True)

    assert utils.get_workspace_dir() == (
        tmp_path / "workspace_default" / "agent" / "jiuwenclaw_workspace"
    )
    assert path_provider[1] == []


@pytest.mark.parametrize("edition", ["personal", "enterprise"])
def test_prepare_workspace_preserves_default_layout_and_seeds_templates(
    tmp_path, monkeypatch, path_provider, edition,
):
    package_root = tmp_path / "package"
    resources = package_root / "resources"
    template = resources / "agent" / "workspace"
    template.mkdir(parents=True)
    (template / "AGENT_EN.md").write_text("agent template", encoding="utf-8")
    (template / "USER.md").write_text("user template", encoding="utf-8")
    (resources / "config.yaml").write_text("version: '1'\n", encoding="utf-8")
    (resources / ".env.template").write_text("", encoding="utf-8")
    data_root = tmp_path / "data"

    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setattr(utils, "_find_package_root", lambda: package_root)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: data_root)
    monkeypatch.setattr(utils, "_config_dir_from_env", lambda: data_root / "config")
    monkeypatch.setattr(utils, "migrate_legacy_user_config_if_needed", lambda: None)
    for attr in ("_initialized", "_config_dir", "_workspace_dir", "_root_dir"):
        monkeypatch.setattr(utils, attr, getattr(utils, attr))

    utils.prepare_workspace(overwrite=False, preferred_language="en", workspace_dir=data_root)

    tenant = (
        data_root / "workspace_default"
        if edition == "enterprise"
        else data_root / "service_default" / "agent_default"
    )
    workspace = tenant / "agent" / "jiuwenclaw_workspace"
    assert (workspace / "AGENT.md").read_text(encoding="utf-8") == "agent template"
    assert (workspace / "USER.md").read_text(encoding="utf-8") == "user template"
    assert (workspace / "projects").is_dir()
    assert (tenant / "agent" / "sessions").is_dir()
    assert not path_provider[0].exists()
    assert path_provider[1] == []


@pytest.mark.parametrize("data", [None, {}, {"chunk_size": 128000}])
def test_file_transfer_default_keeps_relative_posix_layout(data, path_provider):
    from jiuwenswarm.common.file_transfer_config import FileTransferConfig

    config = FileTransferConfig.from_dict(data)
    assert config.received_files_dir == "agent/jiuwenclaw_workspace/received_files"
    assert path_provider[1] == []
