# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Actual SDK + file + host projections; no model or product services."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    SysOperation,
    SysOperationCard,
)
from openjiuwen.core.sys_operation.local._rw_lock_manager import ReadWriteLockManager
from openjiuwen.harness.schema.task import TodoItem, TodoStatus
from openjiuwen.harness.tools.todo import TodoCreateTool

from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    persist_deepresearch_task_update_transactional as persist,
)
from jiuwenswarm.agents.harness.common.tools.todo_compat import CompatibleTodoModifyTool
from jiuwenswarm.common.todo_snapshot import (
    load_todo_event_for_frontend,
    publish_todo_receipt,
)
from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    snapshot_and_isolate_unfinished_todos,
)


@pytest_asyncio.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJIUWEN_TODO_V2", "1")
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    op = SysOperation(
        SysOperationCard(
            id="host-todo-v2",
            work_config=LocalWorkConfig(
                sandbox_root=[str(tmp_path)], restrict_to_sandbox=True
            ),
        )
    )
    # Pin the actual host contract regardless of other modules installing the
    # SDK export patch during pytest collection.
    tool = CompatibleTodoModifyTool(op, str(tmp_path))
    state = {"todo_execution_id_v2": "execution-1"}
    session = SimpleNamespace(
        get_session_id=lambda: "s",
        get_state=lambda key: state.get(key),
        update_state=lambda update: state.update(update),
        write_stream=AsyncMock(),
    )
    await tool.prepare_context(session, new_execution=True)
    yield tool, session, state, op
    await ReadWriteLockManager.close_locks()


def stages(active):
    return {
        "event_type": "task.update",
        "tasks": [
            {
                "task_id": f"deepresearch_stage_{i}",
                "task_content": title,
                "status": "completed"
                if active is None or i < active
                else "in_progress"
                if i == active
                else "pending",
            }
            for i, title in enumerate(DEEPRESEARCH_STAGES, 1)
        ],
    }


def owner(state):
    return {
        **state["todo_call_context_v2"],
        "session_id": "s",
        "workflow_id": "research-one",
    }


@pytest.mark.asyncio
async def test_research_owns_stages_preserves_append_and_never_resurrects_deleted(
    context, tmp_path
):
    tool, session, state, _ = context
    binding = owner(state)
    first = await persist(
        stages(1),
        todo_path=tmp_path / "ignored",
        tool=tool,
        session=session,
        binding=binding,
    )
    assert first.snapshot.metadata["plan_id"] == binding["planned_plan_id"]
    await tool.invoke(
        {
            "action": "append",
            "todos": [{"id": "C", "content": "unrelated", "status": "pending"}],
        },
        session=session,
    )
    await tool.invoke(
        {"action": "delete", "ids": ["deepresearch_stage_2"]}, session=session
    )
    last = await persist(
        stages(None),
        todo_path=tmp_path / "ignored",
        tool=tool,
        session=session,
        binding=binding,
    )
    items = {item["id"]: item for item in last.snapshot.todos}
    assert "deepresearch_stage_2" not in items
    assert items["C"]["status"] == "pending"
    assert items["deepresearch_stage_1"]["status"] == "completed"
    assert last.snapshot.metadata["plan_id"] == first.snapshot.metadata["plan_id"]


@pytest.mark.asyncio
async def test_research_does_not_replace_active_parent_or_write_without_binding(
    context, tmp_path
):
    tool, session, state, _ = context
    await tool.invoke(
        {
            "action": "append",
            "todos": [{"id": "parent", "content": "parent", "status": "pending"}],
        },
        session=session,
    )
    before = await tool.todo_store("s").read()
    assert (
        await persist(
            stages(1),
            todo_path=tmp_path / "ignored",
            tool=tool,
            binding=owner(state),
            session=session,
        )
        is None
    )
    assert await persist(stages(1), todo_path=tmp_path / "ignored", tool=tool) is None
    assert (await tool.todo_store("s").read()).content_sha256 == before.content_sha256


@pytest.mark.asyncio
async def test_research_lost_initial_response_read_confirms_but_new_writer_is_fenced(
    context, tmp_path
):
    tool, session, state, _ = context
    binding = owner(state)
    lost = dict(binding)
    await persist(
        stages(1), todo_path=tmp_path, tool=tool, binding=binding, session=session
    )
    lost["planned_plan_id"] = binding["planned_plan_id"]
    result = await persist(
        stages(2), todo_path=tmp_path, tool=tool, binding=lost, session=session
    )
    assert result.snapshot.todos[0]["status"] == "completed"
    state["todo_execution_id_v2"] = "new-writer"
    await tool.prepare_context(session, new_execution=True)
    with pytest.raises(Exception, match="superseded"):
        await persist(
            stages(None), todo_path=tmp_path, tool=tool, binding=lost, session=session
        )


