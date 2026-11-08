# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P1 compose: Global ⊕ User ⊕ Session, no Agent overlay.

Session cannot write deny/ask. User/Session allow may relax a higher-layer
whole-tool ask; deny still wins.
"""

from __future__ import annotations

from pathlib import Path

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


def test_session_drops_deny_and_ask_and_enabled() -> None:
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
    assert effective["tools"].get("bash") == "allow"
    assert "python" not in effective.get("deny_tools", [])
    assert "write_file" not in effective.get("ask_tools", [])
    assert effective.get("enabled") is True


def test_session_allow_relaxes_user_ask() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True, "defaults": {"*": "allow"}},
        user_permissions={"ask_tools": ["bash"]},
        session_permissions={"allow_tools": ["bash"]},
    )
    assert effective["tools"]["bash"] == "allow"


def test_session_allow_relaxes_global_ask() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "tools": {"mcp_fetch_webpage": "ask"},
            "defaults": {"*": "allow"},
        },
        user_permissions={},
        session_permissions={"allow_tools": ["mcp_fetch_webpage"]},
    )
    assert effective["tools"]["mcp_fetch_webpage"] == "allow"


def test_user_allow_relaxes_global_ask() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "tools": {"mcp_fetch_webpage": "ask"},
            "defaults": {"*": "allow"},
        },
        user_permissions={"allow_tools": ["mcp_fetch_webpage"]},
        session_permissions={},
    )
    assert effective["tools"]["mcp_fetch_webpage"] == "allow"


def test_session_allow_cannot_relax_user_deny() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True, "defaults": {"*": "allow"}},
        user_permissions={"deny_tools": ["bash"]},
        session_permissions={"allow_tools": ["bash"]},
    )
    assert effective["tools"]["bash"] == "deny"


def test_user_allow_cannot_relax_global_deny() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "tools": {"bash": "deny"},
            "defaults": {"*": "allow"},
        },
        user_permissions={"allow_tools": ["bash"]},
        session_permissions={"allow_tools": ["bash"]},
    )
    assert effective["tools"]["bash"] == "deny"


def test_user_ask_still_tightens_global_allow() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
        },
        user_permissions={"ask_tools": ["bash"]},
    )
    assert effective["tools"]["bash"] == "ask"


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


def test_strict_mode_maps_medium_rules_to_ask() -> None:
    """Issue #45: strict + bash allow + MEDIUM pwd/ls rules → ask card."""
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "permission_mode": "strict",
            "package_builtin_rules": False,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
            "rules": [
                {
                    "id": "pwd_medium",
                    "tools": ["bash"],
                    "pattern": "pwd *",
                    "severity": "MEDIUM",
                },
                {
                    "id": "ls_medium",
                    "tools": ["bash"],
                    "pattern": "ls *",
                    "severity": "MEDIUM",
                },
            ],
        }
    )
    by_id = {r["id"]: r for r in effective["rules"]}
    assert by_id["pwd_medium"]["action"] == "ask"
    assert by_id["ls_medium"]["action"] == "ask"
    for command, rule_id in (("pwd", "pwd_medium"), ("ls", "ls_medium")):
        level, matched = evaluate_tiered_policy(
            effective, "bash", {"command": command},
        )
        assert level == PermissionLevel.ASK
        assert rule_id in matched


def test_normal_mode_maps_medium_rules_to_allow() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "permission_mode": "normal",
            "package_builtin_rules": False,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
            "rules": [
                {
                    "id": "pwd_medium",
                    "tools": ["bash"],
                    "pattern": "pwd *",
                    "severity": "MEDIUM",
                },
            ],
        }
    )
    assert effective["rules"][0]["action"] == "allow"
    level, matched = evaluate_tiered_policy(
        effective, "bash", {"command": "pwd"},
    )
    assert level == PermissionLevel.ALLOW
    assert "pwd_medium" in matched


def test_explicit_action_ignores_permission_mode() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "permission_mode": "strict",
            "package_builtin_rules": False,
            "tools": {"bash": "allow"},
            "rules": [
                {
                    "id": "explicit_allow",
                    "tools": ["bash"],
                    "pattern": "pwd *",
                    "severity": "MEDIUM",
                    "action": "allow",
                },
            ],
        }
    )
    assert effective["rules"][0]["action"] == "allow"
    level, _ = evaluate_tiered_policy(effective, "bash", {"command": "pwd"})
    assert level == PermissionLevel.ALLOW


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


def _rules_for(effective: dict, path: str) -> list[dict]:
    """Package builtin globs are appended too; keep only the rules under test."""
    fg = effective.get("file_guard") or {}
    return [item for item in fg.get("paths") or [] if item.get("path") == path]


def test_file_guard_same_path_merges_axes_across_layers() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True},
        user_permissions={
            "file_guard": {"paths": [{"path": "/data", "match": "prefix", "read": "allow", "write": "ask"}]}
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/data", "match": "prefix", "write": "allow"}]}
        },
    )
    rules = _rules_for(effective, "/data")
    # The Engine keeps the first entry per equal prefix, so one entry per path
    # is what makes the session grant reachable at all.
    assert len(rules) == 1
    assert rules[0]["read"] == "allow"
    assert rules[0]["write"] == "allow"
    # No layer spoke about exec; the Engine's own default stays in charge.
    assert "exec" not in rules[0]


