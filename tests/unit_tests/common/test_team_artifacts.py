# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the team shared artifacts directory allocator."""

from __future__ import annotations

import datetime as _datetime

import pytest

from jiuwenswarm.common.team_artifacts import (
    get_team_artifact_workspace,
    get_team_artifacts_dir,
    resolve_member_work_dir,
)


@pytest.fixture(autouse=True)
def _isolate_sessions_dir(tmp_path, monkeypatch):
    """Point session-metadata lookup at an empty temp directory.

    The allocator derives the task date from the session's creation
    timestamp (metadata.json, falling back to the directory's ctime).
    Without isolation a session id that happens to match a real leftover
    directory under ``~/.jiuwenswarm/agent/sessions`` pins the task date
    to that directory's date instead of today.
    """
    sessions_dir = tmp_path / "agent" / "sessions"
    sessions_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda: sessions_dir,
    )


def test_artifacts_dir_under_team_workspace(tmp_path):
    """The artifacts root sits under the team workspace."""
    root = get_team_artifacts_dir(str(tmp_path / "team-workspace"))
    assert root == (tmp_path / "team-workspace" / "artifacts").resolve()


def test_allocates_outputs_no_member_work(tmp_path):
    """Team allocation creates outputs/ but not a per-member work dir."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id="sess-1",
    )
    assert ws.outputs_dir.is_dir()
    # Per-member work is not created by the team allocator; resolve it separately.
    assert not (ws.root_dir / "work").exists()


def test_same_session_reuses_chat_dir(tmp_path):
    """A second call for the same session reuses the same chat-<n> dir."""
    base = str(tmp_path / "team-workspace")
    first = get_team_artifact_workspace(base, session_id="sess-1")
    second = get_team_artifact_workspace(base, session_id="sess-1")
    assert first.root_dir == second.root_dir
    assert first.outputs_dir == second.outputs_dir


def test_distinct_sessions_get_distinct_chat_dirs(tmp_path):
    """Two sessions in the same day allocate different chat-<n> dirs."""
    base = str(tmp_path / "team-workspace")
    first = get_team_artifact_workspace(base, session_id="sess-1")
    second = get_team_artifact_workspace(base, session_id="sess-2")
    assert first.root_dir != second.root_dir
    assert first.root_dir.parent == second.root_dir.parent  # same date dir
    assert first.root_dir.name.startswith("chat-")
    assert second.root_dir.name.startswith("chat-")


def test_member_work_dir_per_member_isolated(tmp_path):
    """Two members get distinct work/<member_slug>/ dirs under the shared root."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id="sess-1",
    )
    alice = resolve_member_work_dir(ws.root_dir, "alice")
    bob = resolve_member_work_dir(ws.root_dir, "bob")
    assert alice.is_dir()
    assert bob.is_dir()
    assert alice != bob
    assert alice.parent == bob.parent == ws.root_dir / "work"


def test_member_work_dir_reuses_existing(tmp_path):
    """A second call for the same member reuses the same work dir."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id="sess-1",
    )
    first = resolve_member_work_dir(ws.root_dir, "alice")
    second = resolve_member_work_dir(ws.root_dir, "alice")
    assert first == second


def test_member_work_dir_empty_name_falls_back(tmp_path):
    """An empty member name still allocates a real (default) work dir."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id="sess-1",
    )
    work = resolve_member_work_dir(ws.root_dir, None)
    assert work.is_dir()
    assert work.name == "default"


def test_chat_dir_under_dated_subdir(tmp_path):
    """chat-<n> lives under artifacts/<YYYY-MM-DD>/."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id="sess-1",
    )
    today = _datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    assert ws.root_dir.parent.name == today
    assert ws.root_dir.parent.parent.name == "artifacts"


def test_registry_survives_removed_dir(tmp_path):
    """A wiped registered root is re-created as a usable outputs directory.

    The registry is the source of truth, but a registered root that no longer
    exists on disk must not break re-use: the allocator ignores it and
    (re)creates a working chat-<n>/outputs/ directory for the same session.
    """
    base = str(tmp_path / "team-workspace")
    first = get_team_artifact_workspace(base, session_id="sess-1")
    import shutil

    shutil.rmtree(first.root_dir)
    second = get_team_artifact_workspace(base, session_id="sess-1")
    assert second.outputs_dir.is_dir()
    assert second.root_dir.is_dir()


def test_empty_session_uses_default_marker(tmp_path):
    """An empty session id still allocates (slug falls back to 'default')."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id=None,
    )
    assert ws.outputs_dir.is_dir()


def test_outputs_under_git_repo_root(tmp_path):
    """Outputs live under the team-workspace tree (git auto-commit boundary)."""
    base = str(tmp_path / "team-workspace")
    ws = get_team_artifact_workspace(base, session_id="sess-1")
    assert str(ws.outputs_dir).startswith(str(get_team_artifacts_dir(base)))


def test_member_work_dir_under_shared_root(tmp_path):
    """A member's work dir lives under the shared chat-<n> root."""
    ws = get_team_artifact_workspace(
        str(tmp_path / "team-workspace"),
        session_id="sess-1",
    )
    work = resolve_member_work_dir(ws.root_dir, "alice")
    assert work.parent.parent == ws.root_dir
