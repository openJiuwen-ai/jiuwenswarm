# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for session_id propagation in diff_watch/files/detail first-snapshot.

P2 修复:首次快照路径与轮询路径(``_compute_and_push``)语义对齐。
只在 ``include_last_turn`` 或 ``source == "last_turn"`` 时传 session_id 给
diff 状态获取器(经 E2A 转发到目标 AgentServer 的 ``PROJECT_GIT_DIFF_STATUS``),
避免 current-only 订阅因 file_ops 历史读取异常而首次订阅失败。

覆盖场景:
  - summary + include_last_turn=False  → session_id=None
  - summary + include_last_turn=True   → session_id="sess-1"
  - files   + source="current"         → session_id=None
  - files   + source="last_turn"       → session_id="sess-1"
  - detail  + source="current"         → session_id=None
  - detail  + source="last_turn"       → session_id="sess-1"
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class FakeWebChannel:
    """记录 send_response 的简单 channel stub。"""

    def __init__(self) -> None:
        self.responses: list[dict] = []

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


class FakeRegistry:
    """模拟 GitDiffWatcherRegistry,触发 on_initial/on_snapshot 回调。"""

    def __init__(self) -> None:
        self.add_watch_calls: list[dict] = []
        self.update_files_calls: list[dict] = []
        self.update_detail_calls: list[dict] = []
        self.commit_summary_calls: list[dict] = []
        self.commit_files_calls: list[dict] = []
        self.commit_detail_calls: list[dict] = []

    async def add_watch(
        self, ws, project_id, session_id, scope="summary", *,
        include_last_turn=True, on_initial=None,
    ):
        self.add_watch_calls.append({
            "project_id": project_id,
            "session_id": session_id,
            "include_last_turn": include_last_turn,
        })
        watch = SimpleNamespace(
            watch_id="wid-summary",
            project_id=project_id,
            session_id=session_id,
            ws=ws,
            scope=scope,
            include_last_turn=bool(include_last_turn) and bool(session_id),
        )
        if on_initial is not None:
            status_dict = await on_initial(watch)
            self.commit_initial_summary(watch.watch_id, status_dict)
        return watch

    async def update_files_with_restore(
        self, watch_id, source, *,
        expected_ws=None, expected_project_id=None, on_snapshot=None,
    ):
        self.update_files_calls.append({
            "watch_id": watch_id,
            "source": source,
            "expected_project_id": expected_project_id,
        })
        watch = SimpleNamespace(
            watch_id=watch_id,
            project_id=expected_project_id or "proj-A",
            session_id="sess-1",
            ws=expected_ws,
            files_source=source,
        )
        if on_snapshot is not None:
            await on_snapshot(watch)
        return watch

    async def update_detail_with_restore(
        self, watch_id, source, files, *,
        expected_ws=None, expected_project_id=None, on_snapshot=None,
    ):
        self.update_detail_calls.append({
            "watch_id": watch_id,
            "source": source,
            "files": list(files),
            "expected_project_id": expected_project_id,
        })
        watch = SimpleNamespace(
            watch_id=watch_id,
            project_id=expected_project_id or "proj-A",
            session_id="sess-1",
            ws=expected_ws,
            detail_source=source,
            detail_files=list(files),
        )
        if on_snapshot is not None:
            await on_snapshot(watch)
        return watch

    def commit_initial_summary(self, watch_id, status_dict):
        self.commit_summary_calls.append({"watch_id": watch_id})

    def commit_initial_files(self, watch_id, status_dict, source):
        self.commit_files_calls.append({"watch_id": watch_id, "source": source})

    def commit_initial_detail(self, watch_id, status_dict, source, detail_files):
        self.commit_detail_calls.append({"watch_id": watch_id, "source": source})


def _make_handler(channel, registry):
    from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import (
        GitDiffWebSocketHandler,
    )
    return GitDiffWebSocketHandler(channel=channel, registry=registry)


def _make_fake_fetcher(fail_when_session_id=False):
    """构造 fake diff 状态获取器,记录调用参数并返回 (ok, status_dict)。

    ``fail_when_session_id=True`` 时,任何非空 session_id 请求返回失败,
    用于模拟 current-only 订阅在 file_ops 历史读取异常时仍成功。
    """
    fetch_calls: list[dict] = []

    async def fake_fetch(
        self, ws, project_id, session_id, *, include_files, include_hunks,
        hunk_paths=None,
    ):
        del self
        fetch_calls.append({
            "project_id": project_id,
            "session_id": session_id,
            "include_files": include_files,
            "include_hunks": include_hunks,
            "hunk_paths": hunk_paths,
        })
        if fail_when_session_id and session_id is not None:
            return False, {"error": "broken file_ops / change_set", "code": "INTERNAL_ERROR"}
        return True, {
            "project_id": project_id,
            "session_id": session_id,
            "repo": {
                "is_git": True,
                "repo_root": "/tmp/proj",
                "branch": "main",
                "head": "abc123",
                "transient": False,
            },
            "current": {"files": {"a.py": {"status": "M"}}},
            "last_turn": None,
        }

    return fake_fetch, fetch_calls


