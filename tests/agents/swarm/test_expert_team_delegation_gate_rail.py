# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.core.single_agent.rail import (
    AgentCallbackContext,
    ModelCallInputs,
    TaskIterationInputs,
    ToolCallInputs,
)
from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.team.rails.expert_team_delegation_gate_rail import (
    ExpertTeamDelegationGateRail,
)
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.context import SwarmBuildContext


class _Session:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.state: dict[str, Any] = {}

    def get_session_id(self) -> str:
        return self.session_id

    def get_state(self, key: str) -> Any:
        return self.state.get(key)

    def update_state(self, data: dict[str, Any]) -> None:
        self.state.update(data)


class _TaskManager:
    def __init__(self, tasks: list[Any] | None = None) -> None:
        self.member_name = "team-leader"
        self.tasks = tasks or []
        self.list_calls = 0

    async def list_tasks(self) -> list[Any]:
        self.list_calls += 1
        return list(self.tasks)


def _outer_ctx(
    session: _Session,
    *,
    follow_up: bool = False,
    iteration: int = 1,
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        session=session,
        inputs=TaskIterationInputs(
            iteration=iteration,
            loop_event=None,
            query="帮我做一份方案",
            is_follow_up=follow_up,
        ),
    )


def _inner_ctx(session: _Session) -> AgentCallbackContext:
    ctx = AgentCallbackContext(agent=None, session=session)
    ctx.extra["_streaming"] = True
    ctx.bind_steering_queue(asyncio.Queue())
    return ctx


def _response(*, content: str, tool_calls: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        content=content,
        reasoning_content="private reasoning",
        tool_calls=tool_calls or [],
    )


def test_provider_resolves_the_runtime_gate_for_a_leader() -> None:
    registry.register_swarm_providers()
    rail = RailSpec(type=registry.EXPERT_TEAM_DELEGATION_GATE).build(
        language="cn",
        context=SwarmBuildContext(language="cn", role="leader"),
    )
    assert isinstance(rail, ExpertTeamDelegationGateRail)


@pytest.mark.asyncio
async def test_direct_leader_answer_is_hidden_and_retried() -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session))
    ctx = _inner_ctx(session)
    answer = _response(content="任务简单，我直接给出方案。")
    ctx.inputs = ModelCallInputs(response=answer)

    await rail.before_model_call(ctx)
    assert ctx.extra["_streaming"] is False
    await rail.after_model_call(ctx)

    assert answer.content == ""
    assert answer.reasoning_content == ""
    assert ctx.consume_force_finish() is None
    assert "create_task" in ctx.steering_queue.get_nowait()


@pytest.mark.asyncio
async def test_raw_create_task_args_drop_review_fields_but_keep_routing() -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    ctx = _inner_ctx(session)
    ctx.inputs = ToolCallInputs(
        tool_name="create_task",
        tool_args=json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "run-s01",
                        "title": "制作 HTML",
                        "content": "生成最终成品",
                        "assignee": "member-1",
                        "depends_on": ["run-s00"],
                        "depended_by": ["run-s02"],
                        "reviewer": ["质量审查"],
                        "reviewers": ["第二审查"],
                        "verifier": "验收员",
                        "verifiers": ["复核员"],
                        "max_review_rounds": 3,
                        "review_required": True,
                        "requires_review": True,
                        "verification_required": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )

    await rail.before_tool_call(ctx)

    clean_args = json.loads(ctx.inputs.tool_args)
    task = clean_args["tasks"][0]
    assert task["assignee"] == "member-1"
    assert task["depends_on"] == ["run-s00"]
    assert task["depended_by"] == ["run-s02"]
    assert task["content"] == "生成最终成品"
    assert not {
        "reviewer",
        "reviewers",
        "verifier",
        "verifiers",
        "max_review_rounds",
        "review_required",
        "requires_review",
        "verification_required",
    }.intersection(task)


@pytest.mark.asyncio
async def test_non_create_tool_args_are_untouched() -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    ctx = _inner_ctx(session)
    original = json.dumps({"reviewer": ["keep-me"]})
    ctx.inputs = ToolCallInputs(tool_name="update_task", tool_args=original)

    await rail.before_tool_call(ctx)

    assert ctx.inputs.tool_args == original


@pytest.mark.asyncio
async def test_successful_scheduled_create_unlocks_following_answer() -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session))
    ctx = _inner_ctx(session)
    ctx.inputs = ModelCallInputs(
        response=_response(content="", tool_calls=[{"name": "create_task"}])
    )

    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    ctx.inputs = ToolCallInputs(
        tool_name="create_task",
        # AbilityManager keeps ToolCall.arguments as raw JSON on the callback
        # context even though it passes parsed arguments to tool.invoke.
        tool_args=json.dumps(
            {"tasks": [{"title": "执行", "assignee": "member-1"}]}
        ),
        tool_result=ToolOutput(
            success=True,
            data={"task_id": "run-s01", "title": "执行", "assignee": "member-1"},
        ),
    )
    await rail.after_tool_call(ctx)
    assert ctx.extra["_streaming"] is True

    final_answer = _response(content="成员已完成，这是汇总结果。")
    ctx.inputs = ModelCallInputs(response=final_answer)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    assert final_answer.content == "成员已完成，这是汇总结果。"


