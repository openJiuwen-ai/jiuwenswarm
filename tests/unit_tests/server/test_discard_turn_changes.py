# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for project.git.discard_turn_changes WS handler.

核心覆盖:
  - session 与 project 绑定校验(不匹配 / 无绑定)
  - busy 会话拒绝
  - 成功撤销:文件恢复、file_ops 截断、watcher 标脏正确项目
  - 部分失败:errors 非空时不截断 file_ops,返回 ok=False + partial=True(P1 修复)
  - handler 显式传入 proj.project_dir 到 restore/truncate(P1 修复:
    避免底层 _get_project_dir_from_metadata 漏读项目目录导致 file_ops 漏扫)
  - 最后一个有修改的轮已 discarded 时拒绝重复撤销(NOTHING_TO_DISCARD,
    与 redo 的 NOTHING_TO_REDO 守卫对称)
  - locator 回归(P1): 已 discarded 的最新修改轮快照丢失时返回零时间戳
    拒绝,不降级选中更早轮次(否则 redo 按时间下界会重放已撤销内容)
  - locator 回归(P2): rewind 后 change_sets/snapshot 残留的轮次按当前
    history 的 user 消息身份校验排除,不定位到已回退的轮次
  - 并发回归(P3): discard/redo 按 session 串行化——并发 discard 恰一个
    成功(另一个 NOTHING_TO_DISCARD),锁按 session 隔离不互相阻塞

注: P1 修复后 ``truncate_file_ops_by_timestamp`` 不再清理全局 file_ops
(避免误伤其他 session 同文件修改),仅清理 session-specific file_ops。
P2 修复后响应显式返回 ``global_file_ops_truncated=False``,
让调用方知晓 last_turn diff 可能残留历史全局记录。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class FakeWebChannel:
    """模拟 WebChannel,记录 send_response 调用与 busy 状态。"""

    channel_id = "web"

    def __init__(self, *, busy_sessions: set[str] | None = None):
        self.responses: list[dict] = []
        self._busy_sessions = busy_sessions or set()
        self.agent_client = _ProjectAdapterAgentClient()

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

    @staticmethod
    def connection_user_id(_ws) -> str:
        return "test-user"


