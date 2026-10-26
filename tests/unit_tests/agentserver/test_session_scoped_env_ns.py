# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common.local_env_config import (
    bind_agent_env_ns,
    effective_tip,
    get_local_config,
    replace_active_env,
    reset_agent_env_ns,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_new_session_scoped_adapter_inherits_tenant_env_namespace() -> None:
    parent = object.__new__(JiuWenSwarmDeepAdapter)
    parent._env_agent_id = "office"
    parent._env_service_id = "default"
    parent._skill_manager = None
    parent._session_adapters = {}
    parent._session_adapter_locks = {}
    # _new_session_scoped_adapter snapshots the Host's personal-context
    # switch; the bypassed __init__ would leave it unset.
    parent._personal_context_runtime_enabled = False

    child = parent._new_session_scoped_adapter("officeclaw_sess_test")

    assert child._is_session_scoped_adapter is True
    assert child._parent_session_id == "officeclaw_sess_test"
    assert child._env_agent_id == "office"
    assert child._env_service_id == "default"


def test_session_children_inherit_distinct_tenant_artifact_workspaces(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    parents = [
        JiuWenSwarmDeepAdapter(
            workspace_dir=str(tmp_path / tenant),
            agent_id=tenant,
            service_id="office",
        )
        for tenant in ("tenant-a", "tenant-b")
    ]

    children = [
        parent._new_session_scoped_adapter(f"session-{index}")
        for index, parent in enumerate(parents)
    ]

    expected = [
        str((tmp_path / tenant).resolve() / "projects")
        for tenant in ("tenant-a", "tenant-b")
    ]
    assert [child._workspace_dir for child in children] == [
        str((tmp_path / "tenant-a").resolve()),
        str((tmp_path / "tenant-b").resolve()),
    ]
    assert [
        child._deepresearch_artifact_output_dir() for child in children
    ] == expected
    assert expected[0] != expected[1]


def test_code_session_child_keeps_polymorphic_constructor_and_tenant_workspace(
    tmp_path: Path,
) -> None:
    parent = JiuwenSwarmCodeAdapter()
    parent._workspace_dir = str(tmp_path / "code-tenant")
    parent._agent_id = "code-agent"
    parent._service_id = "office"

    child = parent._new_session_scoped_adapter("code-session")

    assert isinstance(child, JiuwenSwarmCodeAdapter)
    assert child._workspace_dir == parent._workspace_dir
    assert child._agent_id == "code-agent"
    assert child._service_id == "office"


def test_bound_office_tip_exposes_synced_model_name_not_placeholder() -> None:
    """create_instance must bind office tip so MODEL_NAME is glm-5.1, not .env placeholder."""
    replace_active_env(
        {
            "MODEL_NAME": "glm-5.1",
            "MODEL_PROVIDER": "OpenAI",
            "API_KEY": "test-key",
            "API_BASE": "https://example.test/v1",
        },
        service_id="default",
        agent_id="office",
        clear_staged=True,
    )
    token = bind_agent_env_ns("default", "office")
    try:
        tip = effective_tip()
        assert tip.get("MODEL_NAME") == "glm-5.1"
        assert get_local_config("MODEL_NAME") == "glm-5.1"
        assert get_local_config("API_BASE") == "https://example.test/v1"
    finally:
        reset_agent_env_ns(token)
        replace_active_env({}, service_id="default", agent_id="office", clear_staged=True)
