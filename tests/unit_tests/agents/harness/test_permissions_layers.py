# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side permission layers / mode compose tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "jiuwenswarm"
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True)
    (root / "agent" / "sessions").mkdir(parents=True)
    cfg_file = cfg_dir / "config.yaml"
    cfg_file.write_text("permissions:\n  enabled: true\n  mode: auto\n", encoding="utf-8")

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(root))

    import jiuwenswarm.common.utils as utils
    import jiuwenswarm.common.config as cfg_mod

    monkeypatch.setattr(utils, "_workspace_base_dir", root, raising=False)
    monkeypatch.setattr(utils, "_config_dir", cfg_dir, raising=False)
    monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", cfg_file)
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", cfg_file)

    # dev-stable 的 get_config() 会做模板+用户 override 合并（模板注入
    # mode/schema/rules），覆盖为直读 tmp 用户 config，保证用例只见
    # _write_global 写入的内容。
    def _read_tmp_config() -> dict:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    monkeypatch.setattr(cfg_mod, "get_config", _read_tmp_config)
    return root


def _write_global(root: Path, permissions: dict) -> None:
    cfg = root / "config" / "config.yaml"
    cfg.write_text(yaml.safe_dump({"permissions": permissions}), encoding="utf-8")


def test_compose_full_access_from_legacy_enabled_false(isolated_data_dir: Path) -> None:
    _write_global(isolated_data_dir, {"enabled": False, "tools": {"bash": "ask"}})
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
    )

    eff = compose_host_effective_permissions()
    assert eff["enabled"] is True
    assert eff["mode"] == "full_access"
    assert eff["sandbox_intent"] == "optional"
    assert eff["file_guard"]["enabled"] is False
    assert (eff.get("tools") or {}).get("bash") == "ask"


def test_compose_default_auto(isolated_data_dir: Path) -> None:
    _write_global(isolated_data_dir, {"enabled": True})
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        get_permissions_mode,
    )

    assert get_permissions_mode() == "auto"
    eff = compose_host_effective_permissions()
    assert eff["mode"] == "auto"
    assert eff["sandbox_intent"] == "required"
    assert eff["defaults"]["*"] == "allow"


def test_migrate_global_tool_levels_to_user(isolated_data_dir: Path) -> None:
    _write_global(
        isolated_data_dir,
        {
            "enabled": True,
            "tools": {"todo_list": "allow", "bash": "ask"},
        },
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_user_permissions_path,
        migrate_and_write_global_permissions,
    )

    global_permissions = migrate_and_write_global_permissions()
    user_permissions = yaml.safe_load(
        get_user_permissions_path().read_text(encoding="utf-8"),
    )["permissions"]

    assert user_permissions["allow_tools"] == ["todo_list"]
    assert user_permissions["ask_tools"] == ["bash"]
    assert not {
        "allow_tools",
        "ask_tools",
        "deny_tools",
        "tools",
    }.intersection(global_permissions)


def test_update_mode_and_user_persist(isolated_data_dir: Path) -> None:
    _write_global(isolated_data_dir, {"enabled": True, "mode": "auto"})
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_user_permissions_path,
        persist_user_overlay_from_effective,
        update_permissions_mode,
    )

    assert update_permissions_mode("strict") == "strict"
    ok = persist_user_overlay_from_effective(
        {
            "approval_overrides": [
                {
                    "id": "always_git_status",
                    "tools": ["bash"],
                    "match_type": "command",
                    "pattern": "git status*",
                    "action": "allow",
                }
            ]
        }
    )
    assert ok is True
    raw = yaml.safe_load(get_user_permissions_path().read_text(encoding="utf-8"))
    assert raw["permissions"]["approval_overrides"][0]["id"] == "always_git_status"


def test_session_persist(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_session_permissions_path,
        persist_session_overlay_from_effective,
    )

    sid = "sess-1"
    ok = persist_session_overlay_from_effective(
        {
            "approval_overrides": [
                {
                    "id": "session_pytest",
                    "tools": ["bash"],
                    "match_type": "command",
                    "pattern": "pytest*",
                    "action": "allow",
                }
            ]
        },
        session_id=sid,
    )
    assert ok is True
    path = get_session_permissions_path(sid)
    assert path.is_file()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["permissions"]["approval_overrides"][0]["pattern"] == "pytest*"


