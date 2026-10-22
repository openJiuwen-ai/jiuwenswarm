# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Display metadata must not change permission decisions or execution identity."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_flash_permission_actions as actions
import test_flash_skill_authorization as skills
from flash_sdk import sdk_tools
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)
from openjiuwen.harness.security.models import PermissionConfirmResponse

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.flash.permissions.skill_install import state_for
from jiuwenswarm.agents.harness.flash.permissions.tool_policy_projection import (
    project_operations,
)
from jiuwenswarm.agents.harness.flash.tools.cron_flash import build_cron_flash_tool

permission_adapter = actions.permission_adapter
skill_tool = skills.skill_tool
tool_context = actions.tool_context


@pytest.fixture(params=[None, "call_goal", "callGoal"])
def display_args(request):
    return {request.param: "查看可用资源"} if request.param else {}


async def register_events(ctx):
    ctx.session.write_stream = AsyncMock()
    await ctx.agent.agent_callback_manager.register_rail(
        JiuSwarmStreamEventRail(), ctx.agent
    )


def assert_display(session, display_args):
    if display_args:
        calls = [
            call.args[0].payload["tool_call"]
            for call in session.write_stream.await_args_list
            if call.args[0].type == "tool_call"
        ]
        assert calls[0]["display_name"] == "查看可用资源"
        assert "call_goal" not in json.loads(calls[0]["arguments"])
        assert "callGoal" not in json.loads(calls[0]["arguments"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,approved,executed",
    [
        ("allow", True, True),
        ("deny", True, False),
        ("ask", True, True),
        ("ask", False, False),
    ],
)
async def test_cron_display_metadata_preserves_policy_and_ui(
    permission_adapter, display_args, policy, approved, executed
):
    backend = SimpleNamespace(list_jobs=AsyncMock(return_value={"jobs": []}))
    tool = build_cron_flash_tool(backend, context=None, agent_id="flash-test")
    _, rail = permission_adapter({"cron_flash": "allow", "cron_list_jobs": policy})
    confirm = AsyncMock(return_value=PermissionConfirmResponse(approved=approved))
    rail._host.request_permission_confirmation = confirm
    ctx = tool_context("cron_flash", {"list": {}, **display_args})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        await register_events(native_ctx)
        result = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        assert backend.list_jobs.await_count == int(executed), str(result)
        assert confirm.await_count == int(policy == "ask"), str(result)
        if executed:
            assert "jobs" in str(result)
            assert_display(ctx.session, display_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,approved,installed",
    [
        ("allow", True, True),
        ("deny", True, False),
        ("ask", True, True),
        ("ask", False, False),
    ],
)
async def test_skill_display_metadata_preserves_results_and_install_approval(
    permission_adapter, skill_tool, display_args, policy, approved, installed
):
    tool, search, install = skill_tool
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": policy})
    confirm = AsyncMock(return_value=PermissionConfirmResponse(approved=approved))
    rail._host.request_permission_confirmation = confirm
    ctx = tool_context("search_skill", {"query": "fixture", **display_args})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        await register_events(native_ctx)
        results = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        result = results[0][0]
        assert result["success"] is True, str(results)
        assert result["items"][0]["identifier"] == "fixture-skill"
        assert result["install"]["attempted"] == installed
        assert install.await_count == int(installed)
        assert confirm.await_count == int(policy == "ask")
        assert_display(ctx.session, display_args)
        assert state_for(ctx.session, "call-1")["args"] == {"query": "fixture"}
        # The event rail has removed metadata from the original call before replay.
        replay = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        assert replay[0][0] == result
        search.assert_awaited_once()
        assert install.await_count == int(installed)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["search_only", "empty", "disabled"])
