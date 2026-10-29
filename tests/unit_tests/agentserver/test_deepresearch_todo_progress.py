# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for projecting DeepResearch stage snapshots to the harness todo file."""
# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.tools.todo import TodoItem

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TaskExecutionRail,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    _open_windows_parent,
    _TrustedParent,
    ResearchTodoTarget,
    _verify_named_parent,
    capture_deepresearch_todo_target,
    deepresearch_todo_path,
    persist_deepresearch_task_update,
)


@pytest_asyncio.fixture(autouse=True)
async def isolate_files(tmp_path, monkeypatch):
    from openjiuwen.core.sys_operation.local._rw_lock_manager import (
        ReadWriteLockManager,
    )
    from openjiuwen.harness.tools.todo_store import _BINDING
    token = _BINDING.set(None)
    await ReadWriteLockManager.stop()
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    yield
    _BINDING.reset(token)
    await ReadWriteLockManager.stop()


def _seed_plan(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plan_id": "P", "revision": 1, "todos": [
        {"id": f"deepresearch_stage_{i}", "content": title, "activeForm": title, "status": "pending"}
        for i, title in enumerate(DEEPRESEARCH_STAGES, 1)]}))


async def _persist(payload, *, todo_path):
    from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
    from openjiuwen.harness.tools.todo import TodoModifyTool
    fs = FsOperation("fs", OperationMode.LOCAL, "test", LocalWorkConfig())
    tool = TodoModifyTool(SimpleNamespace(fs=lambda: fs), str(todo_path.parent.parent))
    return await persist_deepresearch_task_update(
        payload, todo_path=todo_path,
        target=ResearchTodoTarget(tool, todo_path.parent.name, "P", asyncio.current_task()),
    )


@pytest.mark.asyncio
async def test_bound_callback_does_not_recreate_missing_stages(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.deepresearch import todo_progress

    path = tmp_path / "todo/session/todo.json"
    _seed_plan(path)
    document = json.loads(path.read_text())
    document["todos"].pop()
    path.write_text(json.dumps(document))
    original = path.read_bytes()
    warning = Mock()
    monkeypatch.setattr(todo_progress.logger, "warning", warning)
    assert not await _persist(_stage_snapshot(None), todo_path=path)
    assert path.read_bytes() == original
    warning.assert_called_once_with(
        "[deepresearch_stream] skip Todo persistence: canonical stages missing or changed"
    )


def _stage_snapshot(active_stage: int | None) -> dict:
    tasks = []
    for index, title in enumerate(DEEPRESEARCH_STAGES, start=1):
        if active_stage is None or index < active_stage:
            status = "completed"
        elif index == active_stage:
            status = "in_progress"
        else:
            status = "pending"
        tasks.append({
            "task_id": f"deepresearch_stage_{index}",
            "task_content": title,
            "status": status,
        })
    return {"event_type": "task.update", "tasks": tasks}


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_ordinary_research_publishes_completed_plan_without_binding(tmp_path, existing):
    from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
    from openjiuwen.harness.tools.todo import TodoModifyTool

    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import (
        _capture_todo_target,
        _get_route,
        _todo_workspace_key,
        push_deepresearch_route,
        reset_deepresearch_route,
    )

    path = tmp_path / "todo" / "session-1" / "todo.json"
    if existing:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([
            {"id": "deepresearch_stage_1", "content": DEEPRESEARCH_STAGES[0], "createdAt": "before"},
            {"id": "old", "content": "old plan"},
        ]))
    fs = FsOperation("fs", OperationMode.LOCAL, "test", LocalWorkConfig())
    tool = TodoModifyTool(SimpleNamespace(fs=lambda: fs), str(path.parent.parent))
    token = push_deepresearch_route("r", "c", "session-1", workspace_key="tenant-1",
                                    todo_tool_provider=lambda: tool)
    try:
        assert _todo_workspace_key(_get_route()) == "tenant-1"
        target = await _capture_todo_target(path, "session-1")
        assert target is not None
        assert await persist_deepresearch_task_update(_stage_snapshot(None), todo_path=path, target=target)
    finally:
        reset_deepresearch_route(token)
    document = json.loads(path.read_text())
    assert document["plan_id"] and document["revision"] == 1
    assert [item["id"] for item in document["todos"]] == [
        f"deepresearch_stage_{index}" for index in range(1, len(DEEPRESEARCH_STAGES) + 1)
    ]
    assert all(item["status"] == "completed" for item in document["todos"])
    if existing:
        assert document["todos"][0]["createdAt"] == "before"


