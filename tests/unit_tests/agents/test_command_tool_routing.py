# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.sys_operation.result import ExecuteCmdData, ExecuteCmdResult

from jiuwenswarm.agents.harness.common.tools import command_tools as commands
from jiuwenswarm.agents.harness.common.tools.mcp_toolkits import get_mcp_tools


@pytest.fixture
def routed_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_resolve_command_workdir", lambda value: tmp_path)
    # No tool-local process may be launched, even when the operation fails.
    monkeypatch.setattr(commands, "_run_command_sync", Mock(side_effect=AssertionError("host bypass")))
    monkeypatch.setattr(commands, "_run_command_background", Mock(side_effect=AssertionError("host bypass")))
    result = ExecuteCmdResult(
        code=StatusCode.SUCCESS.code, message="ok",
        data=ExecuteCmdData(command="echo test", exit_code=7, stdout="output", stderr="error"),
    )
    shell = SimpleNamespace(execute_cmd=AsyncMock(return_value=result), execute_cmd_background=AsyncMock())
    operation = SimpleNamespace(shell=lambda: shell)
    return commands.create_command_tool(operation, agent_id="agent-a"), shell, operation


def test_binding_requires_agent_identity(routed_tool):
    _, _, operation = routed_tool
    with pytest.raises(ValueError, match="agent_id"):
        get_mcp_tools(sys_operation=operation)


def test_unbound_toolkit_does_not_advertise_unusable_command():
    assert all(tool.card.name != "mcp_exec_command" for tool in get_mcp_tools())


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [124, -1])
async def test_provider_timeout_preserves_timeout_message(routed_tool, exit_code):
    bound, shell, _ = routed_tool
    shell.execute_cmd.return_value = ExecuteCmdResult(
        code=StatusCode.SYS_OPERATION_SHELL_EXECUTION_ERROR.code,
        message="shell operation execution error, execution: execute_cmd, reason: execution timeout after 17 seconds",
        data=ExecuteCmdData(command="echo test", exit_code=exit_code),
    )
    assert await bound._func(command="echo test", shell_type="cmd") == "[ERROR]: command timed out after 17s."


@pytest.mark.asyncio
@pytest.mark.parametrize("shell_type", ["bash", "sh", "powershell", "cmd"])
async def test_command_routes_to_bound_operation(routed_tool, tmp_path, shell_type):
    bound_tool, shell, _ = routed_tool
    result = json.loads(await bound_tool.invoke({
        "command": "echo test", "shell_type": shell_type, "timeout_seconds": 17,
    }))
    shell.execute_cmd.assert_awaited_once_with(
        "echo test", cwd=str(tmp_path), timeout=17, shell_type=shell_type,
    )
    assert result["exit_code"] == 7
    assert result["stdout"] == "output"
    assert result["stderr"] == "error"
    assert result["resolved_shell"] == shell_type


@pytest.mark.asyncio
async def test_missing_binding_never_executes(routed_tool):
    _, shell, _ = routed_tool
    result = await commands.mcp_exec_command.invoke({"command": "echo test", "shell_type": "bash"})
    assert "requires the agent's SysOperation binding" in result
    shell.execute_cmd.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [True, False])
async def test_operation_failure_never_falls_back_in_tool(routed_tool, raises):
    bound_tool, shell, _ = routed_tool
    if raises:
        shell.execute_cmd.side_effect = RuntimeError("sandbox unavailable")
    else:
        shell.execute_cmd.return_value = ExecuteCmdResult(
            code=StatusCode.SYS_OPERATION_SHELL_EXECUTION_ERROR.code, message="sandbox denied",
        )
    result = await bound_tool._func(command="echo test", shell_type="bash")
    assert result.startswith("[ERROR]")
    assert "sandbox" in result


@pytest.mark.asyncio
async def test_background_uses_bound_operation(routed_tool, tmp_path):
    bound_tool, shell, _ = routed_tool
    shell.execute_cmd_background.return_value = SimpleNamespace(
        code=StatusCode.SUCCESS.code, data=SimpleNamespace(pid=123), message="ok",
    )
    result = json.loads(await bound_tool._func(command="echo test", shell_type="sh", background=True))
    assert result["pid"] == 123
    shell.execute_cmd_background.assert_awaited_once_with("echo test", cwd=str(tmp_path), shell_type="sh")
    shell.execute_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_background_does_not_launch_host(routed_tool):
    bound_tool, shell, _ = routed_tool
    shell.execute_cmd_background.side_effect = NotImplementedError("background unsupported")
    result = await bound_tool._func(command="echo test", shell_type="sh", background=True)
    assert "background unsupported" in result


