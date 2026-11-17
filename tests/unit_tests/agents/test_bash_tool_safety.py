# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest
from agent_shield.tool_permission import PermissionLevel, get_permission_manager

from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    _pre_execute_shell_command,
    install_shell_tool_safety_hooks,
    reset_installed_flag,
)

_PERMISSION_CONFIG = {
    "enabled": True,
    "tools": {"bash": "allow", "powershell": "allow"},
    "package_builtin_rules": True,
    "permission_mode": "strict",
    "shell_allowlist": None,
}


@pytest.fixture(autouse=True)
def _reset_install_flag():
    reset_installed_flag()
    yield
    reset_installed_flag()


@pytest.mark.asyncio
async def test_pre_execute_blocks_pkill_on_jiuwenswarm_tui() -> None:
    command = 'pkill -f "jiuwenswarm-tui" 2>/dev/null'
    # The shell hook only enforces the TUI spawn budget; command safety is
    # adjudicated by PermissionManager.
    assert _pre_execute_shell_command(command) is None
    result = await get_permission_manager().check_permission(
        "bash", {"command": command}, config=_PERMISSION_CONFIG
    )
    assert result.permission == PermissionLevel.DENY
    assert "pkill" in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_rm_rf_never_silently_allowed_by_permission_manager() -> None:
    """rm -rf 迁移后仍不得静默放行：由 agent_shield 内置规则判为 deny/ask。"""
    command = "rm -rf /tmp/x"
    assert _pre_execute_shell_command(command) is None
    result = await get_permission_manager().check_permission(
        "bash", {"command": command}, config=_PERMISSION_CONFIG
    )
    assert result.permission != PermissionLevel.ALLOW


def test_pre_execute_allows_host_kill_patterns() -> None:
    """HostGuard 不在执行入口硬拦；交权限 rail 配置。"""

    assert _pre_execute_shell_command('pkill -f "jiuwenswarm-tui" 2>/dev/null') is None
    assert (
        _pre_execute_shell_command(
            "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force"
        )
        is None
    )


def test_pre_execute_allows_unrelated_ps() -> None:
    err = _pre_execute_shell_command("ps aux | grep node | head -5")
    assert err is None


@pytest.mark.asyncio
async def test_pre_execute_blocks_python_stop_process_pipeline() -> None:
    command = "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force"
    assert _pre_execute_shell_command(command) is None
    result = await get_permission_manager().check_permission(
        "powershell",
        {"command": command, "shell_type": "powershell"},
        config=_PERMISSION_CONFIG,
    )
    assert result.permission == PermissionLevel.DENY
    assert "stop-process" in (result.reason or "").lower()


def test_install_wraps_bash_tool_invoke() -> None:
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
