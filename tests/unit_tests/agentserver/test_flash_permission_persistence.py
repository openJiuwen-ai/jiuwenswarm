# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Permanent approval must survive a new rail and session without widening scope."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)
from openjiuwen.harness.security.host import ToolPermissionHost
from openjiuwen.harness.security.models import PermissionConfirmResponse
from test_flash_permission_actions import tool_context

from jiuwenswarm.agents.harness.flash.permissions.flash_permission_rail import (
    FlashPermissionRail,
)

ACTION_ARGS = {
    "run": {"job_id": "fixture-job"},
    "wake": {"text": "fixture message"},
    "status": {},
}


@pytest.fixture(params=ACTION_ARGS)
def action(request):
    return request.param


@pytest.fixture
def policy_store(tmp_path):
    path = tmp_path / "agent.yaml"
    config = {
        "name": "fixture-agent",
        "permissions": {
            "enabled": True,
            "schema": "tiered_policy",
            "defaults": {"*": "allow"},
            "tools": {"cron_flash": "allow", "write_file": "deny"},
        },
    }
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    def load():
        return yaml.safe_load(path.read_text(encoding="utf-8"))["permissions"]

    def build():
        return FlashPermissionRail.create(
            config=load(),
            host=ToolPermissionHost(
                get_permissions_snapshot=load, permission_yaml_path=path
            ),
        )

    return SimpleNamespace(path=path, load=load, build=build)


def action_context(action, **options):
    return tool_context(
        "cron_flash",
        {action: ACTION_ARGS[action], "call_goal": "操作定时任务"},
        **options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["native", "hosted"])
async def test_permanent_action_approval_survives_reload(policy_store, action, channel):
    rail = policy_store.build()
    original = policy_store.load()
    ctx = action_context(action)
    payload = {"approved": True, "auto_confirm": True, "persist_allow": True}
    if channel == "hosted":
        rail._host.request_permission_confirmation = AsyncMock(
            return_value=PermissionConfirmResponse(**payload)
        )
        decision = await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
        rail._host.request_permission_confirmation.assert_awaited_once()
    else:
        assert isinstance(
            await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None),
            InterruptResult,
        )
        decision = await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, payload)
    assert isinstance(decision, ApproveResult)
    expected = deepcopy(original)
    expected["tools"][f"cron_flash.{action}"] = "allow"
    assert policy_store.load() == expected
    assert yaml.safe_load(policy_store.path.read_text())["name"] == "fixture-agent"

    reopened = policy_store.build()
    next_ctx = action_context(action, session_id="new-session", call_id="next-call")
    assert isinstance(
        await reopened.resolve_interrupt(next_ctx, next_ctx.inputs.tool_call, None),
        ApproveResult,
    )
    for other in ACTION_ARGS.keys() - {action}:
        other_ctx = action_context(other, session_id="new-session", call_id=other)
        assert isinstance(
            await reopened.resolve_interrupt(
                other_ctx, other_ctx.inputs.tool_call, None
            ),
            InterruptResult,
        )

    # Removing the saved rule takes effect on the next call of the same rail.
    policy_store.path.write_text(yaml.safe_dump({"permissions": original}))
    revoked = action_context(action, session_id="new-session", call_id="revoked")
    assert isinstance(
        await reopened.resolve_interrupt(revoked, revoked.inputs.tool_call, None),
        InterruptResult,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"approved": True},
        {"approved": True, "auto_confirm": True, "persist_allow": False},
        {"approved": False, "auto_confirm": True, "persist_allow": True},
    ],
)
async def test_nonpermanent_or_rejected_approval_does_not_write(
    policy_store, action, payload
):
    rail = policy_store.build()
    original = policy_store.path.read_bytes()
    ctx = action_context(action)
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), InterruptResult
    )
    decision = await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, payload)
    assert isinstance(decision, ApproveResult if payload["approved"] else RejectResult)
    assert policy_store.path.read_bytes() == original
    fresh = action_context(action, session_id="new-session")
    assert isinstance(
        await policy_store.build().resolve_interrupt(
            fresh, fresh.inputs.tool_call, None
        ),
        InterruptResult,
    )


@pytest.mark.asyncio
async def test_failed_action_persistence_keeps_policy_unchanged(policy_store, action):
    rail = policy_store.build()
    original = policy_store.load()
    persist = Mock(return_value=False)
    rail._host.persist_allow_rule = persist
    ctx = action_context(action)
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), InterruptResult
    )
    decision = await rail.resolve_interrupt(
        ctx,
        ctx.inputs.tool_call,
        {"approved": True, "auto_confirm": True, "persist_allow": True},
    )
    assert isinstance(decision, ApproveResult)
    persist.assert_called_once()
    assert persist.call_args.args[0]["tools"][f"cron_flash.{action}"] == "allow"
    assert rail._engine.config == original
    assert policy_store.load() == original
    fresh = action_context(action, session_id="new-session")
    assert isinstance(
        await policy_store.build().resolve_interrupt(
            fresh, fresh.inputs.tool_call, None
        ),
        InterruptResult,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_tool", ["cron_flash", "cron_flash.run"])
async def test_permanent_approval_cannot_replace_a_revoked_rule(
    policy_store, denied_tool
):
    rail = policy_store.build()
    ctx = action_context("run")
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), InterruptResult
    )
    current = policy_store.load()
    current["tools"][denied_tool] = "deny"
    policy_store.path.write_text(yaml.safe_dump({"permissions": current}))
    decision = await rail.resolve_interrupt(
        ctx,
        ctx.inputs.tool_call,
        {"approved": True, "auto_confirm": True, "persist_allow": True},
    )
    assert isinstance(decision, RejectResult)
    assert policy_store.load() == current


@pytest.mark.asyncio
async def test_disabled_permission_does_not_request_or_persist_approval(policy_store):
    config = policy_store.load()
    config["enabled"] = False
    policy_store.path.write_text(yaml.safe_dump({"permissions": config}))
    rail = policy_store.build()
    confirm, persist = AsyncMock(), Mock()
    rail._host.request_permission_confirmation = confirm
    rail._host.persist_allow_rule = persist
    ctx = action_context("run")
    assert isinstance(
        await rail.resolve_interrupt(ctx, ctx.inputs.tool_call, None), ApproveResult
    )
    confirm.assert_not_awaited()
    persist.assert_not_called()
    assert policy_store.load() == config