@pytest.mark.asyncio
async def test_cancellation_propagates_to_operation(routed_tool):
    bound_tool, shell, _ = routed_tool
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    shell.execute_cmd.side_effect = execute
    task = asyncio.create_task(bound_tool._func(command="echo test", shell_type="bash"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["session", "task", "timeout"])
async def test_local_provider_stops_registered_process(tmp_path, monkeypatch, stop):
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard, OperationMode, LocalWorkConfig
    from openjiuwen.core.sys_operation.local import shell_operation
    from openjiuwen.core.sys_operation import shell_process_registry as registry

    monkeypatch.setattr(commands, "_resolve_command_workdir", lambda value: tmp_path)
    started = asyncio.Event()
    processes = []
    original_track = shell_operation._track_shell_process

    def track(proc):
        sid = original_track(proc)
        processes.append(proc)
        started.set()
        return sid

    monkeypatch.setattr(shell_operation, "_track_shell_process", track)
    operation = SysOperation(SysOperationCard(
        id="cancel-test", mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=[f'"{sys.executable}"'.split()[0]]),
    ))
    bound = commands.create_command_tool(operation, agent_id="cancel-test")
    token = registry.set_shell_session_id("command-cancel-test")
    task = asyncio.create_task(bound._func(
        command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
        shell_type="cmd" if os.name == "nt" else "sh",
        timeout_seconds=1 if stop == "timeout" else 30,
    ))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if stop == "session":
            assert registry.kill_shell_processes_for_session("command-cancel-test") == 1
            result = json.loads(await asyncio.wait_for(task, 10))
            assert result["cancelled"] is True
        elif stop == "task":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        else:
            assert "command timed out after 1s" in await asyncio.wait_for(task, 10)
        await asyncio.wait_for(processes[0].wait(), 5)
        assert processes[0].returncode is not None
        assert not registry.SHELL_PROCESS_REGISTRY._processes.get("command-cancel-test")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        registry.consume_shell_session_cancelled("command-cancel-test")
        registry.reset_shell_session_id(token)


@pytest.mark.asyncio
async def test_toolkit_bindings_are_isolated(routed_tool):
    first, shell, operation = routed_tool
    other_shell = SimpleNamespace(execute_cmd=AsyncMock(return_value=shell.execute_cmd.return_value))
    other_operation = SimpleNamespace(shell=lambda: other_shell)
    tools = get_mcp_tools(sys_operation=other_operation, agent_id="agent-b")
    second = next(tool for tool in tools if tool.card.name == "mcp_exec_command")
    assert first.card.id != second.card.id
    assert "operation" not in second.card.input_params["properties"]
    await asyncio.gather(
        first._func(command="echo first", shell_type="bash"),
        second._func(command="echo second", shell_type="cmd"),
    )
    assert shell.execute_cmd.call_args.args == ("echo first",)
    assert other_shell.execute_cmd.call_args.args == ("echo second",)


@pytest.mark.asyncio
@pytest.mark.parametrize("connector", [False, True])
async def test_command_reuses_connector_host_routing(tmp_path, monkeypatch, connector):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as jb
    from jiuwenswarm.agents.harness.common.tools import connector_host_exec as hooks

    # Installing the production hook is scoped to this test.
    monkeypatch.setattr(hooks, "_installed", False)
    monkeypatch.setattr(jb.JiuwenBoxShellProvider, "execute_cmd", jb.JiuwenBoxShellProvider.execute_cmd)
    monkeypatch.setattr(jb.JiuwenBoxShellProvider, "execute_cmd_stream", jb.JiuwenBoxShellProvider.execute_cmd_stream)
    hooks.install_connector_host_exec_hooks()
    monkeypatch.setattr(commands, "_resolve_command_workdir", lambda value: tmp_path)
    monkeypatch.setattr(hooks, "command_should_host_exec", lambda command: connector)
    monkeypatch.setattr(hooks, "host_shell_argv", lambda command, **kw: ["connector.exe", "--help"])
    host = Mock(return_value={"stdout": "host", "stderr": "", "exit_code": 0})
    monkeypatch.setattr(hooks, "run_host_subprocess", host)
    provider = object.__new__(jb.JiuwenBoxShellProvider)
    provider._launcher_extra_params = Mock(return_value={})
    provider._get_sandbox_id = Mock(return_value="sandbox-a")
    client = Mock()
    client.exec.return_value = {"stdout": "sandbox", "stderr": "", "exit_code": 0}
    provider._get_client = Mock(return_value=client)
    bound = commands.create_command_tool(SimpleNamespace(shell=lambda: provider), agent_id="agent-a")
    result = json.loads(await bound._func(command="echo test", shell_type="cmd"))
    if connector:
        assert result["stdout"] == "host"
        host.assert_called_once()
        client.exec.assert_not_called()
    else:
        assert result["stdout"] == "sandbox"
        client.exec.assert_called_once()
        assert client.exec.call_args.args[1] == ["cmd", "/d", "/s", "/c", "echo test"]
        host.assert_not_called()


@pytest.mark.asyncio
async def test_sub_agent_registration_binds_configured_operation(routed_tool, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools import multi_session_toolkits as sessions

    _, shell, operation = routed_tool
    monkeypatch.setattr(sessions.Runner.resource_mgr, "get_sys_operation", Mock(return_value=operation))
    registered = []
    monkeypatch.setattr(sessions.Runner.resource_mgr, "add_tool", registered.append)
    monkeypatch.setattr(sessions, "ReActAgent", Mock(return_value=Mock()))
    toolkit = sessions.MultiSessionToolkit(
        "session-a", "web", "request-a", SimpleNamespace(sys_operation_id="operation-a"),
    )
    await toolkit.get_sub_agent()
    sessions.Runner.resource_mgr.get_sys_operation.assert_called_once_with("operation-a")
    bound = next(tool for tool in registered if tool.card.name == "mcp_exec_command")
    await bound._func(command="echo test", shell_type="bash")
    shell.execute_cmd.assert_awaited_once()
