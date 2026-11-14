# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Permission failures must stop real callbacks and never reach a tool body."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from agent_shield.tool_permission import get_permission_manager
from flash_sdk import sdk_tools
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
)
from jiuwenswarm.agents.harness.flash.permissions.flash_permission_rail import (
    FlashPermissionRail,
)


def _probe():
    body = AsyncMock(return_value="executed")
    tool = LocalFunction(
        card=ToolCard(
            name="permission_probe",
            id="permission_probe",
            description="probe",
            input_params={"type": "object", "properties": {}},
        ),
        func=body,
    )
    session = SimpleNamespace(
        session_id="callback-session",
        get_state=lambda _: None,
        update_state=lambda _: None,
    )
    call = ToolCall(id="call", type="function", name="permission_probe", arguments="{}")
    return body, tool, session, call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rail_type",
    [PermissionInterruptRail, SkillAuthorizationPermissionRail, FlashPermissionRail],
)
@pytest.mark.parametrize(
    "failure", ["missing_key", "metadata_failure", "scene_failure"]
)
async def test_native_ordinary_tool_never_executes_without_complete_permission_validation(
    rail_type, failure
):
    config = {
        "enabled": True,
        "tools": {"permission_probe": "allow"},
        "defaults": {"*": "allow"},
    }
    rail = rail_type(config=config, permission_key="callback-test-" + uuid4().hex)
    if failure == "missing_key":
        get_permission_manager().remove_config(rail.permission_key)
    elif failure == "metadata_failure":
        # The SDK returned ALLOW, but execution routing still failed afterwards.
        rail.outbound_checks_enabled = Mock(
            side_effect=RuntimeError("routing unavailable")
        )
    else:
        rail._host.permission_scene_hook = AsyncMock(
            side_effect=AttributeError("metadata unavailable")
        )
    body, tool, session, call = _probe()
    async with sdk_tools(rail, session, [tool]) as (manager, ctx):
        result = await manager.execute(ctx, call, session)
        assert "tool execution stopped" in str(result)
    body.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_skill_overlay_deny_stops_native_tool():
    """Skill overlay is applied via temp config=; deny must stop the tool body."""
    from openjiuwen.harness.security.skill_authorization import (
        GrantDecision,
        SkillManifest,
        SkillTrustLevel,
        get_skill_grant_store,
    )

    config = {
        "enabled": True,
        "tools": {"permission_probe": "allow"},
        "skill_authorization": {"enabled": True},
    }
    rail = SkillAuthorizationPermissionRail(config=config)
    store = get_skill_grant_store()
    sid = "overlay-deny-session"
    try:
        manifest = SkillManifest(
            "fixture",
            "test",
            None,
            SkillTrustLevel.OTHER,
            "hash",
            skill_md_hash="markdown-hash",
            overlay={"tools": {"permission_probe": "deny"}},
        )
        store.create_pending_grant(sid, "main", manifest, decision=GrantDecision.LOCAL)
        store.activate_pending(sid, "main", manifest.skill_name, manifest)
        rail.set_skill_scope_resolver(lambda _: (sid, "main"))
        body, tool, session, call = _probe()
        session.session_id = sid
        async with sdk_tools(rail, session, [tool]) as (manager, ctx):
            result = await manager.execute(ctx, call, session)
            assert "PERMISSION_DENIED" in str(result)
        body.assert_not_awaited()
    finally:
        store.clear_session(sid)
        rail.uninit(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_publisher",
        "missing_skill_key",
        "storage_failure",
        "config_failure",
        "execution_state_failure",
    ],
)
async def test_subagent_scope_errors_stop_native_tool(monkeypatch, failure):
    from jiuwenswarm.agents.harness.common.rails import subagent_permission_rail
    from jiuwenswarm.agents.harness.common.rails.permissions import published_scopes

    config = {"enabled": True, "skill_authorization": {"enabled": True}}
    store = SimpleNamespace(get_active_skill_execution=lambda *_: "fixture")
    rail = subagent_permission_rail.SubagentPermissionRail(
        agent_scope_id="child",
        permission_key="callback-test-" + uuid4().hex,
        grant_store=store,
        config_provider=lambda: config,
    )
    rail._resolve_scope = Mock(return_value=("callback-session", "child"))
    if failure == "config_failure":
        rail._config_provider = Mock(side_effect=OSError("configuration unavailable"))
    elif failure == "execution_state_failure":
        store.get_active_skill_execution = Mock(return_value=None)
        store.get_active_skill_execution_for_permission = Mock(
            side_effect=OSError("state unavailable")
        )
    elif failure != "missing_publisher":
        error = (
            KeyError("unpublished key")
            if failure == "missing_skill_key"
            else OSError("state unavailable")
        )
        publisher = SimpleNamespace(key_for=Mock(side_effect=error))
        monkeypatch.setattr(
            published_scopes, "find_published_scopes", lambda *_: publisher
        )
    body, tool, session, call = _probe()
    async with sdk_tools(rail, session, [tool]) as (manager, ctx):
        result = await manager.execute(ctx, call, session)
        assert "Permission validation failed" in str(result)
    body.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_rail_factory_preserves_owner_restrictions():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        PermissionRailBuildOptions,
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext,
        TOOL_PERMISSION_CONTEXT,
    )

    config = {
        "enabled": True,
        "tools": {"permission_probe": "allow"},
        "owner_scopes": {
            "web": {"fixture-user": {"tools": {"permission_probe": "deny"}}}
        },
    }

    def factory(**kwargs):
        kwargs["permission_key"] = "callback-test-" + uuid4().hex
        return PermissionInterruptRail(**kwargs)

    rail = build_permission_rail(
        {}, PermissionRailBuildOptions(permission_config=config, rail_factory=factory)
    )
    assert rail is not None
    body, tool, session, call = _probe()
    token = TOOL_PERMISSION_CONTEXT.set(
        PermissionContext(channel_id="web", principal_user_id="fixture-user")
    )
    try:
        async with sdk_tools(rail, session, [tool]) as (manager, ctx):
            result = await manager.execute(ctx, call, session)
            assert "PERMISSION_DENIED" in str(result)
        body.assert_not_awaited()
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)