@pytest.mark.asyncio
async def test_research_keeps_reminders_and_rejects_unsafe_leaf(context, tmp_path):
    tool, session, state, _ = context
    await tool.update_todos(
        "s",
        lambda items: items.append(
            TodoItem(id="reminder", content="remember", meta_data={"persistent": True})
        ),
        session=session,
    )
    result = await persist(
        stages(1), todo_path=tmp_path, tool=tool, binding=owner(state), session=session
    )
    assert result.snapshot.todos[-1]["id"] == "reminder"
    target = Path(tool.todo_store("s").path)
    other = tmp_path / "other.json"
    target.rename(other)
    target.symlink_to(other)
    with pytest.raises(OSError, match="unsafe"):
        await persist(
            stages(2),
            todo_path=tmp_path,
            tool=tool,
            binding=owner(state),
            session=session,
        )
    assert other.exists()


@pytest.mark.asyncio
async def test_receipt_events_can_arrive_backwards_and_history_matches_commit(
    context, tmp_path
):
    tool, session, state, _ = context
    first = await tool.invoke(
        {
            "action": "append",
            "todos": [
                {"id": "A", "content": "A", "status": "pending"},
                {"id": "B", "content": "B", "status": "pending"},
            ],
        },
        session=session,
    )
    second = await tool.invoke(
        {"action": "update", "todos": [{"id": "B", "status": "completed"}]},
        session=session,
    )
    await publish_todo_receipt(session, second.todo_receipt)
    await publish_todo_receipt(session, first.todo_receipt)
    emitted = [call.args[0].payload for call in session.write_stream.call_args_list]
    assert int(emitted[0]["todo_meta"]["revision"]) > int(
        emitted[1]["todo_meta"]["revision"]
    )
    assert emitted[0]["todos"][1]["status"] == "completed"
    assert emitted[1]["todos"][1]["status"] == "pending"
    assert load_todo_event_for_frontend("s", todo_root=tmp_path) == emitted[0]
    session.write_stream.side_effect = RuntimeError("sink unavailable")
    assert await publish_todo_receipt(session, second.todo_receipt) is False
    assert state["todo_snapshot_resync_pending"] is True
    assert session.write_stream.call_count == 5
    assert (await tool.todo_store("s").read()).metadata[
        "revision"
    ] == second.todo_receipt.snapshot.metadata["revision"]


@pytest.mark.asyncio
async def test_pause_is_one_transaction_and_does_not_drop_reminders(context):
    tool, session, _state, _ = context
    await tool.update_todos(
        "s",
        lambda items: items.extend(
            [
                TodoItem(id="done", content="done", status=TodoStatus.COMPLETED),
                TodoItem(id="work", content="work", status=TodoStatus.IN_PROGRESS),
                TodoItem(
                    id="reminder", content="remember", meta_data={"persistent": True}
                ),
            ]
        ),
        session=session,
    )
    before = await tool.todo_store("s").read()
    snapshot = await snapshot_and_isolate_unfinished_todos(tool, "s")
    after = await tool.todo_store("s").read()
    assert {item["id"] for item in after.todos} == {"done", "reminder"}
    assert int(after.metadata["revision"]) == int(before.metadata["revision"]) + 1
    assert snapshot["todo_binding"] == after.binding()


@pytest.mark.asyncio
async def test_flash_actual_engines_forward_plan_action_and_keep_compat_batch_semantics(
    context, tmp_path
):
    from openjiuwen.harness.tools.todo import TodoGetTool, TodoListTool

    from jiuwenswarm.agents.harness.common.tools.todo_compat import (
        CompatibleTodoModifyTool,
    )
    from jiuwenswarm.agents.harness.flash.tools.flash_todo import UnifiedTodoTool

    tool, session, state, operation = context
    engines = {
        "create": TodoCreateTool(operation, str(tmp_path)),
        "modify": CompatibleTodoModifyTool(operation, str(tmp_path)),
        "list": TodoListTool(operation, str(tmp_path)),
        "get": TodoGetTool(operation, str(tmp_path)),
    }
    flash = UnifiedTodoTool(engines, "en")
    created = await flash.invoke(
        {
            "action": "create",
            "tasks": [{"id": "A", "content": "A"}, {"id": "B", "content": "B"}],
        },
        session=session,
    )
    plan = created.todo_receipt.snapshot.metadata["plan_id"]
    await asyncio.gather(
        *(
            flash.invoke(
                {
                    "action": "modify",
                    "modify_action": "update",
                    "todos": [{"id": item, "status": "completed"}],
                },
                session=session,
            )
            for item in ("A", "B")
        )
    )
    assert all(
        item.status == TodoStatus.COMPLETED for item in await tool.load_todos("s")
    )
    before = await tool.todo_store("s").read()
    with pytest.raises(BaseError):
        await flash.invoke(
            {
                "action": "modify",
                "modify_action": "update",
                "todos": [
                    {"id": "A", "status": "pending"},
                    {"id": "missing", "status": "completed"},
                ],
            },
            session=session,
        )
    assert (await tool.todo_store("s").read()).content_sha256 == before.content_sha256
    state["todo_execution_id_v2"] = "next-round"
    await engines["create"].prepare_context(session, new_execution=True)
    engines["create"].reset_invoke_marker("s")
    result = await flash.invoke(
        {
            "action": "create",
            "plan_action": "new",
            "tasks": [{"id": "A", "content": "new"}],
        },
        session=session,
    )
    assert result.todo_receipt.snapshot.metadata["plan_id"] != plan


