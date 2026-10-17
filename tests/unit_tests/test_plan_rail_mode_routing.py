# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gate tests for P4 rail mode routing onto the new canonical mode strings.

The new three-segment canonical modes (``agent.work.plan`` /
``agent.code.plan`` / ``team.work.plan`` / ``team.code.plan`` ...) reach the
swarm rail builder with the *new* string. The old routing predicate
``ctx.mode == TEAM_PLAN_NORMAL_MODE`` would silently miss the new
``team.work.plan`` leader and leave the ``WorkAgentModeRail`` un-mounted,
breaking team plan. P4.1 swaps that comparison for the
:func:`is_team_plan_mode` predicate (whose set was extended in P1.5 to
include the new team-plan variants).

These tests pin the routing decision itself: each new canonical mode lands
on the correct rail, and the existing ``switch_mode``-exit block survives
the swap.
"""

# pylint: disable=protected-access

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.swarm import register_swarm_providers
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import code_rails


def _ctx(mode: str, *, role: str = "leader") -> SwarmBuildContext:
    """Build a minimal swarm context for the rail router."""
    return SwarmBuildContext(mode=mode, role=role, config={"preferred_language": "zh"})


def test_team_work_plan_leader_routes_to_work_rail() -> None:
    """``team.work.plan`` + ``role="leader"`` routes to WorkAgentModeRail.

    Guards :354 against regression to ``ctx.mode == TEAM_PLAN_NORMAL_MODE``
    string compare: ``team.work.plan`` is the new canonical for the old
    ``team.plan.normal``, so a string compare would skip this branch and
    silently drop the Team Plan leader rail. 两条原 ``test_work_plan_mode_routes_to_work_rail``
    与 ``test_team_work_plan_leader_routes_to_work_rail`` 的 ctx 完全等价
    （均 ``team.work.plan`` + leader），合并成一条避免重复。
    """
    register_swarm_providers()
    ctx = _ctx("team.work.plan", role="leader")
    rail = code_rails.build_code_agent_mode({}, ctx)

    from jiuwenswarm.agents.harness.work.rails.work_agent_mode_rail import (
        WorkAgentModeRail,
    )

    assert isinstance(rail, WorkAgentModeRail)


def test_team_code_plan_leader_routes_to_code_rail() -> None:
    """``team.code.plan`` + ``role="leader"`` routes to CodeAgentModeRail.

    The Team Plan Code leader gets a CodeAgentModeRail (not WorkAgentModeRail)
    with the Team-exit notification attached. This guards the second half of
    the routing path: new team-code canonical strings must reach the code
    rail branch via :func:`is_code_profile_mode` (extended in P1.5).
    """
    register_swarm_providers()
    ctx = _ctx("team.code.plan", role="leader")
    rail = code_rails.build_code_agent_mode({}, ctx)

    from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import (
        CodeAgentModeRail,
    )

    # Strict type check: WorkAgentModeRail subclasses CodeAgentModeRail, so
    # isinstance would be a false positive that hides a work-profile regression.
    assert type(rail) is CodeAgentModeRail
    # The Team Plan Code leader gets the Team-exit notification attached.
    assert rail._exit_plan_notification is not None


def test_code_plan_mode_routes_to_code_rail() -> None:
    """``agent.code.plan`` routes to CodeAgentModeRail (not WorkAgentModeRail)."""
    register_swarm_providers()
    # Non-leader agent.code.plan: still routes via is_code_profile_mode branch.
    ctx = _ctx("agent.code.plan", role="teammate")
    rail = code_rails.build_code_agent_mode({}, ctx)

    from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import (
        CodeAgentModeRail,
    )

    assert isinstance(rail, CodeAgentModeRail)


def test_agent_work_normal_returns_none() -> None:
    """``agent.work.normal`` is neither team-plan nor code-profile → no rail.

    Guards against over-routing: the new non-plan work canonical must not
    accidentally land on either rail.
    """
    register_swarm_providers()
    ctx = _ctx("agent.work.normal", role="leader")
    assert code_rails.build_code_agent_mode({}, ctx) is None


@pytest.mark.asyncio
async def test_new_plan_still_blocks_switch_mode_exit() -> None:
    """A WorkAgentModeRail built for the new canonical still blocks
    ``switch_mode`` from exiting plan mode.

    The rail itself is unchanged; only the routing predicate was swapped.
    This re-checks the existing block on a rail produced via the new
    canonical path, so a regression that bypasses the rail (e.g. path-2
    pre-normalization) would surface here as a missing block.
    """
    register_swarm_providers()
    ctx = _ctx("team.work.plan", role="leader")
    rail = code_rails.build_code_agent_mode({}, ctx)

    from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import (
        CodeAgentModeRail,
    )
    from jiuwenswarm.agents.harness.work.rails.work_agent_mode_rail import (
        WorkAgentModeRail,
    )
    from openjiuwen.harness.rails.agent_mode_rail import AgentModeRail

    assert isinstance(rail, WorkAgentModeRail)

    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="slug")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    rail._agent = agent

    # WorkAgentModeRail has no before_tool_call override; the call resolves
    # to CodeAgentModeRail.before_tool_call. Patch only the grandparent
    # (AgentModeRail.before_tool_call) so CodeAgentModeRail's switch_mode
    # block runs for real and rejects the call before reaching the parent.
    parent = AsyncMock()
    with patch.object(AgentModeRail, "before_tool_call", parent):
        ctx_call = SimpleNamespace(
            session=SimpleNamespace(),
            inputs=SimpleNamespace(
                tool_name="switch_mode",
                tool_call=SimpleNamespace(
                    id="call_1",
                    arguments='{"mode": "normal"}',
                ),
                tool_args={"mode": "normal"},
            ),
            extra={},
        )
        await rail.before_tool_call(ctx_call)

    parent.assert_not_awaited()
    assert ctx_call.extra.get("_skip_tool") is True


# 注：原 ``test_new_plan_blocks_non_readonly_tools`` 与上面这条都重新验证
# ``before_tool_call`` 的阻塞语义，rail 本身未改动，仅 routing 谓词换了。
# 上面那条更核心（守 switch_mode 退出阻塞），非只读 bash 那条已删以避免冗余。
