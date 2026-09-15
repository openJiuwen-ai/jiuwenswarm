# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Permission defaults survive user overrides, startup cleanup and migration."""

from copy import deepcopy

import pytest
import yaml

from openjiuwen.harness.security.tiered_policy import evaluate_tiered_policy

from jiuwenswarm.common import config, utils


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "_current_config_yaml_path", lambda: path)
    return path


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _decision(permissions, tool_name):
    level, _ = evaluate_tiered_policy(
        permissions, tool_name, {"display_name": "permission regression team"}
    )
    return level.value


@pytest.mark.parametrize(
    "tool_name",
    [
        "build_team",
        "clean_team",
        "spawn_teammate",
        "spawn_human_agent",
        "spawn_bridge_agent",
        "spawn_external_cli",
        "shutdown_member",
        "approve_plan",
        "approve_tool",
        "list_members",
        "create_task",
        "update_task",
        "claim_task",
        "submit_plan",
        "verify_task",
        "member_complete_task",
        "view_task",
        "send_message",
        "send_message_scheduled",
        "workspace_meta",
        "swarmflow",
        "async_tasks_list",
        "async_task_output",
        "async_task_cancel",
    ],
)
def test_shipped_team_tools_allow_with_user_default_ask(user_config, tool_name):
    _write_yaml(
        user_config, {"permissions": {"enabled": True, "defaults": {"*": "ask"}}}
    )

    permissions = config.get_merged_config_dict()["permissions"]

    assert _decision(permissions, tool_name) == "allow"
    assert _decision(permissions, "unconfigured_plugin_tool") == "ask"


@pytest.mark.parametrize("level", ["allow", "ask", "deny"])
@pytest.mark.parametrize("tool_name", ["build_team", "custom_plugin_tool"])
def test_user_tool_permission_overrides_shipped_defaults(user_config, tool_name, level):
    _write_yaml(
        user_config,
        {
            "permissions": {
                "enabled": True,
                "defaults": {"*": "ask"},
                "tools": {tool_name: level},
            }
        },
    )

    permissions = config.get_merged_config_dict()["permissions"]

    assert _decision(permissions, tool_name) == level


def test_explicit_shell_rule_still_restricts_allowed_tool(user_config):
    _write_yaml(
        user_config,
        {
            "permissions": {
                "enabled": True,
                "defaults": {"*": "ask"},
                "tools": {"bash": "allow"},
                "rules": [
                    {"tools": ["bash"], "pattern": "echo forbidden", "action": "deny"}
                ],
            }
        },
    )

    permissions = config.get_merged_config_dict()["permissions"]

    level, _ = evaluate_tiered_policy(
        permissions, "bash", {"command": "echo forbidden"}
    )
    assert level.value == "deny"


def test_tool_definitions_are_user_owned_and_other_obsolete_fields_are_removed():
    template = {
        "permissions": {
            "enabled": True,
            "tools": {
                "custom_plugin_tool": {"*": "allow", "patterns": {"trusted*": "allow"}},
                "build_team": "allow",
            },
        },
        "react": {"enabled": True, "tools": {}},
    }
    override = {
        "permissions": {
            "obsolete": True,
            "tools": {
                "custom_plugin_tool": {"*": "deny"},
                "new_plugin_tool": {"*": "ask", "patterns": {"read*": "allow"}},
            },
        },
        "react": {"obsolete": True, "tools": {"old_setting": True}},
        "obsolete": True,
    }
    original_template, original_override = deepcopy(template), deepcopy(override)

    merged = utils.merge_template_with_override(template, override)

    assert merged == {
        "permissions": {
            "enabled": True,
            "tools": {
                "custom_plugin_tool": {"*": "deny"},
                "new_plugin_tool": {"*": "ask", "patterns": {"read*": "allow"}},
                "build_team": "allow",
            },
        },
        "react": {"enabled": True, "tools": {}},
    }
    # Later permission mutation must not change either source configuration.
    merged["permissions"]["tools"]["new_plugin_tool"]["patterns"]["read*"] = "deny"
    assert template == original_template
    assert override == original_override


@pytest.mark.parametrize("operation", ["cleanup", "migrate"])
def test_config_upgrade_preserves_tools_on_disk_and_after_reload(
    tmp_path,
    monkeypatch,
    user_config,
    operation,
):
    template_path = tmp_path / "template.yaml"
    _write_yaml(
        template_path,
        {
            "version": 1,
            "permissions": {
                "enabled": True,
                "defaults": {"*": "ask"},
                "tools": {
                    "build_team": "allow",
                    "spawn_teammate": "allow",
                },
            },
        },
    )
    user_tools = {
        "build_team": "deny",
        "custom_plugin_tool": "allow",
        "custom_sensitive_tool": {"*": "deny", "patterns": {"read*": "ask"}},
    }
    _write_yaml(
        user_config,
        {
            "version": 1,
            "permissions": {"tools": user_tools, "obsolete": True},
            "obsolete": True,
        },
    )
    monkeypatch.setattr(
        config, "resolve_shipped_template_config_path", lambda: template_path
    )
    upgrade = (
        config.cleanup_override_against_template
        if operation == "cleanup"
        else config.migrate_config_from_template
    )

    assert upgrade(template_path, user_config) is True
    saved = yaml.safe_load(user_config.read_text(encoding="utf-8"))
    assert "obsolete" not in saved
    assert "obsolete" not in saved["permissions"]
    for name, definition in user_tools.items():
        assert saved["permissions"]["tools"][name] == definition
    if operation == "cleanup":
        assert saved["permissions"] == {"tools": user_tools}
    assert upgrade(template_path, user_config) is False

    permissions = config.get_merged_config_dict()["permissions"]
    assert _decision(permissions, "build_team") == "deny"
    assert _decision(permissions, "spawn_teammate") == "allow"
    assert _decision(permissions, "custom_plugin_tool") == "allow"
    assert _decision(permissions, "custom_sensitive_tool") == "deny"


def test_permission_reload_keeps_defaults_and_new_user_rules(user_config, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import config_loader

    monkeypatch.setattr(config_loader, "is_enterprise", lambda: False)
    token = config_loader.setup_permissions_agent_base(None)
    try:
        _write_yaml(
            user_config, {"permissions": {"enabled": True, "defaults": {"*": "ask"}}}
        )
        config.clear_config_cache()
        before = config_loader.get_effective_permissions_config(force_reload=True)
        assert _decision(before, "build_team") == "allow"

        _write_yaml(
            user_config,
            {
                "permissions": {
                    "enabled": True,
                    "defaults": {"*": "ask"},
                    "tools": {"build_team": "deny", "custom_plugin_tool": "allow"},
                }
            },
        )
        config.clear_config_cache()
        after = config_loader.get_effective_permissions_config(force_reload=True)
        assert _decision(after, "build_team") == "deny"
        assert _decision(after, "spawn_teammate") == "allow"
        assert _decision(after, "custom_plugin_tool") == "allow"
    finally:
        config_loader.reset_permissions_agent_base(token)
        config_loader.clear_permissions_config_cache()
        config.clear_config_cache()