@pytest.mark.asyncio
async def test_ordinary_research_does_not_replace_concurrent_updates(tmp_path):
    from openjiuwen.core.session.agent import Session
    from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
    from openjiuwen.harness.tools.todo import TodoModifyTool

    path = tmp_path / "todo" / "session-1" / "todo.json"
    _seed_plan(path)
    fs = FsOperation("fs", OperationMode.LOCAL, "test", LocalWorkConfig())
    tool = TodoModifyTool(SimpleNamespace(fs=lambda: fs), str(path.parent.parent))
    target = await capture_deepresearch_todo_target(path, tool=tool, session_id="session-1")
    await tool.invoke({"action": "append", "todos": [{"id": "new", "content": "concurrent", "status": "pending"}]},
                      session=Session("session-1"))
    before = path.read_bytes()
    assert not await persist_deepresearch_task_update(_stage_snapshot(None), todo_path=path, target=target)
    assert path.read_bytes() == before


def test_deepresearch_todo_path_uses_tenant_workspace(tmp_path, monkeypatch):
    calls = []

    def _resolve(workspace_key, **_kwargs):
        calls.append(workspace_key)
        return tmp_path

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "get_tenant_agent_workspace_dir",
        _resolve,
    )

    path = deepresearch_todo_path(
        session_id="session-1",
        workspace_key="ws-1",
    )

    assert path == tmp_path / "todo" / "session-1" / "todo.json"
    assert calls == ["ws-1"]


@pytest.mark.parametrize(
    "value",
    ["", "..", "a/b", r"a\b", "nul\0byte", "x" * 256],
)
@pytest.mark.parametrize("field", ["session_id", "workspace_key"])
def test_deepresearch_todo_path_rejects_unsafe_opaque_components(
    field, value, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "get_tenant_agent_workspace_dir",
        lambda workspace_key, **_kwargs: tmp_path,
    )
    values = {
        "session_id": "session-1",
        "workspace_key": "ws-1",
    }
    values[field] = value

    with pytest.raises(ValueError, match="^deepresearch_todo_invalid_path$"):
        deepresearch_todo_path(**values)


def test_deepresearch_todo_path_cannot_escape_tenant_workspace(
    tmp_path, monkeypatch
):
    tenant = tmp_path / "tenant-a"
    tenant.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "get_tenant_agent_workspace_dir",
        lambda workspace_key, **_kwargs: tenant,
    )

    with pytest.raises(ValueError, match="^deepresearch_todo_invalid_path$"):
        deepresearch_todo_path(
            session_id="../../tenant-b",
            workspace_key="ws-1",
        )


@pytest.mark.asyncio
async def test_persist_deepresearch_task_update_ignores_incomplete_snapshots(tmp_path):
    todo_path = tmp_path / "session-1" / "todo.json"

    for active_stage in range(1, len(DEEPRESEARCH_STAGES) + 1):
        assert not await _persist(
            _stage_snapshot(active_stage=active_stage),
            todo_path=todo_path,
        )
        assert not todo_path.exists()


