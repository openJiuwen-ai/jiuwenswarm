# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Follow-up contracts for actual host writers and lifecycle boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    SysOperation,
    SysOperationCard,
)
from openjiuwen.core.sys_operation.local._rw_lock_manager import ReadWriteLockManager
from openjiuwen.harness.schema.task import TodoItem
from openjiuwen.harness.tools.todo import TodoModifyTool

from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    capture_deepresearch_todo_binding,
    persist_deepresearch_task_update_transactional,
)


@pytest_asyncio.fixture
async def legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENJIUWEN_TODO_V2", raising=False)
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    op = SysOperation(
        SysOperationCard(
            id="todo-followup",
            work_config=LocalWorkConfig(
                sandbox_root=[str(tmp_path)], restrict_to_sandbox=True
            ),
        )
    )
    tool = TodoModifyTool(op, str(tmp_path))
    state = {}
    session = SimpleNamespace(
        get_session_id=lambda: "s",
        get_state=state.get,
        update_state=state.update,
        write_stream=AsyncMock(),
    )
    yield tool, session, tmp_path
    await ReadWriteLockManager.close_locks()


def stages(active=None):
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


@pytest.mark.asyncio
async def test_legacy_research_merges_only_owned_stages_with_other_writer(legacy):
    tool, session, root = legacy
    binding = await capture_deepresearch_todo_binding(tool, "s", "research")
    first = await persist_deepresearch_task_update_transactional(
        stages(1), todo_path=root, tool=tool, binding=binding, session=session
    )
    assert first.snapshot.metadata is None
    await asyncio.gather(
        persist_deepresearch_task_update_transactional(
            stages(), todo_path=root, tool=tool, binding=binding, session=session
        ),
        tool.invoke(
            {
                "action": "append",
                "todos": [{"id": "C", "content": "keep", "status": "pending"}],
            },
            session=session,
        ),
    )
    items = {item.id: item for item in await tool.load_todos("s")}
    assert items["C"].content == "keep"
    assert all(
        items[f"deepresearch_stage_{i}"].status.value == "completed"
        for i in range(1, 5)
    )


@pytest.mark.asyncio
async def test_legacy_old_initialization_cannot_replace_later_parent(legacy):
    tool, session, root = legacy
    binding = await capture_deepresearch_todo_binding(tool, "s", "research")
    await tool.invoke(
        {
            "action": "append",
            "todos": [{"id": "parent", "content": "keep", "status": "pending"}],
        },
        session=session,
    )
    before = await tool.todo_store("s").read()
    result = await persist_deepresearch_task_update_transactional(
        stages(), todo_path=root, tool=tool, binding=binding, session=session
    )
    assert result is None
    assert (await tool.todo_store("s").read()).content_sha256 == before.content_sha256


@pytest.mark.asyncio
async def test_legacy_deleted_stages_do_not_return_and_reminder_survives(legacy):
    tool, session, root = legacy
    await tool.save_todos(
        "s",
        [TodoItem(id="reminder", content="remember", meta_data={"persistent": True})],
    )
    binding = await capture_deepresearch_todo_binding(tool, "s", "research")
    await persist_deepresearch_task_update_transactional(
        stages(1), todo_path=root, tool=tool, binding=binding, session=session
    )
    await tool.invoke(
        {"action": "delete", "ids": [f"deepresearch_stage_{i}" for i in range(1, 5)]},
        session=session,
    )
    await persist_deepresearch_task_update_transactional(
        stages(), todo_path=root, tool=tool, binding=binding, session=session
    )
    assert [item.id for item in await tool.load_todos("s")] == ["reminder"]


@pytest.mark.asyncio
async def test_unbound_legacy_research_has_no_file_side_effect(legacy):
    tool, session, root = legacy
    result = await persist_deepresearch_task_update_transactional(
        stages(), todo_path=root / "guessed.json", tool=tool, session=session
    )
    assert result is None
    assert not (root / "guessed.json").exists()
    assert not (root / "s" / "todo.json").exists()


@pytest.mark.asyncio
async def test_actual_rail_captures_legacy_owner_and_sender_commits_to_actual_store(
    legacy,
):
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
        DeepResearchExecutionRail,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
        deepresearch_todo_context,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import (
        _persist_todo_progress,
    )

    tool, session, root = legacy
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    operation = SimpleNamespace(fs=lambda: tool.fs)
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            sys_operation=operation,
            workspace=SimpleNamespace(get_node_path=lambda node: root),
        )
    )
    rail.init(agent)
    ctx = SimpleNamespace(
        agent=agent,
        session=session,
        extra={},
        inputs=ToolCallInputs(
            tool_name="deepresearch_execute",
            tool_call=SimpleNamespace(id="workflow", arguments={}),
            tool_args={},
        ),
    )
    await rail.before_tool_call(ctx)
    try:
        captured, binding, actual_session = deepresearch_todo_context()
        assert (
            captured is not None
            and binding["session_id"] == "s"
            and actual_session is session
        )
        push = SimpleNamespace(send_push=AsyncMock())
        receipt = await _persist_todo_progress(
            stages(),
            todo_path=root / "wrong-route.json",
            route={"request_id": "req", "channel_id": "test", "session_id": "s"},
            push=push,
        )
        assert receipt.changed
        assert not (root / "wrong-route.json").exists()
        assert len(await tool.load_todos("s")) == 4
        assert (
            push.send_push.await_args.args[0]["payload"]["event_type"] == "todo.updated"
        )
    finally:
        await rail.after_tool_call(ctx)
    assert deepresearch_todo_context() == (None, None, None)


