# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager
from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools.todo import TodoTool

from jiuwenswarm.agents.harness.code.rails.code_task_planning_rail import (
    CodeTaskPlanningRail,
)


class FakeSession:
    def __init__(self, session_id: str = "sess1", state: dict | None = None) -> None:
        self._session_id = session_id
        self._state = state if state is not None else {}

    def get_session_id(self) -> str:
        return self._session_id

    def get_state(self, key: str):
        return self._state.get(key)

    def update_state(self, values: dict) -> None:
        self._state.update(values)


class FakeAgent:
    def __init__(self, session_states: dict[str, dict] | None = None) -> None:
        self.prompt_attachment_manager = PromptAttachmentManager()
        self.config = SimpleNamespace(model_name="")
        self._llm = None
        self.session_states = session_states if session_states is not None else {}

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

    def cleanup_session(self, session_id: str) -> None:
        assert session_id


class FakeModel:
    def __init__(self, client_id: str, model_name: str) -> None:
        self.model_client_config = SimpleNamespace(client_id=client_id)
        self.model_config = SimpleNamespace(model_name=model_name)


class FakeHistoryContext:
    def __init__(self) -> None:
        self.messages = []

    def get_messages(self, with_history: bool = False):
        del with_history
        return list(self.messages)

    async def add_messages(self, *messages) -> None:
        self.messages.extend(messages)


def _ctx(
    agent: FakeAgent,
    *,
    tool_name: str = "",
    session_id: str = "sess1",
    context=None,
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=agent,
        session=FakeSession(
            session_id,
            agent.session_states.setdefault(session_id, {}),
        ),
        inputs=ToolCallInputs(tool_name=tool_name),
        context=context,
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
async def test_code_task_planning_rail_counts_model_retry_once():
    agent = FakeAgent()
    rail = _rail([])
    ctx = _ctx(agent)

    await rail.before_model_call(ctx)
    await rail.before_model_call(ctx)

    state = rail._load_task_reminder_state("sess1")
    assert state.turns_since_task_management == 1
    assert state.turns_since_task_reminder == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_injects_one_attachment_per_reminder_period():
    agent = FakeAgent()
    history = FakeHistoryContext()
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

    for _ in range(9):
        await rail.before_model_call(_ctx(agent))
        assert await agent.prompt_attachment_manager.sync_to_context(
            history, "sess1"
        ) is None

    await rail.before_model_call(_ctx(agent))
    first_message = await agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    )
    assert first_message is not None

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.kind == "todo_reminder"
    assert reminder.metadata["delivery_sequence"] == 1
    assert "Use the task tools when the current work would benefit" in reminder.content
    assert "todo_list to read the latest task state" in reminder.content
    assert "Locate failing behavior" not in reminder.content
    assert "Verify the fix" not in reminder.content
    assert "<!-- task-reminder-delivery:1 -->" in reminder.content

    await rail.before_model_call(_ctx(agent))
    assert await agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is None

    for _ in range(8):
        await rail.before_model_call(_ctx(agent))
        assert await agent.prompt_attachment_manager.sync_to_context(
            history, "sess1"
        ) is None

    await rail.before_model_call(_ctx(agent))
    second_message = await agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    )
    assert second_message is not None
    assert len(history.messages) == 2
    assert all("no longer active" not in message.content for message in history.messages)

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    assert reminders[0].metadata["delivery_sequence"] == 2
    assert "<!-- task-reminder-delivery:2 -->" in reminders[0].content


