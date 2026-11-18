# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""invoke_tool 转发安全展开（issue #6499）。

复现链：invoke_tool 外层不在 tools 配置 → ``defaults`` 放行 → PowerShell 载荷
经 progressive 转发零评估执行。修复：``build_permission_rail`` 装配期经
``install_meta_tool_expanders`` 注册展开契约，agent_shield 引擎对内层
``(tool_name, arguments)`` 递归判定并与外层合并取最严。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    import agent_shield  # noqa: F401
except ModuleNotFoundError:
    # 本地旧 venv 未同步安全栈依赖时的兄弟仓回退（CI / uv sync 环境下不走此
    # 分支）。agent-security 与本仓在工作区同级。
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[5] / "agent-security")
    )
    import agent_shield  # noqa: F401

from agent_shield.tool_permission import (  # noqa: E402
    PermissionLevel,
    registered_meta_tools,
    unregister_meta_tool,
)

from jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime import (  # noqa: E402
    check_permission_by_temp_config,
    install_meta_tool_expanders,
)

# issue #6499 的四条 PowerShell 混淆载荷（原样取自复现步骤）。
ISSUE_PAYLOADS = [
    '[ScriptBlock]::Create("Get-Date")',
    "& (Get-Command notepad)",
    "powershell.exe -EncodedCommand aABlAGwAbABvAA==",
    'iex "Get-Process"',
]


def _forward(target: str, args: dict) -> dict:
    return {"tool_name": target, "arguments": args}


def _config(**extra) -> dict:
    # defaults '*' allow 复现生产 config.yaml：invoke_tool 不在 tools 列表。
    return {
        "package_builtin_rules": False,
        "defaults": {"*": "allow"},
        **extra,
    }


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    unregister_meta_tool("invoke_tool")


def test_install_registers_invoke_tool_idempotently():
    install_meta_tool_expanders()
    assert "invoke_tool" in registered_meta_tools()
    install_meta_tool_expanders()
    assert "invoke_tool" in registered_meta_tools()


@pytest.mark.parametrize("command", ISSUE_PAYLOADS)
async def test_forwarded_issue_payloads_not_allowed(command):
    install_meta_tool_expanders()
    result = await check_permission_by_temp_config(
        "invoke_tool",
        _forward("create_terminal", {"command": command}),
        _config(tools={"create_terminal": "ask"}),
    )
    assert result.permission in (PermissionLevel.ASK, PermissionLevel.DENY)


async def test_forwarded_benign_command_still_allowed():
    install_meta_tool_expanders()
    result = await check_permission_by_temp_config(
        "invoke_tool",
        _forward("bash", {"command": "dir"}),
        _config(tools={"bash": "allow"}),
    )
    assert result.permission is PermissionLevel.ALLOW


async def test_forwarded_outer_explicit_deny_still_wins():
    install_meta_tool_expanders()
    result = await check_permission_by_temp_config(
        "invoke_tool",
        _forward("bash", {"command": "echo hi"}),
        _config(tools={"invoke_tool": "deny", "bash": "allow"}),
    )
    assert result.permission is PermissionLevel.DENY


async def test_forwarded_inner_name_whitespace_normalized():
    """内层名带空白时按 strip 后名字判定（判据与返回值一致，堵空白填充失配 tools 配置）。"""
    install_meta_tool_expanders()
    result = await check_permission_by_temp_config(
        "invoke_tool",
        _forward("  bash  ", {"command": "echo hi"}),
        _config(tools={"bash": "deny"}),
    )
    assert result.permission is PermissionLevel.DENY


def test_build_permission_rail_registers_expander(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )
    unregister_meta_tool("invoke_tool")
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    assert "invoke_tool" in registered_meta_tools()


def test_build_permission_rail_disabled_does_not_register(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": False},
    )
    unregister_meta_tool("invoke_tool")
    rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is None
    assert "invoke_tool" not in registered_meta_tools()


@pytest.mark.parametrize("exc", [RuntimeError, ImportError])
def test_build_permission_rail_tolerates_expander_registration_failure(monkeypatch, exc):
    """注册失败不让权限 rail 装配崩溃：告警 + 降级为仅外层判定（S_17 fail-open 契约）。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )

    def _boom():
        raise exc("agent_shield expander registration unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permission_runtime.install_meta_tool_expanders",
        _boom,
    )
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
