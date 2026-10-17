# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for task-level auto permission config normalization."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
    is_auto_permission_enabled,
    is_auto_permission_mode,
    is_permission_boundary_enabled,
    normalize_permissions_for_runtime,
    resolve_declared_auto_workspace,
    resolve_permission_runtime_mode,
)
from jiuwenswarm.agents.harness.common.rails.permissions.protected_paths import (
    JIUWENCLAW_PROTECTED_WRITE_PATHS,
)


def test_default_mode_is_manual() -> None:
    assert resolve_permission_runtime_mode({}) == "manual"
    assert is_auto_permission_mode({}) is False


def test_declared_auto_workspace_accepts_project_workspace_and_equivalent_paths(
    tmp_path,
) -> None:
    project = tmp_path / "project"

    assert resolve_declared_auto_workspace({"project_dir": str(project)}) == project
    assert resolve_declared_auto_workspace({"workspace_dir": str(project)}) == project
    assert resolve_declared_auto_workspace(
        {"workspace_dir": str(project / ".")},
        {"project_dir": str(project)},
    ) == project
    assert resolve_declared_auto_workspace({}, {}) is None


def test_declared_auto_workspace_rejects_conflicting_roots(tmp_path) -> None:
    with pytest.raises(ValueError, match="workspace_conflict"):
        resolve_declared_auto_workspace(
            {"workspace_dir": str(tmp_path / "scratch")},
            {"project_dir": str(tmp_path / "project")},
        )


def test_auto_mode_preserves_disabled_boundary_and_defaults_to_ask() -> None:
    raw = {"mode": "auto", "enabled": False, "defaults": {"*": "allow"}}
    normalized = normalize_permissions_for_runtime(raw)
    assert normalized["enabled"] is False
    assert normalized["defaults"]["*"] == "ask"
    assert normalized["auto"]["reviewer_timeout_ms"] == 60000
    assert normalized["auto"]["reviewer_min_confidence"] == 0.7
    assert normalized["auto"]["persistent_audit_enabled"] is False
    assert normalized["auto"]["bounded_write_max_files"] == 3
    assert (
        normalized["auto"]["bounded_write_excluded_paths"]
        == JIUWENCLAW_PROTECTED_WRITE_PATHS
    )
    assert raw["enabled"] is False
    assert raw["defaults"]["*"] == "allow"


@pytest.mark.parametrize(
    ("config", "boundary_enabled", "auto_enabled"),
    [
        ({"enabled": True, "mode": "auto"}, True, True),
        ({"enabled": True, "mode": "manual"}, True, False),
        ({"enabled": False, "mode": "auto"}, False, False),
        ({"enabled": "true", "mode": "auto"}, False, False),
        ({"enabled": 1, "mode": "auto"}, False, False),
        ({"mode": "auto"}, False, False),
        ({"enabled": True, "mode": "unexpected"}, True, False),
    ],
)
def test_permission_activation_uses_exact_enabled_auto_truth_table(
    config: dict[str, object],
    boundary_enabled: bool,
    auto_enabled: bool,
) -> None:
    assert is_permission_boundary_enabled(config) is boundary_enabled
    assert is_auto_permission_enabled(config) is auto_enabled


def test_auto_normalization_does_not_insert_missing_enabled() -> None:
    normalized = normalize_permissions_for_runtime({"mode": "auto"})

    assert "enabled" not in normalized


def test_auto_mode_ignores_attempt_to_disable_fixed_reviewer_route() -> None:
    raw = {
        "mode": "auto",
        "auto": {
            "reviewer_enabled": False,
            "reviewer_scope": "foundation",
            "review_policy_levels": ["deny"],
            "review_default_deny": True,
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert "reviewer_enabled" not in normalized["auto"]
    assert "reviewer_scope" not in normalized["auto"]
    assert "reviewer_observe_only" not in normalized["auto"]
    assert "review_policy_levels" not in normalized["auto"]
    assert "review_default_deny" not in normalized["auto"]
    assert "production_reviewer" not in normalized["auto"]


def test_auto_mode_preserves_explicit_default_deny() -> None:
    raw = {"mode": "auto", "defaults": {"*": "deny"}}

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["defaults"]["*"] == "deny"
    assert raw["defaults"]["*"] == "deny"


def test_invalid_mode_falls_back_to_manual() -> None:
    raw = {"mode": "unexpected", "enabled": False}
    normalized = normalize_permissions_for_runtime(raw)
    assert resolve_permission_runtime_mode(raw) == "manual"
    assert normalized["enabled"] is False


def test_permission_mode_is_not_runtime_mode() -> None:
    raw = {"permission_mode": "auto", "enabled": False}
    assert resolve_permission_runtime_mode(raw) == "manual"
    assert normalize_permissions_for_runtime(raw)["enabled"] is False


def test_auto_mode_keeps_explicit_deny_for_risky_tools() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "bash": "allow",
            "mcp_unknown_tool": "allow",
            "upload_file": "deny",
        },
    }
    normalized = normalize_permissions_for_runtime(raw)
    assert normalized["tools"]["bash"] == "ask"
    assert normalized["tools"]["mcp_unknown_tool"] == "ask"
    assert normalized["tools"]["upload_file"] == "deny"


