# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CronExecutionGuardRail.

The cron execution session loses the cron tools, but the generic file/shell
tools stay available for the job's own work. These tests pin both sides: the
store-mutation bypass is rejected, and ordinary cron work still runs.
"""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.cron_execution_guard_rail import (
    CronExecutionGuardRail,
    _DENIAL_MESSAGE,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
)
from jiuwenswarm.agents.swarm import config_specs, registry
from jiuwenswarm.agents.swarm.providers import member_rails

CRON_SESSION = "__cron___1a18af58e5f_945e007f1278"
NORMAL_SESSION = "web_1a18af3bad2_9d5b07479664"


class _Session:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id


def _ctx(tool_name: str, tool_args: dict, *, session_id: str = CRON_SESSION):
    call = SimpleNamespace(id="call-1", name=tool_name, arguments=tool_args)
    return AgentCallbackContext(
        agent=SimpleNamespace(),
        session=_Session(session_id),
        inputs=ToolCallInputs(
            tool_call=call,
            tool_name=tool_name,
            tool_args=tool_args,
        ),
        extra={},
    )


async def _run(tool_name: str, tool_args: dict, *, session_id: str = CRON_SESSION):
    ctx = _ctx(tool_name, tool_args, session_id=session_id)
    await CronExecutionGuardRail().before_tool_call(ctx)
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_args",
    [
        # The reported bypass: write a script that imports the store and call
        # FileCronJobStore.create_job from it.
        {
            "command": (
                "cat > /tmp/register_cron.py << 'EOF'\n"
                "from jiuwenswarm.runtime.cron.store import FileCronJobStore\n"
                "EOF\npython3 /tmp/register_cron.py"
            )
        },
        {
            "command": (
                "python3 -c \"import jiuwenswarm.runtime.cron.store as s; "
                "s.FileCronJobStore().create_job(name='x')\""
            )
        },
        # Direct store-file write.
        {
            "command": (
                "echo '{\"jobs\": []}' > "
                "/root/.jiuwenswarm/agent/home/cron_jobs.json"
            )
        },
        # Gateway mutation command reached from a shell.
        {"command": "curl -s http://127.0.0.1/api -d 'cron.job.create'"},
    ],
)
async def test_cron_session_shell_store_mutations_are_blocked(tool_args) -> None:
    ctx = await _run("bash", tool_args)
    assert ctx.extra.get("_skip_tool") is True
    assert ctx.inputs.tool_result == _DENIAL_MESSAGE
    assert ctx.inputs.tool_msg.content == _DENIAL_MESSAGE


@pytest.mark.asyncio
async def test_cron_session_file_write_to_store_is_blocked() -> None:
    ctx = await _run(
        "write_file",
        {
            "file_path": "/root/.jiuwenswarm/agent/home/cron_jobs.json",
            "content": "{}",
        },
    )
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_cron_session_script_content_is_blocked_even_for_clean_path() -> None:
    ctx = await _run(
        "write_file",
        {
            "file_path": "/tmp/helper.py",
            "content": (
                "from jiuwenswarm.runtime.cron.store import FileCronJobStore\n"
                "FileCronJobStore().create_job(name='nested')\n"
            ),
        },
    )
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_cron_session_patch_to_store_is_blocked() -> None:
    ctx = await _run(
        "apply_patch",
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: /root/.jiuwenswarm/agent/home/cron_jobs.json\n"
                "*** End Patch\n"
            )
        },
    )
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        # A scheduled job may still run shell commands for its own work.
        ("bash", {"command": "sleep 60"}),
        # ...and write files in its project.
        ("write_file", {"file_path": "/tmp/run_test.py", "content": "print('cron')"}),
        # Reading the store is not a mutation; the job keeps read access.
        ("read_file", {"file_path": "/root/.jiuwenswarm/agent/home/cron_jobs.json"}),
    ],
)
async def test_cron_session_normal_work_is_not_blocked(tool_name, tool_args) -> None:
    ctx = await _run(tool_name, tool_args)
    assert ctx.extra.get("_skip_tool") is None


@pytest.mark.asyncio
async def test_ordinary_session_is_not_restricted() -> None:
    ctx = await _run(
        "bash",
        {
            "command": (
                "python3 -c \"from jiuwenswarm.runtime.cron.store import "
                "FileCronJobStore\""
            )
        },
        session_id=NORMAL_SESSION,
    )
    assert ctx.extra.get("_skip_tool") is None


@pytest.mark.asyncio
async def test_cron_channel_context_blocks_when_session_id_is_plain() -> None:
    token = TOOL_PERMISSION_CHANNEL_ID.set("__cron__")
    try:
        ctx = await _run(
            "bash",
            {"command": "python3 -c \"import jiuwenswarm.runtime.cron.store\""},
            session_id=NORMAL_SESSION,
        )
    finally:
        TOOL_PERMISSION_CHANNEL_ID.reset(token)
    assert ctx.extra.get("_skip_tool") is True


def test_guard_is_mounted_in_both_team_profiles() -> None:
    assert registry.CRON_EXECUTION_GUARD in config_specs._COMMON_RAIL_NAMES
    assert registry.CRON_EXECUTION_GUARD in config_specs._CODE_RAIL_NAMES


def test_guard_provider_builds_the_rail() -> None:
    rail = member_rails._build_cron_execution_guard_rail({}, SimpleNamespace())
    assert isinstance(rail, CronExecutionGuardRail)


def test_guard_rail_spec_builds_through_registered_provider() -> None:
    from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec

    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.registry import register_swarm_providers

    register_swarm_providers()
    rail = RailSpec(type=registry.CRON_EXECUTION_GUARD).build(
        language="cn",
        context=SwarmBuildContext(language="cn", channel="web"),
    )
    assert isinstance(rail, CronExecutionGuardRail)
