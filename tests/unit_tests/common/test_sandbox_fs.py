# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.common import sandbox_fs


@pytest.mark.skipif(os.name != "posix", reason="sandbox fs alignment is posix-only")
def test_ensure_sandbox_writable_noops_when_not_root(tmp_path, monkeypatch):
    target = tmp_path / "work"
    target.mkdir()
    monkeypatch.setattr(sandbox_fs.os, "geteuid", lambda: 1000)
    before = target.stat()
    sandbox_fs.ensure_sandbox_writable(target, uid=1234, gid=1234)
    after = target.stat()
    assert after.st_uid == before.st_uid
    assert after.st_gid == before.st_gid


@pytest.mark.skipif(os.name != "posix", reason="sandbox fs alignment is posix-only")
def test_ensure_sandbox_writable_chowns_when_root(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("chown to arbitrary uid requires root")
    target = tmp_path / "work"
    target.mkdir()
    monkeypatch.setattr(sandbox_fs.os, "geteuid", lambda: 0)
    sandbox_fs.ensure_sandbox_writable(target, uid=1000, gid=1000)
    st = target.stat()
    assert st.st_uid == 1000
    assert st.st_gid == 1000


@pytest.mark.skipif(os.name != "posix", reason="sandbox fs alignment is posix-only")
def test_ensure_sandbox_writable_recursive(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("chown to arbitrary uid requires root")
    root = tmp_path / "chat"
    nested = root / "work"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sandbox_fs.os, "geteuid", lambda: 0)
    sandbox_fs.ensure_sandbox_writable(root, uid=1000, gid=1000, recursive=True)
    assert root.stat().st_uid == 1000
    assert nested.stat().st_uid == 1000
    assert (nested / "a.txt").stat().st_uid == 1000


def test_sandbox_fs_uid_gid_from_env(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_SANDBOX_UID", "2001")
    monkeypatch.setenv("JIUWENSWARM_SANDBOX_GID", "2002")
    assert sandbox_fs.sandbox_fs_uid() == 2001
    assert sandbox_fs.sandbox_fs_gid() == 2002


def test_ensure_sandbox_writable_missing_path_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_fs.os, "geteuid", lambda: 0)
    sandbox_fs.ensure_sandbox_writable(tmp_path / "missing")


def test_projectless_workspace_aligns_sandbox_writable_dirs(tmp_path, monkeypatch):
    from jiuwenswarm.common import projectless_workspace

    calls: list[Path] = []

    def _capture(path, **_kwargs):
        calls.append(Path(path))

    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(
        "jiuwenswarm.common.sandbox_fs.ensure_sandbox_writable",
        _capture,
    )
    workspace = projectless_workspace.get_projectless_task_workspace("sess-1", "demo")
    assert workspace.work_dir.is_dir()
    assert workspace.outputs_dir.is_dir()
    assert workspace.root_dir in calls
    assert workspace.work_dir in calls
    assert workspace.outputs_dir in calls
