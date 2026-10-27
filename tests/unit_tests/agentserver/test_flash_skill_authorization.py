# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Installation HITL and Flash's use of the authoritative session grant store."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_file_skill_grant_store as grants
import test_flash_permission_actions as actions
from flash_sdk import sdk_tools

permission_adapter = actions.permission_adapter
tool_context = actions.tool_context
stores = grants.stores
manifest = grants.manifest
activate = grants.activate


@pytest.fixture
def skill_tool(monkeypatch):
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
    return toolkit.get_tools()[0], search, toolkit.install_skill


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_install_asks_after_search_before_download(
    permission_adapter, skill_tool, approved
):
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    tool, search, install = skill_tool
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": "ask"})
    entered, answer = asyncio.Event(), asyncio.Event()

    async def confirm(request):
        assert request.tool_call.name == "install_skill"
        assert request.tool_call.id == "call-1"
        search.assert_awaited_once()
        install.assert_not_awaited()
        entered.set()
        await answer.wait()
        return PermissionConfirmResponse(approved=approved)

    rail._host.request_permission_confirmation = confirm
    ctx = tool_context("search_skill", {"query": "fixture"})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        task = asyncio.create_task(
            manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        )
        await asyncio.wait_for(entered.wait(), 5)
        install.assert_not_awaited()
        answer.set()
        result = await task
        assert install.await_count == int(approved), str(result)
        assert "fixture-skill" in str(result)
        search.assert_awaited_once()
        # Replay of the same SDK call cannot download twice.
        await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        assert install.await_count == int(approved)


@pytest.mark.asyncio
@pytest.mark.parametrize("install_flag,items", [(False, True), (True, False)])
async def test_pure_or_empty_search_needs_no_install_approval(
    permission_adapter, skill_tool, install_flag, items
):
    tool, search, install = skill_tool
    if not items:
        search.return_value = {"success": True, "items": []}
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": "ask"})
    confirm = AsyncMock(side_effect=AssertionError("unexpected installation approval"))
    rail._host.request_permission_confirmation = confirm
    ctx = tool_context("search_skill", {"query": "fixture", "install": install_flag})
    async with sdk_tools(rail, ctx.session, [tool]) as (manager, native_ctx):
        await manager.execute(native_ctx, ctx.inputs.tool_call, ctx.session)
        search.assert_awaited_once()
        confirm.assert_not_awaited()
        install.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_install_resume_reuses_search_and_rejects_stale_approval(
    permission_adapter, skill_tool, monkeypatch
):
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.rails.interrupt.interrupt_base import (
        ApproveResult,
        InterruptResult,
        RejectResult,
    )

    tool, search, install = skill_tool
    monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda **_: tool)
    _, rail = permission_adapter({"search_skill": "allow", "install_skill": "ask"})
    ctx = tool_context("search_skill", {"query": "fixture"})
    ctx.agent.ability_manager = SimpleNamespace(get=lambda _: tool.card)
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), InterruptResult
    )
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, {"approved": True}),
        ApproveResult,
    )
    await tool.invoke(
        {"query": "fixture"}, session=ctx.session, tool_call_id="call-1"
    )
    search.assert_awaited_once()
    install.assert_awaited_once()
    changed = ctx.inputs.tool_call.model_copy(
        update={"arguments": '{"query":"different"}'}
    )
    assert isinstance(
        await rail.resolve_interrupt(ctx, changed, {"approved": True}), RejectResult
    )
    install.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_file_grants_reopen_revoke_and_never_cross_session(
    permission_adapter, stores, manifest, monkeypatch
):
    from openjiuwen.harness.security import skill_authorization
    from openjiuwen.harness.security.models import PermissionLevel

    from jiuwenswarm.agents.harness.common.rails.skill_authorization_rail import (
        SkillAuthorizationRail,
    )

    first, reopened = stores
    manifest = replace(manifest, overlay={"tools": {"web_search": "allow"}})
    activate(first, manifest)
    skill_authorization.set_skill_grant_store(reopened)
    monkeypatch.setattr(
        skill_authorization, "get_skill_authorization_context", lambda: None
    )
    _, rail = permission_adapter(
        {"web_flash": "allow", "web_search": "ask"},
        skill_authorization={"enabled": True},
    )
    gate = SkillAuthorizationRail(engine=rail._engine, grant_store=reopened)
    rail.set_skill_scope_resolver(gate.resolve_scope)
    for session_id, expected in [
        ("session", PermissionLevel.ALLOW),
        ("other", PermissionLevel.ASK),
    ]:
        ctx = tool_context(
            "web_flash", {"search": {"query": "fixture"}}, session_id=session_id
        )
        rail._refresh_permissions_for_tool_call(ctx)
        result = await rail._engine.check_permission(
            "web_flash", {"search": {"query": "fixture"}}
        )
        assert result.permission == expected
    first.revoke_grants("session", "main")
    ctx = tool_context(
        "web_flash", {"search": {"query": "fixture"}}, session_id="session"
    )
    rail._refresh_permissions_for_tool_call(ctx)
    assert (
        await rail._engine.check_permission(
            "web_flash", {"search": {"query": "fixture"}}
        )
    ).permission == PermissionLevel.ASK
