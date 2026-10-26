"""Host rails/writers against the new SDK and actual temporary Todo files."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from openjiuwen.core.session.agent import Session
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
from openjiuwen.core.sys_operation.local._rw_lock_manager import ReadWriteLockManager
from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
from openjiuwen.harness.tools.todo import TodoCreateTool, TodoModifyTool
from openjiuwen.harness.tools.todo_store import (
    _BINDING,
    bind_todo_batch,
    prepare_todo_batch,
)

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    capture_deepresearch_todo_target,
    persist_deepresearch_task_update,
)
from jiuwenswarm.agents.harness.common.tools.todo_compat import (
    _install_backlog_create_patch,
)
from jiuwenswarm.agents.harness.common.tools.todo_resume import remember_turn_user_query
from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    pause_pending_todos_on_tool,
)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    await ReadWriteLockManager.stop()
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    token = _BINDING.set(None)
    fs = FsOperation("fs", OperationMode.LOCAL, "test", LocalWorkConfig())
    op = SimpleNamespace(fs=lambda: fs)
    session = Session("s")
    yield (
        TodoCreateTool(op, str(tmp_path)),
        TodoModifyTool(op, str(tmp_path)),
        session,
        tmp_path / "s/todo.json",
    )
    _BINDING.reset(token)
    await ReadWriteLockManager.stop()


@pytest.mark.asyncio
async def test_reminder_create_and_mark_are_one_commit(env, monkeypatch):
    c, _, session, path = env

    # Patch a disposable subclass so no other test's global tool gets rebound.
    class ReminderCreator(TodoCreateTool):
        pass

    _install_backlog_create_patch(ReminderCreator)
    tool = ReminderCreator(SimpleNamespace(fs=lambda: c.fs), c.workspace)
    remember_turn_user_query(session, "提醒我明天下午提交报告")
    writes = []
    original = tool.fs.write_file

    async def capture(*args, **kwargs):
        writes.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(tool.fs, "write_file", capture)
    result = await tool.invoke(
        {"tasks": [{"id": "reminder", "content": "明天下午提交报告"}]}, session=session
    )
    assert len(writes) == 1
    saved = json.loads(path.read_text())
    assert saved["revision"] == 1
    assert saved["todos"][0]["status"] == "pending"
    assert saved["todos"][0]["meta_data"]["persistent"] is True
    assert result.todo_snapshot == saved


@pytest.mark.asyncio
async def test_pause_and_concurrent_completion_preserve_new_state(env):
    c, m, session, path = env
    await c.invoke(
        {"tasks": [{"id": "A", "content": "A"}, {"id": "B", "content": "B"}]},
        session=session,
    )
    await asyncio.gather(
        pause_pending_todos_on_tool(m, "s"),
        m.invoke(
            {"action": "update", "todos": [{"id": "A", "status": "completed"}]},
            session=session,
        ),
    )
    assert json.loads(path.read_text())["todos"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_event_uses_its_commit_even_after_another_write(env):
    c, m, session, path = env
    result = await c.invoke({"tasks": [{"id": "A", "content": "A"}]}, session=session)
    await m.invoke(
        {"action": "update", "todos": [{"id": "A", "status": "completed"}]},
        session=session,
    )
    events = []

    async def write_stream(event):
        events.append(event)

    sink = SimpleNamespace(write_stream=write_stream)
    rail = JiuSwarmStreamEventRail()
    await rail._emit_todo_updated(sink, "s", snapshot=result.todo_snapshot)
    assert events[0].payload["todo_meta"]["revision"] == 1
    assert events[0].payload["todos"][0]["status"] == "in_progress"
    assert json.loads(path.read_text())["revision"] == 2


@pytest.mark.asyncio
async def test_research_updates_only_captured_plan_existing_stages(env):
    c, m, session, path = env
    tasks = [
        {"id": f"deepresearch_stage_{i}", "content": title}
        for i, title in enumerate(DEEPRESEARCH_STAGES, 1)
    ]
    tasks.append({"id": "keep", "content": "unrelated"})
    await c.invoke({"tasks": tasks}, session=session)
    ctx = SimpleNamespace(extra={}, session=session)
    prepare_todo_batch(ctx)
    bind_todo_batch(ctx, m)
    target = await capture_deepresearch_todo_target(path)
    payload = {
        "event_type": "task.update",
        "tasks": [
            {"task_id": t["id"], "task_content": t["content"], "status": "completed"}
            for t in tasks[:-1]
        ],
    }
    assert await persist_deepresearch_task_update(
        payload, todo_path=path, target=target
    )
    saved = json.loads(path.read_text())
    assert (
        saved["todos"][-1]["id"] == "keep" and saved["todos"][-1]["status"] == "pending"
    )
    c.reset_invoke_marker("s")
    await c.invoke({"tasks": tasks, "plan_action": "new"}, session=session)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="older plan"):
        await persist_deepresearch_task_update(payload, todo_path=path, target=target)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_auto_advance_uses_latest_plan_and_refuses_old_work(env):
    from openjiuwen.harness.schema.task import TodoStatus

    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    c, m, session, path = env
    await c.invoke(
        {"tasks": [{"id": "A", "content": "A"}, {"id": "B", "content": "B"}]},
        session=session,
    )
    await m.invoke(
        {"action": "update", "todos": [{"id": "A", "status": "completed"}]},
        session=session,
    )
    events = []

    async def stream(event):
        events.append(event)

    session.write_stream = stream
    rail = TaskExecutionRail()
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            sys_operation=SimpleNamespace(fs=lambda: m.fs),
            workspace=SimpleNamespace(get_node_path=lambda _node: path.parent.parent),
        )
    )
    ctx = SimpleNamespace(session=session, extra={}, inputs=SimpleNamespace())
    prepare_todo_batch(ctx)
    bind_todo_batch(ctx, m)
    rail._todo_map = rail._build_map_from_todo_items(rail._load_todo_from_json("s"))
    await rail._auto_advance_pending_to_in_progress(ctx, task_id="B")
    assert (await m.load_todos("s"))[1].status == TodoStatus.IN_PROGRESS
    assert (
        events[-1].payload["todo_meta"]["revision"]
        == json.loads(path.read_text())["revision"]
    )
    c.reset_invoke_marker("s")
    await c.invoke(
        {
            "plan_action": "new",
            "tasks": [{"id": "A", "content": "New"}, {"id": "B", "content": "New B"}],
        },
        session=session,
    )
    rail._todo_map["B"]["status"] = "pending"
    before = path.read_bytes()
    await rail._auto_advance_pending_to_in_progress(ctx, task_id="B")
    assert path.read_bytes() == before
