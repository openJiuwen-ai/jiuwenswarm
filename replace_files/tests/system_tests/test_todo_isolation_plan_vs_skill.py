# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System test: plan-session todos vs skill-step todos isolation (commit 155a107).

commit 155a107 split user-task tracking (``TodoToolkit``) from SKILL step
tracking (``SkillStepToolkit``). Each writes a different filename under the
session dir, so the two surfaces never overwrite each other, and switching
the plan-session ContextVar switches the file location without rebuilding
the toolkits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenclaw.agentserver import plan_todo_context
from jiuwenclaw.agentserver.tools import todo_toolkits
from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
from jiuwenclaw.agentserver.tools.todo_toolkits import TodoToolkit

pytestmark = [pytest.mark.integration, pytest.mark.system]


@pytest.fixture
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(todo_toolkits, "get_agent_sessions_dir", lambda: sessions)
    return sessions


@pytest.fixture
def set_session():
    tokens = []

    def _set(session_id: str) -> None:
        tokens.append(plan_todo_context.PLAN_TODO_SESSION_ID.set(session_id))

    yield _set

    for tok in reversed(tokens):
        plan_todo_context.PLAN_TODO_SESSION_ID.reset(tok)


def test_todo_and_skill_step_write_to_separate_files(sessions_dir: Path, set_session):
    set_session("sess-a")

    todo = TodoToolkit()
    skill = SkillStepToolkit()

    todo.todo_create(["plan task one", "plan task two"])
    skill.todo_create(["skill step one"])

    session_dir = sessions_dir / "sess-a"
    todo_file = session_dir / "todo.md"
    skill_file = session_dir / "skill_step.md"

    assert todo_file.exists()
    assert skill_file.exists()

    todo_content = todo_file.read_text(encoding="utf-8")
    skill_content = skill_file.read_text(encoding="utf-8")

    assert "plan task one" in todo_content
    assert "plan task two" in todo_content
    assert "skill step one" not in todo_content
    assert "skill step one" in skill_content
    assert "plan task one" not in skill_content


def test_skill_step_toolkit_exposes_only_facade_tool(sessions_dir: Path, set_session):
    """SkillStepToolkit collapses to a single ``skill_step`` facade tool, while
    TodoToolkit keeps its ``todo_*`` surface untouched.
    """
    set_session("sess-prefix")

    todo_tools = {t.card.name for t in TodoToolkit().get_tools()}
    skill_tools = {t.card.name for t in SkillStepToolkit().get_tools()}

    assert "todo_create" in todo_tools
    assert skill_tools == {"skill_step"}
    assert todo_tools.isdisjoint(skill_tools)


def test_plan_session_switch_routes_to_different_session_dirs(sessions_dir: Path, set_session):
    todo = TodoToolkit()

    set_session("sess-one")
    todo.todo_create(["task in one"])

    set_session("sess-two")
    todo.todo_create(["task in two"])

    one = (sessions_dir / "sess-one" / "todo.md").read_text(encoding="utf-8")
    two = (sessions_dir / "sess-two" / "todo.md").read_text(encoding="utf-8")

    assert "task in one" in one and "task in two" not in one
    assert "task in two" in two and "task in one" not in two