@pytest.mark.asyncio
async def test_relay_bootstrap_cache_does_not_publish_native_staged_save(
    legacy, monkeypatch
):
    from pathlib import Path

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[4] / "relay-claw" / "extensions")
    )
    from todo_read_cache.rail import install_todo_read_cache

    tool, session, root = legacy
    assert install_todo_read_cache() is False
    await tool.save_todos("s", [TodoItem(id="A", content="original")])
    before = (root / "s" / "todo.json").read_bytes()

    async def abort():
        rows = await tool.load_todos("s")
        rows[0].content = "uncommitted"
        await tool.save_todos("s", rows)
        raise ValueError("abort staged write")

    with pytest.raises(ValueError, match="abort staged"):
        await tool.transact("s", abort, session=session)
    assert (root / "s" / "todo.json").read_bytes() == before
    assert (await tool.load_todos("s"))[0].content == "original"


@pytest.mark.asyncio
async def test_peak_queue_with_cancel_io_failure_unknown_commit_and_event_retry(
    tmp_path, monkeypatch
):
    from jiuwenswarm.common.todo_snapshot import publish_todo_receipt

    monkeypatch.setenv("OPENJIUWEN_TODO_V2", "1")
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    operation = SysOperation(
        SysOperationCard(
            id="todo-peak-faults",
            work_config=LocalWorkConfig(
                sandbox_root=[str(tmp_path)], restrict_to_sandbox=True
            ),
        )
    )
    tool = TodoModifyTool(operation, str(tmp_path))
    state = {}
    sink = AsyncMock(side_effect=OSError("injected transport outage"))
    session = SimpleNamespace(
        get_session_id=lambda: "peak",
        get_state=state.get,
        update_state=state.update,
        write_stream=sink,
    )
    await tool.save_todos(
        "peak", [TodoItem(id=str(i), content=f"item-{i}") for i in range(32)]
    )
    original = tool.fs.write_file

    async def faulted_write(*args, **kwargs):
        name = asyncio.current_task().get_name()
        index = int(name.removeprefix("fault-")) if name.startswith("fault-") else -1
        if index >= 0 and index % 7 == 0:
            return SimpleNamespace(
                code=1, message="injected I/O failure", failure_kind="io"
            )
        result = await original(*args, **kwargs)
        if index >= 0 and index % 11 == 0:
            result.data.atomic_replace = None
        return result

    monkeypatch.setattr(tool.fs, "write_file", faulted_write)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
        return {}

    owner_task = asyncio.create_task(tool.todo_store("peak").transact(hold))
    await entered.wait()
    queued = [asyncio.Event() for _ in range(32)]

    async def complete(index):
        queued[index].set()
        result = await tool.invoke(
            {"action": "update", "todos": [{"id": str(index), "status": "completed"}]},
            session=session,
        )
        assert await publish_todo_receipt(session, result.todo_receipt) is False
        return result

    tasks = [asyncio.create_task(complete(i), name=f"fault-{i}") for i in range(32)]
    try:
        await asyncio.gather(*(event.wait() for event in queued))
        for i, task in enumerate(tasks):
            if i % 5 == 0:
                task.cancel()
        release.set()
        await owner_task
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 15
        )
        snapshot = await tool.todo_store("peak").read()
        persisted = {
            int(row["id"]) for row in snapshot.todos if row["status"] == "completed"
        }
        succeeded = {i for i, result in enumerate(results) if isinstance(result, dict)}
        unknown = {i for i in range(32) if i % 5 != 0 and i % 7 != 0 and i % 11 == 0}
        assert succeeded | unknown == persisted
        assert all(
            isinstance(results[i], asyncio.CancelledError) for i in range(0, 32, 5)
        )
        assert all(not isinstance(results[i], dict) for i in range(7, 32, 7))
        assert sink.await_count == len(succeeded) * 3
        assert state["todo_snapshot_resync_pending"] is True
        sink.side_effect = None
        latest = SimpleNamespace(snapshot=snapshot)
        assert await publish_todo_receipt(session, latest) is True
        assert state["todo_snapshot_resync_pending"] is False
        assert (await tool.todo_store("peak").read()).metadata[
            "revision"
        ] == snapshot.metadata["revision"]
        # A new independent object still progresses after failures and drain.
        other = TodoModifyTool(operation, str(tmp_path / "other"))
        await asyncio.wait_for(
            other.save_todos("next", [TodoItem(id="next", content="healthy")]), 3
        )
    finally:
        release.set()
        await asyncio.gather(owner_task, *tasks, return_exceptions=True)
        await ReadWriteLockManager.close_locks()
