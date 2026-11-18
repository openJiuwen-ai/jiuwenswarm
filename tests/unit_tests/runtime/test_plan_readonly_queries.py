# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only session inspection must not change Plan mode during startup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.plan import PlanModeController


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["agent", "team"])
@pytest.mark.parametrize("profile", ["work", "code"])
@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.SESSION_ASSETS_LIST,
        ReqMethod.A4P_AUTHORIZATION_PENDING,
        ReqMethod.SESSION_PLAN_STATUS,
        ReqMethod.COMMAND_GOAL,
    ],
)
async def test_inspection_cannot_exit_plan_before_first_turn(
    monkeypatch: pytest.MonkeyPatch,
    base: str,
    profile: str,
    method: ReqMethod,
) -> None:
    controller = PlanModeController()
    session_id = "first-plan-turn"
    request = AgentRequest(
        request_id="inspect-during-startup",
        channel_id="web",
        session_id=session_id,
        req_method=method,
        params={"mode": f"{base}.{profile}.plan", "action": "get"},
    )
    original_params = dict(request.params)
    controller.active_sessions.add(session_id)
    open_state = AsyncMock()
    monkeypatch.setattr(controller, "open_state_session", open_state)

    result = await controller.ensure_state(
        request, "code" if profile == "code" else "agent", "plan", object(),
    )
    events = await controller.check_post_process_exit(request, object())

    assert result.events == []
    assert events == []
    assert request.params == original_params
    assert controller.active_sessions == {session_id}
    assert controller.exited_sessions == set()
    open_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["work", "code"])
@pytest.mark.parametrize("state_mode", ["plan", "normal"])
async def test_chat_reports_only_a_completed_plan_exit(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    state_mode: str,
) -> None:
    controller = PlanModeController()
    session_id = "plan-approval-turn"
    controller.active_sessions.add(session_id)
    request = AgentRequest(
        request_id="plan-turn",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": f"agent.{profile}.plan"},
    )
    agent = SimpleNamespace(
        load_state=lambda _session: SimpleNamespace(
            plan_mode=SimpleNamespace(mode=state_mode),
        ),
    )
    monkeypatch.setattr(
        controller, "open_state_session", AsyncMock(return_value=(agent, object(), True)),
    )

    events = await controller.check_post_process_exit(request, agent)

    if state_mode == "normal":
        assert events == [{"event_type": "plan.mode_exited", "mode": f"agent.{profile}.normal"}]
        assert session_id in controller.exited_sessions
        assert session_id not in controller.active_sessions
    else:
        assert events == []
        assert session_id not in controller.exited_sessions
        assert session_id in controller.active_sessions
