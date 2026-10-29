# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side permission layers / mode compose tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def isolated_data_dir(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
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

    # 合成全局层走 config_loader 生效段（round-023 D1），其
    # _cached_global 与 CONFIG_YAML_PATH 重定向无失效联动：进用例
    # 先清缓存，退出再清，防 tmp 配置与其他用例缓存互相污染。
    from jiuwenswarm.agents.harness.common.rails.permissions import config_loader

    config_loader.clear_permissions_config_cache()
    request.addfinalizer(config_loader.clear_permissions_config_cache)
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


def test_compose_reloads_after_external_yaml_write(isolated_data_dir: Path) -> None:
    # E2E round-024 S4 回归：网关进程 mode.set 落盘后，引擎进程的
    # config_loader 缓存必须经 mtime 失步探测重读，否则引擎始终以
    # 启动时模式决策，且其 runtime persist 会反向 clobber 对侧切换。
    _write_global(isolated_data_dir, {"enabled": True, "mode": "auto"})
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
    )

    assert compose_host_effective_permissions()["mode"] == "auto"

    # 模拟另一进程直写 yaml（不经本进程任何 persist 入口）
    _write_global(isolated_data_dir, {"enabled": True, "mode": "strict"})

    eff = compose_host_effective_permissions()
    assert eff["mode"] == "strict"
    assert eff["defaults"]["*"] == "ask"


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


def test_compose_includes_global_file_guard_paths(isolated_data_dir: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
    )

    _write_global(
        isolated_data_dir,
        {
            "enabled": True,
            "mode": "auto",
            "file_guard": {
                "enabled": True,
                "paths": [
                    {
                        "path": "/org/secrets/**",
                        "match": "glob",
                        "read": "ask",
                        "write": "ask",
                        "exec": "deny",
                    },
                ],
            },
        },
    )
    eff = compose_host_effective_permissions()
    paths = (eff.get("file_guard") or {}).get("paths") or []
    org = next(p for p in paths if p.get("path") == "/org/secrets/**")
    assert org["exec"] == "deny"
    # preset 底线仍在
    assert any(p.get("path") == "**/.ssh/**" for p in paths)


def test_migrate_enabled_false_with_legacy_strict_reads_full_access(
    isolated_data_dir: Path,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_permissions_mode,
        migrate_and_write_global_permissions,
    )

    _write_global(
        isolated_data_dir,
        {"enabled": False, "permission_mode": "strict"},
    )
    migrate_and_write_global_permissions()
    # 迁移落盘结果与 get_permissions_mode 读取优先级一致：
    # enabled:false → full_access 优先于 legacy strict
    assert get_permissions_mode() == "full_access"


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


def test_mode_get_and_sandbox_follow_effective_config(isolated_data_dir: Path) -> None:
    # MR 评审（2026-10-29）：MODE_GET/SANDBOX 必须读生效段而非 yaml 直读，
    # persist 链路变更后回读不得拿旧值；sandbox_intent 跟随 mode。
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_permissions_mode,
        get_sandbox_intent,
        update_permissions_mode,
    )

    _write_global(isolated_data_dir, {"enabled": True, "mode": "auto"})
    assert get_permissions_mode() == "auto"
    assert get_sandbox_intent() == "required"

    assert update_permissions_mode("strict") == "strict"
    assert get_permissions_mode() == "strict"
    assert get_sandbox_intent() == "required"

    assert update_permissions_mode("full_access") == "full_access"
    assert get_permissions_mode() == "full_access"
    assert get_sandbox_intent() == "optional"


def test_invalid_session_id_rejected(isolated_data_dir: Path) -> None:
    # MR 评审（2026-10-29）：session_id 作路径分量前必须过
    # is_valid_session_id，防 ``../`` 穿越数据目录写权限覆盖段。
    import pytest

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_session_permissions_path,
        load_session_permissions,
        persist_session_overlay_from_effective,
    )

    for bad in ("../../config", "a/b", ".hidden", "sess/../x"):
        with pytest.raises(ValueError):
            get_session_permissions_path(bad)
    assert load_session_permissions("../../config") == {}
    assert (
        persist_session_overlay_from_effective(
            {"approval_overrides": [{"id": "x", "action": "allow"}]},
            session_id="../../config",
        )
        is False
    )


