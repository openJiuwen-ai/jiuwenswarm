# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash actions must retain the underlying operation's authorization."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.permissions import config_loader
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


@pytest.fixture
def permission_adapter(tmp_path):
    def create(tools, **options):
        config = {
            "enabled": True,
            "schema": "tiered_policy",
            "defaults": {"*": "allow"},
            "tools": tools,
            **options,
        }
        token = config_loader.setup_permissions_agent_base(config)
        try:
            effective = config_loader.get_effective_permissions_config()
        finally:
            config_loader.reset_permissions_agent_base(token)
        adapter = JiuwenSwarmFlashAdapter()
        adapter._agent_permissions_body = effective
        adapter._workspace_dir = tmp_path
        rail = adapter._build_permission_rail_for_agent(config={})
        assert rail is not None
        rail._host.permission_scene_hook = None
        rail._host.request_permission_confirmation = None
        return adapter, rail

    return create


def tool_context(name, args, session_id="session-a", call_id="call-1"):
    state = {}
    session = SimpleNamespace(
        session_id=session_id,
        get_session_id=lambda: session_id,
        get_state=state.get,
        update_state=state.update,
    )
    call = ToolCall(id=call_id, name=name, type="function", arguments=json.dumps(args))
    return AgentCallbackContext(
        agent=SimpleNamespace(),
        session=session,
        inputs=ToolCallInputs(tool_call=call, tool_name=name, tool_args=call.arguments),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args,legacy",
    [
        ("web_flash", {"search": {"query": "permission fixture"}}, "web_search"),
        (
            "web_flash",
            {"fetch": {"url": ["https://example.invalid/fixture"]}},
            "mcp_fetch_webpage",
        ),
        (
            "cron_flash",
            {
                "add": {
                    "name": "fixture",
                    "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *"},
                    "description": "fixture",
                }
            },
            "cron_create_job",
        ),
        (
            "todo",
            {"action": "create", "tasks": [{"id": "1", "content": "fixture"}]},
            "todo_create",
        ),
    ],
)
async def test_legacy_deny_blocks_aggregate_before_execution(
    permission_adapter, name, args, legacy
):
    _, rail = permission_adapter({name: "allow", legacy: "deny"})
    ctx = tool_context(name, args)
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
    assert "PERMISSION_DENIED" in str(ctx.inputs.tool_result)


