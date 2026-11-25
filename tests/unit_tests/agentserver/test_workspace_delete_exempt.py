# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作空间内删除豁免（workspace_delete_exempt）单元测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.workspace_delete_exempt import (
    delete_operands_confined,
    in_workspace_delete_exempt_enabled,
    maybe_approve_in_workspace_delete,
)

ZONE = Path(r"C:\Users\admin\Desktop\img")
OUTSIDE = Path(r"C:\Users\admin\Desktop")


def _base_config() -> dict:
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
                    "path": str(ZONE),
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
    """返回 (zones, stub 构造器)；immutable roots 指向 tmp_path 避免触碰真实目录。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt import workspace_delete_exempt as mod

    monkeypatch.setattr(mod, "_immutable_control_roots", lambda: [str(tmp_path)])
    zones = [ZONE]

    def make_inp(command: str, *, user_input=None):
        from openjiuwen.harness.security.permission_engine.core import PermissionEngine

        engine = PermissionEngine(config=_base_config(), workspace_root=None)
        return SimpleNamespace(
            user_input=user_input,
            normalized_tool_name="bash",
            tool_args={"command": command, "timeout": 30},
            engine=engine,
        )

    return zones, make_inp


def test_in_workspace_rm_absolute_approved(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp(f'rm "{ZONE}\\tset.txt"')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is True


def test_relative_rm_inside_approved(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("rm tset.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is True


def test_compound_rm_with_echo_inside_approved(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("echo hi && rm tset.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is True


def test_out_of_workspace_rm_kept(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp(f'rm "{OUTSIDE}\\x.txt"')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_variable_operand_rejected(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("rm $TARGET")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_glob_operand_rejected(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("rm *.log")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_parent_escape_rejected(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("rm ..\\..\\outside.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_chmod_compound_rejected(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("rm tset.txt && chmod +x build.sh")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_cd_outside_then_relative_rejected(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp(f'cd "{OUTSIDE}" && rm tset.txt')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_pushd_breaks_cwd_tracking(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp(f'pushd "{OUTSIDE}" && popd && rm tset.txt')
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_disabled_knob(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp(f'rm "{ZONE}\\tset.txt"')
    assert asyncio.run(
        maybe_approve_in_workspace_delete(inp, zones=zones, knob={"enabled": False})
    ) is False


def test_resume_user_input_never_exempt(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp(f'rm "{ZONE}\\tset.txt"', user_input={"approved": True})
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_non_delete_command_ignored(exempt_env):
    zones, make_inp = exempt_env
    inp = make_inp("find . -type f | wc -l")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=zones)) is False


def test_empty_zones_fail_closed(exempt_env):
    _zones, make_inp = exempt_env
    inp = make_inp("rm tset.txt")
    assert asyncio.run(maybe_approve_in_workspace_delete(inp, zones=[])) is False


def test_delete_operands_confined_unit():
    assert delete_operands_confined('rm "C:/a/t.txt"', [Path("C:/a")]) is True
    assert delete_operands_confined("rm t.txt", [Path("C:/a")]) is True
    assert delete_operands_confined("rm t.txt", []) is False
    assert delete_operands_confined("rm $X", [Path("C:/a")]) is False
    assert delete_operands_confined('rm "C:/b/t.txt"', [Path("C:/a")]) is False
    assert delete_operands_confined("echo ok && rm t.txt", [Path("C:/a")]) is True


def test_knob_default_enabled():
    assert in_workspace_delete_exempt_enabled({}) is True
    assert in_workspace_delete_exempt_enabled({"enabled": False}) is False
    assert in_workspace_delete_exempt_enabled(None) is True


def test_scene_hook_end_to_end(tmp_path, monkeypatch):
    """rail 级集成：真实 build_permission_rail + trusted_dirs 注入 + 真实场景钩子。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_trusted_dirs,
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.interrupt import workspace_delete_exempt as mod
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext,
        TOOL_PERMISSION_CONTEXT,
    )
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    monkeypatch.setattr(mod, "_immutable_control_roots", lambda: [str(tmp_path)])
    rail = build_permission_rail({"permissions": _base_config()})
    assert rail is not None
    apply_permission_trusted_dirs(rail, [str(ZONE)])
    hook = rail._host.permission_scene_hook

    def make_input(command: str):
        return PermissionSceneHookInput(
            ctx=SimpleNamespace(session=None),
            tool_call=SimpleNamespace(id="call_1", name="bash", arguments={}),
            user_input=None,
            normalized_tool_name="bash",
            tool_args={"command": command, "timeout": 30},
            engine=rail._engine,
        )

    token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(channel_id="desktop"))
    try:
        inside = asyncio.run(hook(make_input(f'rm "{ZONE}\\tset.txt"')))
        assert inside is not None and inside[0] == "approve"
        outside = asyncio.run(hook(make_input(f'rm "{OUTSIDE}\\x.txt"')))
        assert outside is None
        variable = asyncio.run(hook(make_input("rm $TARGET")))
        assert variable is None
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)


def test_scene_hook_exempts_without_permission_context(tmp_path, monkeypatch):
    """桌面普通会话不设置 TOOL_PERMISSION_CONTEXT（仅数字分身/记忆门控设置）。

    回归：豁免分支曾排在 perm_ctx 早退之后导致生产环境永不生效。
    """
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_trusted_dirs,
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.interrupt import workspace_delete_exempt as mod
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    monkeypatch.setattr(mod, "_immutable_control_roots", lambda: [str(tmp_path)])
    rail = build_permission_rail({"permissions": _base_config()})
    assert rail is not None
    apply_permission_trusted_dirs(rail, [str(ZONE)])
    hook = rail._host.permission_scene_hook

    def make_input(command: str):
        return PermissionSceneHookInput(
            ctx=SimpleNamespace(session=None),
            tool_call=SimpleNamespace(id="call_1", name="bash", arguments={}),
            user_input=None,
            normalized_tool_name="bash",
            tool_args={"command": command, "timeout": 30},
            engine=rail._engine,
        )

    inside = asyncio.run(hook(make_input("rm -f tset.txt && echo \"deleted\"")))
    assert inside is not None and inside[0] == "approve"