@pytest.mark.asyncio
async def test_code_task_planning_rail_resets_cadence_without_removal_after_todo_tool_use():
    agent = FakeAgent()
    history = FakeHistoryContext()
    rail = _rail([])

    for _ in range(10):
        await rail.before_model_call(_ctx(agent))
    assert await agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is not None

    await rail.after_tool_call(_ctx(agent, tool_name="todo_modify"))
    await rail.before_model_call(_ctx(agent))

    assert await agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is None
    assert len(await _task_reminders(agent)) == 1
    assert len(history.messages) == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_restores_attachment_before_sync_after_manager_recreation():
    session_states: dict[str, dict] = {}
    first_agent = FakeAgent(session_states)
    first_rail = _rail(
        [],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    history = FakeHistoryContext()

    await first_rail.before_model_call(_ctx(first_agent))
    assert await first_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is not None

    recreated_agent = FakeAgent(session_states)
    recreated_rail = _rail([])
    await recreated_rail.on_user_message(_ctx(recreated_agent, context=history))

    assert await recreated_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is None
    assert len(history.messages) == 1
    assert "no longer active" not in history.messages[0].content
    reminders = await _task_reminders(recreated_agent)
    assert len(reminders) == 1
    assert reminders[0].metadata["delivery_sequence"] == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_continues_delivery_sequence_after_manager_recreation():
    session_states: dict[str, dict] = {}
    first_agent = FakeAgent(session_states)
    first_rail = _rail(
        [],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    history = FakeHistoryContext()

    await first_rail.before_model_call(_ctx(first_agent))
    await first_agent.prompt_attachment_manager.sync_to_context(history, "sess1")

    recreated_agent = FakeAgent(session_states)
    recreated_rail = _rail(
        [],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    await recreated_rail.on_user_message(_ctx(recreated_agent, context=history))
    await recreated_rail.before_model_call(_ctx(recreated_agent))
    second_message = await recreated_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    )

    assert second_message is not None
    assert "no longer active" not in second_message.content
    assert len(history.messages) == 2
    reminders = await _task_reminders(recreated_agent)
    assert len(reminders) == 1
    assert reminders[0].metadata["delivery_sequence"] == 2
    assert "<!-- task-reminder-delivery:2 -->" in reminders[0].content


@pytest.mark.asyncio
async def test_code_task_planning_rail_rebuilds_pre_reminder_cadence_from_history():
    history = FakeHistoryContext()
    history.messages.extend(
        AssistantMessage(content=f"response {index}") for index in range(5)
    )
    agent = FakeAgent()
    rail = _rail([])
    await rail.on_user_message(_ctx(agent, context=history))

    for _ in range(4):
        await rail.before_model_call(_ctx(agent))
        assert await _task_reminders(agent) == []

    await rail.before_model_call(_ctx(agent))

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    assert reminders[0].metadata["delivery_sequence"] == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_rebuilds_post_reminder_cadence_from_history():
    history = FakeHistoryContext()
    first_agent = FakeAgent()
    first_rail = _rail(
        [],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    await first_rail.before_model_call(_ctx(first_agent))
    await first_agent.prompt_attachment_manager.sync_to_context(history, "sess1")
    history.messages.extend(
        AssistantMessage(content=f"response {index}") for index in range(5)
    )

    recreated_agent = FakeAgent()
    recreated_rail = _rail([])
    await recreated_rail.on_user_message(
        _ctx(recreated_agent, context=history)
    )

    for _ in range(5):
        await recreated_rail.before_model_call(_ctx(recreated_agent))
        reminders = await _task_reminders(recreated_agent)
        assert reminders[0].metadata["delivery_sequence"] == 1

    await recreated_rail.before_model_call(_ctx(recreated_agent))

    reminders = await _task_reminders(recreated_agent)
    assert reminders[0].metadata["delivery_sequence"] == 2


@pytest.mark.asyncio
async def test_code_task_planning_rail_rebuilds_todo_reset_from_history():
    history = FakeHistoryContext()
    history.messages.extend(
        AssistantMessage(content=f"old response {index}") for index in range(5)
    )
    history.messages.extend(
        [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-call",
                        type="function",
                        name="todo_modify",
                        arguments="{}",
                    )
                ],
            ),
            ToolMessage(content="updated", tool_call_id="todo-call"),
            AssistantMessage(content="response after todo"),
        ]
    )
    agent = FakeAgent()
    rail = _rail(
        [],
        task_reminder_turns_since_management=3,
        task_reminder_turns_between_reminders=1,
    )
    await rail.on_user_message(_ctx(agent, context=history))

    await rail.before_model_call(_ctx(agent))
    assert await _task_reminders(agent) == []

    await rail.before_model_call(_ctx(agent))
    assert len(await _task_reminders(agent)) == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_recovers_legacy_attachment_from_history():
    history = FakeHistoryContext()
    legacy_agent = FakeAgent()
    legacy_content = (
        "The task tools haven't been used recently. Use them when relevant.\n\n"
        "Here are the existing tasks:\n\n#legacy. [pending] Old task"
    )
    await legacy_agent.prompt_attachment_manager.add_section(
        session_id="sess1",
        section="task_reminder",
        kind="todo_reminder",
        content=legacy_content,
        priority=60,
        source="jiuwenswarm.code_task_planning.task_reminder",
        metadata={"item_count": 1},
        content_kind="text/markdown",
    )
    assert await legacy_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is not None

    recreated_agent = FakeAgent()
    recreated_rail = _rail([])
    await recreated_rail.on_user_message(_ctx(recreated_agent, context=history))

    assert await recreated_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is None
    reminders = await _task_reminders(recreated_agent)
    assert len(reminders) == 1
    assert reminders[0].content == legacy_content


