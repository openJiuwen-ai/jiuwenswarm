# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagent permission check must not construct a nested PermissionInterruptRail."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import (
    SubagentPermissionRail,
)
from jiuwenswarm.agents.harness.common.tools.subagent_executor.authorization import (
    build_subagent_authorization_rails,
)


def test_build_subagent_rails_does_not_attach_nested_permission_interrupt_rail():
    import inspect

    from jiuwenswarm.agents.harness.common.tools.subagent_executor import authorization as auth_mod

    source = inspect.getsource(auth_mod.build_subagent_authorization_rails)
    assert "SkillAuthorizationPermissionRail(" not in source

    rails = build_subagent_authorization_rails(
        "child",
        "session",
        permission_key="parent-instance-key",
        config_provider=lambda: {
            "enabled": True,
            "skill_authorization": {"enabled": True},
        },
    )
    assert len(rails) == 2
    assert type(rails[1]).__name__ == "SubagentPermissionRail"
    assert not hasattr(rails[1], "_tool_check_rail")


@pytest.mark.asyncio
async def test_adjudicate_uses_temp_config_without_manager_key(monkeypatch):
    rail = object.__new__(SubagentPermissionRail)
    rail._config_provider = lambda: {
        "enabled": True,
        "skill_authorization": {"enabled": True},
        "tools": {"probe": "deny"},
    }
    rail._permission_key = "unused-unregistered-key"
    rail._grant_store = SimpleNamespace(
        get_active=lambda *_: SimpleNamespace(
            overlay_snapshot={"tools": {"probe": "allow"}},
        ),
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader."
        "has_session_permissions_overlay",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.skill_authorization.is_skill_authorization_enabled",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.skill_authorization.compose_skill_permissions",
        lambda base, overlay: {**dict(base), **dict(overlay)},
    )
    check_temp = AsyncMock(
        return_value=PermissionResult(PermissionLevel.ALLOW, "temp", "ok"),
    )
    check_key = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime."
        "check_permission_by_temp_config",
        check_temp,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime."
        "check_permission_by_key",
        check_key,
    )

    result, config = await SubagentPermissionRail._adjudicate_tool_call(
        rail, "probe", {}, session_id="session", agent_scope_id="child",
    )
    assert result.permission == PermissionLevel.ALLOW
    assert config["tools"]["probe"] == "allow"
    check_temp.assert_awaited_once()
    check_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_adjudicate_unregistered_key_fails_closed(monkeypatch):
    rail = object.__new__(SubagentPermissionRail)
    rail._config_provider = lambda: {"enabled": True}
    rail._permission_key = "missing-key"
    rail._grant_store = SimpleNamespace(get_active=lambda *_: None)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime."
        "check_permission_by_key",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.temp_permission_config."
        "assemble_temp_permission_config",
        lambda *_a, **_k: None,
        raising=False,
    )

    result, _ = await SubagentPermissionRail._adjudicate_tool_call(
        rail, "probe", {}, session_id="session", agent_scope_id="child",
    )
    assert result.permission == PermissionLevel.DENY
    assert result.matched_rule == "permission_key:unregistered"
