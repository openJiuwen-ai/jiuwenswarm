# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作空间内删除豁免（workspace_delete_exempt）单元测试。

用例基于 tmp_path 平台无关路径；Windows 盘符/MSYS 语义的断言以 skipif 隔离，
保证在 Linux/macOS CI 上同样可执行。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.workspace_delete_exempt import (
    delete_operands_confined,
    exempt_rule_ids,
    in_workspace_delete_exempt_enabled,
    maybe_approve_in_workspace_delete,
)


def _base_config(zone: Path) -> dict:
    """镜像桌面「替我审批」档：bash allow + 路径层信任目录放行 + 删除/改权限命令 ask。"""
    return {
        "enabled": True,
        "permission_mode": "normal",
        "tools": {"bash": "allow"},
        "package_builtin_rules": False,
        "shell_guard": {"builtin_rules_enabled": False, "unknown_structure": False},
        "rules": [
            {
                "id": "shell_delete_files",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": r"re:(?i)(?:^|[;&|()\r\n])\s*[\"']?(?:[^\s\"';&|()]*[\\/])?(?:rm|remove-item|ri|del|erase|rd|rmdir|unlink)(?:\.exe)?[\"']?(?=\s|$|/)",
                "severity": "HIGH",
                "action": "ask",
            },
            {
                "id": "shell_ask_chmod",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": r"re:(?i)(^|(?:&&|\|\||[;|&()\n])\s*)chmod(?:\s|$)",
                "severity": "HIGH",
                "action": "ask",
            },
        ],
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "allow", "write": "allow", "exec": "allow"},
            "paths": [
                {
                    "path": str(zone),
                    "match": "prefix",
                    "read": "allow",
                    "write": "allow",
                    "exec": "allow",
                }
            ],
        },
    }