@pytest.mark.asyncio
async def test_beta3_string_create_result_unlocks_following_answer() -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session))
    ctx = _inner_ctx(session)
    ctx.inputs = ToolCallInputs(
        tool_name="create_task",
        tool_args=json.dumps({"tasks": [{"assignee": "member-1"}]}),
        tool_result=(
            "Task created: task_id=r1-s01 title=执行 "
            "-> member-1 (assigned; the scheduler starts it)"
        ),
    )

    await rail.after_tool_call(ctx)

    final_answer = _response(content="成员已接单。")
    ctx.inputs = ModelCallInputs(response=final_answer)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    assert final_answer.content == "成员已接单。"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["view_task", "ask_user", "create_task"])
async def test_non_successful_delegation_does_not_unlock(tool_name: str) -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session))
    ctx = _inner_ctx(session)
    ctx.inputs = ToolCallInputs(
        tool_name=tool_name,
        tool_args={"tasks": [{"assignee": "member-1"}]},
        tool_result=ToolOutput(success=False, error="not created"),
    )
    await rail.after_tool_call(ctx)

    answer = _response(content="仍然不应直接回答")
    ctx.inputs = ModelCallInputs(response=answer)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    assert answer.content == ""


@pytest.mark.asyncio
async def test_retry_ceiling_fails_closed_without_leaking_model_text() -> None:
    rail = ExpertTeamDelegationGateRail(max_corrections=1)
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session))
    ctx = _inner_ctx(session)

    first = _response(content="first direct answer")
    ctx.inputs = ModelCallInputs(response=first)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    ctx.steering_queue.get_nowait()

    second = _response(content="second direct answer")
    ctx.inputs = ModelCallInputs(response=second)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)

    finish = ctx.consume_force_finish()
    assert second.content == ""
    assert finish is not None
    assert finish.result["result_type"] == "error"
    assert "调度未能启动" in finish.result["output"]


@pytest.mark.asyncio
async def test_follow_up_keeps_delegation_but_new_user_round_resets_it() -> None:
    rail = ExpertTeamDelegationGateRail()
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session))
    ctx = _inner_ctx(session)
    ctx.inputs = ToolCallInputs(
        tool_name="create_task",
        tool_args={"tasks": [{"assignee": "member-1"}]},
        tool_result=ToolOutput(
            success=True,
            data={"task_id": "run-s01", "assignee": "member-1"},
        ),
    )
    await rail.after_tool_call(ctx)

    await rail.before_task_iteration(_outer_ctx(session, follow_up=True))
    follow_up = _inner_ctx(session)
    await rail.before_model_call(follow_up)
    assert follow_up.extra["_streaming"] is True

    await rail.before_task_iteration(_outer_ctx(session, follow_up=False))
    new_round = _inner_ctx(session)
    await rail.before_model_call(new_round)
    assert new_round.extra["_streaming"] is False


@pytest.mark.asyncio
async def test_scheduler_continuation_recovers_delegation_from_persisted_team_task() -> None:
    task_manager = _TaskManager(
        [
            SimpleNamespace(
                task_id="run-s01",
                assignee="member-1",
                status="completed",
            )
        ]
    )
    # Model a native-harness rebuild: both the rail and callback Session are
    # fresh, while the team's persistent task board survives.
    rail = ExpertTeamDelegationGateRail(task_manager=task_manager)
    session = _Session("rebuilt-session")
    # beta3's task-plan continuation is iteration 2 but is_follow_up=False.
    await rail.before_task_iteration(
        _outer_ctx(session, follow_up=False, iteration=2)
    )

    ctx = _inner_ctx(session)
    answer = _response(content="成员已完成，这是汇总结果。")
    ctx.inputs = ModelCallInputs(response=answer)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)

    assert task_manager.list_calls == 1
    assert ctx.extra["_streaming"] is True
    assert answer.content == "成员已完成，这是汇总结果。"


@pytest.mark.asyncio
async def test_new_user_round_does_not_inherit_an_old_team_task() -> None:
    task_manager = _TaskManager(
        [SimpleNamespace(task_id="old-s01", assignee="member-1")]
    )
    rail = ExpertTeamDelegationGateRail(task_manager=task_manager)
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session, follow_up=False))

    ctx = _inner_ctx(session)
    answer = _response(content="不能复用旧任务直接回答。")
    ctx.inputs = ModelCallInputs(response=answer)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)

    assert task_manager.list_calls == 0
    assert answer.content == ""


@pytest.mark.asyncio
async def test_follow_up_ignores_task_without_a_real_member_assignee() -> None:
    task_manager = _TaskManager(
        [
            SimpleNamespace(task_id="unassigned", assignee=None),
            SimpleNamespace(task_id="leader-task", assignee="team-leader"),
        ]
    )
    rail = ExpertTeamDelegationGateRail(task_manager=task_manager)
    session = _Session()
    await rail.before_task_iteration(_outer_ctx(session, follow_up=True))

    ctx = _inner_ctx(session)
    answer = _response(content="没有真实成员任务，仍需委派。")
    ctx.inputs = ModelCallInputs(response=answer)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)

    assert task_manager.list_calls == 2
    assert answer.content == ""