@pytest.mark.asyncio
async def test_auto_advance_uses_actual_store_and_publishes_committed_receipt(
    context, tmp_path
):
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    tool, session, _state, operation = context
    await tool.update_todos(
        "s", lambda items: items.append(TodoItem(id="A", content="A")), session=session
    )
    rail = TaskExecutionRail()
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            sys_operation=operation,
            workspace=SimpleNamespace(get_node_path=lambda node: tmp_path),
        )
    )
    rail._todo_map = rail._build_map_from_todo_items(
        (await tool.todo_store("s").read()).todos
    )
    ctx = SimpleNamespace(session=session, extra={}, inputs=SimpleNamespace())
    await rail._auto_advance_pending_to_in_progress(ctx, task_id="A")
    snapshot = await tool.todo_store("s").read()
    assert snapshot.todos[0]["status"] == "in_progress"
    event = next(
        call.args[0]
        for call in session.write_stream.call_args_list
        if call.args[0].type == "todo.updated"
    )
    assert event.payload["todo_meta"]["revision"] == snapshot.metadata["revision"]


def test_deepresearch_persists_captured_binding_for_hitl_without_aliasing():
    from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
        DeepResearchExecutionContext,
        _persist,
    )

    saved = []
    binding = {
        "store_id": "s",
        "plan_id": "p",
        "writer_epoch": "e",
        "workflow_id": "w",
        "planned_plan_id": "next",
    }
    context = DeepResearchExecutionContext(
        tool_call_id="w",
        agent_id="agent",
        state=None,
        user_input=None,
        model=None,
        save_state=saved.append,
        todo_binding=binding,
    )
    result = _persist(context, {}, "wait_outline")
    binding["writer_epoch"] = "other"
    assert result["todo_binding"]["writer_epoch"] == "e"
    assert saved[0]["todo_binding"]["planned_plan_id"] == "next"


@pytest.mark.asyncio
async def test_code_tools_use_same_commit_protocol_and_public_plan_action(
    context, tmp_path
):
    from jiuwenswarm.agents.harness.code.tools.code_todo_tools import (
        CodeTodoCreateTool,
        CodeTodoModifyTool,
    )

    tool, session, _state, operation = context
    creator = CodeTodoCreateTool(operation, str(tmp_path))
    modifier = CodeTodoModifyTool(operation, str(tmp_path))
    assert "plan_action" in creator.card.input_params["properties"]
    created = await creator.invoke(
        {"tasks": [{"id": "A", "content": "A"}, {"id": "B", "content": "B"}]},
        session=session,
    )
    results = await asyncio.gather(
        *(
            modifier.invoke(
                {"action": "update", "todos": [{"id": item, "status": "completed"}]},
                session=session,
            )
            for item in ("A", "B")
        )
    )
    assert (
        len({result.todo_receipt.snapshot.metadata["revision"] for result in results})
        == 2
    )
    snapshot = await tool.todo_store("s").read()
    assert (
        snapshot.metadata["plan_id"]
        == created.todo_receipt.snapshot.metadata["plan_id"]
    )
    assert all(item["status"] == "completed" for item in snapshot.todos)


async def _ordered_commits(tool, session, first, second):
    """Hold the first transaction open while the second queues on the same store."""
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held():
        entered.set()
        await release.wait()
        return {"result": await first()}

    async def following():
        queued.set()
        return await second()

    owner_task = asyncio.create_task(tool.transact("s", held, session=session))
    await entered.wait()
    following_task = asyncio.create_task(following())
    await queued.wait()
    release.set()
    try:
        return await asyncio.wait_for(asyncio.gather(owner_task, following_task), 5)
    finally:
        release.set()
        await asyncio.gather(owner_task, following_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "flash", "code"])
