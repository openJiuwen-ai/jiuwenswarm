# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for project.git.redo_turn_changes WS handler.

与 ``test_discard_turn_changes`` 对称,只覆盖 redo 独有的行为:
  - 成功 redo:文件重新应用、discarded 状态清除、watcher 标脏
  - 最后一轮未 discarded 时拒绝(NOTHING_TO_REDO)——redo 独有前置条件
  - 部分失败:errors 非空时不清除 discarded 状态(unmark 不调用),可重试

绑定校验 / busy 拒绝 / 异常处理与 discard handler 共用同一套校验代码,
已由 ``test_discard_turn_changes`` 覆盖,此处不重复。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class FakeWebChannel:
    def __init__(self):
        self.responses: list[dict] = []

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code})

    def is_session_busy(self, session_id: str) -> bool:
        return False


class FakeRegistry:
    def __init__(self):
        self.mark_dirty_calls: list[str] = []

    def mark_dirty(self, project_id: str) -> None:
        self.mark_dirty_calls.append(project_id)


def _make_handler():
    from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import GitDiffWebSocketHandler
    return GitDiffWebSocketHandler(channel=FakeWebChannel(), registry=FakeRegistry())


def _make_project(project_id="proj-A", project_dir="/tmp/proj-A"):
    return SimpleNamespace(project_id=project_id, project_dir=project_dir, work_mode="code", git=SimpleNamespace(enabled=True))


def _common_patches(handler, *, status="discarded", redo_result=None, redo_side_effect=None):
    """组装 redo handler 测试通用的 patch 上下文。"""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "jiuwenswarm.gateway.channel_manager.web.git_ws_handler.GitDiffWebSocketHandler._resolve_git_project",
        return_value=(_make_project(), None, None),
    ))
    stack.enter_context(patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"project_id": "proj-A"},
    ))
    stack.enter_context(patch(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_last_turn_info",
        return_value={"turn_index": 2, "timestamp": 1000.0},
    ))
    stack.enter_context(patch(
        "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
        return_value=["/tmp/team-workspace"],
    ))

    unmark_calls: list[dict] = []

    def fake_unmark(session_id, turn_index, project_dir=None, *, extra_history_roots=None):
        unmark_calls.append({"project_dir": project_dir, "extra_history_roots": extra_history_roots})
        return "cs_sess-1_2_test1234"

    fake_diff_service = SimpleNamespace(
        get_turn_diff=lambda sid, **kw: {"status": status},
        unmark_turn_discarded=fake_unmark,
    )
    stack.enter_context(patch(
        "jiuwenswarm.server.utils.diff_service.get_diff_service",
        return_value=fake_diff_service,
    ))

    if redo_side_effect is not None:
        stack.enter_context(patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.redo_session_files",
            side_effect=redo_side_effect,
        ))
    else:
        stack.enter_context(patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.redo_session_files",
            return_value=redo_result,
        ))

    return stack, unmark_calls


@pytest.mark.asyncio
async def test_successful_redo_reapplies_files_and_clears_status():
    """成功 redo:redo/unmark 收到正确 project_dir 与 extra_history_roots,watcher 标脏。"""
    handler = _make_handler()
    redo_result = {
        "session_id": "sess-1", "turn_index": 2,
        "redone_files": ["/tmp/proj/a.py"], "deleted_files": ["/tmp/proj/removed.py"],
        "errors": [],
    }

    stack, unmark_calls = _common_patches(
        handler, status="discarded", redo_result=redo_result,
    )
    with stack:
        await handler._handle_redo_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = handler._channel.responses[0]
    assert resp["ok"] is True
    payload = resp["payload"]
    assert payload["change_set_id"] == "cs_sess-1_2_test1234"
    assert payload["redone_files"] == ["/tmp/proj/a.py"]
    assert payload["deleted_files"] == ["/tmp/proj/removed.py"]
    assert payload["partial"] is False
    # unmark 被调用(无 errors),且收到正确的 project_dir / extra_history_roots
    assert len(unmark_calls) == 1
    assert unmark_calls[0]["project_dir"] == "/tmp/proj-A"
    assert unmark_calls[0]["extra_history_roots"] == ["/tmp/team-workspace"]
    # watcher 标脏
    assert handler._registry.mark_dirty_calls == ["proj-A"]


@pytest.mark.asyncio
async def test_redo_not_discarded_turn_rejected():
    """最后一轮未 discarded 时返回 NOTHING_TO_REDO(redo 独有前置条件)。"""
    handler = _make_handler()
    stack, _ = _common_patches(handler, status="applied")
    with stack:
        await handler._handle_redo_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = handler._channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "NOTHING_TO_REDO"
    assert "not discarded" in resp["error"]
    assert handler._registry.mark_dirty_calls == []


@pytest.mark.asyncio
async def test_redo_partial_failure_keeps_discarded_status():
    """部分失败时 unmark 不调用(discarded 状态保留供重试),返回 PARTIAL_REDO_FAILED。"""
    handler = _make_handler()
    redo_result = {
        "session_id": "sess-1", "turn_index": 2,
        "redone_files": ["/tmp/proj/a.py"], "deleted_files": [],
        "errors": [{"file": "/tmp/proj/locked.py", "error": "PermissionError"}],
    }

    stack, unmark_calls = _common_patches(handler, redo_result=redo_result)
    with stack:
        await handler._handle_redo_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = handler._channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "PARTIAL_REDO_FAILED"
    assert "retryable" in resp["error"]
    assert resp["payload"]["partial"] is True
    assert resp["payload"]["errors"][0]["file"] == "/tmp/proj/locked.py"
    # unmark 未被调用
    assert unmark_calls == []
    # watcher 仍标脏(已 redo 的文件需要刷新)
    assert handler._registry.mark_dirty_calls == ["proj-A"]


@pytest.mark.asyncio
async def test_redo_empty_result_returns_history_missing():
    """空 redo(file_ops 缺失/没被 discarded_out 标记)返回 REDO_HISTORY_MISSING,不清状态。

    场景:turn 状态是 discarded 但没有找到任何可恢复的文件条目。若继续 unmark
    会把状态改回 completed,用户看到"成功"但实际没恢复任何文件,且 discarded
    状态丢失无法重试。应返回失败,保留 discarded 状态,不标脏 watcher。
    """
    handler = _make_handler()
    redo_result = {
        "session_id": "sess-1", "turn_index": 2,
        "redone_files": [], "deleted_files": [], "errors": [],
    }

    stack, unmark_calls = _common_patches(handler, redo_result=redo_result)
    with stack:
        await handler._handle_redo_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = handler._channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "REDO_HISTORY_MISSING"
    assert "no redoable files" in resp["error"]
    assert resp["payload"]["redone_files"] == []
    assert resp["payload"]["deleted_files"] == []
    # unmark 未被调用:discarded 状态保留供排查/重试
    assert unmark_calls == []
    # 没有文件变化,不标脏 watcher
    assert handler._registry.mark_dirty_calls == []