def test_session_override_not_promoted_to_user(isolated_data_dir: Path) -> None:
    # MR 评审（2026-10-29）：永久允许的增量不得携带 compose 已并入的
    # session 层 override；user 层落盘走棘轮合并而非整表替换。
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _derive_user_overlay_delta,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        get_user_permissions_path,
        persist_session_overlay_from_effective,
        persist_user_overlay_from_effective,
    )

    sid = "sess-promote-1"
    session_entry = {
        "id": "session_pytest",
        "tools": ["bash"],
        "match_type": "command",
        "pattern": "pytest*",
        "action": "allow",
    }
    assert persist_session_overlay_from_effective(
        {"approval_overrides": [session_entry]}, session_id=sid
    )
    user_entry = {
        "id": "always_git_status",
        "tools": ["bash"],
        "match_type": "command",
        "pattern": "git status*",
        "action": "allow",
    }
    assert persist_user_overlay_from_effective({"approval_overrides": [user_entry]})

    # rail 整表 payload = 磁盘合成结果 + 本次新批准条目
    new_entry = {
        "id": "always_ls_la",
        "tools": ["bash"],
        "match_type": "command",
        "pattern": "ls -la*",
        "action": "allow",
    }
    baseline = compose_host_effective_permissions(session_id=sid)
    rail_payload = dict(baseline)
    rail_payload["approval_overrides"] = list(
        baseline.get("approval_overrides") or []
    ) + [new_entry]

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        load_global_permissions,
        load_user_permissions,
    )

    delta = _derive_user_overlay_delta(
        rail_payload,
        [load_global_permissions(), {}, load_user_permissions()],
        approval_baseline=baseline,
    )
    kept = delta.get("approval_overrides") or []
    assert [entry["id"] for entry in kept] == ["always_ls_la"]

    assert persist_user_overlay_from_effective(delta)
    raw = yaml.safe_load(get_user_permissions_path().read_text(encoding="utf-8"))
    saved_ids = [
        entry["id"] for entry in raw["permissions"]["approval_overrides"]
    ]
    # user 层新增本次条目；session 层条目不得被固化；存量不丢
    assert saved_ids == ["always_git_status", "always_ls_la"]


def test_user_persist_merges_overrides_instead_of_replace(isolated_data_dir: Path) -> None:
    # MR 评审（2026-10-29）：增量语义下 persist 不得整表替换 user 层存量。
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_user_permissions_path,
        persist_user_overlay_from_effective,
    )

    def _entry(oid: str) -> dict:
        return {
            "id": oid,
            "tools": ["bash"],
            "match_type": "command",
            "pattern": f"{oid}*",
            "action": "allow",
        }

    assert persist_user_overlay_from_effective({"approval_overrides": [_entry("a")]})
    assert persist_user_overlay_from_effective({"approval_overrides": [_entry("b")]})
    raw = yaml.safe_load(get_user_permissions_path().read_text(encoding="utf-8"))
    assert [entry["id"] for entry in raw["permissions"]["approval_overrides"]] == [
        "a",
        "b",
    ]


def test_auto_preset_asks_acp_chat(isolated_data_dir: Path) -> None:
    # R2（2026-10-29）：develop 最新对 acp_chat 保留 ask（整工具无
    # 命令级兜底）；迁移删除 Global tools 名单后由 auto preset 兜底，
    # full_access/strict 不引入。
    _write_global(isolated_data_dir, {"enabled": True, "mode": "auto"})
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
    )

    auto = compose_host_effective_permissions()
    assert "acp_chat" in (auto.get("ask_tools") or [])
    assert "acp_chat" not in (auto.get("allow_tools") or [])

    _write_global(isolated_data_dir, {"enabled": True, "mode": "full_access"})
    full = compose_host_effective_permissions()
    assert "acp_chat" not in (full.get("ask_tools") or [])


def test_auto_preset_acp_chat_explicit_allow_and_deny_win(isolated_data_dir: Path) -> None:
    # preset ask 只兜缺省（无显式 ask 名单场景，即迁移删 Global tools
    # 后的常态）：显式 allow（HITL 永久允许写 user 层）与 deny 均优先
    # 于 preset 底线。
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        get_user_permissions_path,
    )

    _write_global(isolated_data_dir, {"enabled": True, "mode": "auto"})
    user_path = get_user_permissions_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        yaml.safe_dump({"permissions": {"allow_tools": ["acp_chat"]}}),
        encoding="utf-8",
    )
    allowed = compose_host_effective_permissions()
    assert "acp_chat" in (allowed.get("allow_tools") or [])
    assert "acp_chat" not in (allowed.get("ask_tools") or [])

    user_path.write_text(
        yaml.safe_dump({"permissions": {"deny_tools": ["acp_chat"]}}),
        encoding="utf-8",
    )
    denied = compose_host_effective_permissions()
    assert "acp_chat" not in (denied.get("ask_tools") or [])
    assert "acp_chat" not in (denied.get("allow_tools") or [])