async def test_unsupported_backend_stops_actual_callback_boundary_before_model(
    context, tmp_path, monkeypatch, mode
):
    import uuid

    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext,
        AgentCallbackEvent,
    )
    from openjiuwen.core.single_agent.rail.base import rail as boundary

    from jiuwenswarm.agents.harness.code.rails.code_task_planning_rail import (
        CodeTaskPlanningRail,
    )
    from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import (
        ConcurrentSafeTaskPlanningRail,
    )
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail

    _tool, session, _state, operation = context
    _state.pop("todo_call_context_v2", None)
    creator = TodoCreateTool(operation, str(tmp_path / "unsupported"))
    monkeypatch.setattr(
        creator.todo_store("s"), "capable", AsyncMock(return_value=False)
    )
    planning = {
        "normal": ConcurrentSafeTaskPlanningRail,
        "flash": FlashTodoRail,
        "code": CodeTaskPlanningRail,
    }[mode]()
    if mode == "flash":
        planning.engines = {"create": creator}
    else:
        planning.tools = [creator]
    callbacks = AgentCallbackManager(f"todo-preflight-{uuid.uuid4().hex}")
    agent = SimpleNamespace(agent_callback_manager=callbacks)
    ctx = AgentCallbackContext(agent=agent, session=session)
    model = AsyncMock()

    class ModelBoundary:
        @boundary(before=AgentCallbackEvent.BEFORE_MODEL_CALL)
        async def call(self, callback):
            return await model()

    await callbacks.register_rail(planning, agent)
    try:
        result = await ModelBoundary().call(ctx)
        assert (
            result["result_type"] == "error"
            and "atomic compare-and-replace" in result["output"]
        )
        model.assert_not_awaited()
        assert not (tmp_path / "unsupported").exists()
    finally:
        await callbacks.clear()


@pytest.mark.asyncio
async def test_research_native_hitl_reload_reuses_captured_store_and_stage_ownership(
    context, tmp_path
):
    import copy

    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext,
        ToolCallInputs,
    )

    from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
        DeepResearchExecutionRail,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch import execution
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import (
        _persist_todo_progress,
    )

    tool, session, state, operation = context
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            sys_operation=operation,
            workspace=SimpleNamespace(get_node_path=lambda _: tmp_path),
        )
    )
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    rail.init(agent)
    ctx = AgentCallbackContext(
        agent=agent,
        session=session,
        extra={"todo_call_context_v2": state["todo_call_context_v2"]},
        inputs=ToolCallInputs(
            tool_name="deepresearch_execute",
            tool_call=ToolCall(
                id="research",
                name="deepresearch_execute",
                type="function",
                arguments="{}",
            ),
            tool_args={},
        ),
    )
    push = SimpleNamespace(send_push=AsyncMock())
    route = {"request_id": "r", "channel_id": "test", "session_id": "s"}
    await rail.before_tool_call(ctx)
    try:
        first = await _persist_todo_progress(
            stages(1), todo_path=tmp_path / "unused", route=route, push=push
        )
        waiting = execution._persist(
            execution._execution_context.get(), {}, "wait_outline"
        )
        ctx.inputs.tool_result = {
            "schema_version": execution.EXECUTION_SCHEMA,
            "kind": "interaction",
            "state": waiting,
            "interaction": {
                "query": "Confirm outline",
                "questions": [
                    {
                        "header": "Outline",
                        "question": "Continue?",
                        "options": [{"label": "Yes"}],
                    }
                ],
            },
        }
    finally:
        await rail.after_tool_call(ctx)
    assert isinstance(ctx.inputs.tool_result, ToolInterruptException)
    interrupted_call = ctx.inputs.tool_result.tool_call
    assert interrupted_call.id == "research_interaction_1"
    assert execution.deepresearch_todo_context() == (None, None, None)
    await tool.invoke(
        {
            "action": "append",
            "todos": [{"id": "C", "content": "keep", "status": "pending"}],
        },
        session=session,
    )
    restored_state = copy.deepcopy(state)
    restored = SimpleNamespace(
        get_session_id=lambda: "s",
        get_state=restored_state.get,
        update_state=restored_state.update,
        write_stream=AsyncMock(),
    )
    resumed = DeepResearchExecutionRail(model_provider=lambda: None)
    resumed.init(agent)
    resumed_ctx = AgentCallbackContext(
        agent=agent,
        session=restored,
        extra={"todo_call_context_v2": restored_state["todo_call_context_v2"]},
        inputs=ToolCallInputs(
            tool_name="deepresearch_execute", tool_call=interrupted_call, tool_args={}
        ),
    )
    await resumed.before_tool_call(resumed_ctx)
    try:
        _captured_tool, binding, captured_session = (
            execution.deepresearch_todo_context()
        )
        assert binding["workflow_id"] == "research" and captured_session is restored
        last = await _persist_todo_progress(
            stages(None), todo_path=tmp_path / "unused", route=route, push=push
        )
        assert last.snapshot.metadata["plan_id"] == first.snapshot.metadata["plan_id"]
        rows = {row["id"]: row for row in last.snapshot.todos}
        assert rows["C"]["status"] == "pending"
        assert all(
            rows[f"deepresearch_stage_{i}"]["status"] == "completed"
            for i in range(1, 5)
        )
    finally:
        await resumed.after_tool_call(resumed_ctx)
    assert execution.deepresearch_todo_context() == (None, None, None)