def test_session_persist_uses_meta_session_id_and_file_guard_delta(
    isolated_data_dir: Path,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_session_permissions_path,
        load_session_permissions,
        persist_session_overlay_from_effective,
    )

    sid = "sess_fg_delta_1"
    ok = persist_session_overlay_from_effective(
        {
            "_persist_session_id": sid,
            "_file_guard_paths_added": [
                {
                    "path": "C:/Users/hanzhibin/test3.txt",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                }
            ],
            # full effective paths must NOT be written when delta meta is present
            "file_guard": {
                "paths": [
                    {
                        "path": "**/.ssh/**",
                        "match": "glob",
                        "read": "deny",
                        "write": "deny",
                        "exec": "deny",
                    },
                    {
                        "path": "C:/Users/hanzhibin/test3.txt",
                        "read": "allow",
                        "write": "allow",
                        "exec": "ask",
                        "match": "prefix",
                    },
                ]
            },
        },
        session_id=None,
    )
    assert ok is True
    path = get_session_permissions_path(sid)
    assert path.is_file()
    sess = load_session_permissions(sid)
    paths = (sess.get("file_guard") or {}).get("paths") or []
    assert len(paths) == 1
    assert paths[0]["path"] == "C:/Users/hanzhibin/test3.txt"
    assert paths[0]["write"] == "allow"


def test_session_persist_merges_file_guard_delta(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        load_session_permissions,
        persist_session_overlay_from_effective,
    )

    sid = "sess_fg_delta_2"
    assert persist_session_overlay_from_effective(
        {
            "_file_guard_paths_added": [
                {
                    "path": "C:/tmp/a.txt",
                    "read": "allow",
                    "write": "ask",
                    "exec": "ask",
                    "match": "prefix",
                }
            ]
        },
        session_id=sid,
    )
    assert persist_session_overlay_from_effective(
        {
            "_file_guard_paths_added": [
                {
                    "path": "C:/tmp/a.txt",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                }
            ]
        },
        session_id=sid,
    )
    paths = (load_session_permissions(sid).get("file_guard") or {}).get("paths") or []
    assert len(paths) == 1
    assert paths[0]["write"] == "allow"
    # compose 读回：写盘增量必须在合成输出中存活（snapshot→update_config 往返）
    eff = compose_host_effective_permissions(session_id=sid)
    eff_paths = (eff.get("file_guard") or {}).get("paths") or []
    assert any(
        p.get("path") == "C:/tmp/a.txt" and p.get("write") == "allow" for p in eff_paths
    )


