# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Preserved user tool definitions remain compatible with member narrowing."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.rails.team_context import inject_team_handles
from openjiuwen.harness.security.tiered_policy import evaluate_tiered_policy

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.member_rails import _build_team_permission_rail
from jiuwenswarm.common.utils import merge_template_with_override


@pytest.mark.parametrize(
    "definition,default_level,member_level,expected",
    [
        ({"*": "deny", "patterns": {"read*": "ask"}}, "ask", "deny", "deny"),
        ({"*": "deny"}, "ask", "allow", "deny"),
        ({"*": "ask"}, "ask", "allow", "ask"),
        ({"*": "allow"}, "ask", "ask", "ask"),
        ({"*": "allow"}, "ask", "deny", "deny"),
        ({"patterns": {"read*": "allow"}}, "ask", "allow", "ask"),
        ("deny", "ask", "allow", "deny"),
        ({"*": "guard"}, "deny", "allow", "deny"),
        ({"*": {"*": "allow"}}, "deny", "allow", "deny"),
    ],
)
def test_member_narrowing_accepts_preserved_user_tool_definitions(
    definition,
    default_level,
    member_level,
    expected,
):
    merged = merge_template_with_override(
        {
            "permissions": {
                "enabled": True,
                "defaults": {"*": default_level},
                "tools": {},
            }
        },
        {"permissions": {"tools": {"custom_plugin_tool": definition}}},
    )
    permissions = merged["permissions"]
    original = deepcopy(permissions)
    context = SwarmBuildContext()
    inject_team_handles(
        context.extras,
        team_backend=SimpleNamespace(
            team_name="regression",
            member_name="worker",
            leader_member_name="leader",
            db=object(),
        ),
        messager=object(),
        permissions_override={"custom_plugin_tool": member_level},
    )

    rail = _build_team_permission_rail({"permissions_config": permissions}, context)

    assert rail is not None
    level, _ = evaluate_tiered_policy(rail._static_config, "custom_plugin_tool", {})
    assert level.value == expected
    if isinstance(definition, dict):
        assert rail._static_config["tools"]["custom_plugin_tool"] == {
            **definition,
            "*": expected,
        }
    assert permissions == original
