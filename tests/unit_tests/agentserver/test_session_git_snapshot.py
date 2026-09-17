# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the per-conversation git snapshot behind the runtime prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _read_git_head,
)


@pytest.fixture(name="head_file")
def _head_file(tmp_path: Path) -> Path:
    """Create a git HEAD file standing in for a real checkout."""
    path = tmp_path / "HEAD"
    path.write_text("ref: refs/heads/dev/feature\n", encoding="utf-8")
    return path


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter carrying only the snapshot cache."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._session_git_snapshots = {}
    return adapter


def _make_runner(calls: list[list[str]], status: str = " M a.py") -> callable:
    """Return a git runner double recording commands and serving mutable output."""
    state = {"status": status, "branch": "dev/feature"}

    def _run(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return state["branch"]
        if args[0] == "status":
            return state["status"]
        if args[0] == "log":
            return "abc123 first"
        return ""

    _run.state = state
    return _run


def test_snapshot_is_taken_once_per_conversation(head_file: Path) -> None:
    """Later turns reuse the first turn's snapshot instead of re-running git."""
    adapter = _make_adapter()
    calls: list[list[str]] = []
    runner = _make_runner(calls)

    first = adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)
    for _ in range(4):
        adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    assert first.branch == "dev/feature"
    assert first.status == " M a.py"
    assert first.recent_commits == "abc123 first"
    # status + branch + log on the first turn, nothing after.
    assert len(calls) == 3


def test_edits_during_the_conversation_do_not_change_the_snapshot(head_file: Path) -> None:
    """The injected prompt promises a start-of-conversation snapshot.

    Re-reading it per turn is what rewrote the system prompt mid-conversation
    and invalidated the model's cached prefix.
    """
    adapter = _make_adapter()
    calls: list[list[str]] = []
    runner = _make_runner(calls)
    adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    runner.state["status"] = " M a.py\n M b.py\n?? c.py"
    later = adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    assert later.status == " M a.py"


def test_branch_switch_retakes_the_snapshot(head_file: Path) -> None:
    """Moving the checkout invalidates everything the snapshot describes.

    Which branch a commit lands on is not something the agent may be stale
    about, and a switch changes status and recent commits wholesale.
    """
    adapter = _make_adapter()
    calls: list[list[str]] = []
    runner = _make_runner(calls)
    adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)
    calls.clear()

    head_file.write_text("ref: refs/heads/main\n", encoding="utf-8")
    runner.state["branch"] = "main"
    runner.state["status"] = ""

    refreshed = adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    assert refreshed.branch == "main"
    assert refreshed.status == ""
    assert len(calls) == 3


def test_detached_checkout_move_retakes_the_snapshot(head_file: Path) -> None:
    """A detached HEAD writes a commit id, and moving it counts as a move."""
    adapter = _make_adapter()
    runner = _make_runner([])
    head_file.write_text("a" * 40 + "\n", encoding="utf-8")
    adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    head_file.write_text("b" * 40 + "\n", encoding="utf-8")
    runner.state["status"] = " M moved.py"
    refreshed = adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    assert refreshed.status == " M moved.py"


def test_a_new_conversation_takes_a_fresh_snapshot(head_file: Path) -> None:
    """A new session is a new conversation, so it re-reads git."""
    adapter = _make_adapter()
    calls: list[list[str]] = []
    runner = _make_runner(calls)
    adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)
    runner.state["status"] = " M b.py"
    calls.clear()

    fresh = adapter._resolve_session_git_snapshot("/repo", "sess_b", str(head_file), runner)

    assert fresh.status == " M b.py"
    assert len(calls) == 3


def test_switching_project_dir_takes_a_fresh_snapshot(head_file: Path) -> None:
    """Two project dirs in one session must not share a snapshot."""
    adapter = _make_adapter()
    calls: list[list[str]] = []
    runner = _make_runner(calls)
    adapter._resolve_session_git_snapshot("/repo/a", "sess_a", str(head_file), runner)
    calls.clear()

    adapter._resolve_session_git_snapshot("/repo/b", "sess_a", str(head_file), runner)

    assert len(calls) == 3


def test_unreadable_head_degrades_to_holding_the_snapshot(tmp_path: Path) -> None:
    """Losing the HEAD probe must not make every turn re-run git."""
    adapter = _make_adapter()
    calls: list[list[str]] = []
    runner = _make_runner(calls)
    missing = str(tmp_path / "nope" / "HEAD")

    adapter._resolve_session_git_snapshot("/repo", "sess_a", missing, runner)
    calls.clear()
    adapter._resolve_session_git_snapshot("/repo", "sess_a", missing, runner)

    assert calls == []


def test_status_is_capped_at_fifty_lines(head_file: Path) -> None:
    """A large working tree must not flood the prompt."""
    adapter = _make_adapter()
    runner = _make_runner([], status="\n".join(f" M file{i}.py" for i in range(80)))

    snapshot = adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), runner)

    assert len(snapshot.status.splitlines()) == 50


def test_detached_head_falls_back_to_a_stable_name(head_file: Path) -> None:
    """An empty branch read must not leave the prompt with a blank field."""
    adapter = _make_adapter()

    def _run(args: list[str]) -> str:
        return ""

    snapshot = adapter._resolve_session_git_snapshot("/repo", "sess_a", str(head_file), _run)

    assert snapshot.branch == "HEAD"


def test_snapshots_are_immutable(head_file: Path) -> None:
    """A shared snapshot must not be editable by one caller."""
    adapter = _make_adapter()
    snapshot = adapter._resolve_session_git_snapshot(
        "/repo", "sess_a", str(head_file), _make_runner([])
    )

    with pytest.raises(Exception):
        snapshot.status = "tampered"


def test_read_git_head_handles_a_missing_probe() -> None:
    """An unset or unreadable path yields an empty fingerprint, not an error."""
    assert _read_git_head("") == ""
    assert _read_git_head("/nonexistent/path/HEAD") == ""
