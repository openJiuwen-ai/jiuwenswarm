from types import SimpleNamespace

import pytest
from openjiuwen.core.sys_operation import OperationMode

from jiuwenswarm.agents.harness.common.rails.cron_storage_rail import CronStorageRail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cron", "mode", "options", "tool_name", "blocked"),
    [
        (False, OperationMode.LOCAL, {}, "bash", False),
        (True, OperationMode.LOCAL, {}, "bash", True),
        (True, OperationMode.LOCAL, {}, "write_file", True),
        (True, OperationMode.LOCAL, {}, "read_file", False),
        (True, OperationMode.SANDBOX, {}, "bash", False),
        (True, OperationMode.SANDBOX, {"policy": {}}, "bash", True),
        (True, OperationMode.SANDBOX, {"fallback_on_failure": True}, "bash", True),
        (True, OperationMode.SANDBOX, {"excluded_commands": ["some-command"]}, "bash", True),
        (True, OperationMode.SANDBOX, {}, "create_terminal", True),
    ],
)
async def test_cron_storage_tool_gate(cron, mode, options, tool_name, blocked):
    from agent_gateway.common.paths import get_gateway_cron_dir

    directory = str(get_gateway_cron_dir().resolve())
    policy = {"filesystem_policy": {"bind_mounts": [
        {"host_path": directory, "sandbox_path": directory, "mode": "ro"},
    ]}}
    launcher = SimpleNamespace(sandbox_type="jiuwenbox", extra_params={"policy": policy, **options})
    card = SimpleNamespace(
        mode=mode, gateway_config=SimpleNamespace(launcher_config=launcher),
    )
    rail = CronStorageRail(lambda: cron, lambda: card)
    ctx = SimpleNamespace(inputs={"tool_name": tool_name}, extra={})

    await rail.before_tool_call(ctx)

    assert bool(ctx.extra.get("_skip_tool")) is blocked


def test_gateway_cron_directory_is_readonly_under_writable_project(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder

    data_root = tmp_path / "data"
    project = tmp_path
    monkeypatch.setattr("agent_gateway.common.paths.get_user_workspace_dir", lambda: data_root)
    policy, _ = sysop_builder.build_filesystem_policy({}, project_dir=project)
    protected = str((data_root / "gateway" / "cron").resolve())

    fs_policy = policy["filesystem_policy"]
    assert {"host_path": protected, "sandbox_path": protected, "mode": "ro"} in fs_policy[
        "bind_mounts"
    ]
    assert protected in fs_policy["read_only"]
    with pytest.raises(ValueError, match="Gateway cron"):
        sysop_builder.build_filesystem_policy({"allow": [protected]}, project_dir=project)


@pytest.mark.asyncio
async def test_agent_core_dispatch_skips_host_tool_for_cron(monkeypatch):
    from unittest.mock import AsyncMock

    from openjiuwen.core.foundation.llm import ToolCall, ToolMessage
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext,
        AgentCallbackEvent,
        ToolCallInputs,
    )

    rail = CronStorageRail(lambda: True, lambda: None)

    async def execute(event, ctx):
        if event == AgentCallbackEvent.BEFORE_TOOL_CALL:
            await rail.before_tool_call(ctx)

    agent = SimpleNamespace(agent_callback_manager=SimpleNamespace(execute=execute))
    manager = AbilityManager()
    host_tool = AsyncMock()
    monkeypatch.setattr(manager, "_execute_single_tool_call", host_tool)
    call = ToolCall(id="call-1", name="bash", type="function", arguments="{}")
    ctx = AgentCallbackContext(agent=agent, inputs=ToolCallInputs(tool_call=call, tool_name=call.name))

    result, message = await manager._railed_execute_single_tool_call(ctx, call, None)

    host_tool.assert_not_awaited()
    assert result["success"] is False
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == call.id
