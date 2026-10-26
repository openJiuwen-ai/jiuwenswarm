# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""cleanup_orphan_file_ops 对 flash JSONL 账本的覆盖（深查补测）。

回归背景：flash 账本文件名是 ``file_ops_flash_{AGENT_ID}_{session}.jsonl``，
比 stock 多一段 ``flash_``。此前只把扩展名检查放宽到 ``.jsonl``，前缀
检查仍是 ``file_ops_{AGENT_ID}_``——flash 孤儿账本永远不匹配，清理静默
失效（会话删除后 .jsonl 无限堆积）。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common import cleanup as cleanup_module
from jiuwenswarm.common.cleanup import cleanup_orphan_file_ops


@pytest.fixture
def orphan_env(tmp_path, monkeypatch):
    """搭一个带存活会话与孤儿账本的假环境，返回 .agent_history 目录。"""
    sessions_dir = tmp_path / "sessions"
    (sessions_dir / "alive_sess").mkdir(parents=True)

    agent_ws = tmp_path / "agent_ws"
    user_ws = tmp_path / "user_ws"
    hist_dir = agent_ws / ".agent_history"
    hist_dir.mkdir(parents=True)

    keep_stock = hist_dir / "file_ops_jiuwenswarm_alive_sess.json"
    keep_stock.write_text("{}", encoding="utf-8")
    keep_flash = hist_dir / "file_ops_flash_jiuwenswarm_alive_sess.jsonl"
    keep_flash.write_text("{}\n", encoding="utf-8")
    orphan_stock = hist_dir / "file_ops_jiuwenswarm_dead_sess.json"
    orphan_stock.write_text("{}", encoding="utf-8")
    orphan_flash = hist_dir / "file_ops_flash_jiuwenswarm_dead_sess.jsonl"
    orphan_flash.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(cleanup_module, "get_agent_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(cleanup_module, "get_agent_workspace_dir", lambda: agent_ws)
    monkeypatch.setattr(cleanup_module, "get_user_workspace_dir", lambda: user_ws)
    return hist_dir


def test_orphan_flash_jsonl_is_cleaned(orphan_env):
    """核心回归：会话已删除的 flash .jsonl 必须被清掉（此前前缀不匹配漏清理）。"""
    result = cleanup_orphan_file_ops()

    assert result["removed"] >= 1
    assert not (orphan_env / "file_ops_flash_jiuwenswarm_dead_sess.jsonl").exists()
    assert not (orphan_env / "file_ops_jiuwenswarm_dead_sess.json").exists()


def test_alive_session_files_are_kept(orphan_env):
    cleanup_orphan_file_ops()

    assert (orphan_env / "file_ops_jiuwenswarm_alive_sess.json").exists()
    assert (orphan_env / "file_ops_flash_jiuwenswarm_alive_sess.jsonl").exists()


def test_foreign_names_are_untouched(orphan_env):
    """非本 agent 命名 / 非账本文件不得误删（flash 前缀放宽不能伤及无辜）。"""
    foreign = orphan_env / "file_ops_flash_other_agent_dead.jsonl"
    foreign.write_text("{}\n", encoding="utf-8")
    unrelated = orphan_env / "notes.jsonl"
    unrelated.write_text("x\n", encoding="utf-8")

    cleanup_orphan_file_ops()

    assert foreign.exists()
    assert unrelated.exists()
