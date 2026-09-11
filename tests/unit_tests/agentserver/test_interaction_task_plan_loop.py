# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""验证交互模式外层轮循环在 TaskPlan 存在 pending 任务时的推进与收敛行为。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.controller.modules.task_manager import TaskManager
from openjiuwen.core.controller.modules.task_scheduler import (
    TaskExecutorDependencies,
)
from openjiuwen.core.controller.schema.task import (
    Task as CoreTask,
    TaskStatus as CoreTaskStatus,
)
from openjiuwen.core.single_agent import AgentCard, create_agent_session
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import RoundWorkItem
from openjiuwen.harness.schema.task import TaskPlan, TodoItem, TodoStatus
from openjiuwen.harness.task_loop.task_loop_event_executor import (
    DEEP_TASK_TYPE,
    TaskLoopEventExecutor,
)


def _make_plan() -> TaskPlan:
    return TaskPlan(
        goal="goal",
        tasks=[
            TodoItem(id="t1", content="done-task", activeForm="done-task",
                     status=TodoStatus.COMPLETED),
            TodoItem(id="t2", content="pending-task", activeForm="pending-task",
                     status=TodoStatus.PENDING),
        ],
    )


def _make_agent_with_plan() -> tuple[DeepAgent, object]:
    agent = DeepAgent(AgentCard(name="test-agent"))
    agent._react_agent = SimpleNamespace(
        invoke=AsyncMock(return_value={"output": "任务已完成", "result_type": "answer"}),
    )
    session = create_agent_session(session_id="sess-loop", card=agent.card)
    state = agent.load_state(session)
    state.task_plan = _make_plan()
    agent.save_state(session, state)
    return agent, session


async def _run_round(agent: DeepAgent, session, task_id: str) -> None:
    task_manager = TaskManager(config=None)
    deps = TaskExecutorDependencies(
        config=None,
        ability_manager=None,
        context_engine=None,
        task_manager=task_manager,
        event_queue=None,
    )
    core_task = CoreTask(
        session_id=session.get_session_id(),
        task_id=task_id,
        task_type=DEEP_TASK_TYPE,
        description="user query",
        status=CoreTaskStatus.SUBMITTED,
    )
    await task_manager.add_task(core_task)
    executor = TaskLoopEventExecutor(deps, agent)
    async for _chunk in executor.execute_ability(task_id, session):
        pass


@pytest.mark.asyncio
async def test_interaction_round_with_random_task_id_keeps_plan_pending():
    """验证 交互轮使用随机 task_id 提交时 TaskPlan 的 pending 任务不推进。"""
    agent, session = _make_agent_with_plan()
    random_id = uuid.uuid4().hex

    await _run_round(agent, session, random_id)

    state = agent.load_state(session)
    plan = state.task_plan
    assert plan.get_task("t2").status is TodoStatus.PENDING
    assert plan.get_next_task() is not None
    assert agent._has_remaining_tasks(session) is True


@pytest.mark.asyncio
async def test_round_bound_to_plan_task_marks_it_completed():
    """验证 轮次绑定 TaskPlan 的 pending 任务 id 时该任务被执行并标记完成。"""
    agent, session = _make_agent_with_plan()

    await _run_round(agent, session, "t2")

    state = agent.load_state(session)
    plan = state.task_plan
    assert plan.get_task("t2").status is TodoStatus.COMPLETED
    assert plan.get_next_task() is None
    assert agent._has_remaining_tasks(session) is False


@pytest.mark.asyncio
async def test_next_work_regenerated_while_plan_has_pending():
    """验证 plan 有 pending 时每轮结束后仍会生成下一轮 work，循环不终止。"""
    agent, session = _make_agent_with_plan()
    random_id = uuid.uuid4().hex

    coordinator, controller = await agent.prepare_interaction_task_loop(session)
    work = RoundWorkItem.user(request_id="req-1", inputs={"query": "user query"})
    next_work = agent._build_interaction_next_work(
        work=work,
        result={"output": "任务已完成", "result_type": "answer"},
        session=session,
        coordinator=coordinator,
        controller=controller,
    )
    assert next_work is not None

    await _run_round(agent, session, random_id)
    next_work = agent._build_interaction_next_work(
        work=work,
        result={"output": "任务已完成", "result_type": "answer"},
        session=session,
        coordinator=coordinator,
        controller=controller,
    )
    assert next_work is not None


@pytest.mark.asyncio
async def test_held_output_lease_blocks_new_attach():
    """验证 已有消费者持有输出租约时新的 attach 拿不到租约。"""
    from openjiuwen.harness.schema.interaction import OutputLeaseManager

    manager = OutputLeaseManager()

    first = await manager.attach()
    assert first is not None

    second = await manager.attach()
    assert second is None
    assert manager.has_consumer() is True

    await manager.detach(first.token)
    reopened = await manager.attach()
    assert reopened is not None
