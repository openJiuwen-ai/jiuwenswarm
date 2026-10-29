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
from openjiuwen.harness.tools.todo import TodoCreateTool, TodoModifyTool, TodoTool
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
    CompatibleTodoModifyTool,
    _install_backlog_create_patch,
)
from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    mark_persistent_todo,
    remember_turn_user_query,
)
from jiuwenswarm.common.todo_snapshot import TODO_SNAPSHOT_MODE
from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    pause_pending_todos_on_tool,
    snapshot_and_isolate_unfinished_todos,
)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    await ReadWriteLockManager.stop()
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    token = _BINDING.set(None)
    output_token = TODO_SNAPSHOT_MODE.set("versioned")
    fs = FsOperation("fs", OperationMode.LOCAL, "test", LocalWorkConfig())
    op = SimpleNamespace(fs=lambda: fs)
    session = Session("s")
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import (
        bind_todo_lifecycle,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata,
    )
    root = tmp_path / "sessions"
    init_session_metadata(session_id="s", sessions_root=root, new_todo_object=True)
    monkeypatch.setattr("jiuwenswarm.agents.harness.flash.rails.flash_todo_rail.get_agent_sessions_dir", lambda: root)
    creator, modifier = TodoCreateTool(op, str(tmp_path)), TodoModifyTool(op, str(tmp_path))
    bind_todo_lifecycle(creator)
    bind_todo_lifecycle(modifier)
    yield (
        creator,
        modifier,
        session,
        tmp_path / "s/todo.json",
    )
    _BINDING.reset(token)
    TODO_SNAPSHOT_MODE.reset(output_token)
    await ReadWriteLockManager.stop()


@pytest.mark.parametrize("language", ["cn", "en"])
def test_flash_prompt_keeps_explicit_rebuild_rule(language):
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail

    sections = []
    rail = FlashTodoRail()
    rail.system_prompt_builder = SimpleNamespace(language=language, add_section=sections.append)
    rail._inject_todo_section()
    content = sections[0].content[language]
    assert "plan_action=new/rebuild" in content
    assert "todo(action=modify)" in content
    assert "todo_create" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("versioned", [False, True])
async def test_normal_plan_refresh_preserves_legacy_empty_behavior(env, versioned):
    from openjiuwen.harness.schema.task import TaskPlan, TodoItem

    from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import (
        ConcurrentSafeTaskPlanningRail,
    )

    _, modifier, session, path = env
    path.parent.mkdir()
    path.write_text(json.dumps({"plan_id": "P", "revision": 1, "todos": []} if versioned else []))
    state = SimpleNamespace(task_plan=TaskPlan(tasks=[TodoItem(id="old", content="old")]))
    agent = SimpleNamespace(load_state=lambda _session: state, save_state=lambda *_args: None)
    rail = ConcurrentSafeTaskPlanningRail()
    rail.tools = [modifier]
    await rail._refresh_task_plan_from_todos(SimpleNamespace(session=session, agent=agent))
    assert (state.task_plan is None) is versioned


