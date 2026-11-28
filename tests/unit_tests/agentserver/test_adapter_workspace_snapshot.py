# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File services inspect the real adapter's immutable session binding."""

import pytest

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session import workspace_files


@pytest.fixture(params=[JiuWenSwarmDeepAdapter, JiuwenSwarmCodeAdapter])
def bound_adapter(request, tmp_path):
    sid = "workspace-snapshot-review"
    project = tmp_path / "project"
    cwd = project / "src"
    cwd.mkdir(parents=True)
    snapshot = RuntimeWorkspacePaths(
        internal_workspace_dir=tmp_path / "internal",
        runtime_workspace_root=project, cwd=cwd, project_root=project,
    )
    child = request.param()
    child._is_session_scoped_adapter = True
    child._parent_session_id = sid
    child._session_workspace_snapshot = snapshot
    root = request.param()
    root._session_adapters[sid] = child
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = root
    manager = object.__new__(AgentManager)
    manager.agents = {"web": {"agent": facade}}
    return sid, root, child, manager, snapshot


async def test_file_service_preserves_bound_cwd_and_rejects_changed_project(
    bound_adapter, tmp_path, monkeypatch,
):
    sid, _root, _child, manager, snapshot = bound_adapter
    metadata = {"project_dir": str(snapshot.project_root), "mode": "agent.work.normal"}
    monkeypatch.setattr(workspace_files, "get_session_metadata", lambda *_args, **_kwargs: metadata)
    service = workspace_files.SessionWorkspaceFiles(snapshot=manager.get_session_workspace_paths)

    current = await service.workspace(sid)
    assert current.status == "ready"
    assert current.root == snapshot.runtime_workspace_root
    assert current.cwd == snapshot.cwd

    changed = tmp_path / "different-project"
    changed.mkdir()
    (changed / "foreign.txt").write_text("not the running session's project", encoding="utf-8")
    metadata["project_dir"] = str(changed)
    result = await service.files(sid, [])
    assert result["status"] == "unavailable"
    assert result["treeData"] == []
    assert result["message"] == "The session workspace binding has changed"


def test_snapshot_lookup_does_not_attach_missing_or_external_sessions(bound_adapter):
    sid, root, child, manager, snapshot = bound_adapter
    assert manager.get_session_workspace_paths("web", sid) is snapshot
    assert manager.get_session_workspace_paths("web", "missing-session") is None
    assert child.get_session_workspace_paths("different-session") is None
    assert set(root._session_adapters) == {sid}
    child._external_harness_binding = object()
    assert manager.get_session_workspace_paths("web", sid) is None