def test_file_guard_user_ask_does_not_tighten_global_allow() -> None:
    # The approval flow writes ask for the actions it did not grant, so a User
    # read grant must not turn a Global write=allow back into a prompt. exec is
    # a different axis: no layer allows it here, so the ask stays and the
    # Engine keeps prompting for it.
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {
                "paths": [{"path": "/data", "match": "prefix", "read": "allow", "write": "allow"}]
            },
        },
        user_permissions={
            "file_guard": {
                "paths": [
                    {"path": "/data", "match": "prefix", "read": "allow", "write": "ask", "exec": "ask"}
                ]
            }
        },
    )
    rules = _rules_for(effective, "/data")
    assert len(rules) == 1
    assert rules[0]["write"] == "allow"
    assert rules[0]["read"] == "allow"
    assert rules[0]["exec"] == "ask"


def test_file_guard_user_ask_survives_a_partial_global_rule() -> None:
    # compose seeds a scope from the first layer entry that mentions it, so a
    # Global rule which omits an axis is the only place a dropped User ask goes
    # missing. legacy compiles that absence as allow, so the ask must survive.
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/data", "match": "prefix", "read": "allow"}]},
        },
        user_permissions={
            "file_guard": {
                "paths": [{"path": "/data", "match": "prefix", "write": "ask", "exec": "ask"}]
            }
        },
    )
    rules = _rules_for(effective, "/data")
    assert len(rules) == 1
    assert rules[0]["read"] == "allow"
    assert rules[0]["write"] == "ask"
    assert rules[0]["exec"] == "ask"


def test_file_guard_user_ask_below_session_allow_stays_allow() -> None:
    # The session grant is what !9724 exists for; the User fill ask must not
    # shadow it.
    effective = compose_host_effective_permissions(
        global_permissions={"enabled": True},
        user_permissions={
            "file_guard": {"paths": [{"path": "/data", "match": "prefix", "write": "ask", "exec": "ask"}]}
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/data", "match": "prefix", "write": "allow"}]}
        },
    )
    rules = _rules_for(effective, "/data")
    assert len(rules) == 1
    assert rules[0]["write"] == "allow"
    assert rules[0]["exec"] == "ask"


def test_file_guard_legacy_compile_keeps_the_user_ask(tmp_path: Path) -> None:
    from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
        build_file_guard_checker,
    )

    target = (tmp_path / "shared").resolve()
    target.mkdir()
    # external_directory keeps the Engine on legacy semantics; an explicit
    # file_guard.enabled would let compose inject the package glob paths and
    # flip the config back to native.
    global_permissions = {
        "enabled": True,
        "external_directory": {"*": "allow"},
        "file_guard": {"paths": [{"path": target.as_posix(), "match": "prefix", "read": "allow"}]},
    }
    user_permissions = {
        "file_guard": {"paths": [{"path": target.as_posix(), "match": "prefix", "write": "ask"}]}
    }
    effective = compose_host_effective_permissions(
        global_permissions=global_permissions,
        user_permissions=user_permissions,
    )
    checker = build_file_guard_checker(effective, workspace_root=tmp_path)
    assert checker is not None
    # A legacy path rule compiles an absent axis as read/write allow, so a
    # dropped User ask would let the write through without a prompt.
    assert checker._resolve_one(target / "out.txt", "write")[0].value == "ask"
    assert checker._resolve_one(target / "out.txt", "read")[0].value == "allow"


def test_file_guard_native_compile_keeps_the_user_ask(tmp_path: Path) -> None:
    from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
        build_file_guard_checker,
    )

    target = (tmp_path / "shared").resolve()
    target.mkdir()
    # Native semantics need an explicit defaults dict; file_guard.enabled keeps
    # the checker from coming back as None.
    global_permissions = {
        "enabled": True,
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [{"path": target.as_posix(), "match": "prefix", "read": "allow"}],
        },
    }
    user_permissions = {
        "file_guard": {"paths": [{"path": target.as_posix(), "match": "prefix", "write": "ask"}]}
    }
    effective = compose_host_effective_permissions(
        global_permissions=global_permissions,
        user_permissions=user_permissions,
    )
    checker = build_file_guard_checker(effective, workspace_root=tmp_path)
    assert checker is not None
    assert checker._resolve_one(target / "out.txt", "write")[0].value == "ask"


def test_file_guard_keeps_unopinionated_axes_absent() -> None:
    # legacy mode compiles an absent axis as allow, so materializing "ask"
    # would add prompts to a rule that never had any.
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/data", "match": "prefix", "read": "allow"}]},
        },
    )
    rules = _rules_for(effective, "/data")
    assert rules == [{"path": "/data", "match": "prefix", "read": "allow"}]


def test_file_guard_deny_in_durable_layers_survives_session_allow() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/secret", "match": "prefix", "read": "deny"}]},
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/secret", "match": "prefix", "read": "allow"}]}
        },
    )
    rules = _rules_for(effective, "/secret")
    assert len(rules) == 1
    assert rules[0]["read"] == "deny"


def test_file_guard_distinct_paths_keep_first_seen_order() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/a", "match": "prefix", "read": "allow"}]},
        },
        user_permissions={
            "file_guard": {"paths": [{"path": "/b", "match": "prefix", "read": "allow"}]}
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/c", "match": "prefix", "read": "allow"}]}
        },
    )
    prefixes = [
        item["path"]
        for item in effective["file_guard"]["paths"]
        if item.get("match") != "glob"
    ]
    assert prefixes == ["/a", "/b", "/c"]


def test_file_guard_prefix_and_glob_entries_stay_separate() -> None:
    effective = compose_host_effective_permissions(
        global_permissions={
            "enabled": True,
            "file_guard": {"paths": [{"path": "/x", "match": "prefix", "read": "allow"}]},
        },
        session_permissions={
            "file_guard": {"paths": [{"path": "/x", "match": "glob", "read": "deny"}]}
        },
    )
    rules = _rules_for(effective, "/x")
    assert {(item["match"], item["read"]) for item in rules} == {
        ("prefix", "allow"),
        ("glob", "deny"),
    }