@pytest.mark.asyncio
async def test_late_work_after_tool_callback_cannot_advance_same_id_in_new_plan(
    context, tmp_path
):
    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext,
        ToolCallInputs,
    )

    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    tool, session, _state, operation = context
    await tool.update_todos(
        "s",
        lambda rows: rows.append(TodoItem(id="A", content="old work")),
        session=session,
    )
    old_snapshot = await tool.todo_store("s").read()
    rail = TaskExecutionRail()
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            sys_operation=operation,
            workspace=SimpleNamespace(get_node_path=lambda _: tmp_path),
        )
    )
    rail._todo_map = rail._build_map_from_todo_items(rail._load_todo_from_json("s"))
    ctx = AgentCallbackContext(
        agent=rail._deep_agent,
        session=session,
        extra={},
        inputs=ToolCallInputs(
            tool_name="local_work",
            tool_call=ToolCall(
                id="old-work", name="local_work", type="function", arguments="{}"
            ),
            tool_args={},
        ),
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def work():
        await rail.before_tool_call(ctx)
        result = await operation.shell().execute_cmd(
            "echo old-work-finished", cwd=str(tmp_path), shell_type="sh", timeout=2
        )
        assert result.code == 0 and result.data.exit_code == 0
        started.set()
        await release.wait()
        await rail.after_tool_call(ctx)

    old_task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), 3)
        await TodoCreateTool(operation, str(tmp_path)).invoke(
            {"plan_action": "new", "tasks": [{"id": "A", "content": "new work"}]},
            session=session,
        )
        await tool.invoke(
            {"action": "update", "todos": [{"id": "A", "status": "pending"}]},
            session=session,
        )
        new_snapshot = await tool.todo_store("s").read()
        assert new_snapshot.metadata["plan_id"] != old_snapshot.metadata["plan_id"]
        session.write_stream.reset_mock()
        release.set()
        with pytest.raises(BaseError, match="superseded"):
            await asyncio.wait_for(old_task, 3)
        assert (
            await tool.todo_store("s").read()
        ).content_sha256 == new_snapshot.content_sha256
        session.write_stream.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(old_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["flash", "code", "normal"])
