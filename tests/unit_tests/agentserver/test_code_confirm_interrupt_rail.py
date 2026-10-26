# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodeConfirmInterruptRail plan-mode switch_mode guard."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult

from jiuwenswarm.agents.harness.code.rails.code_confirm_interrupt_rail import (
    CodeConfirmInterruptRail,
)


@pytest.mark.asyncio
async def test_switch_mode_exit_rejected_in_plan_without_confirm_ui() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="test-plan")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    agent.system_prompt_builder = SimpleNamespace(language="cn")
    rail.init(agent)

    tool_call = SimpleNamespace(
        name="switch_mode",
        arguments='{"mode": "normal"}',
    )
    ctx = SimpleNamespace(agent=SimpleNamespace(), session=SimpleNamespace())

    decision = await rail.resolve_interrupt(ctx, tool_call, user_input=None)

    assert isinstance(decision, RejectResult)
    assert "switch_mode" in str(decision.tool_result)


@pytest.mark.asyncio
async def test_switch_mode_exit_rejected_even_after_user_approves_confirm() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="test-plan")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    agent.system_prompt_builder = SimpleNamespace(language="cn")
    rail.init(agent)

    tool_call = SimpleNamespace(
        name="switch_mode",
        arguments='{"mode": "normal"}',
    )
    ctx = SimpleNamespace(agent=SimpleNamespace(), session=SimpleNamespace())

    decision = await rail.resolve_interrupt(
        ctx, tool_call, user_input={"approved": True}
    )

    assert isinstance(decision, RejectResult)


@pytest.mark.asyncio
async def test_switch_mode_enter_plan_does_not_load_state_from_react_agent() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    stateful_agent = MagicMock()
    rail.init(stateful_agent)
    ctx = SimpleNamespace(agent=SimpleNamespace(), session=SimpleNamespace())
    tool_call = SimpleNamespace(name="switch_mode", arguments='{"mode": "plan"}')

    decision = await rail.resolve_interrupt(ctx, tool_call, user_input=None)

    stateful_agent.load_state.assert_not_called()
    assert decision is not None


def test_uninit_releases_stateful_agent() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    agent = MagicMock()
    rail.init(agent)

    rail.uninit(agent)

    assert rail._agent is None


def test_init_and_uninit_preserve_parent_lifecycle() -> None:
    rail = CodeConfirmInterruptRail(tool_names=["switch_mode"])
    agent = MagicMock()
    parent = CodeConfirmInterruptRail.__bases__[0]

    with patch.object(parent, "init") as parent_init, patch.object(
        parent, "uninit"
    ) as parent_uninit:
        rail.init(agent)
        rail.uninit(agent)

    parent_init.assert_called_once_with(agent)
    parent_uninit.assert_called_once_with(agent)
    assert rail._agent is None