@pytest.mark.asyncio
async def test_install_deny_returns_search_results_without_install(
    permission_adapter, monkeypatch
):
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
    from jiuwenswarm.agents.harness.flash.tools.slim_skill_toolkit import (
        SlimSkillToolkit,
    )

    toolkit = object.__new__(SlimSkillToolkit)
    search = AsyncMock(
        return_value={
            "success": True,
            "items": [
                {
                    "identifier": "fixture-skill",
                    "source": "builtin",
                    "name": "fixture",
                    "installed": False,
                }
            ],
        }
    )
    monkeypatch.setattr(SkillToolkit, "search_skill", search)
    toolkit.install_skill = AsyncMock(return_value={"success": True})
    tool = toolkit.get_tools()[0]
    monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda **_: tool)
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": "deny"})
    ctx = tool_context("search_skill", {"query": "fixture"})
    ctx.agent.ability_manager = SimpleNamespace(get=lambda _: tool.card)
    await rail.before_tool_call(ctx)
    assert not ctx.extra.get("_skip_tool")
    result = await tool.invoke(
        {"query": "fixture"}, session=ctx.session, tool_call_id="call-1"
    )
    assert result["items"][0]["identifier"] == "fixture-skill"
    toolkit.install_skill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,legacy,args",
    [
        ("status", "cron_flash.status", {}),
        ("list", "cron_list_jobs", {}),
        ("get", "cron_get_job", {"job_id": "a"}),
        ("preview", "cron_preview_job", {"job_id": "a"}),
        (
            "add",
            "cron_create_job",
            {
                "name": "a",
                "schedule": {"kind": "cron", "expr": "0 0 9 * * ? *"},
                "description": "fixture",
            },
        ),
        ("update", "cron_update_job", {"job_id": "a", "name": "updated"}),
        ("remove", "cron_delete_job", {"job_id": "a"}),
        ("toggle", "cron_toggle_job", {"job_id": "a", "enabled": True}),
        ("run", "cron_flash.run", {"job_id": "a"}),
        ("wake", "cron_flash.wake", {"text": "fixture"}),
    ],
)
@pytest.mark.parametrize("policy", ["allow", "deny"])
async def test_cron_actions_native_sdk_backend_counts(
    permission_adapter, action, legacy, args, policy, record_property, monkeypatch
):
    from flash_sdk import sdk_tools
    from openjiuwen.harness.security.core import PermissionEngine

    from jiuwenswarm.agents.harness.flash.tools.cron_flash import build_cron_flash_tool

    observed_args = {}
    original_check = PermissionEngine.check_permission

    async def observe(self, tool_name, tool_args):
        observed_args[tool_name] = dict(tool_args)
        return await original_check(self, tool_name, tool_args)

    monkeypatch.setattr(PermissionEngine, "check_permission", observe)

    methods = {
        name: AsyncMock(return_value={"job_count": 0})
        for name in (
            "status",
            "list_jobs",
            "get_job",
            "preview_job",
            "create_job",
            "update_job",
            "delete_job",
            "toggle_job",
            "run_now",
            "wake",
        )
    }
    backend = SimpleNamespace(**methods)
    tool = build_cron_flash_tool(backend, context=None, agent_id="flash-test")
    _, rail = permission_adapter({"cron_flash": "allow", legacy: policy})
    ctx = tool_context("cron_flash", {action: args})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        results = await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        count = sum(method.await_count for method in methods.values())
        record_property(
            "permission_matrix",
            json.dumps(
                {
                    "flash_tool": "cron_flash",
                    "action": action,
                    "legacy_operation": legacy,
                    "operation_args": observed_args[legacy],
                    "policy_key": legacy,
                    "resource_targets": args.get("job_id"),
                    "expected_decision": policy.upper(),
                    "observed_decision": type(
                        native_ctx.extra.get("_interrupt_decision")
                    ).__name__,
                    "side_effect_count": count,
                    "scope": ctx.session.session_id,
                    "policy_source": "agent_template",
                    "enabled": True,
                },
                sort_keys=True,
            ),
        )
        assert count == (1 if policy == "allow" else 0), str(results)
        if policy == "deny":
            assert "PERMISSION_DENIED" in str(results)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "wake", "status"])
async def test_unmapped_cron_requires_approval(permission_adapter, action):
    from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult

    _, rail = permission_adapter({"cron_flash": "allow"})
    ctx = tool_context("cron_flash", {action: {}})
    decision = await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
    assert isinstance(decision, InterruptResult)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_web_hosted_ask_waits_and_executes_once(
    permission_adapter, monkeypatch, approved
):
    from flash_sdk import sdk_tools
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    from jiuwenswarm.agents.harness.flash.tools.web_flash import (
        JiuwenHarnessFetchWebpageTool,
        build_web_flash_tool,
    )

    fetch = AsyncMock(return_value="fixture result")
    monkeypatch.setattr(JiuwenHarnessFetchWebpageTool, "invoke", fetch)
    entered, answer = asyncio.Event(), asyncio.Event()

    async def confirm(request):
        assert request.tool_call.name == "web_flash"
        entered.set()
        await answer.wait()
        return PermissionConfirmResponse(approved=approved)

    _, rail = permission_adapter({"web_flash": "allow", "mcp_fetch_webpage": "ask"})
    rail._host.request_permission_confirmation = confirm
    tool = build_web_flash_tool(agent_id="flash-test")
    ctx = tool_context("web_flash", {"fetch": {"url": ["https://example.invalid/a"]}})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        task = asyncio.create_task(
            manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        )
        await asyncio.wait_for(entered.wait(), 5)
        fetch.assert_not_awaited()
        answer.set()
        await task
        assert fetch.await_count == int(approved)