@pytest.fixture()
def exempt_env(monkeypatch, tmp_path):
    """返回 (zones, make_inp)；zone 为 tmp_path 下目录，immutable roots 指向独立目录。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt import workspace_delete_exempt as mod
    from openjiuwen.harness.security.permission_engine.core import PermissionEngine

    config_root = tmp_path / "config_root"
    config_root.mkdir()
    monkeypatch.setattr(mod, "_immutable_control_roots", lambda: [str(config_root)])
    zone = tmp_path / "ws"
    zone.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    def make_inp(command: str, *, user_input=None):
        engine = PermissionEngine(config=_base_config(zone), workspace_root=None)
        return SimpleNamespace(
            user_input=user_input,
            normalized_tool_name="bash",
            tool_args={"command": command, "timeout": 30},
            engine=engine,
        )

    return zone, outside, make_inp


def test_in_workspace_rm_absolute_approved(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp(f'rm "{zone / "tset.txt"}"')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is True


def test_relative_rm_inside_approved(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("rm tset.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is True


def test_compound_rm_with_echo_inside_approved(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("echo hi && rm tset.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is True


def test_out_of_workspace_rm_kept(exempt_env):
    zone, outside, make_inp = exempt_env
    inp = make_inp(f'rm "{outside / "x.txt"}"')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_variable_operand_rejected(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("rm $TARGET")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_glob_operand_rejected(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("rm *.log")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_parent_escape_rejected(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("rm " + os.path.join("..", "..", "outside.txt"))
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_chmod_compound_rejected(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("rm tset.txt && chmod +x build.sh")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_cd_outside_then_relative_rejected(exempt_env):
    zone, outside, make_inp = exempt_env
    inp = make_inp(f'cd "{outside}" && rm tset.txt')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_pushd_breaks_cwd_tracking(exempt_env):
    zone, outside, make_inp = exempt_env
    inp = make_inp(f'pushd "{outside}" && popd && rm tset.txt')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_disabled_knob(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp(f'rm "{zone / "tset.txt"}"')
    assert asyncio.run(
        maybe_approve_in_workspace_delete(inp, zones=[zone], knob={"enabled": False})
    ) is False


def test_empty_rules_knob_never_exempt(exempt_env):
    """显式 rules: [] = 不豁免任何规则（不回落默认 shell_delete_files）。"""
    zone, _outside, make_inp = exempt_env
    inp = make_inp(f'rm "{zone / "tset.txt"}"')
    assert exempt_rule_ids({"rules": []}) == set()
    assert asyncio.run(
        maybe_approve_in_workspace_delete(inp, zones=[zone], knob={"enabled": True, "rules": []})
    ) is False


def test_resume_user_input_never_exempt(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp(f'rm "{zone / "tset.txt"}"', user_input={"approved": True})
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_non_delete_command_ignored(exempt_env):
    zone, _outside, make_inp = exempt_env
    inp = make_inp("find . -type f | wc -l")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[zone])) is False


def test_empty_zones_fail_closed(exempt_env):
    _zone, _outside, make_inp = exempt_env
    inp = make_inp("rm tset.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[])) is False


def test_delete_operands_confined_unit(tmp_path):
    zone = tmp_path / "ws"
    zone.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    # 相对路径按 cwd（=zones[0]）解析：落在 zone 内 → True
    assert delete_operands_confined('rm "t.txt"', [zone]) is True
    assert delete_operands_confined("rm t.txt", []) is False
    assert delete_operands_confined("rm $X", [zone]) is False
    # 区外绝对路径 → False
    assert delete_operands_confined(f'rm "{outside / "x.txt"}"', [zone]) is False
    assert delete_operands_confined("echo ok && rm t.txt", [zone]) is True
    # 多 zones：任一命中即可
    assert delete_operands_confined('rm "t.txt"', [tmp_path / "ws2", zone]) is True


@pytest.mark.skipif(os.name != "nt", reason="MSYS 盘符映射仅 Windows git-bash 语义")
def test_msys_path_inside_approved():
    zone = Path(r"C:\Users\admin\Desktop\ws")
    assert delete_operands_confined("rm /c/Users/admin/Desktop/ws/t.txt", [zone]) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX 绝对路径语义")
def test_posix_absolute_outside_rejected(tmp_path):
    zone = tmp_path / "ws"
    zone.mkdir()
    assert delete_operands_confined("rm /etc/passwd", [zone]) is False


def test_knob_default_enabled():
    assert in_workspace_delete_exempt_enabled({}) is True
    assert in_workspace_delete_exempt_enabled({"enabled": False}) is False
    assert in_workspace_delete_exempt_enabled(None) is True
    assert exempt_rule_ids(None) == {"shell_delete_files"}
    assert exempt_rule_ids({"rules": ["shell_delete_files", "other_rule"]}) == {
        "shell_delete_files",
        "other_rule",
    }


def _build_rail(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_trusted_dirs,
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.interrupt import workspace_delete_exempt as mod

    config_root = tmp_path / "config_root"
    config_root.mkdir()
    monkeypatch.setattr(mod, "_immutable_control_roots", lambda: [str(config_root)])
    rail = build_permission_rail({"permissions": _base_config(tmp_path / "ws")})
    assert rail is not None
    zone = tmp_path / "ws"
    zone.mkdir(exist_ok=True)
    apply_permission_trusted_dirs(rail, [str(zone)])
    return rail, zone


def _hook_input(rail, command: str):
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    return PermissionSceneHookInput(
        ctx=SimpleNamespace(session=None),
        tool_call=SimpleNamespace(id="call_1", name="bash", arguments={}),
        user_input=None,
        normalized_tool_name="bash",
        tool_args={"command": command, "timeout": 30},
        engine=rail._engine,
    )


def test_scene_hook_end_to_end(tmp_path, monkeypatch):
    """rail 级集成：真实 build_permission_rail + trusted_dirs 注入 + 真实场景钩子。

    上下文为 channel_id-only（无 principal，对应记忆门控等非受限会话）——豁免生效。
    """
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext,
        TOOL_PERMISSION_CONTEXT,
    )

    rail, zone = _build_rail(tmp_path, monkeypatch)
    hook = rail._host.permission_scene_hook

    token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(channel_id="desktop"))
    try:
        inside = asyncio.run(hook(_hook_input(rail, f'rm "{zone / "tset.txt"}"')))
        assert inside is not None and inside[0] == "approve"
        outside = asyncio.run(
            hook(_hook_input(rail, f'rm "{tmp_path / "outside" / "x.txt"}"'))
        )
        assert outside is None
        variable = asyncio.run(hook(_hook_input(rail, "rm $TARGET")))
        assert variable is None
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)


def test_scene_hook_exempts_without_permission_context(tmp_path, monkeypatch):
    """桌面普通会话不设置 TOOL_PERMISSION_CONTEXT（仅数字分身/记忆门控设置）。

    回归：豁免分支曾排在 perm_ctx 早退之后导致生产环境永不生效。
    """
    rail, zone = _build_rail(tmp_path, monkeypatch)
    hook = rail._host.permission_scene_hook

    outcome = asyncio.run(hook(_hook_input(rail, 'rm -f tset.txt && echo "deleted"')))
    assert outcome is not None and outcome[0] == "approve"


def test_scene_hook_owner_principal_never_exempt(tmp_path, monkeypatch):
    """owner_scopes 场景（有 principal_user_id）不得被删除豁免绕过。

    回归：豁免分支曾位于 owner_scopes 判定之前，owner 级 deny 会被区内 rm 绕过。
    """
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext,
        TOOL_PERMISSION_CONTEXT,
    )

    rail, zone = _build_rail(tmp_path, monkeypatch)
    hook = rail._host.permission_scene_hook

    token = TOOL_PERMISSION_CONTEXT.set(
        PermissionContext(channel_id="desktop", principal_user_id="u1")
    )
    try:
        outcome = asyncio.run(hook(_hook_input(rail, f'rm "{zone / "tset.txt"}"')))
        assert outcome is None
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)


def test_scene_hook_avatar_scene_never_exempt(tmp_path, monkeypatch):
    """数字分身场景不豁免（交互限制硬性优先）。"""
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext,
        TOOL_PERMISSION_CONTEXT,
    )

    rail, zone = _build_rail(tmp_path, monkeypatch)
    hook = rail._host.permission_scene_hook

    token = TOOL_PERMISSION_CONTEXT.set(
        PermissionContext(channel_id="desktop", group_digital_avatar=True)
    )
    try:
        try:
            outcome = asyncio.run(hook(_hook_input(rail, f'rm "{zone / "tset.txt"}"')))
        except Exception:
            # 数字分身链路运行时依赖缺失时异常回落引擎，同样不产生豁免放行
            outcome = None
        assert outcome is None or outcome[0] in ("reject",)
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)