def _patch_fetcher(fake_fetch):
    """patch handler 的 _fetch_diff_status_via_e2a。"""
    return patch(
        "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
        "GitDiffWebSocketHandler._fetch_diff_status_via_e2a",
        fake_fetch,
    )


# ---------------------------------------------------------------------------
# summary 路径:_handle_diff_watch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_include_last_turn_false_passes_no_session_id():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher()

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "session_id": "sess-1",
                "include_last_turn": False,
            },
        )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] is None
    resp = channel.responses[0]
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_summary_include_last_turn_true_passes_session_id():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher()

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "session_id": "sess-1",
                "include_last_turn": True,
            },
        )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# files 路径:_handle_diff_files_watch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_files_source_current_passes_no_session_id():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher()

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_files_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-1",
                "source": "current",
                "session_id": "sess-1",
            },
        )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] is None
    assert fetch_calls[0]["hunk_paths"] is None
    resp = channel.responses[0]
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_files_source_last_turn_passes_session_id():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher()

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_files_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-1",
                "source": "last_turn",
                "session_id": "sess-1",
            },
        )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] == "sess-1"
    assert fetch_calls[0]["hunk_paths"] is None


# ---------------------------------------------------------------------------
# detail 路径:_handle_diff_detail_watch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_source_current_passes_no_session_id():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher()

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_detail_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-1",
                "source": "current",
                "session_id": "sess-1",
                "files": ["a.py"],
            },
        )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] is None
    assert fetch_calls[0]["hunk_paths"] == ["a.py"]
    resp = channel.responses[0]
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_detail_source_last_turn_passes_session_id():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher()

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_detail_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-1",
                "source": "last_turn",
                "session_id": "sess-1",
                "files": ["a.py"],
            },
        )

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# 关键回归:current-only 订阅在 file_ops 历史异常时仍能成功
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_current_only_subscription_survives_file_ops_failure():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_fetch, fetch_calls = _make_fake_fetcher(fail_when_session_id=True)

    with _patch_fetcher(fake_fetch):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "session_id": "sess-1",
                "include_last_turn": False,
            },
        )

    resp = channel.responses[0]
    assert resp["ok"] is True
    assert resp["payload"]["watch_id"] == "wid-summary"
    assert resp["payload"]["snapshot"]["last_turn"] is None


# ---------------------------------------------------------------------------
# 首次快照失败时结构化错误码保留
# 修复回归:AgentServer 返回 {error, code} 时不得退化为 INTERNAL_ERROR
# ---------------------------------------------------------------------------

def _make_failing_fetcher(error: str, code: str | None):
    async def fake_fetch(
        self, ws, project_id, session_id, *, include_files, include_hunks,
        hunk_paths=None,
    ):
        del self
        status: dict = {"error": error}
        if code is not None:
            status["code"] = code
        return False, status

    return fake_fetch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,expected_code",
    [
        ("PROJECT_NOT_FOUND", "PROJECT_NOT_FOUND"),
        ("FORBIDDEN", "FORBIDDEN"),
    ],
)
async def test_summary_first_snapshot_failure_preserves_error_code(code, expected_code):
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with _patch_fetcher(_make_failing_fetcher("project not found", code)):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={"project_id": "proj-A", "session_id": "sess-1"},
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == expected_code
    assert resp["error"] == "project not found"
    # GitError 结构化明细随 payload.detail 下发（与 Git RPC 错误契约一致）
    assert resp["payload"]["detail"]["code"] == expected_code


@pytest.mark.asyncio
async def test_files_first_snapshot_failure_without_code_degrades_to_internal_error():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with _patch_fetcher(_make_failing_fetcher("transport broken", None)):
        await handler._handle_diff_files_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-summary",
                "source": "current",
            },
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_detail_first_snapshot_failure_preserves_error_code():
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    with _patch_fetcher(_make_failing_fetcher("project not found", "PROJECT_NOT_FOUND")):
        await handler._handle_diff_detail_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-summary",
                "source": "current",
                "files": ["a.py"],
            },
        )

    resp = channel.responses[0]
    assert resp["ok"] is False
    assert resp["code"] == "PROJECT_NOT_FOUND"