async def test_real_mode_rail_registers_tools_executed_by_ability_manager(
    context, tmp_path, mode
):
    import json
    import uuid

    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
    from openjiuwen.harness.deep_agent import DeepAgent

    from jiuwenswarm.agents.harness.code.rails.code_task_planning_rail import (
        CodeTaskPlanningRail,
    )
    from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import (
        ConcurrentSafeTaskPlanningRail,
    )
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail

    tool, session, _state, operation = context
    agent = DeepAgent(
        AgentCard(id=f"mode-todo-{uuid.uuid4().hex}", name="Todo mode test")
    )
    agent._deep_config = SimpleNamespace(
        sys_operation=operation,
        workspace=SimpleNamespace(get_node_path=lambda _: tmp_path),
    )
    rail = {
        "flash": FlashTodoRail,
        "code": CodeTaskPlanningRail,
        "normal": ConcurrentSafeTaskPlanningRail,
    }[mode]()
    rail.set_sys_operation(operation)
    rail.set_workspace(agent.deep_config.workspace)
    rail.init(agent)
    registered = [card.id for card in agent.ability_manager.list()]
    ctx = AgentCallbackContext(agent=agent, session=session)
    try:
        await rail.before_invoke(ctx)
        await rail.before_tool_call(ctx)
        create_name = "todo" if mode == "flash" else "todo_create"
        modify_name = "todo" if mode == "flash" else "todo_modify"
        args = {"tasks": [{"id": "A", "content": "A"}, {"id": "B", "content": "B"}]}
        if mode == "flash":
            args["action"] = "create"
        created = await agent.ability_manager.execute(
            ctx,
            [
                ToolCall(
                    id="create",
                    type="function",
                    name=create_name,
                    arguments=json.dumps(args),
                )
            ],
            session,
            parallel_tool_calls=True,
        )
        assert created[0][0].todo_receipt.changed
        # Creation ran in a tool task. The next model boundary adopts its
        # committed plan before the following batch captures tool context.
        await rail.before_model_call(ctx)
        await rail.before_tool_call(ctx)
        calls = []
        for item in ["A", "B"]:
            args = {"action": "update", "todos": [{"id": item, "status": "completed"}]}
            if mode == "flash":
                args.update(action="modify", modify_action="update")
            calls.append(
                ToolCall(
                    id=item,
                    type="function",
                    name=modify_name,
                    arguments=json.dumps(args),
                )
            )
        results = await agent.ability_manager.execute(
            ctx, calls, session, parallel_tool_calls=True
        )
        assert all(result[0].todo_receipt.changed for result in results)
        snapshot = await tool.todo_store("s").read()
        assert [row["status"] for row in snapshot.todos] == ["completed", "completed"]
        assert (
            snapshot.metadata["plan_id"]
            == created[0][0].todo_receipt.snapshot.metadata["plan_id"]
        )
    finally:
        rail.uninit(agent)
        agent.ability_manager.teardown_tools()
        await agent.agent_callback_manager.clear()
        for tool_id in registered:
            if Runner.resource_mgr.get_tool(tool_id) is not None:
                Runner.resource_mgr.remove_tool(tool_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn, new_owner",
    [
        ({"query": "adjust the description"}, True),
        ({"query": "继续"}, True),
        ({"query": "extra detail", "is_supplement": True}, False),
        ({"query": "answer", "answers": {"question": "yes"}}, False),
    ],
)
async def test_actual_request_preparation_preserves_plan_and_fences_replaced_execution(
    context, turn, new_owner
):
    from jiuwenswarm.server.runtime.agent_adapter.stale_todo_cleanup_helpers import (
        prepare_stale_todo_cleanup_for_request,
    )

    tool, session, _state, _ = context
    await tool.update_todos(
        "s", lambda rows: rows.append(TodoItem(id="A", content="keep")), session=session
    )
    before = await tool.todo_store("s").read()
    ready, release = asyncio.Event(), asyncio.Event()

    async def late_work():
        ready.set()
        await release.wait()
        return await tool.invoke(
            {"action": "update", "todos": [{"id": "A", "status": "completed"}]},
            session=session,
        )

    old_task = asyncio.create_task(late_work())
    await ready.wait()
    try:
        request = SimpleNamespace(
            session_id="s",
            request_id="execution-2",
            params={"mode": "agent.plan", **turn},
        )
        changed = await prepare_stale_todo_cleanup_for_request(
            request, agent_card=object(), runtime_session=session
        )
        assert changed is new_owner
        await tool.prepare_context(session, new_execution=changed)
        current = await tool.todo_store("s").read()
        assert current.metadata["plan_id"] == before.metadata["plan_id"]
        assert current.todos == before.todos
        assert (
            current.metadata["writer_epoch"] != before.metadata["writer_epoch"]
        ) is new_owner
        release.set()
        if new_owner:
            with pytest.raises(BaseError, match="superseded"):
                await old_task
            assert (await tool.load_todos("s"))[0].status == TodoStatus.PENDING
        else:
            await old_task
            assert (await tool.load_todos("s"))[0].status == TodoStatus.COMPLETED
    finally:
        release.set()
        await asyncio.gather(old_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", ["empty", "completed", "reminder"])
async def test_host_create_and_reminder_only_append_keep_one_atomic_plan(
    context, tmp_path, existing
):
    from jiuwenswarm.agents.harness.common.tools.todo_compat import (
        _install_backlog_create_patch,
    )
    from jiuwenswarm.agents.harness.common.tools.todo_resume import (
        remember_turn_user_query,
    )

    class HostCreator(TodoCreateTool):
        pass

    _install_backlog_create_patch(HostCreator)
    tool, session, _state, operation = context
    if existing != "empty":
        row = (
            TodoItem(id="old", content="old", status=TodoStatus.COMPLETED)
            if existing == "completed"
            else TodoItem(id="R", content="remember", meta_data={"persistent": True})
        )
        await tool.update_todos("s", lambda rows: rows.append(row), session=session)
    creator = HostCreator(operation, str(tmp_path))
    before = await tool.todo_store("s").read()
    result = await creator.invoke(
        {"tasks": [{"id": "A", "content": "work"}]}, session=session
    )
    created = result.todo_receipt.snapshot
    assert int(created.metadata["revision"]) == int(before.metadata["revision"]) + 1
    assert created.metadata["plan_id"] != before.metadata["plan_id"]
    assert created.todos[0]["status"] == "in_progress"
    if existing == "reminder":
        assert any(
            row["id"] == "R" and row["status"] == "pending" for row in created.todos
        )
    remember_turn_user_query(session, "提醒我明天提交报告")
    backlog = await creator.invoke(
        {"tasks": [{"id": "later", "content": "tomorrow"}]}, session=session
    )
    after = backlog.todo_receipt.snapshot
    assert after.metadata["plan_id"] == created.metadata["plan_id"]
    assert after.todos[:-1] == created.todos
    assert after.todos[-1]["status"] == "pending"
    with pytest.raises(BaseError, match="Duplicate Todo id"):
        await creator.invoke(
            {"tasks": [{"id": "later", "content": "collision"}]}, session=session
        )
    assert (await tool.todo_store("s").read()).content_sha256 == after.content_sha256
    remember_turn_user_query(session, "开始新的任务")
    with pytest.raises(BaseError, match="persistent reminders"):
        await creator.invoke(
            {"plan_action": "new", "tasks": [{"id": "later", "content": "collision"}]},
            session=session,
        )
    assert (await tool.todo_store("s").read()).content_sha256 == after.content_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["before_model_call", "after_invoke"])