class _ProjectAdapterAgentClient:
    """Execute the E2A request in the production AgentServer adapter."""

    server_ready = True

    async def send_request(self, request):
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import ProjectAdapter

        response = await ProjectAdapter().handle(AgentRequest(
            request_id=request.request_id,
            channel_id=request.channel,
            session_id=request.session_id,
            req_method=ReqMethod(request.method),
            params=dict(request.params or {}),
            user_id=request.user_id or "",
        ))
        return SimpleNamespace(ok=response.ok, payload=response.payload)


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
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
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
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
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

    def fake_mark_discarded(session_id, turn_index, project_dir=None, *, extra_history_roots=None, target=None):
        mark_discarded_calls.append({
            "session_id": session_id,
            "turn_index": turn_index,
            "project_dir": project_dir,
            "extra_history_roots": extra_history_roots,
            "target": target,
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

    guard_target = {"status": "completed", "change_set_id": "cs_sess-1_2_test1234"}
    get_turn_diff_calls: list[dict] = []

    def fake_get_turn_diff(*args, **kwargs):
        get_turn_diff_calls.append({"args": args, "kwargs": kwargs})
        return guard_target

    fake_diff_service = SimpleNamespace(
        get_turn_diff=fake_get_turn_diff,
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
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            side_effect=fake_get_meta,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
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

    # 优化:守卫查到的 target 直接复用给 mark_turn_discarded,
    # 整个 discard 流程只调用一次 get_turn_diff(省一次完整 diff 计算)
    assert len(get_turn_diff_calls) == 1
    assert mark_discarded_calls[0]["target"] is guard_target

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
        get_turn_diff=lambda *args, **kwargs: {"status": "completed"},
        truncate_file_ops_by_timestamp=fake_truncate,
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
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
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
            return_value={"turn_index": 2, "timestamp": 1000.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            side_effect=RuntimeError("broken file_ops"),
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=SimpleNamespace(
                get_turn_diff=lambda *args, **kwargs: {"status": "completed"},
            ),
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


@pytest.mark.asyncio
async def test_discard_locates_target_via_last_modified_turn_info():
    """discard 通过 get_last_modified_turn_info 定位目标轮并传递 project_dir 与 roots。

    覆盖修复:末尾轮次为纯对话(无文件修改)时,目标轮是最后一个有修改的
    轮次,而不是最后一条 user 消息所在的轮次。
    """
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    locator_calls: list[dict] = []

    def fake_locator(*, session_id, project_dir=None, extra_history_roots=None):
        locator_calls.append({
            "session_id": session_id,
            "project_dir": project_dir,
            "extra_history_roots": extra_history_roots,
        })
        return {"turn_index": 1, "timestamp": 500.0}

    truncate_calls: list[dict] = []
    restore_calls: list[dict] = []

    def fake_truncate(session_id, cutoff_ts, project_dir=None, soft=False, *, extra_history_roots=None, discarded=False):
        truncate_calls.append({"cutoff_ts": cutoff_ts})

    def fake_restore(*, session_id, turn_index, project_dir=None, extra_history_roots=None):
        restore_calls.append({"turn_index": turn_index})
        return {
            "session_id": session_id,
            "turn_index": turn_index,
            "restored_files": ["/tmp/proj/a.py"],
            "deleted_files": [],
            "errors": [],
        }

    fake_diff_service = SimpleNamespace(
        get_turn_diff=lambda *args, **kwargs: {"status": "completed"},
        mark_turn_discarded=lambda *args, **kwargs: "cs_sess-1_1_test1234",
        truncate_file_ops_by_timestamp=fake_truncate,
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
            side_effect=fake_locator,
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

    resp = channel.responses[0]
    assert resp["ok"] is True
    assert resp["payload"]["turn_index"] == 1

    # 定位函数收到 project_dir 与 extra_history_roots(修复点)
    assert len(locator_calls) == 1
    assert locator_calls[0]["project_dir"] == "/tmp/proj-A"
    assert locator_calls[0]["extra_history_roots"] == ["/tmp/team-workspace"]
    # restore/truncate 作用于定位到的轮次与时间戳
    assert restore_calls[0]["turn_index"] == 1
    assert truncate_calls[0]["cutoff_ts"] == 500.0


@pytest.mark.asyncio
async def test_discard_already_discarded_returns_nothing_to_discard():
    """最后一个有修改的轮已处于 discarded 状态时拒绝重复撤销(不触碰文件)。

    覆盖修复:discarded 轮的快照保留文件列表,仍会被
    get_last_modified_turn_info 定位为 undo 目标;此时直接调 API 重复
    discard 不应再次 restore/标记,应返回 NOTHING_TO_DISCARD
    (与 redo 的 NOTHING_TO_REDO 守卫对称)。
    """
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    restore_calls: list[dict] = []

    def fake_restore(*, session_id, turn_index, project_dir=None, extra_history_roots=None):
        restore_calls.append({"turn_index": turn_index})
        return {
            "session_id": session_id,
            "turn_index": turn_index,
            "restored_files": ["/tmp/proj/a.py"],
            "deleted_files": [],
            "errors": [],
        }

    fake_diff_service = SimpleNamespace(
        get_turn_diff=lambda *args, **kwargs: {"status": "discarded", "change_set_id": "cs_sess-1_2_test1234"},
        mark_turn_discarded=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not mark discarded")),
        truncate_file_ops_by_timestamp=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not truncate")),
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
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

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "NOTHING_TO_DISCARD"
    assert "already discarded" in resp["error"]
    # 守卫在 restore 之前拦截:不触碰文件、不标脏
    assert restore_calls == []
    assert registry.mark_dirty_calls == []


@pytest.mark.asyncio
async def test_discard_rejects_target_without_timestamp_before_restoring_files():
    """缺少截断时间戳时不能恢复工作区后再返回假成功。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
            return_value={"turn_index": 2, "timestamp": 0.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            side_effect=AssertionError("workspace must not be touched"),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
            return_value=["/tmp/team-workspace"],
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "DIFF_HISTORY_EXPIRED"
    assert registry.mark_dirty_calls == []


@pytest.mark.asyncio
async def test_discard_returns_diff_history_expired_when_target_lookup_raises():
    """守卫查询目标轮时 diff 历史已过期 → DIFF_HISTORY_EXPIRED(不触碰文件)。

    覆盖:get_turn_diff 抛 DiffHistoryExpiredError 时优雅返回该错误码,
    而不是被外层兜成 INTERNAL_ERROR。
    """
    from jiuwenswarm.server.utils.diff_service import DiffHistoryExpiredError

    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
            return_value={"turn_index": 2, "timestamp": 1000.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            side_effect=AssertionError("workspace must not be touched"),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
            return_value=["/tmp/team-workspace"],
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=SimpleNamespace(
                get_turn_diff=lambda *args, **kwargs: (_ for _ in ()).throw(
                    DiffHistoryExpiredError("diff history expired for turn_index=2")
                ),
            ),
        ),
    ):
        await handler._handle_discard_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "DIFF_HISTORY_EXPIRED"
    assert "expired" in resp["error"]
    assert registry.mark_dirty_calls == []


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def test_get_last_modified_turn_info_skips_empty_turns():
    """末尾轮次无文件修改时,定位到最后一个有修改的轮次(核心修复)。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import get_last_modified_turn_info
    from jiuwenswarm.server.utils.diff_service import DiffService

    history = [
        {"role": "user", "timestamp": 100.0, "content": "edit files", "request_id": "r1", "id": "r1:user"},
        {"role": "assistant", "timestamp": 110.0, "content": "ok", "request_id": "r1", "id": "r1:assistant"},
        {"role": "user", "timestamp": 200.0, "content": "just chat", "request_id": "r2", "id": "r2:user"},
        {"role": "assistant", "timestamp": 210.0, "content": "reply", "request_id": "r2", "id": "r2:assistant"},
    ]
    file_ops = {
        "/proj/a.py": [{
            "action": "write",
            "timestamp": _ts(110.0),
            "old_content": "old\n",
            "new_content": "new\n",
        }],
    }

    with (
        patch.object(DiffService, "_read_history", return_value=history),
        patch.object(DiffService, "_read_agent_history", return_value=file_ops),
        patch.object(DiffService, "_load_change_sets", return_value=[]),
        patch.object(DiffService, "_save_change_sets", return_value=None),
        patch.object(DiffService, "_load_turn_snapshot", lambda self, session_id, change_set_id: None),
        patch.object(DiffService, "_save_turn_snapshot", lambda self, session_id, turn: None),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=history,
        ),
    ):
        info = get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")

    assert info == {"turn_index": 1, "timestamp": 100.0}


def test_get_last_modified_turn_info_returns_zero_without_modified_turns():
    """所有轮次都没有文件修改时返回零值(discard/redo 报 NO_TURN_*)。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import get_last_modified_turn_info
    from jiuwenswarm.server.utils.diff_service import DiffService

    history = [
        {"role": "user", "timestamp": 100.0, "content": "just chat", "request_id": "r1", "id": "r1:user"},
        {"role": "assistant", "timestamp": 110.0, "content": "reply", "request_id": "r1", "id": "r1:assistant"},
    ]

    with (
        patch.object(DiffService, "_read_history", return_value=history),
        patch.object(DiffService, "_read_agent_history", return_value={}),
        patch.object(DiffService, "_load_change_sets", return_value=[]),
        patch.object(DiffService, "_save_change_sets", return_value=None),
        patch.object(DiffService, "_load_turn_snapshot", lambda self, session_id, change_set_id: None),
        patch.object(DiffService, "_save_turn_snapshot", lambda self, session_id, turn: None),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=history,
        ),
    ):
        info = get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")

    assert info == {"turn_index": 0, "timestamp": 0.0}


def test_get_last_modified_turn_info_propagates_history_read_failure():
    """history 读取失败向上传播,不降级成零值空态。

    回归:吞掉异常返回 {turn_index: 0} 会让 discard/redo 报
    NO_TURN_TO_DISCARD("当前会话没有可撤销的修改"),把内部错误
    伪装成业务空态误导用户。
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        get_last_modified_turn_info,
    )

    with (
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            side_effect=OSError("disk error"),
        ),
    ):
        with pytest.raises(OSError, match="disk error"):
            get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")


def test_get_last_modified_turn_info_propagates_summaries_failure():
    """turn diff 计算失败(如持久化数据畸形)向上传播,不降级成零值空态。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        get_last_modified_turn_info,
    )

    history = [
        {"role": "user", "timestamp": 100.0, "content": "edit files", "request_id": "r1", "id": "r1:user"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=history,
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=SimpleNamespace(
                get_turn_diff_summaries=lambda *args, **kwargs: (_ for _ in ()).throw(
                    KeyError("timestamp")
                ),
            ),
        ),
    ):
        with pytest.raises(KeyError, match="timestamp"):
            get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")


def _run_discard_concurrently(params: dict, count: int = 2) -> list:
    """在线程池中并发调用 discard 入口(与 asyncio.to_thread 的真实并发一致)."""
    from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import _run_discard_turn_changes

    results: list = []
    lock = threading.Lock()

    def _call():
        result = _run_discard_turn_changes(dict(params))
        with lock:
            results.append(result)

    threads = [threading.Thread(target=_call) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_discard_serialized_per_session():
    """并发 discard 按 session 串行:恰一个成功,另一个 NOTHING_TO_DISCARD。

    回归(P3): 状态校验(get_turn_diff 的 status 检查)与状态变更之间非原子,
    两个客户端可同时通过检查,重复 discard 会双双返回成功(违反
    NOTHING_TO_DISCARD 契约)。串行化后第二个请求在锁内重读状态,看到
    前一个已写入的 discarded。
    """
    state = {"status": "completed"}

    def fake_restore(*, session_id, turn_index, project_dir=None, extra_history_roots=None):
        # 放大"读 status 之后 → mark 之前"的 TOCTOU 窗口:无锁时两个线程
        # 都会在对方 mark 前完成 status 读取(双双成功)。
        time.sleep(0.15)
        return {
            "session_id": session_id,
            "turn_index": turn_index,
            "restored_files": ["/tmp/proj-A/a.py"],
            "deleted_files": [],
            "errors": [],
        }

    def fake_mark(*args, **kwargs):
        state["status"] = "discarded"
        return "cs-x"

    fake_diff_service = SimpleNamespace(
        get_turn_diff=lambda *args, **kwargs: {"status": state["status"], "change_set_id": "cs-x"},
        mark_turn_discarded=fake_mark,
        truncate_file_ops_by_timestamp=lambda *args, **kwargs: None,
    )

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project("proj-A", project_dir="/tmp/proj-A"), None, None),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value={"project_id": "proj-A"},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
            return_value={"turn_index": 2, "timestamp": 1000.0},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.restore_session_files",
            side_effect=fake_restore,
        ),
        patch(
            "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
            return_value=[],
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=fake_diff_service,
        ),
    ):
        results = _run_discard_concurrently({"project_id": "proj-A", "session_id": "sess-1"})

    codes = sorted("ok" if err is None else err["code"] for _, err in results)
    assert codes == ["NOTHING_TO_DISCARD", "ok"]


def test_turn_mutation_lock_does_not_block_other_sessions():
    """串行化锁按 session 隔离:session A 的慢操作不阻塞 session B。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import turn_mutation_lock

    order: list[str] = []

    def _slow():
        with turn_mutation_lock("sess-lock-A"):
            time.sleep(0.3)
            order.append("A")

    thread = threading.Thread(target=_slow)
    thread.start()
    time.sleep(0.05)
    with turn_mutation_lock("sess-lock-B"):
        order.append("B")
    thread.join()

    assert order == ["B", "A"]


def test_get_last_modified_turn_info_rejects_when_latest_snapshot_missing():
    """已 discarded 的最新修改轮快照丢失时拒绝,不降级选中更早轮次。

    change_sets 残留该轮记录但快照不可读时,summaries 会降级构造
    files={} 而 stats.filesChanged>0 的摘要;它仍是最后一个有修改的轮,
    跳过它会误选更早轮次——discard 作用到错误范围,redo 还会按时间
    下界重放该轮已撤销的内容。应返回零时间戳交由调用方守卫拒绝
    (discard: cut_timestamp<=0;redo: get_turn_diff 抛 DiffHistoryExpiredError)。
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import get_last_modified_turn_info
    from jiuwenswarm.server.utils.diff_service import DiffService

    history = [
        {"role": "user", "timestamp": 100.0, "content": "edit a", "request_id": "r1", "id": "r1:user"},
        {"role": "assistant", "timestamp": 110.0, "content": "ok", "request_id": "r1", "id": "r1:assistant"},
        {"role": "user", "timestamp": 200.0, "content": "edit b", "request_id": "r2", "id": "r2:user"},
        {"role": "assistant", "timestamp": 210.0, "content": "ok", "request_id": "r2", "id": "r2:assistant"},
    ]
    # 轮2 已 discard: b.py 条目带 discarded_out 标记,显示层不可见
    file_ops = {
        "/proj/a.py": [{
            "action": "write",
            "timestamp": _ts(110.0),
            "old_content": "old\n",
            "new_content": "new\n",
        }],
    }
    change_sets = [{
        "change_set_id": "cs-2",
        "turn_index": 2,
        "request_id": "r2",
        "user_message_id": "r2:user",
        "status": "discarded",
        "stats": {"filesChanged": 1, "linesAdded": 2, "linesRemoved": 1},
    }]

    with (
        patch.object(DiffService, "_read_history", return_value=history),
        patch.object(DiffService, "_read_agent_history", return_value=file_ops),
        patch.object(DiffService, "_load_change_sets", return_value=change_sets),
        patch.object(DiffService, "_save_change_sets", return_value=None),
        patch.object(DiffService, "_load_turn_snapshot", lambda self, session_id, change_set_id: None),
        patch.object(DiffService, "_save_turn_snapshot", lambda self, session_id, turn: None),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=history,
        ),
    ):
        info = get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")

    # 不降级选中轮1,而是返回轮2 + 零时间戳(调用方按 DIFF_HISTORY_EXPIRED 拒绝)
    assert info == {"turn_index": 2, "timestamp": 0.0}


def test_get_last_modified_turn_info_skips_rewound_turns():
    """rewind 后 change_sets/snapshot 残留的轮次不构成撤销目标。

    rewind_session 只截断 history 并软隐藏 file_ops,不清理 change_sets;
    summaries 仍会带回该轮快照(files 非空)。直接选最大轮次会定位到
    当前会话已不存在的轮次:restore 空转返回成功。应按当前 history 的
    user 消息身份校验,回退到仍存在的最后一个修改轮。
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import get_last_modified_turn_info
    from jiuwenswarm.server.utils.diff_service import DiffService

    # rewind 已移除轮2,当前 history 仅剩轮1
    history = [
        {"role": "user", "timestamp": 100.0, "content": "edit a", "request_id": "r1", "id": "r1:user"},
        {"role": "assistant", "timestamp": 110.0, "content": "ok", "request_id": "r1", "id": "r1:assistant"},
    ]
    # 轮2 的 b.py 条目带 rewound_out 标记,显示层不可见
    file_ops = {
        "/proj/a.py": [{
            "action": "write",
            "timestamp": _ts(110.0),
            "old_content": "old\n",
            "new_content": "new\n",
        }],
    }
    change_sets = [{
        "change_set_id": "cs-2",
        "turn_index": 2,
        "request_id": "r2",
        "user_message_id": "r2:user",
        "status": "completed",
        "stats": {"filesChanged": 1, "linesAdded": 2, "linesRemoved": 1},
    }]
    snapshots = {
        "cs-2": {
            "turnIndex": 2,
            "change_set_id": "cs-2",
            "request_id": "r2",
            "user_message_id": "r2:user",
            "status": "completed",
            "start_timestamp": 200.0,
            "files": {"/proj/b.py": {}},
        },
    }

    with (
        patch.object(DiffService, "_read_history", return_value=history),
        patch.object(DiffService, "_read_agent_history", return_value=file_ops),
        patch.object(DiffService, "_load_change_sets", return_value=change_sets),
        patch.object(DiffService, "_save_change_sets", return_value=None),
        patch.object(
            DiffService, "_load_turn_snapshot",
            lambda self, session_id, change_set_id: snapshots.get(change_set_id),
        ),
        patch.object(DiffService, "_save_turn_snapshot", lambda self, session_id, turn: None),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=history,
        ),
    ):
        info = get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")

    # 轮2 快照虽带 files,但已不在当前 history → 回落到轮1
    assert info == {"turn_index": 1, "timestamp": 100.0}


def test_get_last_modified_turn_info_returns_zero_when_only_modified_turn_rewound():
    """唯一有修改的轮被 rewind 移除时返回零值(NO_TURN_*),而非残留轮次。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import get_last_modified_turn_info
    from jiuwenswarm.server.utils.diff_service import DiffService

    history = [
        {"role": "user", "timestamp": 100.0, "content": "just chat", "request_id": "r1", "id": "r1:user"},
        {"role": "assistant", "timestamp": 110.0, "content": "reply", "request_id": "r1", "id": "r1:assistant"},
    ]
    change_sets = [{
        "change_set_id": "cs-2",
        "turn_index": 2,
        "request_id": "r2",
        "user_message_id": "r2:user",
        "status": "completed",
        "stats": {"filesChanged": 1, "linesAdded": 2, "linesRemoved": 1},
    }]
    snapshots = {
        "cs-2": {
            "turnIndex": 2,
            "change_set_id": "cs-2",
            "request_id": "r2",
            "user_message_id": "r2:user",
            "status": "completed",
            "start_timestamp": 200.0,
            "files": {"/proj/b.py": {}},
        },
    }

    with (
        patch.object(DiffService, "_read_history", return_value=history),
        patch.object(DiffService, "_read_agent_history", return_value={}),
        patch.object(DiffService, "_load_change_sets", return_value=change_sets),
        patch.object(DiffService, "_save_change_sets", return_value=None),
        patch.object(
            DiffService, "_load_turn_snapshot",
            lambda self, session_id, change_set_id: snapshots.get(change_set_id),
        ),
        patch.object(DiffService, "_save_turn_snapshot", lambda self, session_id, turn: None),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.history_exists",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=history,
        ),
    ):
        info = get_last_modified_turn_info(session_id="sess-1", project_dir="/proj")

    assert info == {"turn_index": 0, "timestamp": 0.0}