async def test_skill_display_metadata_without_install_gate(
    permission_adapter, skill_tool, display_args, scenario
):
    tool, search, install = skill_tool
    _, rail = permission_adapter(
        {"search_skill": "allow", "install_skill": "ask"},
    )
    if scenario == "disabled":
        rail.update_config({**rail._static_config, "enabled": False})
    args = {"query": "fixture", **display_args}
    if scenario == "search_only":
        args["install"] = False
    elif scenario == "empty":
        search.return_value = {"success": True, "items": []}
    confirm = AsyncMock(side_effect=AssertionError("unexpected approval"))
    rail._host.request_permission_confirmation = confirm
    ctx = tool_context("search_skill", args)
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        await register_events(native_ctx)
        results = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        assert results[0][0]["success"] is True, str(results)
        search.assert_awaited_once()
        confirm.assert_not_awaited()
        assert install.await_count == int(scenario == "disabled")
        assert_display(ctx.session, display_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [{}, {"query": "other"}, {"source": "builtin"}, {"limit": 1}, {"install": False}],
)
async def test_skill_resume_compares_business_args(
    permission_adapter, skill_tool, changed
):
    tool, search, install = skill_tool
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": "ask"})
    ctx = tool_context("search_skill", {"query": "fixture", "call_goal": "查找技能"})
    async with sdk_tools(rail, ctx.session, [tool]) as (_, native_ctx):
        call = ctx.inputs.tool_call
        original_args = call.arguments
        assert isinstance(
            await rail.resolve_interrupt(native_ctx, call, None), InterruptResult
        )
        assert call.arguments == original_args
        resumed_args = {"query": "fixture", "callGoal": "安装技能", **changed}
        resumed = call.model_copy(update={"arguments": json.dumps(resumed_args)})
        decision = await rail.resolve_interrupt(native_ctx, resumed, {"approved": True})
        if changed.get("install") is False:
            # Explicit search-only execution does not require installation approval.
            assert isinstance(decision, ApproveResult)
            result = await tool.invoke(
                resumed_args, session=ctx.session, tool_call_id=call.id
            )
            assert result["success"] is False
            install.assert_not_awaited()
        elif changed:
            assert isinstance(decision, RejectResult)
            install.assert_not_awaited()
        else:
            assert isinstance(decision, ApproveResult)
            result = await tool.invoke(
                resumed_args, session=ctx.session, tool_call_id=call.id
            )
            assert result["success"] is True
            install.assert_awaited_once()
        search.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["a", "b"])
async def test_cron_resume_ignores_display_but_binds_target(permission_adapter, job_id):
    _, rail = permission_adapter({"cron_flash": "allow", "cron_delete_job": "ask"})
    ctx = tool_context(
        "cron_flash", {"remove": {"job_id": "a"}, "call_goal": "删除任务"}
    )
    call = ctx.inputs.tool_call
    assert isinstance(await rail.resolve_interrupt(ctx, call, None), InterruptResult)
    resumed = call.model_copy(
        update={"arguments": json.dumps({"remove": {"job_id": job_id}})}
    )
    decision = await rail.resolve_interrupt(ctx, resumed, {"approved": True})
    assert isinstance(decision, ApproveResult if job_id == "a" else RejectResult)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"list": {}, "run": {}}, {"list": {}, "unknown": 1}])
async def test_display_metadata_does_not_hide_invalid_cron_args(
    permission_adapter, args
):
    _, rail = permission_adapter({"cron_flash": "allow"})
    ctx = tool_context("cron_flash", {**args, "call_goal": "查看任务"})
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), RejectResult
    )


@pytest.mark.asyncio
async def test_skill_execution_still_rejects_changed_business_args(
    permission_adapter, skill_tool
):
    tool, search, install = skill_tool
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": "allow"})
    ctx = tool_context("search_skill", {"query": "fixture", "call_goal": "查找技能"})
    async with sdk_tools(rail, ctx.session, [tool]) as (_, native_ctx):
        assert isinstance(
            await rail.resolve_interrupt(native_ctx, ctx.inputs.tool_call, None),
            ApproveResult,
        )
        result = await tool.invoke(
            {"query": "other", "call_goal": "查找技能"},
            session=ctx.session,
            tool_call_id="call-1",
        )
        assert result["success"] is False
        assert "Installation approval mismatch" in result["detail"]
        search.assert_awaited_once()
        install.assert_not_awaited()


def test_projection_removes_only_top_level_display_metadata():
    args = {
        "action": "create",
        "display_name": "业务名称",
        "tasks": [{"content": "保留任务内容", "call_goal": "嵌套数据"}],
        "call_goal": "创建待办",
        "callGoal": "展示别名",
    }
    original = deepcopy(args)
    (operation,) = project_operations("todo", args, {})
    assert operation.args == {"display_name": "业务名称", "tasks": args["tasks"]}
    assert args == original