async def test_host_callback_resends_latest_commit_without_business_replay(
    context, tmp_path, monkeypatch, hook
):
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    tool, session, state, operation = context
    result = await tool.update_todos(
        "s",
        lambda rows: rows.append(TodoItem(id="A", content="latest")),
        session=session,
    )
    session.write_stream.side_effect = OSError("queue unavailable")
    assert not await publish_todo_receipt(session, result.todo_receipt)
    before = await tool.todo_store("s").read()
    session.write_stream.side_effect = None
    session.write_stream.reset_mock()
    rail = JiuSwarmStreamEventRail()
    rail._deep_agent = SimpleNamespace(
        card=SimpleNamespace(id="resync-test"),
        deep_config=SimpleNamespace(
            sys_operation=operation,
            workspace=SimpleNamespace(get_node_path=lambda _: tmp_path),
        ),
    )
    ctx = SimpleNamespace(session=session, extra={rail._SID_KEY: "s"}, context=None)
    # Only unrelated prompt/checkpoint/cwd facilities are isolated; Todo lookup,
    # file read, pending marker, callback dispatch and publication are real.
    monkeypatch.setattr(rail, "_rebind_runtime_cwd", lambda **_: None)
    monkeypatch.setattr(rail, "_inject_tool_call_goal_schema", lambda _: None)
    monkeypatch.setattr(rail, "_ensure_tool_call_goal_prompt", lambda: None)
    monkeypatch.setattr(rail, "_maybe_early_checkpoint", AsyncMock())
    await getattr(rail, hook)(ctx)
    assert session.write_stream.await_count == 1
    assert session.write_stream.await_args.args[
        0
    ].payload == load_todo_event_for_frontend("s", todo_root=tmp_path)
    assert state["todo_snapshot_resync_pending"] is False
    assert (await tool.todo_store("s").read()).content_sha256 == before.content_sha256
    session.write_stream.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await publish_todo_receipt(session, result.todo_receipt)


@pytest.mark.asyncio
async def test_list_and_modify_projection_share_a_complete_reminder_snapshot(
    context, tmp_path
):
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )
    from jiuwenswarm.agents.harness.common.tools.todo_resume import (
        remember_turn_user_query,
    )

    tool, session, _state, _ = context
    await tool.update_todos(
        "s",
        lambda rows: rows.extend(
            [
                TodoItem(id="A", content="A"),
                TodoItem(id="R", content="remember", meta_data={"persistent": True}),
            ]
        ),
        session=session,
    )
    rail = JiuSwarmStreamEventRail()
    rail._main_todo_tool = tool
    await rail._emit_todo_updated(session, "s", tool_name="todo_list")
    await rail._emit_todo_updated(
        session,
        "s",
        tool_name="todo_modify",
        arguments={"action": "update", "todos": [{"id": "A"}]},
    )
    events = [call.args[0].payload for call in session.write_stream.call_args_list]
    assert events[0] == events[1]
    assert {row["id"] for row in events[0]["todos"]} == {"A", "R"}
    remember_turn_user_query(session, "删除所有待办")
    await tool.invoke({"action": "delete", "ids": ["A", "R"]}, session=session)
    assert load_todo_event_for_frontend("s", todo_root=tmp_path)["todos"] == []
    (tmp_path / "s/todo.json").write_text(
        '{"schema_version":2,"todos":[],"revision":"bad"}'
    )
    assert load_todo_event_for_frontend("s", todo_root=tmp_path) is None


