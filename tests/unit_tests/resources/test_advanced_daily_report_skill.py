"""Contract guards for the bundled advanced-daily-report skill script.

``test_run_report_imap_id_uses_specific_exceptions`` moved here from
``tests/unit_tests/gateway/test_cron_safe_aclose.py``: the upstream
agent-gateway copy of that mirror test is skipped with the reason
"skill resource lives in the jiuwenswarm repo; move with resource fixtures",
so the assertion belongs to this repo and must not be dropped with the
gateway mirror layer.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

_SKILL_DIR = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin"
    / "references/capabilities/advanced-daily-report"
)
_RUN_REPORT = _SKILL_DIR / "run_report.py"


def _todo_collector_class():
    """Import the bundled collector package from the skill directory."""
    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parents[3]
            / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin"
            / "references/capabilities/advanced-daily-report"
        ),
    )
    module = importlib.import_module("collectors.todo_collector")
    return module.TodoCollector


def test_run_report_imap_id_uses_specific_exceptions() -> None:
    src = _RUN_REPORT.read_text(encoding="utf-8")
    # The ID-command guard must catch IMAP/OS errors, not everything.
    assert 'mail._simple_command("ID", args)' in src
    assert "except (imaplib.IMAP4.error, OSError)" in src
    # No bare except left in this skill entry script.
    for line in src.splitlines():
        assert line.strip() != "except:", line


def test_run_report_collects_todos_from_workspace_todo_json() -> None:
    src = _RUN_REPORT.read_text(encoding="utf-8")
    # 日报必须读取 DeepAgent workspace 的 todo.json，而不是只扫旧版 todo.md。
    assert "TodoCollector(AGENT_ROOT).collect()" in src
    assert 'session_dir.rglob("todo.md")' not in src


def test_todo_collector_reads_workspace_todo_json(tmp_path: Path) -> None:
    TodoCollector = _todo_collector_class()
    todo_dir = tmp_path / "agent" / "workspace" / "todo" / "web_session"
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.json").write_text(
        json.dumps(
            [
                {"id": "a", "content": "done", "status": "completed"},
                {"id": "b", "content": "doing", "status": "in_progress"},
                {"id": "c", "content": "todo", "status": "pending"},
                {"id": "d", "content": "dropped", "status": "cancelled"},
                {"id": "e", "content": "removed", "status": "deleted"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    stats = TodoCollector(tmp_path / "agent").collect()

    assert stats.total == 4
    assert stats.completed == 1
    assert stats.running == 1
    assert stats.waiting == 1
    assert stats.cancelled == 1
    assert [t.content for t in stats.tasks] == ["done", "doing", "todo", "dropped"]


def test_todo_collector_falls_back_to_legacy_markdown(tmp_path: Path) -> None:
    TodoCollector = _todo_collector_class()
    session_dir = tmp_path / "agent" / "sessions" / "legacy_session"
    session_dir.mkdir(parents=True)
    (session_dir / "todo.md").write_text(
        "\n".join(["- [x] done task", "- [ ] todo task"]),
        encoding="utf-8",
    )

    stats = TodoCollector(tmp_path / "agent").collect()

    assert stats.total == 2
    assert stats.completed == 1
    assert stats.waiting == 1
    assert [t.content for t in stats.tasks] == ["done task", "todo task"]


def test_todo_collector_prefers_todo_json_over_legacy_markdown(tmp_path: Path) -> None:
    TodoCollector = _todo_collector_class()
    session_id = "same_session"
    session_dir = tmp_path / "agent" / "sessions" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "todo.md").write_text("- [ ] 1. stale task | waiting", encoding="utf-8")
    todo_dir = tmp_path / "agent" / "workspace" / "todo" / session_id
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.json").write_text(
        json.dumps([{"id": "n", "content": "current task", "status": "pending"}]),
        encoding="utf-8",
    )

    stats = TodoCollector(tmp_path / "agent").collect()

    assert stats.total == 1
    assert stats.tasks[0].content == "current task"
