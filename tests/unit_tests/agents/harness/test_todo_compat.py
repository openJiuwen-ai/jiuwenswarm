# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``CompatibleTodoModifyTool`` 的 update 归一化与错误可操作性。

复现的生产故障：模型发出 ``{step_4: completed, step_5: in_progress}``，而列表里
仍留着一个更早的 ``in_progress``（模型跳过了该步的状态流转）。上游以一句不含任务
id、不含补救方式的话拒绝，模型逐字重发同一调用直至 runner 的
``tool_args_bailout_threshold`` 兜底中止整个任务。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from openjiuwen.harness.prompts.tools import build_tool_card
from openjiuwen.harness.schema.task import TodoItem, TodoStatus


def _load_todo_compat():
    """直接加载模块，避免 tools/__init__ 拉起 memory/skill/symphony 依赖。"""
    mod_path = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "tools"
        / "todo_compat.py"
    )
    spec = importlib.util.spec_from_file_location("todo_compat_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CompatibleTodoModifyTool = _load_todo_compat().CompatibleTodoModifyTool


class _StubTodoModifyTool(CompatibleTodoModifyTool):
    """Bypass the filesystem wiring and capture what would be saved."""

    def __init__(self) -> None:  # noqa: D107 - deliberately skips TodoTool.__init__
        self._card = build_tool_card("todo_modify", "TodoModifyTool", "en")
        self.saved: list[TodoItem] | None = None

    async def save_todos(self, session_id: str, todos: list[TodoItem]) -> None:
        self.saved = todos


def _todos(*pairs: tuple[str, TodoStatus]) -> list[TodoItem]:
    return [
        TodoItem(
            id=todo_id,
            content=todo_id,
            activeForm=todo_id,
            description=todo_id,
            status=status,
        )
        for todo_id, status in pairs
    ]


def _statuses(todos: list[TodoItem]) -> dict[str, TodoStatus]:
    return {todo.id: todo.status for todo in todos}


@pytest.mark.asyncio
async def test_stale_in_progress_is_demoted_instead_of_rejected():
    tool = _StubTodoModifyTool()
    current = _todos(
        ("step_1_scope", TodoStatus.COMPLETED),
        ("step_2_sweep", TodoStatus.IN_PROGRESS),
        ("step_4_verify", TodoStatus.PENDING),
        ("step_5_rank", TodoStatus.PENDING),
    )

    message = await tool._update_todos(
        "s1",
        [
            {"id": "step_4_verify", "status": "completed"},
            {"id": "step_5_rank", "status": "in_progress"},
        ],
        current,
    )

    assert _statuses(tool.saved) == {
        "step_1_scope": TodoStatus.COMPLETED,
        "step_2_sweep": TodoStatus.PENDING,
        "step_4_verify": TodoStatus.COMPLETED,
        "step_5_rank": TodoStatus.IN_PROGRESS,
    }
    assert "step_2_sweep" in message
    assert "pending" in message


@pytest.mark.asyncio
async def test_bare_promotion_demotes_the_previous_task():
    tool = _StubTodoModifyTool()
    current = _todos(
        ("scope_repo", TodoStatus.IN_PROGRESS), ("rank_findings", TodoStatus.PENDING)
    )

    await tool._update_todos(
        "s1", [{"id": "rank_findings", "status": "in_progress"}], current
    )

    assert _statuses(tool.saved) == {
        "scope_repo": TodoStatus.PENDING,
        "rank_findings": TodoStatus.IN_PROGRESS,
    }


@pytest.mark.asyncio
async def test_two_promotions_in_one_payload_are_still_rejected():
    tool = _StubTodoModifyTool()
    current = _todos(
        ("scope_repo", TodoStatus.PENDING), ("rank_findings", TodoStatus.PENDING)
    )

    with pytest.raises(Exception) as excinfo:
        await tool._update_todos(
            "s1",
            [
                {"id": "scope_repo", "status": "in_progress"},
                {"id": "rank_findings", "status": "in_progress"},
            ],
            current,
        )

    assert tool.saved is None
    reason = str(excinfo.value)
    assert "scope_repo" in reason and "rank_findings" in reason
    assert "Retrying the same call will fail again" in reason


def test_rejection_names_the_conflicting_task_ids():
    tool = _StubTodoModifyTool()
    todos = _todos(
        ("scope_repo", TodoStatus.IN_PROGRESS), ("rank_findings", TodoStatus.IN_PROGRESS)
    )

    with pytest.raises(Exception) as excinfo:
        tool._validate_single_in_progress(todos)

    reason = str(excinfo.value)
    assert "Tasks in conflict: scope_repo, rank_findings" in reason


@pytest.mark.asyncio
async def test_single_in_progress_payload_is_untouched():
    tool = _StubTodoModifyTool()
    current = _todos(
        ("scope_repo", TodoStatus.IN_PROGRESS), ("rank_findings", TodoStatus.PENDING)
    )

    message = await tool._update_todos(
        "s1", [{"id": "rank_findings", "status": "pending"}], current
    )

    assert _statuses(tool.saved) == {
        "scope_repo": TodoStatus.IN_PROGRESS,
        "rank_findings": TodoStatus.PENDING,
    }
    assert "stale" not in message


@pytest.mark.asyncio
async def test_deleted_status_still_maps_to_a_delete():
    tool = _StubTodoModifyTool()
    current = _todos(
        ("scope_repo", TodoStatus.IN_PROGRESS), ("rank_findings", TodoStatus.PENDING)
    )

    message = await tool._update_todos(
        "s1",
        [
            {"id": "scope_repo", "status": "deleted"},
            {"id": "rank_findings", "status": "in_progress"},
        ],
        current,
    )

    assert _statuses(tool.saved) == {"rank_findings": TodoStatus.IN_PROGRESS}
    assert "deleted 1 task(s)" in message