@pytest.mark.asyncio
async def test_task_plan_sync_merges_status_intent_with_append_and_newer_completion(
    context,
):
    from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import (
        ConcurrentSafeTaskPlanningRail,
    )

    tool, session, _state, _ = context
    await tool.update_todos(
        "s",
        lambda rows: rows.extend(
            [TodoItem(id="A", content="A"), TodoItem(id="B", content="B")]
        ),
        session=session,
    )
    rail = ConcurrentSafeTaskPlanningRail()
    rail.tools = [tool]
    rail._todos_cache["s"] = await tool.load_todos("s")
    plan = SimpleNamespace(
        tasks=[
            TodoItem(id="A", content="A", status=TodoStatus.COMPLETED),
            TodoItem(id="B", content="B", status=TodoStatus.IN_PROGRESS),
        ]
    )
    ctx = SimpleNamespace(
        session=session,
        agent=SimpleNamespace(load_state=lambda _: SimpleNamespace(task_plan=plan)),
    )

    async def newer_changes():
        await tool.invoke(
            {
                "action": "append",
                "todos": [{"id": "C", "content": "C", "status": "pending"}],
            },
            session=session,
        )
        return await tool.invoke(
            {"action": "update", "todos": [{"id": "B", "status": "completed"}]},
            session=session,
        )

    await _ordered_commits(
        tool, session, newer_changes, lambda: rail._sync_todos_from_plan(ctx)
    )
    snapshot = await tool.todo_store("s").read()
    assert [(row["id"], row["status"]) for row in snapshot.todos] == [
        ("A", "completed"),
        ("B", "completed"),
        ("C", "pending"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_first", [True, False])
@pytest.mark.parametrize("action", ["delete", "complete"])
async def test_auto_advance_races_with_deletion_or_completion(
    context, tmp_path, auto_first, action
):
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    tool, session, _state, operation = context
    await tool.update_todos(
        "s",
        lambda rows: rows.extend(
            [TodoItem(id="A", content="A"), TodoItem(id="B", content="B")]
        ),
        session=session,
    )
    rail = TaskExecutionRail()
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            sys_operation=operation,
            workspace=SimpleNamespace(get_node_path=lambda _: tmp_path),
        )
    )
    rail._todo_map = rail._build_map_from_todo_items(
        (await tool.todo_store("s").read()).todos
    )
    ctx = SimpleNamespace(session=session, extra={}, inputs=SimpleNamespace())
    args = (
        {"action": "delete", "ids": ["A"]}
        if action == "delete"
        else {"action": "update", "todos": [{"id": "A", "status": "completed"}]}
    )
    auto = lambda: rail._auto_advance_pending_to_in_progress(ctx, task_id="A")
    modify = lambda: tool.invoke(args, session=session)
    await _ordered_commits(
        tool, session, *((auto, modify) if auto_first else (modify, auto))
    )
    items = {row.id: row for row in await tool.load_todos("s")}
    assert items["B"].status == TodoStatus.PENDING
    if action == "delete":
        assert "A" not in items
    else:
        assert items["A"].status == TodoStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_first", [True, False])
async def test_pause_and_completion_cannot_restore_stale_unfinished_state(
    context, complete_first
):
    tool, session, _state, _ = context
    await tool.update_todos(
        "s",
        lambda rows: rows.extend(
            [
                TodoItem(id="A", content="A", status=TodoStatus.IN_PROGRESS),
                TodoItem(id="reminder", content="keep", meta_data={"persistent": True}),
            ]
        ),
        session=session,
    )
    complete = lambda: tool.invoke(
        {"action": "update", "todos": [{"id": "A", "status": "completed"}]},
        session=session,
    )
    pause = lambda: snapshot_and_isolate_unfinished_todos(tool, "s")
    if complete_first:
        await _ordered_commits(tool, session, complete, pause)
    else:
        with pytest.raises(BaseError, match="not found"):
            await _ordered_commits(tool, session, pause, complete)
    items = {row.id: row for row in await tool.load_todos("s")}
    assert items["reminder"].meta_data == {"persistent": True}
    if complete_first:
        assert items["A"].status == TodoStatus.COMPLETED
    else:
        assert "A" not in items
        resume = {
            "action": "append",
            "todos": [{"id": "A", "content": "A", "status": "pending"}],
        }
        await tool.invoke(resume, session=session)
        with pytest.raises(BaseError, match="duplicated"):
            await tool.invoke(resume, session=session)
        assert [row.id for row in await tool.load_todos("s")].count("A") == 1
