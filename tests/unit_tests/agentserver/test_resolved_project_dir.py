# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""get_resolved_project_dir: tenant sessions + global sessions fallback."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.server.runtime.session.session_metadata import get_resolved_project_dir


def _write_meta(root: Path, session_id: str, project_dir: Path) -> None:
    d = root / session_id
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(
        json.dumps({"project_dir": str(project_dir)}),
        encoding="utf-8",
    )


def test_prefers_tenant_sessions_root(tmp_path: Path, monkeypatch):
    tenant_sessions = tmp_path / "tenant_sessions"
    global_sessions = tmp_path / "global_sessions"
    project = tmp_path / "proj_a"
    project.mkdir()
    other = tmp_path / "proj_b"
    other.mkdir()
    _write_meta(tenant_sessions, "s1", project)
    _write_meta(global_sessions, "s1", other)
    default = tmp_path / "default_ws"
    default.mkdir()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: global_sessions,
    )

    resolved = get_resolved_project_dir(
        "s1", tenant_sessions, default=default
    )
    assert Path(resolved).resolve() == project.resolve()


def test_falls_back_to_global_sessions(tmp_path: Path, monkeypatch):
    tenant_sessions = tmp_path / "tenant_sessions"
    tenant_sessions.mkdir()
    global_sessions = tmp_path / "global_sessions"
    project = tmp_path / "proj_global"
    project.mkdir()
    _write_meta(global_sessions, "s2", project)
    default = tmp_path / "default_ws"
    default.mkdir()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: global_sessions,
    )

    resolved = get_resolved_project_dir(
        "s2", tenant_sessions, default=default
    )
    assert Path(resolved).resolve() == project.resolve()


def test_falls_back_to_default_when_unset(tmp_path: Path, monkeypatch):
    tenant_sessions = tmp_path / "tenant_sessions"
    tenant_sessions.mkdir()
    global_sessions = tmp_path / "global_sessions"
    global_sessions.mkdir()
    default = tmp_path / "default_ws"
    default.mkdir()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: global_sessions,
    )

    resolved = get_resolved_project_dir(
        "missing", tenant_sessions, default=default
    )
    assert Path(resolved).resolve() == default.resolve()