@pytest.mark.asyncio
async def test_native_approval_binds_action_args_and_rechecks_policy(
    permission_adapter,
):
    from openjiuwen.harness.rails.interrupt.interrupt_base import (
        ApproveResult,
        InterruptResult,
        RejectResult,
    )

    _, rail = permission_adapter({"web_flash": "allow", "mcp_fetch_webpage": "ask"})
    ctx = tool_context("web_flash", {"fetch": {"url": ["https://example.invalid/a"]}})
    first = await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
    assert isinstance(first, InterruptResult)
    decision = await rail.resolve_interrupt(
        ctx, ctx.inputs.tool_call, {"approved": True}
    )
    assert isinstance(decision, ApproveResult)
    next_call = ctx.inputs.tool_call.model_copy(
        update={
            "id": "next",
            "arguments": json.dumps({"fetch": {"url": ["https://example.invalid/b"]}}),
        }
    )
    assert isinstance(
        await rail.resolve_interrupt(ctx, next_call, None), InterruptResult
    )
    rail.update_config({**rail._static_config, "tools": {"mcp_fetch_webpage": "deny"}})
    assert isinstance(
        await rail.resolve_interrupt(ctx, next_call, {"approved": True}), RejectResult
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        (
            "web_flash",
            {"search": {"query": "a"}, "fetch": {"url": ["https://example.invalid"]}},
        ),
        ("cron_flash", {"list": {}, "run": {"job_id": "a"}}),
        ("todo", {"action": "purge"}),
    ],
)
async def test_ambiguous_or_unknown_action_is_denied(permission_adapter, name, args):
    _, rail = permission_adapter({name: "allow"})
    ctx = tool_context(name, args)
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_disabled_permission_preserves_existing_execution(permission_adapter):
    from openjiuwen.harness.security.models import PermissionLevel

    _, rail = permission_adapter({"web_flash": "deny", "web_search": "deny"})
    rail.update_config({**rail._static_config, "enabled": False})
    assert (
        await rail._engine.check_permission("web_flash", {"search": {"query": "a"}})
    ).permission == PermissionLevel.ALLOW


@pytest.mark.asyncio
async def test_same_card_search_allow_fetch_deny(permission_adapter):
    from openjiuwen.harness.security.models import PermissionLevel

    _, rail = permission_adapter(
        {"web_flash": "allow", "web_search": "allow", "mcp_fetch_webpage": "deny"}
    )
    assert (
        await rail._engine.check_permission("web_flash", {"search": {"query": "a"}})
    ).permission == PermissionLevel.ALLOW
    assert (
        await rail._engine.check_permission(
            "web_flash", {"fetch": {"url": ["https://example.invalid/a"]}}
        )
    ).permission == PermissionLevel.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,legacy,extra",
    [
        ("create", "todo_create", {"tasks": [{"id": "a"}]}),
        ("list", "todo_list", {}),
        ("get", "todo_get", {"id": "a"}),
        ("modify", "todo_modify", {"modify_action": "update", "todos": [{"id": "a"}]}),
        ("modify", "todo_insert", {"modify_action": "append", "todos": [{"id": "a"}]}),
        (
            "modify",
            "todo_complete",
            {"modify_action": "update", "todos": [{"id": "a", "status": "completed"}]},
        ),
        ("modify", "todo_remove", {"modify_action": "delete", "ids": ["a"]}),
    ],
)
async def test_todo_preserves_operation_denials(
    permission_adapter, action, legacy, extra
):
    _, rail = permission_adapter({"todo": "allow", legacy: "deny"})
    ctx = tool_context("todo", {"action": action, **extra})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_outer_deny_cannot_be_relaxed_by_legacy_allow(permission_adapter):
    _, rail = permission_adapter({"web_flash": "deny", "web_search": "allow"})
    ctx = tool_context("web_flash", {"search": {"query": "fixture"}})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_projected_always_allow_uses_stock_persistence(permission_adapter):
    from openjiuwen.harness.rails.interrupt.interrupt_base import (
        ApproveResult,
        InterruptResult,
    )

    _, rail = permission_adapter({"web_flash": "allow", "mcp_fetch_webpage": "ask"})
    snapshots = []
    rail._host.persist_allow_rule = lambda cfg: snapshots.append(cfg) or True
    ctx = tool_context("web_flash", {"fetch": {"url": ["https://example.invalid/a"]}})
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), InterruptResult
    )
    decision = await rail.resolve_interrupt(
        ctx,
        ctx.inputs.tool_call,
        {
            "approved": True,
            "auto_confirm": True,
            "persist_allow": True,
        },
    )
    assert isinstance(decision, ApproveResult)
    assert len(snapshots) == 1
    assert (
        snapshots[0].get("approval_overrides")
        or snapshots[0]["tools"]["mcp_fetch_webpage"] == "allow"
    )


