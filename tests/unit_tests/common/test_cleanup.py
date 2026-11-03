# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gateway.cleanup import (
    cleanup_old_sessions,
    cleanup_orphan_file_ops,
)


class TestCleanup:

    @staticmethod
    def test_removes_old_sessions_by_mtime(tmp_path, monkeypatch):
        """会话目录 mtime 超过保留期时被删除（与 cc 一致）。"""
        sessions_dir = tmp_path / "sessions"
        stale = sessions_dir / "session-old"
        stale.mkdir(parents=True)

        # 直接修改目录 mtime 到很久以前
        old_time = time.time() - 999 * 86400
        os.utime(stale, (old_time, old_time))

        monkeypatch.setattr("gateway.cleanup.get_agent_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr("gateway.cleanup._get_cleanup_period_days", lambda: 30)

        result = cleanup_old_sessions()
        assert result["removed"] >= 1
        assert not stale.exists()

    @staticmethod
    def test_removes_file_ops_without_session(tmp_path, monkeypatch):
        """file_ops 对应的 session 目录已不存在时被删除。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)

        hist_dir = tmp_path / "agent-ws" / ".agent_history"
        hist_dir.mkdir(parents=True)
        fop = hist_dir / "file_ops_jiuwenswarm_dead_session.json"
        fop.write_text("{}", encoding="utf-8")

        monkeypatch.setattr("gateway.cleanup.get_agent_workspace_dir", lambda: tmp_path / "agent-ws")
        monkeypatch.setattr("gateway.cleanup.get_user_workspace_dir", lambda: tmp_path / "user-ws")
        monkeypatch.setattr("gateway.cleanup.get_agent_sessions_dir", lambda: sessions_dir)

        result = cleanup_orphan_file_ops()
        assert result["removed"] >= 1
        assert not fop.exists()


@pytest.mark.parametrize("days", [0, 14, "0", "90", -1, "invalid", None])
def test_configured_cleanup_retention(
    days: int | str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero means forever; invalid configuration preserves the default."""
    from gateway.cleanup import _get_cleanup_period_days

    monkeypatch.delenv("CLEANUP_PERIOD_DAYS", raising=False)
    monkeypatch.setattr(
        "gateway.cleanup.get_config",
        lambda: {"session_cleanup": {"retention_days": days}},
    )
    expected = {0: 0, 14: 14, "0": 0, "90": 90}.get(days, 30)
    assert _get_cleanup_period_days() == expected


def test_cleanup_environment_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing environment-based deployments retain precedence."""
    from gateway.cleanup import _get_cleanup_period_days

    monkeypatch.setattr(
        "gateway.cleanup.get_config",
        lambda: {"session_cleanup": {"retention_days": 30}},
    )
    monkeypatch.setenv("CLEANUP_PERIOD_DAYS", "0")
    assert _get_cleanup_period_days() == 0


def test_forever_retention_keeps_old_sessions_and_orphan_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic cleanup must preserve data older than the default window."""
    sessions = tmp_path / "sessions"
    stale = sessions / "old-session"
    stale.mkdir(parents=True)
    stamp = time.time() - 999 * 86400
    os.utime(stale, (stamp, stamp))
    workspace = tmp_path / "workspace"
    history = workspace / ".agent_history"
    history.mkdir(parents=True)
    orphan = history / "file_ops_jiuwenswarm_missing.json"
    orphan.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("CLEANUP_PERIOD_DAYS", raising=False)
    monkeypatch.setattr(
        "gateway.cleanup.get_config",
        lambda: {"session_cleanup": {"retention_days": 0}},
    )
    monkeypatch.setattr("gateway.cleanup.get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr("gateway.cleanup.get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr("gateway.cleanup.get_user_workspace_dir", lambda: workspace)

    assert cleanup_old_sessions() == {"removed": 0, "errors": 0}
    assert cleanup_orphan_file_ops() == {"removed": 0, "errors": 0}
    assert stale.is_dir()
    assert orphan.is_file()
