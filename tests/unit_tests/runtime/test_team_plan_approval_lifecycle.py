# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team plan answers retain their Runtime owner after the plan stream ends."""

from contextlib import aclosing
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session.model import (
    TEAM_EXECUTION_CONTEXT_METADATA_KEY,
    SessionExecutionState,
    SessionWorkKind,
    TeamWaiterKind,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["work", "code"])
@pytest.mark.parametrize("action", ["plan_execute", "plan_skip", "plan_revise"])
async def test_team_plan_answer_resumes_parked_execution(monkeypatch, tmp_path, profile, action):
    session_id = f"team-plan-{profile}-{action}"
    mode = f"team.{profile}.plan"
    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: {"mode": mode, "work_mode": profile},
    )
    runtime = AgentRuntime(agent_manager=Mock(), initializer=AsyncMock())
    runtime._started = True
    monkeypatch.setattr(runtime, "_team_lifecycle", Mock())
    monkeypatch.setattr(runtime, "_remove_team_response_waiter", AsyncMock())
    monkeypatch.setattr(runtime, "_team_waiter_kind", Mock(return_value=TeamWaiterKind.REQUEST))
    coordinator = runtime._session_coordinator
    await coordinator.register_session(session_id, "web")
    observed = []

    async def agent_stream(request, **kwargs):
        del kwargs
        context = request.metadata[TEAM_EXECUTION_CONTEXT_METADATA_KEY]
        observed.append((request.request_id, context.work_kind))
        answers = request.params.get("answers")
        selected = answers[0]["selected_options"][0] if answers else None
        if selected is None or selected == "plan_revise":
            interaction_id = "approval-1" if selected is None else "approval-2"
            yield RuntimeEvent(
                request_id=request.request_id,
                channel_id="web",
                session_id=session_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": interaction_id,
                    "source": "confirm_interrupt",
                    "plan_approval_kind": "plan_approval",
                },
            )
        yield RuntimeEvent(
            request_id=request.request_id,
            channel_id="web",
            session_id=session_id,
            payload={"event_type": "chat.final", "content": "plan response"},
            is_complete=True,
        )

    monkeypatch.setattr(runtime, "_stream_started", agent_stream)

    async def collect(request):
        async with aclosing(runtime.stream(request, trigger_hook=False)) as stream:
            return [event async for event in stream]

    def answer(request_id, interaction_id, selected):
        return AgentRequest(
            request_id=request_id,
            channel_id="web",
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={
                "query": "",
                "mode": mode,
                "request_id": interaction_id,
                "source": "confirm_interrupt",
                "answers": [{"selected_options": [selected], "custom_input": "拆细任务"}],
                "plan_approval_kind": "plan_approval",
                "plan_content": "# Team plan",
                "plan_language": "cn",
            },
        )

    try:
        events = await collect(AgentRequest(
            request_id="generate-plan",
            channel_id="web",
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={"query": "Prepare a plan", "mode": mode},
        ))
        assert [event.event_type for event in events] == ["chat.ask_user_question", "chat.final"]
        root = coordinator.snapshot_session(session_id).executions[0]
        assert root.work_kind is SessionWorkKind.TEAM_REQUEST
        assert root.state is SessionExecutionState.WAITING_FOR_CONTROL
        assert coordinator.has_control_target(session_id, "approval-1")

        resumed = await collect(answer("answer-plan", "approval-1", action))
        assert not any(event.event_type == "chat.error" for event in resumed)
        assert observed == [
            ("generate-plan", SessionWorkKind.TEAM_REQUEST),
            ("answer-plan", SessionWorkKind.TEAM_CONTROL),
        ]
        assert not coordinator.has_control_target(session_id, "approval-1")
        if action == "plan_revise":
            assert coordinator.has_control_target(session_id, "approval-2")
            await collect(answer("answer-revised-plan", "approval-2", "plan_skip"))
            assert observed[-1] == ("answer-revised-plan", SessionWorkKind.TEAM_CONTROL)
        assert not coordinator.unanswered_interactions(session_id)
        assert all(
            execution.state is SessionExecutionState.SUCCEEDED
            for execution in coordinator.snapshot_session(session_id).executions
        )
    finally:
        await coordinator.close()
