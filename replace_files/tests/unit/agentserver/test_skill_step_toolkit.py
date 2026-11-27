# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for ``SkillStepToolkit`` — the skill-scoped variant of
``TodoToolkit``.

Two surfaces matter:

1. **Tool list shape**: only the unified ``skill_step`` facade tool is
   exposed to the agent; ``todo_*`` on the parent toolkit is unaffected.
2. **Batch completion semantics**: indices must be strictly ascending,
   contiguous, and start at the first open task. Validation failures must
   leave persisted state untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepInput, SkillStepToolkit
from jiuwenclaw.agentserver.tools.todo_toolkits import (
    TaskStatus,
    TodoOpKind,
    TodoToolkit,
    consume_last_op_result,
    reset_op_results,
)


@pytest.fixture(autouse=True)
def _clean_op_result():
    reset_op_results()
    yield
    reset_op_results()


@pytest.fixture
def skill_kit(tmp_path: Path) -> SkillStepToolkit:
    return SkillStepToolkit(session_id="s-skill-step", todo_dir=tmp_path)


@pytest.fixture
def todo_kit(tmp_path: Path) -> TodoToolkit:
    return TodoToolkit(session_id="s-todo", todo_dir=tmp_path / "t")


# ---------------- tool list shape -------------------------------------------

def test_skill_step_tool_list_exposes_only_facade(skill_kit):
    """Agent surface is collapsed: a single ``skill_step`` tool, no
    per-action ``skill_step_*`` tools.
    """
    tools = skill_kit.get_tools()
    assert len(tools) == 1
    assert tools[0].card.name == "skill_step"


def test_todo_tool_list_unchanged(todo_kit):
    """The parent toolkit must keep its existing surface — no batch tool,
    ``todo_start`` retained.
    """
    names = {t.card.name for t in todo_kit.get_tools()}
    assert "todo_start" in names
    assert "todo_complete" in names
    assert "todo_complete_batch" not in names


# ---------------- batch completion happy path -------------------------------

def test_complete_batch_marks_contiguous_steps_completed(skill_kit):
    skill_kit.todo_create(["a", "b", "c", "d"])
    consume_last_op_result(skill_kit.session_id)  # drain CREATE event

    msg = skill_kit.todo_complete_batch([1, 2, 3], ["ra", "rb", "rc"])
    assert "Tasks [1, 2, 3] marked as completed." in msg

    op = consume_last_op_result(skill_kit.session_id)
    assert op is not None
    assert op.kind == TodoOpKind.COMPLETE
    assert op.success is True
    assert op.remaining_count == 1  # task 4 still open
    assert op.total_count == 4
    assert op.all_completed is False

    tasks = skill_kit.load_tasks()
    assert [t.status for t in tasks] == [
        TaskStatus.COMPLETED, TaskStatus.COMPLETED,
        TaskStatus.COMPLETED, TaskStatus.WAITING,
    ]
    assert [t.result for t in tasks[:3]] == ["ra", "rb", "rc"]


def test_complete_batch_closing_all_emits_all_completed(skill_kit):
    skill_kit.todo_create(["a", "b"])
    consume_last_op_result(skill_kit.session_id)

    skill_kit.todo_complete_batch([1, 2])

    op = consume_last_op_result(skill_kit.session_id)
    assert op is not None
    assert op.success is True
    assert op.all_completed is True


def test_complete_batch_default_results_use_done(skill_kit):
    skill_kit.todo_create(["a", "b"])
    consume_last_op_result(skill_kit.session_id)

    skill_kit.todo_complete_batch([1, 2])
    tasks = skill_kit.load_tasks()
    assert [t.result for t in tasks] == ["done", "done"]


# ---------------- batch completion validation -------------------------------

def _expect_failure(skill_kit: SkillStepToolkit, indices, results=None,
                   *, fragment: str):
    msg = skill_kit.todo_complete_batch(indices, results)
    assert "Error" in msg
    assert fragment in msg
    op = consume_last_op_result(skill_kit.session_id)
    assert op is not None
    assert op.success is False
    assert op.kind == TodoOpKind.COMPLETE


