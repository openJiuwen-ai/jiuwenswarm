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


def test_build_permission_rail_disabled_still_builds_and_registers(monkeypatch):
    """enabled=false 不再短路（S_17 N1）：rail 照常构建并注册展开契约，
    always_on_components 随配置发布给 agent_shield 引擎裁决。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {
            "enabled": False,
            "always_on_components": ["host_guard"],
            "tools": {},
            "rules": [],
        },
    )
    unregister_meta_tool("invoke_tool")
    rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is not None
    assert "invoke_tool" in registered_meta_tools()
    static_cfg = getattr(rail, "_static_config", {})
    assert static_cfg.get("enabled") is False
    assert static_cfg.get("always_on_components") == ["host_guard"]


def test_build_permission_rail_disabled_seeds_default_and_respects_explicit_empty(monkeypatch):
    """disabled 缺键时播种模板默认 ["host_guard"]（企业旧模板 body 兜底）；
    用户显式 [] 表示主动关闭底线，不被覆盖。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": False, "tools": {}, "rules": []},
    )
    rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is not None
    assert getattr(rail, "_static_config", {}).get("always_on_components") == ["host_guard"]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": False, "always_on_components": [], "tools": {}, "rules": []},
    )
    rail_empty = build_permission_rail({"permissions": {"enabled": False}})
    assert rail_empty is not None
    assert getattr(rail_empty, "_static_config", {}).get("always_on_components") == []


def test_build_permission_rail_disabled_warns_on_malformed_always_on(monkeypatch, caplog):
    """非 list 值（YAML 误写成标量）与未知组件名走告警分支：误配标量与缺键同样
    兜底播种 ["host_guard"]（引擎对非 list 静默置空，只告警透传会让一个字符级
    误配重新打开"关审批即失守卫"缺口，检视意见 2）。"""
    import logging

    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {
            "enabled": False,
            "always_on_components": "host_guard",
            "tools": {},
            "rules": [],
        },
    )
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.common.utils"):
        rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is not None
    # 误配标量兜底播种，与缺键同向（显式 [] 才是主动关闭底线）。
    assert getattr(rail, "_static_config", {}).get("always_on_components") == ["host_guard"]
    assert "must be a list" in caplog.text

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {
            "enabled": False,
            "always_on_components": ["host_gaurd"],
            "tools": {},
            "rules": [],
        },
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.common.utils"):
        rail_unknown = build_permission_rail({"permissions": {"enabled": False}})
    assert rail_unknown is not None
    assert "unknown names" in caplog.text

    # strip 归一正向：引擎先 strip 再收录，带空白的合法名不告警。
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {
            "enabled": False,
            "always_on_components": ["  host_guard  "],
            "tools": {},
            "rules": [],
        },
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.common.utils"):
        rail_padded = build_permission_rail({"permissions": {"enabled": False}})
    assert rail_padded is not None
    assert "unknown names" not in caplog.text


def test_permissions_snapshot_normalizes_disabled_floor(monkeypatch):
    """检视意见 1：effective 快照在 persist 合并等刷新点被重新消费——配置源缺键时
    归一兜底，构建期播种的底线不被无键快照冲掉（引擎置空 → 底线全关）。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": False, "tools": {}, "rules": []},
    )
    rail = build_permission_rail({"permissions": {"enabled": False}})
    assert rail is not None

    snapshot = rail._host.get_permissions_snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["always_on_components"] == ["host_guard"]


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