def test_compose_round_trips_rules_overrides_and_paths(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        get_user_permissions_path,
        persist_user_overlay_from_effective,
    )

    _write_global(
        isolated_data_dir,
        {
            "enabled": True,
            "mode": "auto",
            "rules": [
                {
                    "id": "shell_sensitive_read",
                    "tools": ["bash"],
                    "pattern": "grep *",
                    "severity": "HIGH",
                },
            ],
        },
    )
    user_path = get_user_permissions_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        yaml.safe_dump(
            {
                "permissions": {
                    "approval_overrides": [
                        {
                            "id": "always_git_status",
                            "tools": ["bash"],
                            "match_type": "command",
                            "pattern": "git status*",
                            "action": "allow",
                        },
                    ],
                    "rules": [
                        {
                            "id": "user_deny_wipe",
                            "tools": ["bash"],
                            "pattern": "rm -rf *",
                            "severity": "CRITICAL",
                        },
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    # HITL 永久允许写盘：pattern 类 + path 类增量
    assert persist_user_overlay_from_effective(
        {
            "approval_overrides": [
                {
                    "id": "always_git_status",
                    "tools": ["bash"],
                    "match_type": "command",
                    "pattern": "git status*",
                    "action": "allow",
                },
                {
                    "id": "always_pytest",
                    "tools": ["bash"],
                    "match_type": "command",
                    "pattern": "pytest*",
                    "action": "allow",
                },
            ],
            "_file_guard_paths_added": [
                {
                    "path": "C:/tmp/b.txt",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                },
            ],
        }
    )
    eff = compose_host_effective_permissions(session_id="sess_roundtrip_1")
    rule_ids = [rule.get("id") for rule in eff.get("rules") or []]
    assert "shell_sensitive_read" in rule_ids
    assert "user_deny_wipe" in rule_ids
    override_ids = [entry.get("id") for entry in eff.get("approval_overrides") or []]
    assert "always_git_status" in override_ids
    assert "always_pytest" in override_ids
    eff_paths = (eff.get("file_guard") or {}).get("paths") or []
    assert any(
        p.get("path") == "C:/tmp/b.txt" and p.get("write") == "allow" for p in eff_paths
    )
    # preset 底线（敏感路径）仍在
    assert any(p.get("path") == "**/.ssh/**" for p in eff_paths)


def test_append_session_allow_tool_persists_and_reloads(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        append_allow_tool,
        compose_host_effective_permissions,
        get_session_permissions_path,
        load_session_permissions,
        update_permissions_mode,
    )

    update_permissions_mode("strict")
    sid = "sess_allow_tools_1"
    session_path = get_session_permissions_path(sid)
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        yaml.safe_dump(
            {
                "permissions": {
                    "ask_tools": ["todo_list"],
                    "deny_tools": ["legacy_tool"],
                    "tools": {"legacy_tool": "deny"},
                }
            }
        ),
        encoding="utf-8",
    )
    assert append_allow_tool("todo_list", scope="session", session_id=sid) is True
    sess = load_session_permissions(sid)
    assert sess.get("allow_tools") == ["todo_list"]
    assert "ask_tools" not in sess
    assert "deny_tools" not in sess
    assert "tools" not in sess

    eff = compose_host_effective_permissions(session_id=sid)
    assert (eff.get("tools") or {}).get("todo_list") == "allow"


def test_append_user_allow_tool_does_not_write_session(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        append_allow_tool,
        get_session_permissions_path,
        get_user_permissions_path,
        load_user_permissions,
    )

    sid = "sess_allow_tools_2"
    get_user_permissions_path().write_text(
        yaml.safe_dump({"permissions": {"ask_tools": ["memory_get"]}}),
        encoding="utf-8",
    )
    assert append_allow_tool("memory_get", scope="user") is True
    user = load_user_permissions()
    assert "memory_get" in (user.get("allow_tools") or [])
    assert "ask_tools" not in user
    assert not get_session_permissions_path(sid).is_file()


def test_persist_session_overlay_keeps_allow_tools(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        append_allow_tool,
        load_session_permissions,
        persist_session_overlay_from_effective,
    )

    sid = "sess_allow_tools_3"
    append_allow_tool("todo_list", scope="session", session_id=sid)
    ok = persist_session_overlay_from_effective(
        {
            "allow_tools": ["todo_list", "user_only_tool"],
            "approval_overrides": [],
            "_allow_tools_added": ["todo_list"],
            "_persist_scope": "session",
        },
        session_id=sid,
    )
    assert ok is True
    sess = load_session_permissions(sid)
    assert sess.get("allow_tools") == ["todo_list"]
    assert "user_only_tool" not in (sess.get("allow_tools") or [])


def test_user_tools_map_roundtrip_allow_ask_deny(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        delete_user_tool,
        get_user_tools_map,
        set_user_tool_level,
    )

    set_user_tool_level("bash", "ask")
    set_user_tool_level("todo_list", "allow")
    set_user_tool_level("rm_tool", "deny")
    tools = get_user_tools_map()
    assert tools["bash"] == "ask"
    assert tools["todo_list"] == "allow"
    assert tools["rm_tool"] == "deny"
    assert delete_user_tool("bash") is True
    assert "bash" not in get_user_tools_map()


def test_compose_full_access_without_core_api(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    monkeypatch.setattr(layers, "_import_core_compose", lambda: None)
    monkeypatch.setattr(layers, "_import_mode_controller", lambda: None)
    _write_global(isolated_data_dir, {"enabled": False, "tools": {"bash": "ask"}})

    eff = layers.compose_host_effective_permissions()
    assert eff["enabled"] is True
    assert eff["mode"] == "full_access"
    assert eff["sandbox_intent"] == "optional"
    assert eff["file_guard"]["enabled"] is False
    assert (eff.get("tools") or {}).get("bash") == "ask"


def test_migrate_global_tool_levels_without_core_api(
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    monkeypatch.setattr(layers, "_import_mode_controller", lambda: None)
    _write_global(
        isolated_data_dir,
        {"enabled": True, "tools": {"todo_list": "allow", "bash": "ask"}},
    )

    global_permissions = layers.migrate_and_write_global_permissions()
    user_permissions = yaml.safe_load(
        layers.get_user_permissions_path().read_text(encoding="utf-8"),
    )["permissions"]

    assert user_permissions["allow_tools"] == ["todo_list"]
    assert user_permissions["ask_tools"] == ["bash"]
    assert not {
        "allow_tools",
        "ask_tools",
        "deny_tools",
        "tools",
    }.intersection(global_permissions)


def test_build_permission_rail_mounts_for_legacy_full_access(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is not None
    assert getattr(rail, "permission_mode", None) == "full_access"
    assert getattr(rail, "sandbox_intent", None) == "optional"


def test_build_permission_rail_with_local_compose_fallback(
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    monkeypatch.setattr(layers, "_import_core_compose", lambda: None)
    monkeypatch.setattr(layers, "_import_mode_controller", lambda: None)

    rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is not None
    assert getattr(rail, "permission_mode", None) == "full_access"
    assert getattr(rail, "sandbox_intent", None) == "optional"
