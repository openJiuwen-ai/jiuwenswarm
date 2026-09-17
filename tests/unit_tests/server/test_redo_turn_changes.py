# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for project.git.redo_turn_changes WS handler.

与 ``test_discard_turn_changes`` 对称,只覆盖 redo 独有的行为:
  - 成功 redo:文件重新应用、discarded 状态清除、watcher 标脏
  - 最后一轮未 discarded 时拒绝(NOTHING_TO_REDO)——redo 独有前置条件
  - 部分失败:errors 非空时不清除 discarded 状态(unmark 不调用),可重试
  - 并发回归(P3): 并发 redo 按 session 串行——恰一个成功,另一个
    NOTHING_TO_REDO(而非误报 REDO_HISTORY_MISSING)

绑定校验 / busy 拒绝 / 异常处理与 discard handler 共用同一套校验代码,
已由 ``test_discard_turn_changes`` 覆盖,此处不重复。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class FakeWebChannel:
    channel_id = "web"

    def __init__(self):
        self.responses: list[dict] = []
        self.agent_client = _ProjectAdapterAgentClient()

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code})

    def is_session_busy(self, session_id: str) -> bool:
        return False

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
        "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
        return_value=(_make_project(), None, None),
    ))
    stack.enter_context(patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"project_id": "proj-A"},
    ))
    stack.enter_context(patch(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_last_modified_turn_info",
        return_value={"turn_index": 2, "timestamp": 1000.0},
    ))
    stack.enter_context(patch(
        "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
        return_value=["/tmp/team-workspace"],
    ))

    unmark_calls: list[dict] = []
    get_turn_diff_calls: list[dict] = []

    def fake_unmark(session_id, turn_index, project_dir=None, *, extra_history_roots=None, target=None):
        unmark_calls.append({"project_dir": project_dir, "extra_history_roots": extra_history_roots, "target": target})
        return "cs_sess-1_2_test1234"

    def fake_get_turn_diff(sid, **kw):
        target = {"status": status, "change_set_id": "cs_sess-1_2_test1234"}
        get_turn_diff_calls.append(target)
        return target

    fake_diff_service = SimpleNamespace(
        get_turn_diff=fake_get_turn_diff,
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

    return stack, unmark_calls, get_turn_diff_calls


@pytest.mark.asyncio
async def test_successful_redo_reapplies_files_and_clears_status():
    """成功 redo:redo/unmark 收到正确 project_dir 与 extra_history_roots,watcher 标脏。"""
    handler = _make_handler()
    redo_result = {
        "session_id": "sess-1", "turn_index": 2,
        "redone_files": ["/tmp/proj/a.py"], "deleted_files": ["/tmp/proj/removed.py"],
        "errors": [],
    }

    stack, unmark_calls, get_turn_diff_calls = _common_patches(
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
    # 优化:守卫查到的 target 直接复用给 unmark_turn_discarded,
    # 整个 redo 流程只调用一次 get_turn_diff(省一次完整 diff 计算)
    assert len(get_turn_diff_calls) == 1
    assert unmark_calls[0]["target"] is get_turn_diff_calls[0]
    # watcher 标脏
    assert handler._registry.mark_dirty_calls == ["proj-A"]


@pytest.mark.asyncio
async def test_redo_not_discarded_turn_rejected():
    """最后一轮未 discarded 时返回 NOTHING_TO_REDO(redo 独有前置条件)。"""
    handler = _make_handler()
    stack, _, _ = _common_patches(handler, status="applied")
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

    stack, unmark_calls, _ = _common_patches(handler, redo_result=redo_result)
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

    stack, unmark_calls, _ = _common_patches(handler, redo_result=redo_result)
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


@pytest.mark.asyncio
async def test_redo_returns_diff_history_expired_when_target_lookup_raises():
    """守卫查询目标轮时 diff 历史已过期 → DIFF_HISTORY_EXPIRED(不触碰文件)。

    覆盖:get_turn_diff 抛 DiffHistoryExpiredError 时优雅返回该错误码,
    而不是被外层兜成 INTERNAL_ERROR。
    """
    from jiuwenswarm.server.utils.diff_service import DiffHistoryExpiredError

    handler = _make_handler()
    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project(), None, None),
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
            "jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots",
            return_value=["/tmp/team-workspace"],
        ),
        patch(
            "jiuwenswarm.server.utils.diff_service.get_diff_service",
            return_value=SimpleNamespace(
                get_turn_diff=lambda sid, **kw: (_ for _ in ()).throw(
                    DiffHistoryExpiredError("diff history expired for turn_index=2")
                ),
            ),
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.redo_session_files",
            side_effect=AssertionError("workspace must not be touched"),
        ),
    ):
        await handler._handle_redo_turn_changes(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = handler._channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "DIFF_HISTORY_EXPIRED"
    assert "expired" in resp["error"]
    assert handler._registry.mark_dirty_calls == []


def test_concurrent_redo_serialized_per_session():
    """并发 redo 按 session 串行:恰一个成功,另一个 NOTHING_TO_REDO。

    回归(P3): 状态校验(status == discarded)与状态变更(redo + unmark)
    之间非原子,两个客户端可同时通过检查;后者读不到已被前者 unmark
    移除的 discarded_out 条目,误报 REDO_HISTORY_MISSING。串行化后
    第二个请求在锁内重读状态,看到前一个已写回的 completed。
    """
    import threading
    import time

    from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import _run_redo_turn_changes

    state = {"status": "discarded", "unmarked": False}

    def fake_redo(*, session_id, turn_index, project_dir=None, extra_history_roots=None):
        # 放大 TOCTOU 窗口;前一个请求 unmark 后 discarded_out 条目不可见
        time.sleep(0.15)
        redone = [] if state["unmarked"] else ["/tmp/proj-A/a.py"]
        return {
            "session_id": session_id,
            "turn_index": turn_index,
            "redone_files": redone,
            "deleted_files": [],
            "errors": [],
        }

    def fake_unmark(*args, **kwargs):
        state["unmarked"] = True
        state["status"] = "completed"
        return "cs-x"

    fake_diff_service = SimpleNamespace(
        get_turn_diff=lambda *args, **kwargs: {"status": state["status"], "change_set_id": "cs-x"},
        unmark_turn_discarded=fake_unmark,
    )

    results: list = []
    lock = threading.Lock()

    def _call():
        result = _run_redo_turn_changes({"project_id": "proj-A", "session_id": "sess-1"})
        with lock:
            results.append(result)

    with (
        patch(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            return_value=(_make_project(), None, None),
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
            "jiuwenswarm.agents.harness.common.session_ops_service.redo_session_files",
            side_effect=fake_redo,
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
        threads = [threading.Thread(target=_call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    codes = sorted("ok" if err is None else err["code"] for _, err in results)
    assert codes == ["NOTHING_TO_REDO", "ok"]