def test_corrupt_user_yaml_raises_not_silent_empty(isolated_data_dir: Path) -> None:
    # 评审（2026-10-29）：user 层文件存在但读取失败不得静默当 {}——
    # deny/ask/rules 全消失属 fail-open；抛出走快照 None → 引擎沿用
    # 构建期 _static_config。
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        get_user_permissions_path,
        load_user_permissions,
    )

    _write_global(isolated_data_dir, {"enabled": True, "mode": "auto"})
    user_path = get_user_permissions_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("permissions: [unclosed", encoding="utf-8")

    with pytest.raises(Exception):
        load_user_permissions()
    with pytest.raises(Exception):
        compose_host_effective_permissions()


def test_snapshot_failure_keeps_static_deny_not_ask(isolated_data_dir: Path) -> None:
    # 评审（2026-10-29，renquanjia 两条 Should Fix）：overlay 损坏/compose
    # 异常时快照必须回 None（引擎沿用构建期 _static_config），不得以 {}
    # 整体替换策略——否则 deny 名单/file_guard 底线降档为 ASK。行为路径
    # 穿透：构建 → overlay 事后写坏 → 快照 None → 引擎判定仍 DENY。
    import asyncio
    from types import SimpleNamespace

    from openjiuwen.harness.security.models import PermissionLevel

    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        get_user_permissions_path,
    )

    _write_global(
        isolated_data_dir,
        {"enabled": True, "mode": "auto", "tools": {"evil_cmd": "deny"}},
    )
    # 构建（agent 组装）以传入的 permissions 段为全局层源
    rail = build_permission_rail(
        {"permissions": {"enabled": True, "mode": "auto", "tools": {"evil_cmd": "deny"}}}
    )
    assert rail is not None
    assert "evil_cmd" in (rail._static_config.get("deny_tools") or [])

    # 模拟跨进程外部写坏 user overlay（构建后发生）
    user_path = get_user_permissions_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("permissions: [unclosed", encoding="utf-8")

    # compose 失败 → 快照 None（不是 {}）
    snap = rail._host.get_permissions_snapshot()
    assert snap is None

    # 引擎刷新回落 _static_config，deny 仍为 DENY 而非 ASK
    rail._refresh_permissions_for_tool_call(SimpleNamespace())
    result = asyncio.run(rail._engine.check_permission("evil_cmd", {}))
    assert result.permission == PermissionLevel.DENY


def test_file_guard_path_merge_blocks_ask_to_deny(isolated_data_dir: Path) -> None:
    # 评审（2026-10-29）：paths 棘轮须全级别（deny<ask<allow），ask
    # 不得被低级别层覆盖为 deny。
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        _merge_file_guard_path_entries,
    )

    section = {
        "file_guard": {
            "paths": [{"path": "**/.ssh/**", "read": "ask", "write": "ask", "exec": "ask"}]
        }
    }
    _merge_file_guard_path_entries(
        section,
        [{"path": "**/.ssh/**", "read": "deny", "write": "allow", "exec": ""}],
    )
    merged = section["file_guard"]["paths"][0]
    assert merged["read"] == "ask"  # ask 不被降级为 deny
    assert merged["write"] == "allow"  # allow 棘轮照常生效
    assert merged["exec"] == "ask"  # 空值不覆盖


def test_ratchet_merge_skips_missing_action(isolated_data_dir: Path) -> None:
    # 评审（2026-10-29）：无 action 条目不得默认 allow 持久化（fail-open）。
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        _ratchet_merge_approval_overrides,
    )

    existing = [
        {"id": "a", "tools": ["bash"], "match_type": "command", "pattern": "a*", "action": "deny"}
    ]
    merged = _ratchet_merge_approval_overrides(
        existing,
        [{"tools": ["bash"], "match_type": "command", "pattern": "b*"}],
    )
    # 无 action 新键被跳过；存量保留
    assert [entry["id"] for entry in merged] == ["a"]
