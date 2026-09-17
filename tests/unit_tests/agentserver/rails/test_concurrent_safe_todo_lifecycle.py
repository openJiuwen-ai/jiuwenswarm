"""Regression coverage for checklist creation and execution-plan ownership."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.task import TaskPlan
from openjiuwen.harness.tools.todo import TodoItem, TodoStatus
from jiuwenswarm.agents.harness.common.rails import concurrent_safe_rails as module


class LocalFiles:
    async def write_file(self, path, content, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return SimpleNamespace(code=0)

    async def read_file(self, path, **kwargs):
        return SimpleNamespace(code=0, data=SimpleNamespace(
            content=Path(path).read_text(encoding="utf-8")))


def make_agent(tmp_path, fs=None):
    fs = fs if fs is not None else LocalFiles()
    agent = Mock(spec=DeepAgent)
    agent.card = SimpleNamespace(id="todo-regression")
    agent.system_prompt_builder = SimpleNamespace(language="cn")
    agent.deep_config = SimpleNamespace(
        sys_operation=SimpleNamespace(fs=lambda: fs),
        workspace=SimpleNamespace(get_node_path=lambda node: tmp_path),
    )
    agent.ability_manager = Mock()
    agent.ability_manager.list.return_value = []
    return agent


@pytest.mark.asyncio
async def test_registered_create_instance_is_reset_and_new_round_replaces_list(tmp_path, monkeypatch):
    registry = {}
    manager = SimpleNamespace(
        get_tool=lambda tool_id: registry.get(tool_id),
        add_tool=lambda tool: registry.setdefault(tool.card.id, tool),
    )
    monkeypatch.setattr(module, "Runner", SimpleNamespace(resource_mgr=manager))
    fs = LocalFiles()
    first = module.ConcurrentSafeTaskPlanningRail()
    first.init(make_agent(tmp_path, fs))
    second = module.ConcurrentSafeTaskPlanningRail()
    second.init(make_agent(tmp_path, fs))
    creator = first._find_todo_create_tool()
    assert second._find_todo_create_tool() is creator
    session = SimpleNamespace(get_session_id=lambda: "session")
    ctx = AgentCallbackContext(agent=second._state_agent, session=session)
    await first.before_invoke(ctx)
    result = await creator._create_from_list("session", [{"id": "old", "content": "Old task"}])
    assert (await creator.load_todos("session"))[0].status == TodoStatus.IN_PROGRESS
    assert "Immediately execute" in result
    with pytest.raises(Exception, match="already rebuilt"):
        await creator._create_from_list("session", [{"id": "bad", "content": "Duplicate"}])
    await second.before_invoke(ctx)
    await creator._create_from_list("session", [{"id": "new", "content": "New task"}])
    todos = await creator.load_todos("session")
    assert [todo.id for todo in todos] == ["new"]
    # Explicit status updates retain the original behavior.
    todos[0].status = TodoStatus.IN_PROGRESS
    await creator.save_todos("session", todos)
    assert (await creator.load_todos("session"))[0].status == TodoStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_checklist_retains_original_outer_execution_plan_bridge():
    rail = module.ConcurrentSafeTaskPlanningRail()
    tool = SimpleNamespace(load_todos=AsyncMock(return_value=[
        TodoItem(id="week", content="Wait for next week", status=TodoStatus.PENDING),
    ]))
    rail._find_todo_tool = lambda: tool
    state = SimpleNamespace(task_plan=None)
    agent = SimpleNamespace(load_state=lambda session: state, save_state=Mock())
    ctx = AgentCallbackContext(agent=agent, session=SimpleNamespace(get_session_id=lambda: "s"))
    for _ in range(3):
        await rail.after_task_iteration(ctx)
        assert state.task_plan.get_next_task().id == "week"
    assert agent.save_state.call_count == 3


@pytest.mark.asyncio
async def test_after_task_iteration_persists_memory_completion_before_disk_refresh():
    """OuterLoop mark_completed must land on disk; refresh must not wipe it.

    Regression for overnight token burn: after_task_iteration used to refresh
    TaskPlan from disk *before* syncing plan→disk, so in-memory mark_completed
    was overwritten by stale PENDING todos and the outer loop never advanced.
    """
    rail = module.ConcurrentSafeTaskPlanningRail()
    disk_todos = [
        TodoItem(id="a", content="Done earlier", status=TodoStatus.COMPLETED),
        TodoItem(id="b", content="Just finished in memory", status=TodoStatus.PENDING),
        TodoItem(id="c", content="Still pending", status=TodoStatus.PENDING),
    ]
    tool = SimpleNamespace(
        load_todos=AsyncMock(return_value=disk_todos),
        save_todos=AsyncMock(),
    )
    rail._find_todo_tool = lambda: tool
    # Executor already marked the current round's task completed in memory.
    state = SimpleNamespace(task_plan=TaskPlan(tasks=[
        TodoItem(id="a", content="Done earlier", status=TodoStatus.COMPLETED),
        TodoItem(id="b", content="Just finished in memory", status=TodoStatus.COMPLETED),
        TodoItem(id="c", content="Still pending", status=TodoStatus.PENDING),
    ]))
    agent = SimpleNamespace(load_state=lambda session: state, save_state=Mock())
    ctx = AgentCallbackContext(
        agent=agent,
        session=SimpleNamespace(get_session_id=lambda: "s"),
        inputs=SimpleNamespace(result={"result_type": "completed", "output": "ok"}),
    )

    await rail.after_task_iteration(ctx)

    tool.save_todos.assert_awaited()
    saved = tool.save_todos.await_args.args[1]
    assert [t.status for t in saved] == [
        TodoStatus.COMPLETED,
        TodoStatus.COMPLETED,
        TodoStatus.PENDING,
    ]
    assert [t.status for t in state.task_plan.tasks] == [
        TodoStatus.COMPLETED,
        TodoStatus.COMPLETED,
        TodoStatus.PENDING,
    ]
    assert state.task_plan.get_next_task().id == "c"


@pytest.mark.asyncio
async def test_inner_callback_syncs_owner_plan_without_mutating_context():
    rail = module.ConcurrentSafeTaskPlanningRail()
    todo = TodoItem(id="task", content="Task", status=TodoStatus.COMPLETED)
    tool = SimpleNamespace(load_todos=AsyncMock(return_value=[todo]))
    rail._find_todo_tool = lambda: tool
    state = SimpleNamespace(task_plan=TaskPlan(tasks=[
        TodoItem(id="task", content="Task", status=TodoStatus.IN_PROGRESS),
    ]))
    rail._state_agent = SimpleNamespace(load_state=lambda session: state, save_state=Mock())
    inner = SimpleNamespace()
    ctx = AgentCallbackContext(
        agent=inner, session=SimpleNamespace(get_session_id=lambda: "s"),
        inputs=ToolCallInputs(tool_name="todo_modify", tool_args={}),
    )
    await rail.after_tool_call(ctx)
    assert ctx.agent is inner
    assert state.task_plan.tasks[0].status == TodoStatus.COMPLETED
    rail._state_agent.save_state.assert_called()


@pytest.mark.asyncio
async def test_existing_plan_sync_preserves_terminal_todos():
    rail = module.ConcurrentSafeTaskPlanningRail()
    todos = [
        TodoItem(id="done", content="Done", status=TodoStatus.IN_PROGRESS),
        TodoItem(id="cancelled", content="Cancelled", status=TodoStatus.CANCELLED),
    ]
    tool = SimpleNamespace(load_todos=AsyncMock(return_value=todos), save_todos=AsyncMock())
    rail._find_todo_tool = lambda: tool
    state = SimpleNamespace(task_plan=TaskPlan(tasks=[
        TodoItem(id="done", content="Done", status=TodoStatus.COMPLETED),
        TodoItem(id="cancelled", content="Cancelled", status=TodoStatus.IN_PROGRESS),
    ]))
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(load_state=lambda session: state),
        session=SimpleNamespace(get_session_id=lambda: "s"),
    )
    await rail._sync_todos_from_plan(ctx)
    assert [todo.status for todo in todos] == [TodoStatus.COMPLETED, TodoStatus.CANCELLED]
    tool.save_todos.assert_awaited_once_with("s", todos)


@pytest.mark.asyncio
async def test_uninitialized_rail_skips_inner_callback():
    rail = module.ConcurrentSafeTaskPlanningRail()
    ctx = AgentCallbackContext(agent=SimpleNamespace())
    await rail.after_tool_call(ctx)
    assert not hasattr(ctx.agent, "load_state")


@pytest.mark.parametrize("conflict", ["type", "workspace", "fs"])
def test_late_binding_conflict_fails_without_partial_registration(tmp_path, monkeypatch, conflict):
    fs = LocalFiles()
    agent = make_agent(tmp_path, fs)
    stale = module.TodoCreateTool(
        agent.deep_config.sys_operation, str(tmp_path / "old"), "cn", "old",
    )
    agent.ability_manager.list.return_value = [stale.card]
    wrong = SimpleNamespace(workspace=str(tmp_path), fs=fs)
    if conflict != "type":
        wrong = module.TodoModifyTool(
            agent.deep_config.sys_operation, str(tmp_path), "cn", "conflict",
        )
        if conflict == "workspace":
            wrong.workspace = str(tmp_path / "other")
        else:
            wrong.fs = LocalFiles()

    def get_tool(tool_id):
        if tool_id == stale.card.id:
            return stale
        if tool_id.startswith("TodoModifyTool_"):
            return wrong
        return None

    manager = SimpleNamespace(get_tool=get_tool, add_tool=Mock())
    monkeypatch.setattr(module, "Runner", SimpleNamespace(resource_mgr=manager))
    rail = module.ConcurrentSafeTaskPlanningRail()
    previous_tools = rail.tools
    with pytest.raises(RuntimeError, match="Todo tool binding mismatch"):
        rail.init(agent)
    assert rail.tools is previous_tools
    agent.ability_manager.add.assert_not_called()
    agent.ability_manager.remove.assert_not_called()
    manager.add_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("different_workspace,different_fs", [(True, False), (False, True), (True, True)])
async def test_registry_keeps_workspace_and_filesystem_bindings_isolated(
    tmp_path, monkeypatch, different_workspace, different_fs,
):
    registry = {}
    monkeypatch.setattr(module, "Runner", SimpleNamespace(resource_mgr=SimpleNamespace(
        get_tool=lambda tool_id: registry.get(tool_id),
        add_tool=lambda tool: registry.setdefault(tool.card.id, tool),
    )))
    first_fs = LocalFiles()
    second_fs = LocalFiles() if different_fs else first_fs
    first = module.ConcurrentSafeTaskPlanningRail()
    first.init(make_agent(tmp_path / "a", first_fs))
    second_root = tmp_path / ("b" if different_workspace else "a")
    second_agent = make_agent(second_root, second_fs)
    # An inherited ability card must not keep pointing at the old binding.
    second_agent.ability_manager.list.return_value = [tool.card for tool in first.tools]
    second = module.ConcurrentSafeTaskPlanningRail()
    second.init(second_agent)
    for old, new in zip(first.tools, second.tools):
        assert old.card.id != new.card.id
        assert registry[new.card.id] is new
        assert new.fs is second_fs
        assert new.workspace == str(second_root)
    assert second_agent.ability_manager.remove.call_count == 3
    if different_workspace:
        await first._find_todo_create_tool()._create_from_list("same-session", [
            {"id": "a", "content": "Tenant A"},
        ])
        await second._find_todo_create_tool()._create_from_list("same-session", [
            {"id": "b", "content": "Tenant B"},
        ])
        for rail, expected in ((first, "a"), (second, "b")):
            for tool in rail.tools:
                assert [todo.id for todo in await tool.load_todos("same-session")] == [expected]
