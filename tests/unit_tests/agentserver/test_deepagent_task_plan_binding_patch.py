# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""验证交互续轮绑定 TaskPlan 任务的补丁行为。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.controller.modules.task_manager import TaskFilter
from openjiuwen.core.controller.schema.task import (
    Task as CoreTask,
    TaskStatus as CoreTaskStatus,
)
from openjiuwen.core.single_agent import AgentCard, create_agent_session
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import (
    ActiveInteractionRound,
    RoundWorkItem,
)
from openjiuwen.harness.schema.task import TaskPlan, TodoItem, TodoStatus
from openjiuwen.harness.task_loop.task_loop_event_executor import (
    DEEP_TASK_TYPE,
)

from jiuwenswarm.server.runtime.agent_adapter.deepagent_task_plan_binding_patch import (
    _make_patched_run_one_round,
    _resolve_plan_task_id,
    apply_deepagent_task_plan_binding_patch,
    remove_deepagent_task_plan_binding_patch,
)


@pytest.fixture
def plan_binding_patch():
    apply_deepagent_task_plan_binding_patch()
    yield
    remove_deepagent_task_plan_binding_patch()


def _make_agent_with_plan() -> tuple[DeepAgent, object]:
    agent = DeepAgent(AgentCard(name="test-agent"))
    agent._react_agent = SimpleNamespace(
        invoke=AsyncMock(return_value={"output": "任务已完成", "result_type": "answer"}),
        write_invoke_result_to_stream=AsyncMock(return_value=None),
    )
    session = create_agent_session(session_id="sess-patch", card=agent.card)
    state = agent.load_state(session)
    state.task_plan = TaskPlan(
        goal="goal",
        tasks=[
            TodoItem(id="t1", content="done-task", activeForm="done-task",
                     status=TodoStatus.COMPLETED),
            TodoItem(id="t2", content="pending-task", activeForm="pending-task",
                     status=TodoStatus.PENDING),
        ],
    )
    agent.save_state(session, state)
    return agent, session


def _continuation_work() -> RoundWorkItem:
    return RoundWorkItem.user(
        request_id="req-1",
        inputs={"query": "user query"},
        reset_loop=False,
    )


@pytest.mark.asyncio
async def test_continuation_round_binds_plan_task_and_converges(plan_binding_patch):
    """验证 补丁后续轮绑定 TaskPlan pending 任务，任务完成且循环终止。"""
    agent, session = _make_agent_with_plan()
    coordinator, controller = await agent.prepare_interaction_task_loop(session)

    outcome = await agent.run_one_round(
        _continuation_work(), uuid.uuid4().hex, session
    )

    state = agent.load_state(session)
    plan = state.task_plan
    assert plan.get_task("t2").status is TodoStatus.COMPLETED
    assert plan.get_next_task() is None
    assert outcome.next_work is None

    next_work = agent._build_interaction_next_work(
        work=_continuation_work(),
        result={"output": "任务已完成", "result_type": "answer"},
        session=session,
        coordinator=coordinator,
        controller=controller,
    )
    assert next_work is None

    await controller.stop()


@pytest.mark.asyncio
async def test_first_round_keeps_random_task_id(plan_binding_patch):
    """验证 首轮（reset_loop=True）不绑定 plan 任务，保持原有行为。"""
    agent, session = _make_agent_with_plan()
    _coordinator, controller = await agent.prepare_interaction_task_loop(session)

    first_round = RoundWorkItem.user(
        request_id="req-1",
        inputs={"query": "user query"},
        reset_loop=True,
    )
    await agent.run_one_round(first_round, uuid.uuid4().hex, session)

    state = agent.load_state(session)
    assert state.task_plan.get_task("t2").status is TodoStatus.PENDING

    await controller.stop()


@pytest.mark.asyncio
async def test_follow_up_round_keeps_random_task_id(plan_binding_patch):
    """验证 follow_up 轮不绑定 plan 任务，保持原有行为。"""
    agent, session = _make_agent_with_plan()
    _coordinator, controller = await agent.prepare_interaction_task_loop(session)

    follow_up = RoundWorkItem.user(
        request_id="req-1",
        inputs={"query": "user query"},
        is_follow_up=True,
        reset_loop=False,
    )
    await agent.run_one_round(follow_up, uuid.uuid4().hex, session)

    state = agent.load_state(session)
    assert state.task_plan.get_task("t2").status is TodoStatus.PENDING

    await controller.stop()


@pytest.mark.asyncio
async def test_active_round_task_id_rebound_to_plan_task():
    """验证 绑定发生时 ActiveInteractionRound.task_id 同步为 plan 任务 id。"""
    agent, session = _make_agent_with_plan()

    work = _continuation_work()
    random_id = uuid.uuid4().hex
    agent._active_interaction_round = ActiveInteractionRound(work=work, task_id=random_id)

    captured = {}

    async def _probe(self, w, task_id, sess):
        captured["task_id"] = task_id
        captured["active_task_id"] = self._active_interaction_round.task_id
        return SimpleNamespace(next_work=None, error_code=None, error_message=None)

    patched = _make_patched_run_one_round(_probe)
    await patched(agent, work, random_id, session)

    assert captured["task_id"] == "t2"
    assert captured["active_task_id"] == "t2"

    agent._active_interaction_round = None


