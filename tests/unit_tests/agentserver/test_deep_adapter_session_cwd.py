# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A session's scheduler must start in its own workspace."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _make_adapter(
    session_id: str | None,
    *,
    project_dir: str | None = None,
) -> JiuWenSwarmDeepAdapter:
    """Build a bare adapter holding only the attributes the cwd seed reads."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = session_id
    adapter._project_dir = project_dir
    return adapter


def _session_dir(root, session_id):
    raw = str(session_id or "").strip()
    if not raw:
        return root
    path = root / raw
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(name="projects_root")
def _projects_root(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Point the shared ``projects`` root at a temp dir."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(
        interface_deep,
        "get_default_project_session_workspace_dir",
        lambda session_id=None: _session_dir(root, session_id),
    )
    return root


def test_construction_seeds_the_session_directory_not_the_shared_root(projects_root):
    """A session-scoped adapter starts in ``projects/<session_id>``."""
    adapter = _make_adapter("slack_C0123_456")

    assert adapter._initial_runtime_workspace() == str(projects_root / "slack_C0123_456")
    assert adapter._initial_runtime_workspace() != str(projects_root)


def test_two_sessions_get_two_directories(projects_root):
    """Distinct sessions must not resolve to the same working directory."""
    first = _make_adapter("cron_job_a")
    second = _make_adapter("cron_job_b")

    assert first._initial_runtime_workspace() != second._initial_runtime_workspace()


def test_root_adapter_without_a_session_keeps_the_shared_root(projects_root):
    """A root adapter without a session keeps the shared root."""
    adapter = _make_adapter(None)

    assert adapter._initial_runtime_workspace() == str(projects_root)


def test_a_configured_project_dir_still_wins(projects_root, tmp_path):
    """An explicitly configured project dir outranks the per-session default."""
    configured = tmp_path / "checkout"
    configured.mkdir()
    adapter = _make_adapter("web_1", project_dir=str(configured))

    assert adapter._initial_runtime_workspace() == str(configured)
