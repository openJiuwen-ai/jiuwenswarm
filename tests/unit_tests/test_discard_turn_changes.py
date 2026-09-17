# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for project.git.discard_turn_changes WS handler.

核心覆盖:
  - session 与 project 绑定校验(不匹配 / 无绑定)
  - busy 会话拒绝
  - 成功撤销:文件恢复、file_ops 截断、watcher 标脏正确项目
  - 部分失败:errors 非空时不截断 file_ops,返回 ok=False + partial=True(P1 修复)
  - handler 显式传入 proj.project_dir 到 restore/truncate(P1 修复:
    避免底层 _get_project_dir_from_metadata 漏读项目目录导致 file_ops 漏扫)

注: P1 修复后 ``truncate_file_ops_by_timestamp`` 不再清理全局 file_ops
(避免误伤其他 session 同文件修改),仅清理 session-specific file_ops。
P2 修复后响应显式返回 ``global_file_ops_truncated=False``,
让调用方知晓 last_turn diff 可能残留历史全局记录。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class FakeWebChannel:
    """模拟 WebChannel,记录 send_response 调用与 busy 状态。"""

    def __init__(self, *, busy_sessions: set[str] | None = None):
        self.responses: list[dict] = []
        self._busy_sessions = busy_sessions or set()

    async def send_response(
        self, ws, req_id, *, ok, payload=None, error=None, code=None,
    ):
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload,
                "error": error,
                "code": code,
            }
        )

    def is_session_busy(self, session_id: str) -> bool:
        return session_id in self._busy_sessions


class FakeRegistry:
    """模拟 GitDiffWatcherRegistry,记录 mark_dirty 调用。"""

    def __init__(self):
        self.mark_dirty_calls: list[str] = []

    def mark_dirty(self, project_id: str) -> None:
        self.mark_dirty_calls.append(project_id)


def _make_handler(channel: FakeWebChannel, registry: FakeRegistry):
    from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import (
        GitDiffWebSocketHandler,
    )
    return GitDiffWebSocketHandler(channel=channel, registry=registry)


def _make_project(project_id: str, project_dir: str | None = "/tmp/proj"):
    return SimpleNamespace(
        project_id=project_id,
        project_dir=project_dir,
        work_mode="code",
        git=SimpleNamespace(enabled=True),
    )