def test_auto_mode_alias_collision_uses_strictest_level() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "mcp_fetch_webpage": "deny",
            "fetch_webpage": "allow",
            "exec_command": "allow",
            "mcp_exec_command": "ask",
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["tools"]["mcp_fetch_webpage"] == "deny"
    assert normalized["tools"]["mcp_exec_command"] == "ask"


def test_auto_mode_alias_collision_is_order_independent() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "fetch_webpage": "allow",
            "mcp_fetch_webpage": "deny",
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["tools"]["mcp_fetch_webpage"] == "deny"


def test_auto_mode_structured_alias_collision_uses_strictest_subrule() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "mcp_exec_command": {
                "*": "ask",
                "commands": {
                    "npm test": "deny",
                    "pwd": "allow",
                },
            },
            "exec_command": {
                "*": "allow",
                "commands": {
                    "npm test": "allow",
                    "python -m pytest": "ask",
                },
            },
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["tools"]["mcp_exec_command"]["*"] == "ask"
    assert normalized["tools"]["mcp_exec_command"]["commands"] == {
        "npm test": "deny",
        "pwd": "allow",
        "python -m pytest": "ask",
    }


def test_auto_mode_structured_list_alias_collision_uses_strictest_subrule() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "mcp_exec_command": {
                "*": "ask",
                "commands": [
                    {"command": "npm test", "action": "deny"},
                    {"command": "pwd", "permission": "allow"},
                ],
            },
            "exec_command": {
                "*": "allow",
                "commands": [
                    {"command": "npm test", "action": "allow"},
                    {"command": "python -m pytest", "level": "ask"},
                ],
            },
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["tools"]["mcp_exec_command"]["*"] == "ask"
    assert normalized["tools"]["mcp_exec_command"]["commands"] == {
        "npm test": "deny",
        "pwd": "allow",
        "python -m pytest": "ask",
    }


def test_auto_mode_structured_list_alias_collision_is_order_independent() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "exec_command": {
                "*": "allow",
                "commands": [{"command": "npm test", "action": "allow"}],
            },
            "mcp_exec_command": {
                "*": "ask",
                "commands": [{"command": "npm test", "action": "deny"}],
            },
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["tools"]["mcp_exec_command"]["*"] == "ask"
    assert normalized["tools"]["mcp_exec_command"]["commands"] == {
        "npm test": "deny",
    }


def test_auto_mode_preserves_structured_tool_deny_rules() -> None:
    raw = {
        "mode": "auto",
        "tools": {
            "read_file": {
                "*": "allow",
                "paths": {"README.md": "deny"},
                "commands": {"cat README.md": "deny"},
            },
            "bash": {
                "*": "allow",
                "commands": {"pwd": "deny"},
            },
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["tools"]["read_file"]["*"] == "allow"
    assert normalized["tools"]["read_file"]["paths"] == {"README.md": "deny"}
    assert normalized["tools"]["read_file"]["commands"] == {"cat README.md": "deny"}
    assert normalized["tools"]["bash"]["*"] == "ask"
    assert normalized["tools"]["bash"]["commands"] == {"pwd": "deny"}


def test_auto_mode_keeps_only_current_option_allowlist() -> None:
    raw = {
        "mode": "auto",
        "auto": {
            "unknown_future_option": {"nested": True},
            "bounded_write_max_files": "5",
            "bounded_write_excluded_paths": [
                " .git ",
                "",
                "jiuwenswarm/agents/harness/common/rails/permissions",
            ],
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert set(normalized["auto"]) == {
        "bounded_write_excluded_paths",
        "bounded_write_max_files",
        "persistent_audit_enabled",
        "reviewer_min_confidence",
        "reviewer_timeout_ms",
    }
    assert normalized["auto"]["bounded_write_max_files"] == 5
    assert normalized["auto"]["bounded_write_excluded_paths"] == (
        *JIUWENCLAW_PROTECTED_WRITE_PATHS,
        ".git",
    )
    assert raw["auto"]["bounded_write_excluded_paths"][0] == " .git "


def test_auto_mode_invalid_bounded_write_options_fall_back_to_safe_defaults() -> None:
    raw = {
        "mode": "auto",
        "auto": {
            "bounded_write_max_files": 0,
            "bounded_write_excluded_paths": "not-a-list",
        },
    }

    normalized = normalize_permissions_for_runtime(raw)

    assert normalized["auto"]["bounded_write_max_files"] == 3
    assert (
        normalized["auto"]["bounded_write_excluded_paths"]
        == JIUWENCLAW_PROTECTED_WRITE_PATHS
    )
