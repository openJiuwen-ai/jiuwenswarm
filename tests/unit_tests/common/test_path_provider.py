import logging
from pathlib import Path

import pytest

from jiuwenswarm.common import path_provider as providers
from jiuwenswarm.common import utils


@pytest.fixture(autouse=True)
def reset_provider():
    providers.reset_path_provider()
    yield
    providers.reset_path_provider()


def test_selective_override_and_explicit_priority():
    original = utils.get_logs_dir(service_id="test-service")

    class Provider(providers.PathProvider):
        name = "logs-only"

        def resolve_path(self, category, ctx, **kwargs):
            if category == providers.PathCategory.LOGS:
                return Path("custom-logs")
            return None

    providers.register_path_provider(Provider())
    assert utils.get_logs_dir() == Path("custom-logs")
    assert utils.get_logs_dir(service_id="test-service") == original
    assert utils.get_agent_memory_dir() == utils.get_agent_workspace_dir() / "memory"


def test_session_context_restores_previous_value():
    before = providers.current_path_context().session_id
    token = providers.bind_path_session_id("session-a")
    try:
        assert providers.current_path_context().session_id == "session-a"
        assert (
            providers.current_path_context(session_id="explicit").session_id
            == "explicit"
        )
    finally:
        providers.reset_path_session_id(token)
    assert providers.current_path_context().session_id == before


def test_all_designed_categories_are_dispatched(tmp_path):
    calls = []

    class Provider(providers.PathProvider):
        name = "all-paths"

        def resolve_path(self, category, ctx, *, node=None, session_id=None):
            calls.append((category, ctx.session_id, node, session_id))
            suffix = node or session_id or category.value
            return tmp_path / category.value / suffix

        def resolve_path_list(self, category, ctx):
            calls.append((category, ctx.session_id, None, None))
            return [tmp_path / "shared-a", tmp_path / "shared-b"]

    providers.register_path_provider(Provider())

    scalar_getters = {
        providers.PathCategory.LOGS: utils.get_logs_dir,
        providers.PathCategory.CHECKPOINT: utils.get_checkpoint_dir,
        providers.PathCategory.WORKSPACE: utils.get_agent_workspace_dir,
        providers.PathCategory.AGENT_ROOT: utils.get_agent_root_dir,
        providers.PathCategory.MEMORY: utils.get_agent_memory_dir,
        providers.PathCategory.SKILLS: utils.get_agent_skills_dir,
        providers.PathCategory.SESSIONS: utils.get_agent_sessions_dir,
        providers.PathCategory.INTERACTIONS: utils.get_interactions_dir,
        providers.PathCategory.PROMPT_ATTACHMENT: utils.get_prompt_attachment_dir,
        providers.PathCategory.PROJECT_WORKSPACE: utils.get_default_project_workspace_dir,
        providers.PathCategory.TODO: utils.get_deepagent_todo_dir,
        providers.PathCategory.MESSAGES: utils.get_deepagent_messages_dir,
        providers.PathCategory.AGENTS: utils.get_deepagent_agents_dir,
        providers.PathCategory.EVOLUTION_TRAJECTORIES: utils.get_agent_evolution_trajectories_dir,
    }
    for category, getter in scalar_getters.items():
        assert getter() == tmp_path / category.value / category.value

    assert utils.get_default_project_session_workspace_dir("session-1") == (
        tmp_path / "project_session_workspace" / "session-1"
    )
    workspace_md_getters = {
        "HEARTBEAT.md": utils.get_deepagent_heartbeat_path,
        "AGENT.md": utils.get_deepagent_agent_md_path,
        "SOUL.md": utils.get_deepagent_soul_md_path,
        "IDENTITY.md": utils.get_deepagent_identity_md_path,
        "USER.md": utils.get_deepagent_user_md_path,
    }
    for node, getter in workspace_md_getters.items():
        assert getter() == tmp_path / "workspace_md" / node

    from jiuwenswarm.server.runtime.debug_trace.paths import debug_trace_dir

    assert debug_trace_dir("code") == tmp_path / "debug_trace" / "code"
    assert utils.get_shared_agent_skills_dirs() == [
        tmp_path / "shared-a",
        tmp_path / "shared-b",
    ]
    assert (
        providers.PathCategory.PROJECT_SESSION_WORKSPACE,
        "session-1",
        None,
        "session-1",
    ) in calls