@pytest.mark.asyncio
@pytest.mark.parametrize("has_marker", [False, True])
async def test_flash_assembly_enforces_lifecycle_on_real_unified_tool(env, has_marker):
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail

    creator, _, session, path = env
    if not has_marker:
        metadata_path = path.parent.parent / "sessions/s/metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("todo_initialized")
        metadata_path.write_text(json.dumps(metadata))
    registered = {}
    rail = FlashTodoRail()
    rail.set_workspace(SimpleNamespace(get_node_path=lambda _node: creator.workspace))
    rail.set_sys_operation(SimpleNamespace(fs=lambda: creator.fs))
    agent = SimpleNamespace(
        card=SimpleNamespace(id="flash-lifecycle"),
        system_prompt_builder=SimpleNamespace(language="cn"),
        ability_manager=SimpleNamespace(add_ability=lambda card, tool: registered.setdefault(card.name, tool)),
    )
    rail.init(agent)
    ctx = SimpleNamespace(extra={}, session=session)
    await rail.before_invoke(ctx)
    tool = registered["todo"]
    result = await tool.invoke({"action": "create", "tasks": [
        {"id": "A", "content": "Flash"}, {"id": "B", "content": "Keep"},
    ]}, session=session)
    assert json.loads(path.read_text()) == result.todo_snapshot
    completed, appended = await asyncio.gather(
        tool.invoke({"action": "modify", "modify_action": "update",
                     "todos": [{"id": "A", "status": "completed"}]}, session=session),
        tool.invoke({"action": "modify", "modify_action": "append",
                     "todos": [{"id": "C", "content": "Added", "status": "pending"}]}, session=session),
    )
    saved = json.loads(path.read_text())
    assert [(row["id"], row["status"]) for row in saved["todos"]] == [
        ("A", "completed"), ("B", "pending"), ("C", "pending"),
    ]
    assert saved["todos"][1] == result.todo_snapshot["todos"][1]
    assert {completed.todo_snapshot["revision"], appended.todo_snapshot["revision"]} == {2, 3}
    await rail.before_invoke(ctx)
    before = path.read_bytes()
    with pytest.raises(Exception, match="active plan"):
        await tool.invoke({"action": "create", "tasks": [{"id": "new", "content": "New"}]}, session=session)
    assert path.read_bytes() == before

    from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
        JiuwenSwarmFlashAdapter,
    )

    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._instance = SimpleNamespace()
    adapter._task_planning_rail = rail
    adapter._stream_event_rail = SimpleNamespace(_format_todos_for_frontend=lambda todos: [t.to_dict() for t in todos])
    assert adapter._get_todo_modify_tool("s") is rail.engines["modify"]
    cancelled = await adapter._cancel_pending_todos("s")
    assert [(row["id"], row["status"]) for row in cancelled] == [
        ("A", "completed"), ("B", "cancelled"), ("C", "cancelled"),
    ]
    path.unlink()
    await rail.before_invoke(ctx)
    with pytest.raises(Exception, match="first initialization"):
        await tool.invoke({"action": "create", "plan_action": "rebuild", "tasks": [{"id": "A", "content": "bad"}]}, session=session)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("had_legacy_todo", [False, True])
async def test_unknown_legacy_empty_and_lost_file_share_the_accepted_initialization_boundary(env, had_legacy_todo):
    creator, _, session, path = env
    metadata_path = path.parent.parent / "sessions/s/metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("todo_initialized")
    metadata_path.write_text(json.dumps(metadata))
    if had_legacy_todo:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"id": "previous", "content": "Previous"}]))
        path.unlink()
    result = await creator.invoke({"tasks": [{"id": "A", "content": "New"}]}, session=session)
    assert result.todo_snapshot == json.loads(path.read_text())
    assert json.loads(metadata_path.read_text())["todo_initialized"] is True
    path.unlink()
    creator.reset_invoke_marker("s")
    with pytest.raises(Exception, match="first initialization"):
        await creator.invoke({"tasks": [{"id": "B", "content": "must not recreate"}]}, session=session)
    assert not path.exists()


@pytest.mark.asyncio
async def test_legacy_session_with_existing_todo_can_modify_and_records_origin(env):
    _, modifier, session, path = env
    metadata_path = path.parent.parent / "sessions/s/metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("todo_initialized")
    metadata_path.write_text(json.dumps(metadata))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": "A", "content": "old", "status": "in_progress"}]))
    result = await modifier.invoke({"action": "update", "todos": [{"id": "A", "status": "completed"}]},
                                   session=session)
    assert result.todo_snapshot == json.loads(path.read_text())
    assert result.todo_snapshot["plan_id"] == "legacy"
    assert result.todo_snapshot["revision"] == 1
    assert result.todo_snapshot["todos"][0]["status"] == "completed"
    assert json.loads(metadata_path.read_text())["todo_initialized"] is True
    path.unlink()
    with pytest.raises(Exception, match="first initialization"):
        await modifier.invoke({"action": "update", "todos": [{"id": "A", "status": "pending"}]}, session=session)
    assert not path.exists()


