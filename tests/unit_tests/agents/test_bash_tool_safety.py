# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools import bash_tool_safety
from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    _pre_execute_shell_command,
    install_shell_tool_safety_hooks,
    reset_installed_flag,
)


@pytest.fixture(autouse=True)
def _reset_install_flag():
    reset_installed_flag()
    yield
    reset_installed_flag()


def test_pre_execute_blocks_rm_rf() -> None:
    err = _pre_execute_shell_command("rm -rf /tmp/x")
    assert err is not None
    assert "rejected for safety" in err


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


def test_install_wraps_bash_tool_invoke() -> None:
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
