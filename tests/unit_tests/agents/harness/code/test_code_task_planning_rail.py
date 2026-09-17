# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager
from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools.todo import TodoTool

from jiuwenswarm.agents.harness.code.rails.code_task_planning_rail import (
    CodeTaskPlanningRail,
)


class FakeSession:
    def __init__(self, session_id: str = "sess1") -> None:
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id


class FakeAgent:
    def __init__(self) -> None:
        self.prompt_attachment_manager = PromptAttachmentManager()
        self.config = SimpleNamespace(model_name="")
        self._llm = None

    def set_llm(self, llm) -> None:
        self._llm = llm


class FakeTodoTool(TodoTool):
    def __init__(self, todos: list[TodoItem]) -> None:
        self._card = ToolCard(
            id="fake_todo_list",
            name="todo_list",
            description="fake todo tool",
            input_params={"type": "object", "properties": {}, "required": []},
        )
        self.todos = todos

    async def load_todos(self, session_id: str) -> list[TodoItem]:
        assert session_id
        return self.todos


class FakeModel:
    def __init__(self, client_id: str, model_name: str) -> None:
        self.model_client_config = SimpleNamespace(client_id=client_id)
        self.model_config = SimpleNamespace(model_name=model_name)


def _ctx(agent: FakeAgent, *, tool_name: str = "", session_id: str = "sess1") -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=agent,
        session=FakeSession(session_id),
        inputs=ToolCallInputs(tool_name=tool_name),
    )


def _rail(todos: list[TodoItem], **kwargs) -> CodeTaskPlanningRail:
    rail = CodeTaskPlanningRail(**kwargs)
    rail.tools = [FakeTodoTool(todos)]
    return rail


async def _task_reminders(agent: FakeAgent):
    return await agent.prompt_attachment_manager.list_by_filter(
        session_id="sess1",
        section="task_reminder",
        source="jiuwenswarm.code_task_planning.task_reminder",
    )


@pytest.mark.asyncio
async def test_code_task_planning_rail_does_not_inject_before_threshold():
    agent = FakeAgent()
    rail = _rail([])

    await rail.before_model_call(_ctx(agent))

    assert await _task_reminders(agent) == []


@pytest.mark.asyncio
async def test_code_task_planning_rail_injects_task_reminder_attachment_after_threshold():
    agent = FakeAgent()
    rail = _rail(
        [
            TodoItem(
                id="locate",
                content="Locate failing behavior",
                activeForm="Locating failing behavior",
                description="Find the relevant code path",
                status=TodoStatus.IN_PROGRESS,
            ),
            TodoItem(
                id="verify",
                content="Verify the fix",
                activeForm="Verifying the fix",
                description="Run focused checks",
                status=TodoStatus.PENDING,
            ),
        ]
    )

    for _ in range(10):
        await rail.before_model_call(_ctx(agent))

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.kind == "todo_reminder"
    assert "The task tools haven't been used recently" in reminder.content
    assert "#locate. [in_progress] Locate failing behavior" in reminder.content
    assert "#verify. [pending] Verify the fix" in reminder.content

    rendered = agent.prompt_attachment_manager.render(reminders)
    assert '<prompt-attachment type="todo_reminder">' in rendered
    assert "<system-reminder>" in rendered


@pytest.mark.asyncio
async def test_code_task_planning_rail_resets_and_clears_reminder_after_todo_tool_use():
    agent = FakeAgent()
    rail = _rail([])

    for _ in range(10):
        await rail.before_model_call(_ctx(agent))
    assert len(await _task_reminders(agent)) == 1

    await rail.after_tool_call(_ctx(agent, tool_name="todo_modify"))
    await rail.before_model_call(_ctx(agent))

    assert await _task_reminders(agent) == []


@pytest.mark.asyncio
async def test_code_task_planning_rail_preserves_parent_model_selection_without_static_prompt():
    default_model = _model("default-client", "default-model")
    target_model = _model("target-client", "target-model")
    agent = FakeAgent()
    agent._llm = default_model
    rail = _rail(
        [
            TodoItem(
                id="implement",
                content="Implement fix",
                activeForm="Implementing fix",
                description="Apply code changes",
                status=TodoStatus.IN_PROGRESS,
                selected_model_id="target-client",
            )
        ],
        model_selection={target_model: "Target model"},
    )
    prompt_builder = SimpleNamespace(
        language="en",
        added_sections=[],
        removed_sections=[],
        add_section=lambda section: prompt_builder.added_sections.append(section),
        remove_section=lambda section: prompt_builder.removed_sections.append(section),
    )
    rail.system_prompt_builder = prompt_builder

    await rail.before_model_call(_ctx(agent))

    assert agent._llm is target_model
    assert agent.config.model_name == "target-model"
    assert prompt_builder.added_sections == []


