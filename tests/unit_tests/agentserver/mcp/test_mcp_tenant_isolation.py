# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP 数据层租户隔离：自定义 MCP / state / 凭证随 TenantContext 分租户落盘.

企业版下 MCP 数据根必须锚定 ``workspace_{tenant_key}/agent/jiuwenclaw_workspace``
（与 skills / memory 同一根），不能落全局 ``~/.jiuwenswarm/agent/workspace``，
否则各租户的自定义 MCP 与凭证互相可见。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key


@pytest.fixture()
def enterprise_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """企业版 + 用户工作区指到 tmp_path，隔离本机 ~/.jiuwenswarm."""
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    return tmp_path


def test_mcp_root_follows_tenant_context(enterprise_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """registry / state_store / credential / marketplace 的 <workspace>/mcp 随租户切换."""
    from jiuwenswarm.server.runtime.mcp import marketplace, registry, state_store
    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore

    key_a = build_tenant_key("group-a", "bot", "user-1")
    key_b = build_tenant_key("group-b", "bot", "user-2")

    with TenantContext.scoped(key_a):
        root_a = registry._mcp_root()
        state_a = state_store._mcp_root()
        cred_a = CredentialStore()._root
        hub_a = marketplace._mcp_root()

    with TenantContext.scoped(key_b):
        root_b = registry._mcp_root()

    assert root_a == enterprise_env / f"workspace_{key_a}" / "agent" / "jiuwenclaw_workspace" / "mcp"
    assert state_a == root_a
    assert hub_a == root_a
    assert cred_a == root_a / "credentials"
    assert root_b == enterprise_env / f"workspace_{key_b}" / "agent" / "jiuwenclaw_workspace" / "mcp"
    assert root_a != root_b


def test_custom_mcp_state_isolated_per_tenant(enterprise_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """租户 A 注册的自定义 MCP 不出现在租户 B 的 state 读取里."""
    from jiuwenswarm.server.runtime.mcp import state_store
    from jiuwenswarm.server.runtime.mcp.registry import register_custom_mcp

    key_a = build_tenant_key("group-a", "bot", "user-1")
    key_b = build_tenant_key("group-b", "bot", "user-2")

    with TenantContext.scoped(key_a):
        register_custom_mcp(
            "my-remote",
            {"transport": "http", "url": "https://example.internal/mcp"},
        )

    with TenantContext.scoped(key_a):
        assert state_store.get_mcp_record("my-remote") is not None
    with TenantContext.scoped(key_b):
        assert state_store.get_mcp_record("my-remote") is None

    # state.json 真实落在租户 A 的 workspace 下，而非全局 agent/workspace。
    tenant_a_state = (
        enterprise_env / f"workspace_{key_a}" / "agent" / "jiuwenclaw_workspace"
        / "mcp" / "state.json"
    )
    global_state = enterprise_env / "agent" / "workspace" / "mcp" / "state.json"
    assert tenant_a_state.is_file()
    assert not global_state.exists()


def test_personal_edition_mcp_root_matches_global_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """个人版行为不变：MCP 数据根仍是全局 agent/workspace."""
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    # 个人版路径不经 TenantContext 绑定（CV 置空），直接走兜底三元组。
    TenantContext.clear()
    from jiuwenswarm.server.runtime.mcp import registry

    root = registry._mcp_root()
    assert root == tmp_path / "agent" / "workspace" / "mcp"