@pytest.mark.asyncio
async def test_code_task_planning_rail_recovers_truncated_legacy_attachment_from_todos():
    todos = [
        TodoItem(
            id=f"legacy-{index}",
            content=f"Legacy task {index}: " + "x" * 100,
            activeForm=f"Working legacy task {index}",
            description=f"Legacy task {index}",
            status=TodoStatus.PENDING,
        )
        for index in range(120)
    ]
    history = FakeHistoryContext()
    legacy_agent = FakeAgent()
    legacy_content = CodeTaskPlanningRail._build_legacy_task_reminder_content(todos)
    assert len(legacy_content) > 12000
    await legacy_agent.prompt_attachment_manager.add_section(
        session_id="sess1",
        section="task_reminder",
        kind="todo_reminder",
        content=legacy_content,
        priority=60,
        source="jiuwenswarm.code_task_planning.task_reminder",
        metadata={"item_count": len(todos)},
        content_kind="text/markdown",
    )
    rendered = await legacy_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    )
    assert rendered is not None
    assert "Prompt attachment truncated" in rendered.content

    recreated_agent = FakeAgent()
    recreated_rail = _rail(todos)
    await recreated_rail.on_user_message(
        _ctx(recreated_agent, context=history)
    )

    assert await recreated_agent.prompt_attachment_manager.sync_to_context(
        history, "sess1"
    ) is None
    reminders = await _task_reminders(recreated_agent)
    assert len(reminders) == 1
    assert reminders[0].content == legacy_content