@pytest.mark.asyncio
async def test_patch_apply_remove_roundtrip():
    """验证 补丁 apply/remove 可往返且幂等（断言失败也不泄漏全局补丁状态）。"""
    original = DeepAgent.run_one_round

    try:
        apply_deepagent_task_plan_binding_patch()
        apply_deepagent_task_plan_binding_patch()
        assert DeepAgent.run_one_round is not original
    finally:
        remove_deepagent_task_plan_binding_patch()
        remove_deepagent_task_plan_binding_patch()
    assert DeepAgent.run_one_round is original


async def _occupy_plan_task(
    agent: DeepAgent, session, task_id: str, status: CoreTaskStatus
) -> None:
    """在已就绪的控制器上注入残留 CoreTask（模拟 SUBMITTED 窗口取消等残留）。"""
    await agent.loop_controller.task_manager.add_task(
        CoreTask(
            session_id=session.get_session_id(),
            task_id=task_id,
            task_type=DEEP_TASK_TYPE,
            description="occupied",
            status=status,
        )
    )


@pytest.mark.asyncio
async def test_zombie_terminal_core_task_marks_plan_cancelled_and_binds_next():
    """验证 终态残留占用时标记 plan 任务取消并绑定下一 pending 任务。"""
    agent, session = _make_agent_with_plan()
    state = agent.load_state(session)
    state.task_plan.tasks.append(
        TodoItem(id="t3", content="next-task", activeForm="next-task",
                 status=TodoStatus.PENDING),
    )
    agent.save_state(session, state)
    _coordinator, controller = await agent.prepare_interaction_task_loop(session)
    # 模拟 SUBMITTED 窗口取消后残留的终态 CoreTask
    await _occupy_plan_task(agent, session, "t2", CoreTaskStatus.CANCELED)

    resolved = await _resolve_plan_task_id(agent, session, _continuation_work())

    assert resolved == "t3"
    plan = agent.load_state(session).task_plan
    assert plan.get_task("t2").status is TodoStatus.CANCELLED
    assert plan.get_task("t3").status is TodoStatus.PENDING

    await controller.stop()


@pytest.mark.asyncio
async def test_all_pending_zombie_marks_all_cancelled_returns_none():
    """验证 全部 pending 被终态残留占用时全部标记取消，循环可收敛。"""
    agent, session = _make_agent_with_plan()
    _coordinator, controller = await agent.prepare_interaction_task_loop(session)
    await _occupy_plan_task(agent, session, "t2", CoreTaskStatus.CANCELED)

    resolved = await _resolve_plan_task_id(agent, session, _continuation_work())

    assert resolved is None
    plan = agent.load_state(session).task_plan
    assert plan.get_task("t2").status is TodoStatus.CANCELLED
    assert agent._has_remaining_tasks(session) is False

    await controller.stop()


@pytest.mark.asyncio
async def test_inflight_core_task_falls_back_to_raw_task_id():
    """验证 非终态（在飞）占用时回退原始 task_id，plan 任务保持不变。"""
    agent, session = _make_agent_with_plan()
    _coordinator, controller = await agent.prepare_interaction_task_loop(session)
    await _occupy_plan_task(agent, session, "t2", CoreTaskStatus.WORKING)

    resolved = await _resolve_plan_task_id(agent, session, _continuation_work())

    assert resolved is None
    plan = agent.load_state(session).task_plan
    assert plan.get_task("t2").status is TodoStatus.PENDING

    await controller.stop()


@pytest.mark.asyncio
async def test_continuation_round_recovers_from_zombie_and_converges(plan_binding_patch):
    """验证 僵尸占用后续轮自愈：跳过僵尸任务、执行下一任务、循环收敛。"""
    agent, session = _make_agent_with_plan()
    state = agent.load_state(session)
    state.task_plan.tasks.append(
        TodoItem(id="t3", content="next-task", activeForm="next-task",
                 status=TodoStatus.PENDING),
    )
    agent.save_state(session, state)
    _coordinator, controller = await agent.prepare_interaction_task_loop(session)
    await _occupy_plan_task(agent, session, "t2", CoreTaskStatus.CANCELED)

    outcome = await agent.run_one_round(
        _continuation_work(), uuid.uuid4().hex, session
    )

    plan = agent.load_state(session).task_plan
    assert plan.get_task("t2").status is TodoStatus.CANCELLED
    assert plan.get_task("t3").status is TodoStatus.COMPLETED
    assert plan.get_next_task() is None
    assert outcome.next_work is None
    assert outcome.error_code is None

    # 僵尸 CoreTask 仍在 TaskManager，但本轮注册的任务是 t3 而非 t2
    registered = await controller.task_manager.get_task(TaskFilter(task_id="t3"))
    assert registered and registered[0].task_id == "t3"

    await controller.stop()