@pytest.mark.asyncio
async def test_code_task_planning_rail_uses_fresh_todos_for_model_selection():
    default_model = _model("default-client", "default-model")
    stale_model = _model("stale-client", "stale-model")
    target_model = _model("target-client", "target-model")
    agent = FakeAgent()
    agent._llm = default_model
    rail = _rail(
        [
            TodoItem(
                id="fresh",
                content="Fresh task",
                activeForm="Working fresh task",
                description="Use the fresh selected model",
                status=TodoStatus.IN_PROGRESS,
                selected_model_id="target-client",
            )
        ],
        model_selection={
            stale_model: "Stale model",
            target_model: "Target model",
        },
    )
    rail._todos_cache["sess1"] = [
        TodoItem(
            id="stale",
            content="Stale task",
            activeForm="Working stale task",
            description="Old cached model choice",
            status=TodoStatus.IN_PROGRESS,
            selected_model_id="stale-client",
        )
    ]

    await rail.before_model_call(_ctx(agent))

    assert agent._llm is target_model
    assert agent.config.model_name == "target-model"


@pytest.mark.asyncio
async def test_code_task_planning_rail_loads_fresh_todos_for_reminder_instead_of_cache():
    agent = FakeAgent()
    stale = TodoItem(
        id="stale",
        content="Old cached task",
        activeForm="Old cached task",
        description="Old cached task",
        status=TodoStatus.IN_PROGRESS,
    )
    fresh = TodoItem(
        id="fresh",
        content="Fresh task from storage",
        activeForm="Fresh task from storage",
        description="Fresh task from storage",
        status=TodoStatus.IN_PROGRESS,
    )
    rail = _rail(
        [fresh],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    rail._todos_cache["sess1"] = [stale]

    await rail.before_model_call(_ctx(agent))

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    assert "#fresh. [in_progress] Fresh task from storage" in reminders[0].content
    assert "Old cached task" not in reminders[0].content


@pytest.mark.asyncio
async def test_code_task_planning_rail_clamps_non_positive_session_cap():
    rail = _rail([], max_tracked_task_reminder_sessions=0)
    agent = FakeAgent()

    await rail.before_model_call(_ctx(agent, session_id="sess1"))
    await rail.before_model_call(_ctx(agent, session_id="sess2"))

    assert rail.max_tracked_task_reminder_sessions == 1
    assert set(rail._turns_since_task_management) == {"sess2"}
    assert set(rail._turns_since_task_reminder) == {"sess2"}


@pytest.mark.asyncio
async def test_code_task_planning_rail_bounds_task_reminder_session_state():
    rail = _rail([], max_tracked_task_reminder_sessions=2)
    agent = FakeAgent()

    await rail.before_model_call(_ctx(agent, session_id="sess1"))
    await rail.before_model_call(_ctx(agent, session_id="sess2"))
    await rail.before_model_call(_ctx(agent, session_id="sess3"))

    assert set(rail._turns_since_task_management) == {"sess2", "sess3"}
    assert set(rail._turns_since_task_reminder) == {"sess2", "sess3"}


@pytest.mark.asyncio
async def test_code_task_planning_rail_clears_task_reminder_state_when_parent_cleanup_fails():
    rail = _rail([])
    agent = FakeAgent()
    rail._turns_since_task_management["sess1"] = 3
    rail._turns_since_task_reminder["sess1"] = 4
    rail._tracked_task_reminder_sessions.append("sess1")

    with pytest.raises(AttributeError):
        await rail.after_invoke(_ctx(agent))

    assert "sess1" not in rail._turns_since_task_management
    assert "sess1" not in rail._turns_since_task_reminder
    assert "sess1" not in rail._tracked_task_reminder_sessions


def _model(client_id: str, model_name: str) -> FakeModel:
    return FakeModel(client_id, model_name)