@pytest.mark.parametrize(
    "session_meta, expected_code",
    [
        # session 属于另一个项目 → PROJECT_SESSION_MISMATCH
        ({"project_id": "proj-B"}, "PROJECT_SESSION_MISMATCH"),
        # session 无 project_id 绑定 → SESSION_NOT_BOUND
        ({"project_id": ""}, "SESSION_NOT_BOUND"),
    ],
    ids=["mismatch", "not_bound"],
)
@pytest.mark.asyncio
async def test_session_project_binding_rejected(session_meta, expected_code):
    """session 与 project 绑定校验不通过时应拒绝。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler.GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project("proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value=session_meta,
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == expected_code
    assert registry.mark_dirty_calls == []


@pytest.mark.asyncio
async def test_busy_session_rejected():
    """会话忙碌时应返回 SESSION_BUSY。"""
    channel = FakeWebChannel(busy_sessions={"sess-1"})
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler.GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project("proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "SESSION_BUSY"
    assert registry.mark_dirty_calls == []


@pytest.mark.asyncio
async def test_successful_discard_restores_files_and_marks_dirty():
    """成功撤销:恢复文件、清理 session-specific file_ops、标脏正确项目。

    覆盖 P1 修复:handler 显式传入 proj.project_dir 到 restore_session_files
    与 truncate_file_ops_by_timestamp,避免底层 _get_project_dir_from_metadata
    在 Web/code 模式(无 channel_metadata.cwd)下漏读项目目录。
    """
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    fake_restore_result = {
        "session_id": "sess-1",
        "turn_index": 2,
        "restored_files": ["/tmp/proj/a.py"],
        "deleted_files": ["/tmp/proj/new_file.py"],
        "errors": [],
    }

    truncate_calls: list[dict] = []
    restore_calls: list[dict] = []

    def fake_truncate(session_id, cutoff_ts, project_dir=None, soft=False, *, extra_history_roots=None, discarded=False):
        truncate_calls.append({
            "session_id": session_id,
            "cutoff_ts": cutoff_ts,
            "project_dir": project_dir,
            "soft": soft,
            "discarded": discarded,
            "extra_history_roots": extra_history_roots,
        })

    mark_discarded_calls: list[dict] = []

    def fake_mark_discarded(session_id, turn_index, project_dir=None, *, extra_history_roots=None):
        mark_discarded_calls.append({
            "session_id": session_id,
            "turn_index": turn_index,
            "project_dir": project_dir,
            "extra_history_roots": extra_history_roots,
        })
        return "cs_sess-1_2_test1234"

    def fake_restore(*, session_id, turn_index, project_dir=None, extra_history_roots=None):
        restore_calls.append({
            "session_id": session_id,
            "turn_index": turn_index,
            "project_dir": project_dir,
            "extra_history_roots": extra_history_roots,
        })
        return fake_restore_result

    fake_diff_service = SimpleNamespace(
        mark_turn_discarded=fake_mark_discarded,
        truncate_file_ops_by_timestamp=fake_truncate,
    )

    meta_calls: list[dict] = []

    def fake_get_meta(session_id, cache_bust=False, *, enable_writeback=True):
        meta_calls.append({
            "session_id": session_id,
            "cache_bust": cache_bust,
            "enable_writeback": enable_writeback,
        })
        return {"project_id": "proj-A"}

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler.GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            side_effect=fake_get_meta,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_turn_info",
            return_value={"turn_index": 2, "timestamp": 1000.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            side_effect=fake_restore,
        ),
        patch(
            "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
            return_value=["/tmp/team-workspace"],
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=fake_diff_service,
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    # 验证响应
    resp = channel.responses[0]
    assert resp["ok"] is True
    payload = resp["payload"]
    assert payload["file_ops_truncated"] is True
    assert payload["global_file_ops_truncated"] is False
    assert payload["change_set_id"] == "cs_sess-1_2_test1234"

    # P2 修复:用 get_session_metadata(enable_writeback=False) 保留推断、避免写盘
    assert len(meta_calls) == 1
    assert meta_calls[0]["enable_writeback"] is False

    # P1 修复:handler 显式传入 proj.project_dir 到 restore / truncate
    assert restore_calls[0]["project_dir"] == "/tmp/proj-A"
    assert mark_discarded_calls[0]["project_dir"] == "/tmp/proj-A"
    assert truncate_calls[0]["project_dir"] == "/tmp/proj-A"
    assert restore_calls[0]["extra_history_roots"] == ["/tmp/team-workspace"]
    assert mark_discarded_calls[0]["extra_history_roots"] == ["/tmp/team-workspace"]
    assert truncate_calls[0]["extra_history_roots"] == ["/tmp/team-workspace"]
    # discard 路径应使用 discarded_out 标记(与 rewind 的 rewound_out 区分)
    assert truncate_calls[0]["soft"] is True
    assert truncate_calls[0]["discarded"] is True

    # watcher 标脏正确项目
    assert registry.mark_dirty_calls == ["proj-A"]


@pytest.mark.asyncio
async def test_partial_failure_keeps_file_ops_for_retry():
    """部分文件恢复失败时:不截断 file_ops,返回 ok=False + partial=True(P1 修复)。

    失败文件的日志保留,调用方可重试。watcher 仍标脏(已恢复的文件需要刷新)。
    """
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    fake_restore_result = {
        "session_id": "sess-1",
        "turn_index": 2,
        "restored_files": ["/tmp/proj/a.py"],
        "deleted_files": [],
        "errors": [{"file": "/tmp/proj/locked.py", "error": "PermissionError"}],
    }

    truncate_calls: list[dict] = []

    def fake_truncate(session_id, cutoff_ts, project_dir=None, soft=False, *, extra_history_roots=None, discarded=False):
        truncate_calls.append({
            "session_id": session_id,
            "cutoff_ts": cutoff_ts,
            "project_dir": project_dir,
            "soft": soft,
            "discarded": discarded,
            "extra_history_roots": extra_history_roots,
        })

    fake_diff_service = SimpleNamespace(
        truncate_file_ops_by_timestamp=fake_truncate,
    )

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler.GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project("proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_turn_info",
            return_value={"turn_index": 2, "timestamp": 1000.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            return_value=fake_restore_result,
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=fake_diff_service,
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    # 验证响应:ok=False,partial=True,file_ops 未截断
    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "PARTIAL_RESTORE_FAILED"
    assert "retryable" in resp["error"]
    payload = resp["payload"]
    assert payload["file_ops_truncated"] is False
    assert payload["global_file_ops_truncated"] is False
    assert payload["partial"] is True
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["file"] == "/tmp/proj/locked.py"

    # 验证 file_ops 未被截断(保留日志供重试)
    assert truncate_calls == []

    # 验证 watcher 仍标脏(已恢复的文件需要刷新前端)
    assert registry.mark_dirty_calls == ["proj-A"]


@pytest.mark.asyncio
async def test_restore_exception_returns_error_response():
    """恢复前置计算抛异常时返回结构化错误,而不是让 WS 连接被关闭。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler.GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project("proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_turn_info",
            return_value={"turn_index": 2, "timestamp": 1000.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            side_effect=RuntimeError("broken file_ops"),
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "INTERNAL_ERROR"
    assert "broken file_ops" in resp["error"]
    assert registry.mark_dirty_calls == []
