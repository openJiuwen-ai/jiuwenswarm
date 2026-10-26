# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tenant sessions tree: sync/init write under resolve_tenant_sessions_dir."""

from __future__ import annotations

import json
import time
from pathlib import Path

from jiuwenswarm.common.utils import resolve_tenant_sessions_dir
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_resolved_project_dir,
    get_session_metadata,
    init_session_metadata,
    sync_session_request_metadata,
)
from tests.unit_tests.tenant_workspace_test_helpers import (
    patch_multi_tenant_workspace_dirs,
    tenant_workspace_root,
)


def test_resolve_tenant_sessions_dir_layout(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    path = resolve_tenant_sessions_dir("office")
    assert path == tenant_workspace_root(tmp_path, workspace_key="office") / "agent" / "sessions"


def test_sync_writes_under_tenant_sessions_root(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    global_sessions = (
        tenant_workspace_root(tmp_path, workspace_key="default") / "agent" / "sessions"
    )
    global_sessions.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: global_sessions,
    )

    tenant_root = resolve_tenant_sessions_dir("svc_bot")
    proj = tmp_path / "proj"
    proj.mkdir()
    effective = sync_session_request_metadata(
        session_id="sess_a",
        channel_id="web",
        mode="agent",
        project_dir=str(proj),
        explicit_mode_provided=True,
        sessions_root=tenant_root,
    )
    assert effective == str(proj)

    deadline = time.time() + 2.0
    meta_path = tenant_root / "sess_a" / "metadata.json"
    while time.time() < deadline and not meta_path.is_file():
        time.sleep(0.05)
    assert meta_path.is_file()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["project_dir"] == str(proj)
    assert not (global_sessions / "sess_a" / "metadata.json").exists()


def test_cache_isolated_by_sessions_root(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    global_sessions = (
        tenant_workspace_root(tmp_path, workspace_key="default") / "agent" / "sessions"
    )
    global_sessions.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: global_sessions,
    )

    root_a = resolve_tenant_sessions_dir("office")
    root_b = resolve_tenant_sessions_dir("assistant")
    assert root_a != root_b
    init_session_metadata(
        session_id="same_sid",
        channel_id="web",
        project_dir=str(tmp_path / "a"),
        sessions_root=root_a,
    )
    init_session_metadata(
        session_id="same_sid",
        channel_id="web",
        project_dir=str(tmp_path / "b"),
        sessions_root=root_b,
    )

    meta_a = get_session_metadata("same_sid", sessions_root=root_a)
    meta_b = get_session_metadata("same_sid", sessions_root=root_b)
    assert meta_a.get("project_dir") == str(tmp_path / "a")
    assert meta_b.get("project_dir") == str(tmp_path / "b")


def test_sync_then_get_resolved_under_tenant_root(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    global_sessions = (
        tenant_workspace_root(tmp_path, workspace_key="default") / "agent" / "sessions"
    )
    global_sessions.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: global_sessions,
    )

    project = tmp_path / "locked"
    project.mkdir()
    tenant_root = resolve_tenant_sessions_dir("office")
    sync_session_request_metadata(
        session_id="chat_sess",
        channel_id="officeclaw",
        mode="agent",
        project_dir=str(project),
        explicit_mode_provided=True,
        sessions_root=tenant_root,
    )
    deadline = time.time() + 2.0
    meta_path = tenant_root / "chat_sess" / "metadata.json"
    while time.time() < deadline and not meta_path.is_file():
        time.sleep(0.05)
    assert meta_path.is_file()

    resolved = get_resolved_project_dir(
        "chat_sess",
        tenant_root,
        default=tmp_path / "default_ws",
    )
    assert Path(resolved).resolve() == project.resolve()