@pytest.mark.asyncio
async def test_reminder_create_and_mark_are_one_commit(env, monkeypatch):
    c, _, session, path = env

    # Patch a disposable subclass so no other test's global tool gets rebound.
    class ReminderCreator(TodoCreateTool):
        pass

    _install_backlog_create_patch(ReminderCreator)
    tool = ReminderCreator(SimpleNamespace(fs=lambda: c.fs), c.workspace)
    tool.snapshot_guard = c.snapshot_guard
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
async def test_pause_isolation_preserves_reminder_without_legacy_cache(env, monkeypatch):
    creator, original, session, path = env
    await creator.invoke(
        {"tasks": [{"id": "reminder", "content": "remind"}, {"id": "work", "content": "work"}]},
        session=session,
    )

    async def mark(items):
        mark_persistent_todo(items[0])

    await original.update_todos("s", mark)
    before = json.loads(path.read_text())
    modifier = CompatibleTodoModifyTool(SimpleNamespace(fs=lambda: original.fs), original.workspace)
    modifier.snapshot_guard = original.snapshot_guard

    async def legacy_cache(*args, **kwargs):
        raise AssertionError("Pause isolation must not use the legacy cache")

    monkeypatch.setattr(TodoTool, "load_todos", legacy_cache)
    monkeypatch.setattr(TodoTool, "save_todos", legacy_cache)
    snapshot = await snapshot_and_isolate_unfinished_todos(modifier, "s")
    saved = json.loads(path.read_text())
    assert saved["revision"] == before["revision"] + 1
    assert [(item["id"], item["status"]) for item in saved["todos"]] == [("reminder", "pending")]
    assert {item["id"] for item in snapshot["unfinished"]} == {"reminder", "work"}


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
async def test_cancelled_research_cannot_write_back_to_same_plan(env):
    c, m, session, path = env
    await c.invoke({"tasks": [
        {"id": f"deepresearch_stage_{i}", "content": title}
        for i, title in enumerate(DEEPRESEARCH_STAGES, 1)
    ]}, session=session)
    ready = asyncio.Event()
    captured = []

    async def research():
        ctx = SimpleNamespace(extra={}, session=session)
        prepare_todo_batch(ctx)
        bind_todo_batch(ctx, m)
        captured.append(await capture_deepresearch_todo_target(path))
        ready.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(research())
    await ready.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    before = path.read_bytes()
    payload = {"event_type": "task.update", "tasks": [
        {"task_id": f"deepresearch_stage_{i}", "task_content": title, "status": "completed"}
        for i, title in enumerate(DEEPRESEARCH_STAGES, 1)
    ]}
    with pytest.raises(ValueError, match="inactive request"):
        await persist_deepresearch_task_update(payload, todo_path=path, target=captured[0])
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="no captured owner"):
        await persist_deepresearch_task_update(payload, todo_path=path)
    # The subsequent request can continue the same plan.
    await m.invoke({"action": "update", "todos": [
        {"id": "deepresearch_stage_1", "status": "completed"}
    ]}, session=session)
    assert json.loads(path.read_text())["revision"] == json.loads(before)["revision"] + 1


@pytest.mark.asyncio
async def test_recreated_tools_reject_missing_file_even_with_explicit_rebuild(env):
    from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import (
        bind_todo_lifecycle,
    )

    c, _, session, path = env
    result = await c.invoke({"tasks": [{"id": "A", "content": "first"}]}, session=session)
    committed = path.read_bytes()
    path.unlink()
    restarted = TodoCreateTool(SimpleNamespace(fs=lambda: c.fs), c.workspace)
    bind_todo_lifecycle(restarted)
    with pytest.raises(Exception, match="first initialization"):
        await restarted.invoke({"plan_action": "rebuild", "tasks": [{"id": "A", "content": "bad"}]}, session=session)
    assert not path.exists()
    # Restoring this exact latest authoritative file keeps the version sequence.
    path.write_bytes(committed)
    after = await restarted.invoke({"plan_action": "rebuild", "tasks": [{"id": "A", "content": "new"}]}, session=session)
    assert after.todo_snapshot["revision"] == result.todo_snapshot["revision"] + 1


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


@pytest.mark.asyncio
async def test_auto_advance_checks_latest_predecessor_state(env):
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    creator, modifier, session, path = env
    await creator.invoke({"tasks": [{"id": "A", "content": "A"}, {"id": "B", "content": "B"}]}, session=session)
    await modifier.invoke({"action": "update", "todos": [{"id": "A", "status": "completed"}]}, session=session)
    rail = TaskExecutionRail()
    rail._deep_agent = SimpleNamespace(deep_config=SimpleNamespace(
        sys_operation=SimpleNamespace(fs=lambda: modifier.fs),
        workspace=SimpleNamespace(get_node_path=lambda _node: path.parent.parent),
    ))
    rail._todo_map = rail._build_map_from_todo_items(rail._load_todo_from_json("s"))
    await modifier.invoke({"action": "update", "todos": [{"id": "A", "status": "pending"}]}, session=session)
    before = path.read_bytes()
    ctx = SimpleNamespace(session=session, extra={}, inputs=SimpleNamespace())
    await rail._auto_advance_pending_to_in_progress(ctx, task_id="B")
    assert path.read_bytes() == before