def test_complete_batch_rejects_non_contiguous(skill_kit):
    skill_kit.todo_create(["a", "b", "c"])
    consume_last_op_result(skill_kit.session_id)
    _expect_failure(skill_kit, [1, 3], fragment="contiguous")
    # Nothing persisted.
    assert all(t.status == TaskStatus.WAITING for t in skill_kit.load_tasks())


def test_complete_batch_rejects_descending(skill_kit):
    skill_kit.todo_create(["a", "b", "c"])
    consume_last_op_result(skill_kit.session_id)
    _expect_failure(skill_kit, [2, 1], fragment="ascending")
    assert all(t.status == TaskStatus.WAITING for t in skill_kit.load_tasks())


def test_complete_batch_rejects_wrong_start(skill_kit):
    skill_kit.todo_create(["a", "b", "c"])
    consume_last_op_result(skill_kit.session_id)
    _expect_failure(skill_kit, [2, 3], fragment="first open")
    assert all(t.status == TaskStatus.WAITING for t in skill_kit.load_tasks())


def test_complete_batch_rejects_already_completed(skill_kit):
    skill_kit.todo_create(["a", "b"])
    consume_last_op_result(skill_kit.session_id)
    skill_kit.todo_complete(1, "ra")
    consume_last_op_result(skill_kit.session_id)
    # Task 1 is closed; the next batch must start at idx 2. Trying to
    # re-close [1, 2] must fail because idx 1 is already completed.
    _expect_failure(skill_kit, [1, 2], fragment="first open")


def test_complete_batch_rejects_results_length_mismatch(skill_kit):
    skill_kit.todo_create(["a", "b"])
    consume_last_op_result(skill_kit.session_id)
    _expect_failure(skill_kit, [1, 2], ["only-one"], fragment="results length")
    assert all(t.status == TaskStatus.WAITING for t in skill_kit.load_tasks())


def test_complete_batch_rejects_empty_indices(skill_kit):
    skill_kit.todo_create(["a"])
    consume_last_op_result(skill_kit.session_id)
    _expect_failure(skill_kit, [], fragment="non-empty")


def test_complete_batch_rejects_when_no_open_tasks(skill_kit):
    skill_kit.todo_create(["a"])
    consume_last_op_result(skill_kit.session_id)
    skill_kit.todo_complete(1)
    consume_last_op_result(skill_kit.session_id)
    _expect_failure(skill_kit, [1], fragment="no open tasks")


# ---------------- facade input normalization --------------------------------

def test_skill_step_input_accepts_json_encoded_tasks_for_create():
    parsed = SkillStepInput.model_validate({
        "action": "create",
        "tasks": '["stage 0", "stage 1"]',
    })
    assert parsed.tasks == ["stage 0", "stage 1"]


def test_skill_step_input_accepts_json_encoded_indices_results_for_batch():
    parsed = SkillStepInput.model_validate({
        "action": "complete_batch",
        "indices": "[1, 2]",
        "results": '["r1", "r2"]',
    })
    assert parsed.indices == [1, 2]
    assert parsed.results == ["r1", "r2"]


def test_skill_step_input_accepts_python_literal_list_string():
    parsed = SkillStepInput.model_validate({
        "action": "create",
        "tasks": "['stage 0', 'stage 1']",
    })
    assert parsed.tasks == ["stage 0", "stage 1"]


def test_skill_step_input_accepts_tuple_literal_for_indices():
    parsed = SkillStepInput.model_validate({
        "action": "complete_batch",
        "indices": "(1, 2, 3)",
    })
    assert parsed.indices == [1, 2, 3]


def test_skill_step_input_accepts_csv_for_indices():
    parsed = SkillStepInput.model_validate({
        "action": "complete_batch",
        "indices": "1, 2,3",
    })
    assert parsed.indices == [1, 2, 3]


def test_skill_step_input_accepts_csv_for_tasks():
    parsed = SkillStepInput.model_validate({
        "action": "create",
        "tasks": "stage 0, stage 1",
    })
    assert parsed.tasks == ["stage 0", "stage 1"]
