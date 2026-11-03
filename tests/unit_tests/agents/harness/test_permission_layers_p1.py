# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers


def test_persist_user_and_session_overlays(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(
        layers,
        "session_permissions_path",
        lambda session_id: tmp_path / session_id / "session_permissions.yaml",
    )
    monkeypatch.setattr(layers, "load_global_permissions", lambda: {})

    effective = {
        "allow_tools": ["bash"],
        "ask_tools": ["write_file"],
        "approval_overrides": [{"id": "x", "action": "allow"}],
        "file_guard": {
            "paths": [
                {"path": "**/.ssh/**", "layer": "builtin", "read": "deny"},
                {"path": "/tmp/extra", "read": "allow", "write": "allow", "exec": "ask"},
            ]
        },
        "net_guard": {
            "enabled": True,
            "defaults": "allow",
            "urls": {"localhost": "deny"},
        },
    }
    assert layers.persist_session_overlay_from_effective("s1", effective) is True
    session = layers.load_session_permissions("s1")
    assert session["allow_tools"] == ["bash"]
    assert "ask_tools" not in session
    assert "deny_tools" not in session

    assert layers.persist_user_overlay_from_effective(effective) is True
    user = layers.load_user_permissions()
    assert user["allow_tools"] == ["bash"]
    assert user["ask_tools"] == ["write_file"]
    assert user["file_guard"]["paths"] == [
        {"path": "/tmp/extra", "read": "allow", "write": "allow", "exec": "ask"},
    ]
    assert "net_guard" not in user
    assert "net_guard" not in session


def test_user_persist_keeps_only_delta_against_global(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(
        layers,
        "load_global_permissions",
        lambda: {
            "tools": {"write_file": "allow", "bash": "ask"},
            "file_guard": {
                "paths": [
                    {
                        "path": "**/.env*",
                        "match": "glob",
                        "read": "ask",
                        "write": "ask",
                        "exec": "ask",
                    }
                ]
            },
        },
    )
    effective = {
        "allow_tools": ["write_file", "todo_list"],
        "ask_tools": ["bash"],
        "approval_overrides": [
            {"id": "user_allow_foo", "tools": ["bash"], "action": "allow", "pattern": "foo"},
        ],
        "file_guard": {
            "paths": [
                {
                    "path": "**/.env*",
                    "match": "glob",
                    "read": "ask",
                    "write": "ask",
                    "exec": "ask",
                },
                {
                    "path": "C:/Users/hanzhibin/test1.txt",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                },
            ]
        },
    }
    assert layers.persist_user_overlay_from_effective(effective) is True
    user = layers.load_user_permissions()
    assert user.get("allow_tools") == ["todo_list"]
    assert "bash" not in (user.get("ask_tools") or [])
    paths = (user.get("file_guard") or {}).get("paths") or []
    assert [p.get("path") for p in paths] == ["C:/Users/hanzhibin/test1.txt"]
    assert [o.get("id") for o in user.get("approval_overrides") or []] == ["user_allow_foo"]


def test_session_persist_keeps_only_delta_against_global_and_user(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        layers,
        "session_permissions_path",
        lambda session_id: tmp_path / session_id / "session_permissions.yaml",
    )
    monkeypatch.setattr(
        layers,
        "load_global_permissions",
        lambda: {
            "tools": {"write_file": "allow", "read_file": "allow"},
            "file_guard": {
                "paths": [
                    {"path": "**/.env*", "match": "glob", "read": "ask", "write": "ask", "exec": "ask"}
                ]
            },
        },
    )
    monkeypatch.setattr(
        layers,
        "load_user_permissions",
        lambda: {
            "allow_tools": ["todo_list"],
            "approval_overrides": [{"id": "user_allow_foo", "action": "allow"}],
            "file_guard": {
                "paths": [
                    {
                        "path": "C:/Users/hanzhibin/test1.txt",
                        "read": "allow",
                        "write": "allow",
                        "exec": "ask",
                        "match": "prefix",
                    }
                ]
            },
        },
    )
    effective = {
        "allow_tools": ["write_file", "read_file", "todo_list", "python"],
        "approval_overrides": [
            {"id": "user_allow_foo", "action": "allow"},
            {"id": "session_allow_bar", "action": "allow"},
        ],
        "file_guard": {
            "paths": [
                {"path": "**/.env*", "match": "glob", "read": "ask", "write": "ask", "exec": "ask"},
                {
                    "path": "C:/Users/hanzhibin/test1.txt",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                },
                {
                    "path": "C:/Users/hanzhibin/.jiuwenswarm/agent/workspace/test2.txt",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                },
            ]
        },
    }
    assert layers.persist_session_overlay_from_effective("s1", effective) is True
    session = layers.load_session_permissions("s1")
    assert session.get("allow_tools") == ["python"]
    assert [o.get("id") for o in session.get("approval_overrides") or []] == ["session_allow_bar"]
    paths = (session.get("file_guard") or {}).get("paths") or []
    assert [p.get("path") for p in paths] == [
        "C:/Users/hanzhibin/.jiuwenswarm/agent/workspace/test2.txt"
    ]


def test_session_persist_extracts_allow_from_tools_projection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        layers,
        "session_permissions_path",
        lambda session_id: tmp_path / session_id / "session_permissions.yaml",
    )
    monkeypatch.setattr(
        layers,
        "load_global_permissions",
        lambda: {"tools": {"mcp_fetch_webpage": "ask", "write_file": "allow"}},
    )
    monkeypatch.setattr(layers, "load_user_permissions", lambda: {})
    effective = {
        "tools": {"mcp_fetch_webpage": "allow", "write_file": "allow"},
        "ask_tools": ["mcp_fetch_webpage"],
        "allow_tools": ["write_file"],
    }
    assert layers.persist_session_overlay_from_effective("s1", effective) is True
    session = layers.load_session_permissions("s1")
    assert session.get("allow_tools") == ["mcp_fetch_webpage"]
    assert "ask_tools" not in session


def test_user_persist_extracts_allow_from_tools_projection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(
        layers,
        "load_global_permissions",
        lambda: {"tools": {"mcp_fetch_webpage": "ask", "write_file": "allow"}},
    )
    effective = {
        "tools": {"mcp_fetch_webpage": "allow", "write_file": "allow"},
        "ask_tools": ["mcp_fetch_webpage"],
        "allow_tools": ["write_file"],
    }
    assert layers.persist_user_overlay_from_effective(effective) is True
    user = layers.load_user_permissions()
    assert user.get("allow_tools") == ["mcp_fetch_webpage"]
    assert "mcp_fetch_webpage" not in (user.get("ask_tools") or [])


def test_load_user_unwraps_permissions_wrapper(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "user_permissions.yaml"
    path.write_text("permissions:\n  allow_tools:\n    - todo_list\n", encoding="utf-8")
    monkeypatch.setattr(layers, "user_permissions_path", lambda: path)
    assert layers.load_user_permissions()["allow_tools"] == ["todo_list"]


def test_user_persist_does_not_copy_session_only_delta(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(
        layers,
        "session_permissions_path",
        lambda session_id: tmp_path / session_id / "session_permissions.yaml",
    )
    monkeypatch.setattr(
        layers,
        "load_global_permissions",
        lambda: {"tools": {"write_file": "allow"}},
    )
    session_path = tmp_path / "s1" / "session_permissions.yaml"
    session_path.parent.mkdir(parents=True)
    session_path.write_text("allow_tools:\n  - python\n", encoding="utf-8")
    effective = {"allow_tools": ["write_file", "python", "todo_list"]}
    assert layers.persist_user_overlay_from_effective(effective, session_id="s1") is True
    user = layers.load_user_permissions()
    assert user.get("allow_tools") == ["todo_list"]


def test_session_overlay_keeps_rule_when_id_collides_with_user_layer() -> None:
    user = {
        "approval_overrides": [
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "echo 你好",
                "action": "allow",
            }
        ]
    }
    effective = {
        "approval_overrides": [
            *user["approval_overrides"],
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "echo 解决",
                "action": "allow",
            },
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "echo 是我",
                "action": "allow",
            },
        ]
    }
    overlay = layers.overlay_from_effective(
        effective, session=True, global_permissions={}, user_permissions=user
    )
    overrides = overlay.get("approval_overrides") or []
    assert [item["pattern"] for item in overrides] == ["echo 解决", "echo 是我"]
    ids = [item["id"] for item in overrides]
    assert len(set(ids)) == 2
    assert all(override_id != "user_allow_bash_command_echo" for override_id in ids)