@pytest.mark.asyncio
async def test_persist_deepresearch_task_update_retains_completed_four_stage_plan(tmp_path):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    _seed_plan(todo_path)

    assert await _persist(
        _stage_snapshot(active_stage=None),
        todo_path=todo_path,
    )

    items = json.loads(todo_path.read_text(encoding="utf-8"))["todos"]
    assert len(items) == 4
    assert [item["id"] for item in items] == [
        f"deepresearch_stage_{index}" for index in range(1, 5)
    ]
    assert [item["content"] for item in items] == list(DEEPRESEARCH_STAGES)
    assert [item["activeForm"] for item in items] == list(DEEPRESEARCH_STAGES)
    assert all(item["status"] == "completed" for item in items)
    for item in items:
        TodoItem.from_dict(item)

    rail = TaskExecutionRail()
    rail.init(SimpleNamespace(
        deep_config=SimpleNamespace(
            workspace=SimpleNamespace(
                get_node_path=lambda _node: tmp_path / "todo",
            )
        )
    ))
    retained = rail._load_todo_from_json("session-1")
    formatted = rail._format_tasks_for_update(retained, source="todo")
    assert [item["task_id"] for item in formatted] == [
        f"deepresearch_stage_{index}" for index in range(1, 5)
    ]
    assert all(item["status"] == "completed" for item in formatted)


@pytest.mark.asyncio
async def test_skill_complete_with_empty_todo_emits_no_task_update():
    rail = TaskExecutionRail()
    session = SimpleNamespace(write_stream=AsyncMock())
    context = SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="skill-complete-1"),
            tool_name="skill_complete",
            tool_args={},
            tool_result={"status": "completed"},
        ),
        extra={},
    )

    await rail.before_tool_call(context)
    await rail.after_tool_call(context)

    assert context.extra == {}
    session.write_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_deepresearch_task_update_publishes_with_atomic_replace(
    tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    _seed_plan(todo_path)
    replacements = []
    real_replace = os.replace

    def tracked_replace(source, destination, **kwargs):
        replacements.append((source, destination, kwargs))
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "os.replace",
        tracked_replace,
    )

    assert await _persist(
        _stage_snapshot(active_stage=None),
        todo_path=todo_path,
    )
    assert len(replacements) == 1
    source, destination, kwargs = replacements[0]
    if kwargs:
        assert destination == todo_path.name
        assert source.startswith(".todo.json-")
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
    else:
        assert destination == todo_path
        assert source.parent == todo_path.parent
        assert not source.exists()


