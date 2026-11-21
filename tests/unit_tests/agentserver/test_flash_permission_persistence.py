# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash/OfficeAce does not persist allow-always; approvals stay session-scoped."""

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
async def test_allow_always_does_not_write_policy(policy_store, action, channel):
    rail = policy_store.build()
    original = policy_store.path.read_bytes()
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
    assert policy_store.path.read_bytes() == original
    assert policy_store.load()["tools"] == {
        "cron_flash": "allow",
        "write_file": "deny",
    }


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
async def test_persist_hook_not_called_for_allow_always(policy_store, action):
    rail = policy_store.build()
    persist = Mock(return_value=True)
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
    persist.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_tool", ["cron_flash", "cron_flash.run"])
async def test_revoked_rule_still_blocks_after_approval_attempt(
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
    rail.update_config(current)
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
