"""Regressions for Issue #5753: the bound project ID owns the directory."""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def binding_store(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import project_store as projects
    from jiuwenswarm.server.runtime.session import session_metadata as metadata

    metadata._METADATA_QUEUE.join()
    metadata._METADATA_CACHE.clear()
    metadata._SESSION_REBIND_GEN.clear()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(metadata, "get_agent_sessions_dir", lambda: sessions)
    project = projects.Project(project_id="proj_bound", name="Bound", project_dir="/bound")
    monkeypatch.setattr(
        projects, "get_project_by_id", lambda pid, cache_bust=False: project if pid == project.project_id else None,
    )
    yield metadata, sessions
    metadata._METADATA_QUEUE.join()
    metadata._METADATA_CACHE.clear()


@pytest.mark.parametrize("project_id, expected_dir", [
    ("default_code", ""),
    ("proj_bound", "/bound"),
])
@pytest.mark.parametrize("operation", ["sync", "update"])
def test_request_cannot_mix_locked_id_with_another_directory(binding_store, project_id, expected_dir, operation):
    metadata, _ = binding_store
    metadata.init_session_metadata(session_id="bound", project_id=project_id, work_mode="code")
    if operation == "sync":
        effective = metadata.sync_session_request_metadata(
            session_id="bound", project_id="proj_other", project_dir="/unrelated",
        )
        assert effective == (expected_dir or None)
    else:
        metadata.update_session_metadata(
            session_id="bound", project_id="proj_other", project_dir="/unrelated",
        )
    metadata._METADATA_QUEUE.join()
    result = metadata.get_session_metadata("bound", cache_bust=True, enable_writeback=False)
    assert (result["project_id"], result["project_dir"]) == (project_id, expected_dir)


def test_queued_directory_repair_follows_current_project_id(binding_store):
    metadata, _ = binding_store
    metadata.init_session_metadata(session_id="bound", project_id="proj_bound", work_mode="code")
    metadata._write_metadata_sync(
        "bound",
        {"project_id": "default_code", "project_dir": ""},
        metadata._MetadataWriteOptions(merge_fields=frozenset({"project_dir"})),
    )
    result = metadata.get_session_metadata("bound", cache_bust=True, enable_writeback=False)
    assert (result["project_id"], result["project_dir"]) == ("proj_bound", "/bound")


def test_directory_only_session_keeps_legacy_binding(binding_store):
    metadata, _ = binding_store
    metadata.init_session_metadata(session_id="legacy", project_dir="/legacy", work_mode="code")
    assert metadata.sync_session_request_metadata(session_id="legacy", project_dir="/unrelated") == "/legacy"


@pytest.mark.parametrize("operation", ["sync", "write"])
def test_project_directory_write_refreshes_stale_registry_cache(tmp_path, monkeypatch, operation):
    from jiuwenswarm.server.runtime.session import project_store, session_metadata

    session_metadata._METADATA_QUEUE.join()
    session_metadata._METADATA_CACHE.clear()
    sessions = tmp_path / "sessions"
    projects_path = tmp_path / "projects.json"
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(project_store, "_projects_file", lambda: projects_path)
    monkeypatch.setattr(project_store, "_CACHE", None)
    project = project_store.create_project("Bound", "/bound", work_mode="code")
    session_metadata.init_session_metadata(session_id="bound", project_id=project.project_id, work_mode="code")

    # Simulate another process without refreshing this process's project cache.
    projects = json.loads(projects_path.read_text(encoding="utf-8"))
    projects["projects"][0]["project_dir"] = "/bound2"
    projects_path.write_text(json.dumps(projects), encoding="utf-8")
    path = sessions / "bound" / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["project_dir"] = "/bound2"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert project_store.get_project_by_id(project.project_id).project_dir == "/bound"

    if operation == "sync":
        effective = session_metadata.sync_session_request_metadata(session_id="bound")
    else:
        effective = session_metadata._write_metadata_sync("bound", metadata)["project_dir"]
    session_metadata._METADATA_QUEUE.join()

    assert effective == "/bound2"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert (persisted["project_id"], persisted["project_dir"]) == (project.project_id, "/bound2")


@pytest.mark.parametrize("request_dir", ["/home/agentos/workspace", None])
def test_agentos_workspace_remains_execution_only(binding_store, monkeypatch, request_dir):
    metadata, sessions = binding_store
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    metadata.init_session_metadata(session_id="cloud", project_id="default_code", work_mode="code")
    path = sessions / "cloud" / "metadata.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["project_dir"] = "/home/agentos/workspace"
    path.write_text(json.dumps(stored), encoding="utf-8")
    effective = metadata.sync_session_request_metadata(
        session_id="cloud", channel_id="web", project_id="default_code", project_dir=request_dir,
    )
    assert effective == "/home/agentos/workspace"
    metadata._METADATA_QUEUE.join()
    result = metadata.get_session_metadata("cloud", cache_bust=True, enable_writeback=False)
    assert result["project_id"] == "default_code"
    assert result["project_dir"] == ""
