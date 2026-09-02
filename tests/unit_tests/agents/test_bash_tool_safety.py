# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    _shell_mismatch,
    _wrap_invoke,
    _pre_execute_shell_command,
    install_shell_tool_safety_hooks,
    reset_installed_flag,
)


@pytest.fixture(autouse=True)
def _reset_install_flag():
    reset_installed_flag()
    yield
    reset_installed_flag()


def test_pre_execute_blocks_pkill_on_jiuwenswarm_tui() -> None:
    err = _pre_execute_shell_command('pkill -f "jiuwenswarm-tui" 2>/dev/null')
    assert err is not None
    assert "rejected for safety" in err


def test_pre_execute_allows_unrelated_ps() -> None:
    err = _pre_execute_shell_command("ps aux | grep node | head -5")
    assert err is None


def test_bash_rejects_powershell_cmdlet() -> None:
    err = _shell_mismatch("bash", 'Move-Item "a" "b"')
    assert err is not None
    assert "powershell" in err


def test_bash_rejects_chained_and_piped_cmdlets() -> None:
    assert _shell_mismatch("bash", 'cd "C:/x" && Move-Item -Path a -Destination b') is not None
    assert _shell_mismatch("bash", "Get-ChildItem | Move-Item a b") is not None
    assert _shell_mismatch("bash", "ls; Remove-Item b") is not None


def test_bash_rejects_powershell_specific_variables_and_herestrings() -> None:
    assert _shell_mismatch("bash", "Get-Content $env:APPDATA\\x") is not None
    assert _shell_mismatch("bash", "echo $null") is not None
    assert _shell_mismatch("bash", "@'\nraw text\n'@ | Set-Content x") is not None


def test_bash_allows_generic_bash_variables() -> None:
    # A bare $var is legal bash; only $env:/$null/$true/$false are PowerShell.
    for command in (
        "echo $HOME",
        "for f in *.txt; do echo $f; done",
        "export VAR=1 && echo $VAR",
        "grep $pattern file.txt",
        "echo $nothing",
        "ls $dir",
    ):
        assert _shell_mismatch("bash", command) is None, command


def test_bash_allows_cmdlet_name_substrings_in_filenames() -> None:
    # Cmdlet detection matches the first word of each segment, never substrings.
    for command in (
        "cat out-file.txt",
        "bash move-item.sh",
        "grep start-process run.log",
    ):
        assert _shell_mismatch("bash", command) is None, command


def test_bash_allows_posix_loops_and_plain_commands() -> None:
    for command in (
        "cd /d/xiaoyi_work && ls -la",
        "mkdir -p build && touch build/a.txt",
        "ls; echo done",
        "git log --format=%H",
    ):
        assert _shell_mismatch("bash", command) is None, command


@pytest.mark.asyncio
async def test_bash_wrapper_forces_bash_shell_type() -> None:
    calls = []

    class Parsed:
        command = "echo hello"

    class Tool:
        def _parse_inputs(self, inputs):
            return Parsed()

    async def original(self, inputs, **kwargs):
        calls.append(dict(inputs))
        return "ok"

    wrapped = _wrap_invoke(original, "bash")
    result = await wrapped(Tool(), {"command": "echo hello"})

    assert result == "ok"
    assert calls == [{"command": "echo hello", "shell_type": "bash"}]


def test_install_routes_generic_semicolon_command_to_bash(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: r"C:\Program Files\Git\bin\bash.exe")
    command = (
        'python --version; python -c "import reportlab; print(\'reportlab ok\')" 2>&1; '
        'python -c "import fpdf; print(\'fpdf ok\')" 2>&1'
    )

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == [r"C:\Program Files\Git\bin\bash.exe", "-lc", command]
    assert use_shell is False
    assert resolved_shell == "bash"


def test_install_routes_semicolon_command_to_powershell_without_bash(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: None)
    monkeypatch.setattr(shell_module, "_available_powershell", lambda: r"C:\Windows\powershell.exe")
    command = "python --version; python -c \"print('ok')\""

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == [r"C:\Windows\powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    assert use_shell is False
    assert resolved_shell == "powershell"


def test_install_keeps_quoted_semicolon_on_cmd(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: r"C:\Program Files\Git\bin\bash.exe")
    command = 'python -c "import reportlab; print(\'reportlab ok\')"'

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == command
    assert use_shell is True
    assert resolved_shell == "cmd"


def test_install_keeps_cmd_supported_and_separator_on_cmd(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: r"C:\Program Files\Git\bin\bash.exe")
    command = "python --version && python -c \"print('ok')\""

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == command
    assert use_shell is True
    assert resolved_shell == "cmd"


def test_install_wraps_bash_tool_invoke() -> None:
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
