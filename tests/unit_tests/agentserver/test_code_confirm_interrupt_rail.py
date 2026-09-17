# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodeConfirmInterruptRail plan-mode switch_mode guard."""

from types import SimpleNamespace
from unittest.mock import MagicMock

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

    tool_call = SimpleNamespace(
        name="switch_mode",
        arguments='{"mode": "normal"}',
    )
    ctx = SimpleNamespace(agent=agent, session=SimpleNamespace())

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

    tool_call = SimpleNamespace(
        name="switch_mode",
        arguments='{"mode": "normal"}',
    )
    ctx = SimpleNamespace(agent=agent, session=SimpleNamespace())

    decision = await rail.resolve_interrupt(
        ctx, tool_call, user_input={"approved": True}
    )

    assert isinstance(decision, RejectResult)