@pytest.mark.asyncio
async def test_persist_rejects_symlink_session_directory(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    todo_root = tmp_path / "todo"
    todo_root.mkdir()
    try:
        (todo_root / "session-1").symlink_to(external, target_is_directory=True)
    except OSError:
        # Creating symlinks needs privilege on Windows; the rejection
        # semantics are covered where symlinks are creatable.
        pytest.skip("symlink creation requires privilege on this platform")

    with pytest.raises(OSError):
        await _persist(
            _stage_snapshot(active_stage=None),
            todo_path=todo_root / "session-1" / "todo.json",
        )

    assert not (external / "todo.json").exists()


@pytest.mark.asyncio
async def test_persist_fails_closed_when_named_parent_is_swapped_after_open(
    tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    todo_path.parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    detached = tmp_path / "detached"
    module_path = (
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "_open_trusted_parent"
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch import (
        todo_progress as todo_module,
    )

    real_open = todo_module._open_trusted_parent
    real_named_temp = tempfile.NamedTemporaryFile
    named_temp_dirs = []

    def open_then_swap(path):
        parent_fd = real_open(path)
        todo_path.parent.rename(detached)
        todo_path.parent.symlink_to(external, target_is_directory=True)
        return parent_fd

    def track_absolute_temp(*args, **kwargs):
        named_temp_dirs.append(Path(kwargs["dir"]).resolve())
        return real_named_temp(*args, **kwargs)

    monkeypatch.setattr(module_path, open_then_swap)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", track_absolute_temp)

    with pytest.raises(OSError):
        await _persist(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )

    assert list(external.iterdir()) == []
    assert list(detached.glob(".todo.json-*.tmp")) == []
    assert not (detached / "todo.json").exists()
    assert named_temp_dirs == []


@pytest.mark.asyncio
async def test_direct_persist_rejects_dotdot_parent_without_writing(tmp_path):
    todo_path = tmp_path / "safe" / ".." / "escaped" / "todo.json"

    with pytest.raises(OSError):
        await _persist(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )

    assert not (tmp_path / "escaped" / "todo.json").exists()


def test_windows_parent_chain_rejects_symlink_ancestor(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError:
        # Creating symlinks needs privilege on Windows; the rejection
        # semantics are covered where symlinks are creatable.
        pytest.skip("symlink creation requires privilege on this platform")

    with pytest.raises(OSError):
        _open_windows_parent(linked / "session" / "todo.json", create=True)

    assert list(external.iterdir()) == []


def test_windows_parent_chain_detects_identity_swap(tmp_path):
    todo_path = tmp_path / "todo" / "session" / "todo.json"
    ancestors = _open_windows_parent(todo_path, create=True)
    parent_identity = ancestors[-1][1]
    trusted = _TrustedParent(None, parent_identity, ancestors)
    detached = tmp_path / "detached"
    todo_path.parent.rename(detached)
    todo_path.parent.mkdir()

    with pytest.raises(OSError):
        _verify_named_parent(todo_path, trusted)


@pytest.mark.parametrize("leaf_type", ["symlink", "fifo", "hardlink", "oversize"])
@pytest.mark.asyncio
async def test_existing_untrusted_todo_leaf_does_not_block_safe_publish(
    leaf_type, tmp_path
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    todo_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    if leaf_type == "symlink":
        try:
            todo_path.symlink_to(outside)
        except OSError:
            # Creating symlinks needs privilege on Windows; the rejection
            # semantics are covered where symlinks are creatable.
            pytest.skip("symlink creation requires privilege on this platform")
    elif leaf_type == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO is unavailable on this platform")
        os.mkfifo(todo_path)
    elif leaf_type == "hardlink":
        os.link(outside, todo_path)
    else:
        todo_path.write_bytes(b"x" * (1_048_576 + 1))

    with pytest.raises(OSError):
        await _persist(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )

    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.asyncio
async def test_persist_cleans_owned_temp_after_serialization_baseexception(
    error_type, tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    _seed_plan(todo_path)

    def fail_dump(*_args, **_kwargs):
        raise error_type("failed")

    monkeypatch.setattr(
        "openjiuwen.harness.tools.todo_store.json.dumps",
        fail_dump,
    )

    with pytest.raises(error_type):
        await _persist(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )
    assert list(todo_path.parent.glob(".todo.json-*.tmp")) == []


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.asyncio
async def test_persist_cleans_owned_temp_after_replace_baseexception(
    error_type, tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    _seed_plan(todo_path)

    def fail_replace(*_args, **_kwargs):
        raise error_type("failed")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "os.replace",
        fail_replace,
    )

    from openjiuwen.core.sys_operation.local._atomic_file import _replace_file
    with pytest.raises(error_type):
        _replace_file(todo_path, b"new", "600")
    assert list(todo_path.parent.glob(".todo.json-*.tmp")) == []


@pytest.mark.asyncio
async def test_incomplete_snapshot_does_not_replace_retained_completed_plan(tmp_path):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    _seed_plan(todo_path)

    assert await _persist(
        _stage_snapshot(active_stage=None),
        todo_path=todo_path,
    )
    retained = todo_path.read_text(encoding="utf-8")

    assert not await _persist(
        _stage_snapshot(active_stage=1),
        todo_path=todo_path,
    )
    assert todo_path.read_text(encoding="utf-8") == retained


@pytest.mark.asyncio
async def test_persist_deepresearch_task_update_ignores_other_snapshots(tmp_path):
    todo_path = tmp_path / "session-1" / "todo.json"
    payload = {
        "event_type": "task.update",
        "tasks": [{
            "task_id": "another_task",
            "task_content": "not DeepResearch",
            "status": "completed",
        }],
    }

    assert not await _persist(payload, todo_path=todo_path)
    assert not todo_path.exists()
