# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for projecting DeepResearch stage snapshots to the harness todo file."""
# pylint: disable=protected-access

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness.tools.todo import TodoItem
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TaskExecutionRail,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    _TrustedParent,
    _open_windows_parent,
    _verify_named_parent,
    deepresearch_todo_path,
    persist_deepresearch_task_update,
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


def test_persist_deepresearch_task_update_ignores_incomplete_snapshots(tmp_path):
    todo_path = tmp_path / "session-1" / "todo.json"

    for active_stage in range(1, len(DEEPRESEARCH_STAGES) + 1):
        assert not persist_deepresearch_task_update(
            _stage_snapshot(active_stage=active_stage),
            todo_path=todo_path,
        )
        assert not todo_path.exists()


def test_persist_deepresearch_task_update_retains_completed_four_stage_plan(tmp_path):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"

    assert persist_deepresearch_task_update(
        _stage_snapshot(active_stage=None),
        todo_path=todo_path,
    )

    items = json.loads(todo_path.read_text(encoding="utf-8"))
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


def test_persist_deepresearch_task_update_publishes_with_atomic_replace(
    tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
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

    assert persist_deepresearch_task_update(
        _stage_snapshot(active_stage=None),
        todo_path=todo_path,
    )
    assert len(replacements) == 1
    source, destination, kwargs = replacements[0]
    if kwargs:
        assert destination == todo_path.name
        assert source.startswith(".deepresearch-todo-")
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
    else:
        assert destination == todo_path
        assert source.parent == todo_path.parent
        assert not source.exists()


def test_persist_rejects_symlink_session_directory(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    todo_root = tmp_path / "todo"
    todo_root.mkdir()
    (todo_root / "session-1").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        persist_deepresearch_task_update(
            _stage_snapshot(active_stage=None),
            todo_path=todo_root / "session-1" / "todo.json",
        )

    assert not (external / "todo.json").exists()


def test_persist_fails_closed_when_named_parent_is_swapped_after_open(
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
        persist_deepresearch_task_update(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )

    assert list(external.iterdir()) == []
    assert list(detached.glob(".deepresearch-todo-*")) == []
    assert not (detached / "todo.json").exists()
    assert named_temp_dirs == []


def test_direct_persist_rejects_dotdot_parent_without_writing(tmp_path):
    todo_path = tmp_path / "safe" / ".." / "escaped" / "todo.json"

    with pytest.raises(OSError):
        persist_deepresearch_task_update(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )

    assert not (tmp_path / "escaped" / "todo.json").exists()


def test_windows_parent_chain_rejects_symlink_ancestor(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(external, target_is_directory=True)

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
def test_existing_untrusted_todo_leaf_does_not_block_safe_publish(
    leaf_type, tmp_path
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"
    todo_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    if leaf_type == "symlink":
        todo_path.symlink_to(outside)
    elif leaf_type == "fifo":
        os.mkfifo(todo_path)
    elif leaf_type == "hardlink":
        os.link(outside, todo_path)
    else:
        todo_path.write_bytes(b"x" * (1_048_576 + 1))

    with pytest.raises(OSError):
        persist_deepresearch_task_update(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )

    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_persist_cleans_owned_temp_after_serialization_baseexception(
    error_type, tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"

    def fail_dump(*_args, **_kwargs):
        raise error_type("failed")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "json.dump",
        fail_dump,
    )

    with pytest.raises(error_type):
        persist_deepresearch_task_update(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )
    assert list(todo_path.parent.glob(".deepresearch-todo-*")) == []


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_persist_cleans_owned_temp_after_replace_baseexception(
    error_type, tmp_path, monkeypatch
):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"

    def fail_replace(*_args, **_kwargs):
        raise error_type("failed")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress."
        "os.replace",
        fail_replace,
    )

    with pytest.raises(error_type):
        persist_deepresearch_task_update(
            _stage_snapshot(active_stage=None),
            todo_path=todo_path,
        )
    assert list(todo_path.parent.glob(".deepresearch-todo-*")) == []


def test_incomplete_snapshot_does_not_replace_retained_completed_plan(tmp_path):
    todo_path = tmp_path / "todo" / "session-1" / "todo.json"

    assert persist_deepresearch_task_update(
        _stage_snapshot(active_stage=None),
        todo_path=todo_path,
    )
    retained = todo_path.read_text(encoding="utf-8")

    assert not persist_deepresearch_task_update(
        _stage_snapshot(active_stage=1),
        todo_path=todo_path,
    )
    assert todo_path.read_text(encoding="utf-8") == retained


def test_persist_deepresearch_task_update_ignores_other_snapshots(tmp_path):
    todo_path = tmp_path / "session-1" / "todo.json"
    payload = {
        "event_type": "task.update",
        "tasks": [{
            "task_id": "another_task",
            "task_content": "not DeepResearch",
            "status": "completed",
        }],
    }

    assert not persist_deepresearch_task_update(payload, todo_path=todo_path)
    assert not todo_path.exists()