@pytest.mark.asyncio
async def test_code_task_planning_rail_prefers_newer_live_attachment_state():
    agent = FakeAgent()
    rail = _rail(
        [],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    await rail.before_model_call(_ctx(agent))
    newer_content = (
        rail._build_task_reminder_content()
        + "\n\n<!-- task-reminder-delivery:2 -->"
    )
    await agent.prompt_attachment_manager.add_section(
        session_id="sess1",
        section="task_reminder",
        kind="todo_reminder",
        content=newer_content,
        priority=60,
        source="jiuwenswarm.code_task_planning.task_reminder",
        metadata={"item_count": 0, "delivery_sequence": 2},
        content_kind="text/markdown",
    )

    await rail.on_user_message(_ctx(agent))

    state = rail._load_task_reminder_state("sess1")
    assert state.delivery_sequence == 2
    assert state.content == newer_content


@pytest.mark.asyncio
async def test_code_task_planning_rail_replaces_older_live_attachment_state():
    agent = FakeAgent()
    rail = _rail(
        [],
        task_reminder_turns_since_management=1,
        task_reminder_turns_between_reminders=1,
    )
    await rail.before_model_call(_ctx(agent))
    await rail.before_model_call(_ctx(agent))
    current = (await _task_reminders(agent))[0]
    older_content = (
        rail._build_task_reminder_content()
        + "\n\n<!-- task-reminder-delivery:1 -->"
    )
    await agent.prompt_attachment_manager.add_section(
        session_id="sess1",
        section="task_reminder",
        kind="todo_reminder",
        content=older_content,
        priority=60,
        source="jiuwenswarm.code_task_planning.task_reminder",
        metadata={"item_count": 0, "delivery_sequence": 1},
        content_kind="text/markdown",
    )

    await rail.on_user_message(_ctx(agent))

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    assert reminders[0].content == current.content
    assert reminders[0].metadata["delivery_sequence"] == 2


@pytest.mark.asyncio
async def test_code_task_planning_rail_continues_cadence_after_invoke_cleanup():
    agent = FakeAgent()
    rail = _rail([])

    for _ in range(5):
        await rail.before_model_call(_ctx(agent))

    await rail.after_invoke(_ctx(agent))

    for _ in range(4):
        await rail.before_model_call(_ctx(agent))
        assert await _task_reminders(agent) == []

    await rail.before_model_call(_ctx(agent))

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    assert reminders[0].metadata["delivery_sequence"] == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_serializes_overlapping_session_updates():
    agent = FakeAgent()
    rail = _rail([])
    for _ in range(9):
        await rail.before_model_call(_ctx(agent))

    original_add_section = agent.prompt_attachment_manager.add_section
    first_add_entered = asyncio.Event()
    release_first_add = asyncio.Event()
    delay_next_add = True

    async def delayed_add_section(**kwargs):
        nonlocal delay_next_add
        if delay_next_add:
            delay_next_add = False
            first_add_entered.set()
            await release_first_add.wait()
        return await original_add_section(**kwargs)

    agent.prompt_attachment_manager.add_section = delayed_add_section
    first_call = asyncio.create_task(rail.before_model_call(_ctx(agent)))
    await first_add_entered.wait()
    second_call = asyncio.create_task(rail.before_model_call(_ctx(agent)))
    await asyncio.sleep(0)
    release_first_add.set()
    await asyncio.gather(first_call, second_call)

    reminders = await _task_reminders(agent)
    assert len(reminders) == 1
    assert reminders[0].metadata["delivery_sequence"] == 1
    state = rail._load_task_reminder_state("sess1")
    assert state.turns_since_task_reminder == 1


@pytest.mark.asyncio
async def test_code_task_planning_rail_does_not_write_cadence_to_session_checkpoint():
    agent = FakeAgent()
    rail = _rail([])
    session_state: dict = {}
    ctx = AgentCallbackContext(
        agent=agent,
        session=FakeSession("sess1", session_state),
        inputs=ToolCallInputs(),
    )

    await rail.before_model_call(ctx)
    await rail.after_invoke(ctx)

    assert session_state == {}


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
async def test_code_task_planning_rail_does_not_persist_mutable_todo_snapshot():
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
    assert "Fresh task" not in reminders[0].content
    assert "Old cached task" not in reminders[0].content
    assert "todo_list to read the latest task state" in reminders[0].content


@pytest.mark.asyncio
async def test_code_task_planning_rail_clamps_non_positive_session_cap():
    rail = _rail([], max_tracked_task_reminder_sessions=0)
    agent = FakeAgent()

    await rail.before_model_call(_ctx(agent, session_id="sess1"))
    await rail.before_model_call(_ctx(agent, session_id="sess2"))

    assert rail.max_tracked_task_reminder_sessions == 1
    assert set(rail._task_reminder_states) == {"sess2"}


@pytest.mark.asyncio
async def test_code_task_planning_rail_bounds_task_reminder_session_state():
    rail = _rail([], max_tracked_task_reminder_sessions=2)
    agent = FakeAgent()

    await rail.before_model_call(_ctx(agent, session_id="sess1"))
    await rail.before_model_call(_ctx(agent, session_id="sess2"))
    await rail.before_model_call(_ctx(agent, session_id="sess3"))

    assert set(rail._task_reminder_states) == {"sess2", "sess3"}


@pytest.mark.asyncio
async def test_code_task_planning_rail_preserves_canonical_state_after_invoke():
    rail = _rail([])
    agent = FakeAgent()
    ctx = _ctx(agent)
    state = rail._load_task_reminder_state("sess1")
    state.turns_since_task_management = 3
    state.turns_since_task_reminder = 4
    rail._save_task_reminder_state("sess1", state)

    await rail.after_invoke(ctx)

    assert rail._task_reminder_states["sess1"].turns_since_task_management == 3
    assert rail._task_reminder_states["sess1"].turns_since_task_reminder == 4


def _model(client_id: str, model_name: str) -> FakeModel:
    return FakeModel(client_id, model_name)