@pytest.mark.asyncio
async def test_hosted_approval_policy_revocation_blocks_side_effect(permission_adapter):
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    _, rail = permission_adapter({"web_flash": "allow", "mcp_fetch_webpage": "ask"})

    async def confirm(_):
        rail.update_config(
            {**rail._static_config, "tools": {"mcp_fetch_webpage": "deny"}}
        )
        return PermissionConfirmResponse(approved=True)

    rail._host.request_permission_confirmation = confirm
    ctx = tool_context("web_flash", {"fetch": {"url": ["https://example.invalid/a"]}})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "", "default"])
async def test_projected_call_needs_trusted_sdk_session(permission_adapter, session_id):
    _, rail = permission_adapter({"web_flash": "allow", "web_search": "allow"})
    ctx = tool_context(
        "web_flash", {"search": {"query": "fixture"}}, session_id=session_id
    )
    if session_id is None:
        ctx.session = None
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["mcp_paid_search", "mcp_free_search"])
async def test_explicit_provider_deny_is_preserved(permission_adapter, provider):
    _, rail = permission_adapter(
        {"web_flash": "allow", "web_search": "allow", provider: "deny"}
    )
    ctx = tool_context("web_flash", {"search": {"query": "fixture"}})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool")


@pytest.mark.asyncio
async def test_scene_allow_does_not_bypass_aggregate_action_ask():
    from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
    from openjiuwen.harness.security.host import ToolPermissionHost

    from jiuwenswarm.agents.harness.flash.permissions.flash_permission_rail import (
        FlashPermissionRail,
    )

    hook = AsyncMock(return_value=("approve",))
    rail = FlashPermissionRail.create(
        config={
            "enabled": True,
            "schema": "tiered_policy",
            "defaults": {"*": "allow"},
            "tools": {"web_flash": "allow", "mcp_fetch_webpage": "ask"},
        },
        host=ToolPermissionHost(permission_scene_hook=hook),
    )
    ctx = tool_context("web_flash", {"fetch": {"url": ["https://example.invalid/a"]}})
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), InterruptResult
    )
    hook.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_first", [True, False])
async def test_todo_close_nudge_skips_action_projection(permission_adapter, permission_first):
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TODO_CLOSE_NUDGE_ID_PREFIX,
    )
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail

    _, rail = permission_adapter({"todo": "allow"})
    todo_rail = FlashTodoRail()
    todo_rail._open_todos = [("deliver", {"content": "发送 PPTX"})]
    ctx = tool_context("todo", {}, call_id=f"{TODO_CLOSE_NUDGE_ID_PREFIX}abc")
    order = [rail, todo_rail] if permission_first else [todo_rail, rail]
    for each in order:
        await each.before_tool_call(ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "[TODO_CLOSE_REQUIRED]" in str(ctx.inputs.tool_result)
    assert "PERMISSION_DENIED" not in str(ctx.inputs.tool_result)


@pytest.mark.asyncio
async def test_forged_nudge_id_on_other_tool_still_checked(permission_adapter):
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TODO_CLOSE_NUDGE_ID_PREFIX,
    )

    _, rail = permission_adapter({"todo": "allow"})
    ctx = tool_context("cron_flash", {"list": {}, "run": {"job_id": "a"}}, call_id=f"{TODO_CLOSE_NUDGE_ID_PREFIX}x")
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
    assert "PERMISSION_DENIED" in str(ctx.inputs.tool_result)
