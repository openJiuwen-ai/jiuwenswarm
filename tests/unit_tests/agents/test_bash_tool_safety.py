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


def _shell_tool(cls_name: str, module: str):
    """Construct a shell tool with a minimal LOCAL SysOperation."""
    import importlib

    from openjiuwen.core.sys_operation import (
        LocalWorkConfig,
        OperationMode,
        SysOperation,
        SysOperationCard,
    )

    operation = SysOperation(
        SysOperationCard(
            id="shell-card-exempt-test",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(shell_allowlist=None, restrict_to_sandbox=False),
        )
    )
    cls = getattr(importlib.import_module(module), cls_name)
    return cls(operation=operation)


def test_shell_tool_card_exempts_outer_call_timeout() -> None:
    """bash / powershell must not be capped by the 300s call-level default.

    Their time budget arrives per call as the ``timeout`` argument. Without
    ``resilience.timeout_s`` the framework's ``DEFAULT_TOOL_CALL_TIMEOUT``
    (300s) silently overrode the caller: production logs contain
    ``Tool 'bash' timed out after 300.0s`` for a command that declared
    ``timeout: 900``.
    """
    install_shell_tool_safety_hooks()

    bash = _shell_tool("BashTool", "openjiuwen.harness.tools.shell.bash._tool")
    assert bash.card.properties["resilience"]["timeout_s"] is None

    try:
        powershell = _shell_tool(
            "PowerShellTool", "openjiuwen.harness.tools.shell.powershell._tool"
        )
    except ImportError:  # pragma: no cover - platform without PowerShell tool
        powershell = None
    if powershell is not None:
        assert powershell.card.properties["resilience"]["timeout_s"] is None


def test_shell_tool_card_exemption_is_idempotent() -> None:
    install_shell_tool_safety_hooks()
    install_shell_tool_safety_hooks()
    bash = _shell_tool("BashTool", "openjiuwen.harness.tools.shell.bash._tool")
    assert bash.card.properties["resilience"] == {"timeout_s": None}


def test_shell_tool_card_keeps_a_caller_declared_budget() -> None:
    """A construction site that already declared a budget keeps it."""
    from openjiuwen.core.sys_operation import (
        LocalWorkConfig,
        OperationMode,
        SysOperation,
        SysOperationCard,
    )

    from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
        _stamp_exempt_tool_card,
    )

    operation = SysOperation(
        SysOperationCard(
            id="shell-card-declared-test",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(shell_allowlist=None, restrict_to_sandbox=False),
        )
    )

    class FakeShellTool:
        """Minimal stand-in: the stamp helper only touches ``card.properties``."""

        def __init__(self, *_args, **_kwargs):
            self.card = type(
                "Card",
                (),
                {"properties": {"resilience": {"timeout_s": 42.0}}},
            )()

    _stamp_exempt_tool_card(FakeShellTool)
    tool = FakeShellTool(operation)
    assert tool.card.properties["resilience"] == {"timeout_s": 42.0}