def test_session_overlay_tolerates_scalar_tools_in_yaml() -> None:
    user = {
        "approval_overrides": [
            {
                "id": "user_allow_bash_command_echo",
                "tools": 1,
                "match_type": "command",
                "pattern": "echo 你好",
                "action": "allow",
            },
            {
                "id": "user_allow_bash_command_echo_2",
                "tools": True,
                "match_type": "command",
                "pattern": "echo 轻松",
                "action": "allow",
            },
        ]
    }
    effective = {
        "approval_overrides": [
            *user["approval_overrides"],
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "echo 解决",
                "action": "allow",
            },
        ]
    }
    overlay = layers.overlay_from_effective(
        effective, session=True, global_permissions={}, user_permissions=user
    )
    overrides = overlay.get("approval_overrides") or []
    assert [item["pattern"] for item in overrides] == ["echo 解决"]


def test_colliding_ids_are_rewritten_per_full_signature() -> None:
    effective = {
        "approval_overrides": [
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "echo 解决",
                "action": "allow",
            },
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash", "shell"],
                "match_type": "command",
                "pattern": "echo 解决",
                "action": "allow",
            },
            {
                "id": "user_allow_bash_command_echo",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "echo 解决",
                "action": "deny",
            },
        ]
    }
    overlay = layers.overlay_from_effective(
        effective, session=True, global_permissions={}, user_permissions={}
    )
    ids = [item["id"] for item in overlay.get("approval_overrides") or []]
    assert len(set(ids)) == 3
    assert all(override_id != "user_allow_bash_command_echo" for override_id in ids)


def test_colliding_ids_without_pattern_are_renamed() -> None:
    user = {
        "approval_overrides": [
            {
                "id": "user_allow_foo",
                "tools": ["bash"],
                "action": "allow",
            }
        ]
    }
    effective = {
        "approval_overrides": [
            *user["approval_overrides"],
            {
                "id": "user_allow_foo",
                "tools": ["shell"],
                "action": "allow",
            },
            {
                "id": "user_allow_bar",
                "action": "allow",
            },
            {
                "id": "user_allow_bar",
                "action": "allow",
            },
        ]
    }
    overlay = layers.overlay_from_effective(
        effective, session=True, global_permissions={}, user_permissions=user
    )
    overrides = overlay.get("approval_overrides") or []
    ids = [item["id"] for item in overrides]
    assert len(set(ids)) == len(overrides)
    assert "user_allow_foo" not in ids
    assert "user_allow_bar" not in ids
