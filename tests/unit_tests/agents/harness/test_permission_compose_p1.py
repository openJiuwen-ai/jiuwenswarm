# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P1 compose: Global ⊕ User ⊕ Session, no Agent overlay, Session cannot tighten."""

from __future__ import annotations

from openjiuwen.harness.security import PermissionLevel
from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
    evaluate_tiered_policy,
)

from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
    compose_host_effective_permissions,
)


def test_missing_user_and_session_are_empty() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "tools": {"bash": "ask"},
            "defaults": {"*": "allow"},
        },
        user_permissions=None,
        session_permissions=None,
    )
    assert effective["enabled"] is True
    assert effective["tools"]["bash"] == "ask"


def test_session_drops_deny_and_cannot_override_user_ask() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True, "defaults": {"*": "allow"}},
        user_permissions={"ask_tools": ["bash"]},
        session_permissions={
            "enabled": False,
            "deny_tools": ["python"],
            "ask_tools": ["write_file"],
            "allow_tools": ["bash", "python"],
        },
    )
    assert effective["tools"].get("bash") == "ask"
    assert "python" not in effective.get("deny_tools", [])
    assert effective.get("enabled") is True


def test_session_allow_relaxes_unlisted_tool() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "defaults": {"*": "ask"},
        },
        user_permissions={},
        session_permissions={"allow_tools": ["bash"]},
    )
    assert effective["tools"]["bash"] == "allow"


def test_user_severity_only_rule_gets_product_action() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
            "rules": [
                {
                    "id": "user_rm_severity_only",
                    "tools": ["bash"],
                    "match_type": "command",
                    "pattern": "re:(?i)compose-p1-severity-marker",
                    "severity": "HIGH",
                }
            ],
        }
    )
    user = next(r for r in effective["rules"] if r.get("id") == "user_rm_severity_only")
    assert user["action"] == "ask"
    level, matched = evaluate_tiered_policy(
        effective, "bash", {"command": "echo compose-p1-severity-marker"},
    )
    assert level == PermissionLevel.ASK
    assert "user_rm_severity_only" in matched


def test_compose_drops_session_net_guard() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True, "defaults": {"*": "allow"}},
        session_permissions={
            "net_guard": {
                "enabled": False,
                "defaults": "deny",
                "urls": {"example.com": "deny"},
            }
        },
    )
    ng = effective["net_guard"]
    assert ng["enabled"] is True
    assert ng["defaults"] == "allow"
    assert "example.com" not in ng["urls"]


def test_user_cannot_disable_net_guard_or_widen_deny() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "defaults": {"*": "allow"},
            "net_guard": {
                "enabled": True,
                "defaults": "deny",
                "urls": {"evil.example": "deny"},
            },
        },
        user_permissions={
            "net_guard": {
                "enabled": False,
                "defaults": "allow",
                "urls": {"evil.example": "allow", "corp.example": "deny"},
            }
        },
    )
    ng = effective["net_guard"]
    assert ng["enabled"] is True
    assert ng["defaults"] == "deny"
    assert ng["urls"]["evil.example"] == "deny"
    assert ng["urls"]["corp.example"] == "deny"


def test_compose_defaults_shell_guard_both_on() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True, "defaults": {"*": "allow"}},
    )
    sg = effective["shell_guard"]
    assert sg["unknown_structure"] is True
    assert sg["interpreter_sink"] is True


def test_compose_drops_session_shell_guard() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True, "defaults": {"*": "allow"}},
        session_permissions={
            "shell_guard": {"unknown_structure": False, "interpreter_sink": False},
        },
    )
    sg = effective["shell_guard"]
    assert sg["unknown_structure"] is True
    assert sg["interpreter_sink"] is True


def test_user_can_disable_shell_guard_flags() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "defaults": {"*": "allow"},
            "shell_guard": {"unknown_structure": True, "interpreter_sink": True},
        },
        user_permissions={
            "shell_guard": {"unknown_structure": False, "interpreter_sink": False},
        },
    )
    sg = effective["shell_guard"]
    assert sg["unknown_structure"] is False
    assert sg["interpreter_sink"] is False

