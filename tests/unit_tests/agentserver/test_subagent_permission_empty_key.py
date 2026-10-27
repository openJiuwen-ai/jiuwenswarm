# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unregistered permission key is registered empty — no yaml fallback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from agent_shield.tool_permission import get_permission_manager

from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import (
    SubagentPermissionRail,
)


def test_ensure_published_config_registers_empty_key_without_yaml(monkeypatch):
    key = "subagent-empty-key-fixture"
    manager = get_permission_manager()
    if manager.has_config(key):
        manager.remove_config(key)

    yaml_provider = Mock(return_value={"tools": {"bash": "allow"}})
    rail = SubagentPermissionRail(
        agent_scope_id="child",
        permission_key=key,
        config_provider=yaml_provider,
        grant_store=SimpleNamespace(
            get_active=lambda *_a, **_k: None,
            get_active_skill_execution=lambda *_a, **_k: None,
        ),
    )

    cfg = rail._ensure_published_config()

    assert manager.has_config(key)
    assert cfg == {}
    yaml_provider.assert_not_called()
    manager.remove_config(key)