def test_provider_exception_falls_back_to_default(caplog):
    expected = utils.get_logs_dir()

    class Provider(providers.PathProvider):
        name = "broken"

        def resolve_path(self, category, ctx, **kwargs):
            raise RuntimeError("unavailable")

    providers.register_path_provider(Provider())
    logger = logging.getLogger("jiuwenswarm.common.utils")
    logger.addHandler(caplog.handler)
    try:
        assert utils.get_logs_dir() == expected
        assert "fallback to default" in caplog.text
    finally:
        logger.removeHandler(caplog.handler)


def test_bound_tenant_paths_are_context_not_implicit_overrides(tmp_path):
    from jiuwenswarm.server.runtime.tenant_context import (
        bind_tenant_workspace_dirs,
        reset_tenant_workspace_dirs,
    )

    class Provider(providers.PathProvider):
        name = "workspace"

        def resolve_path(self, category, ctx, **kwargs):
            if category is providers.PathCategory.WORKSPACE:
                assert ctx.bound_workspace == str(tmp_path / "bound-workspace")
                return tmp_path / "provider-workspace"
            return None

    token = bind_tenant_workspace_dirs(
        jiuwenclaw_workspace=str(tmp_path / "bound-workspace"),
        agent_root=str(tmp_path / "bound-root"),
        tenant_root=str(tmp_path),
    )
    providers.register_path_provider(Provider())
    try:
        assert utils.get_agent_workspace_dir() == tmp_path / "provider-workspace"
    finally:
        reset_tenant_workspace_dirs(token)


def test_workspace_directory_nodes_replace_defaults(tmp_path):
    from openjiuwen.harness.workspace.workspace import Workspace

    class Provider(providers.PathProvider):
        name = "workspace-schema"

        def build_workspace_directories(self, ctx):
            return [
                {
                    "name": "memory",
                    "description": "custom memory",
                    "path": "state/memory",
                    "children": [],
                }
            ]

    provider = Provider()
    workspace = Workspace(root_path=tmp_path)
    workspace.set_directory(
        provider.build_workspace_directories(providers.current_path_context())
    )
    assert workspace.get_node_path("memory") == tmp_path / "state" / "memory"


def test_dispatch_workspace_directories_uses_provider():
    class Provider(providers.PathProvider):
        name = "dirs"

        def resolve_path(self, category, ctx, **kwargs):
            return None

        def build_workspace_directories(self, ctx):
            return [
                {
                    "name": "memory",
                    "description": "custom memory",
                    "path": "state/memory",
                    "children": [],
                }
            ]

    providers.register_path_provider(Provider())
    nodes = utils._dispatch_workspace_directories()
    assert nodes[0]["path"] == "state/memory"


def test_explicit_workspace_root_skips_provider_but_keeps_directory_nodes(tmp_path):
    class Provider(providers.PathProvider):
        name = "split"

        def resolve_path(self, category, ctx, **kwargs):
            if category == providers.PathCategory.WORKSPACE:
                return tmp_path / "hijacked"
            return None

        def build_workspace_directories(self, ctx):
            return [
                {
                    "name": "memory",
                    "description": "",
                    "path": "state/memory",
                    "children": [],
                }
            ]

    providers.register_path_provider(Provider())
    kept = str(tmp_path / "kept")
    assert utils._dispatch_path(
        providers.PathCategory.WORKSPACE, explicit=kept,
    ) is None
    assert utils._dispatch_path(providers.PathCategory.WORKSPACE) == tmp_path / "hijacked"
    assert utils._dispatch_workspace_directories()[0]["name"] == "memory"
