# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for session_id propagation in diff_watch/files/detail first-snapshot.

P2 修复:首次快照路径与轮询路径(``_compute_and_push``)语义对齐。
只在 ``include_last_turn`` 或 ``source == "last_turn"`` 时传 session_id 给
``get_project_diff_status``,避免 current-only 订阅因 file_ops 历史读取异常
而首次订阅失败(异常会向上抛触发 ``remove_watch``)。

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
            # 与真实 registry 一致:空 session_id 时强制关闭 include_last_turn
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
            session_id="sess-1",  # 模拟已有 watcher
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


def _make_project(project_id="proj-A", project_dir="/tmp/proj"):
    return SimpleNamespace(
        project_id=project_id,
        project_dir=project_dir,
        work_mode="code",
        git=SimpleNamespace(enabled=True),
    )


def _make_fake_status_factory():
    """构造 fake DiffStatusService.get_project_diff_status,记录调用参数。

    返回 (service, status_calls)。service 适配 handler 调用签名
    (keyword-only project/session_id/include_files/include_hunks)。
    """
    status_calls: list[dict] = []

    def fake_get_project_diff_status(
        *, project, session_id, include_files, include_hunks, hunk_paths=None,
    ):
        status_calls.append({
            "project_id": project.project_id,
            "session_id": session_id,
            "include_files": include_files,
            "include_hunks": include_hunks,
            "hunk_paths": hunk_paths,
        })
        return SimpleNamespace(
            to_dict=lambda include_hunks=False: {
                "project_id": project.project_id,
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
        )

    service = SimpleNamespace(get_project_diff_status=fake_get_project_diff_status)
    return service, status_calls


def _patch_get_diff_status_service(service):
    """patch handler 内部 import 的 get_diff_status_service。"""
    return patch(
        "jiuwenswarm.server.runtime.session.git_diff_status.get_diff_status_service",
        return_value=service,
    )


# ---------------------------------------------------------------------------
# summary 路径:_handle_diff_watch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_include_last_turn_false_passes_no_session_id():
    """include_last_turn=False 时不应传 session_id 给 get_project_diff_status。

    current-only 订阅场景:file_ops 历史读取异常不应阻断首次订阅。
    """
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_service, status_calls = _make_fake_status_factory()

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "session_id": "sess-1",
                "include_last_turn": False,
            },
        )

    assert len(status_calls) == 1
    # 修复后:include_last_turn=False 不传 session_id
    assert status_calls[0]["session_id"] is None
    resp = channel.responses[0]
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_summary_include_last_turn_true_passes_session_id():
    """include_last_turn=True 时正常传 session_id,语义不变。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_service, status_calls = _make_fake_status_factory()

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "session_id": "sess-1",
                "include_last_turn": True,
            },
        )

    assert len(status_calls) == 1
    assert status_calls[0]["session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# files 路径:_handle_diff_files_watch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_files_source_current_passes_no_session_id():
    """source="current" 时不应传 session_id 给 get_project_diff_status。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_service, status_calls = _make_fake_status_factory()

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
        await handler._handle_diff_files_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-1",
                "source": "current",
                "session_id": "sess-1",
            },
        )

    assert len(status_calls) == 1
    # 修复后:source="current" 不传 session_id
    assert status_calls[0]["session_id"] is None
    assert status_calls[0]["hunk_paths"] is None
    resp = channel.responses[0]
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_files_source_last_turn_passes_session_id():
    """source="last_turn" 时正常传 session_id,语义不变。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_service, status_calls = _make_fake_status_factory()

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
        await handler._handle_diff_files_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "watch_id": "wid-1",
                "source": "last_turn",
                "session_id": "sess-1",
            },
        )

    assert len(status_calls) == 1
    assert status_calls[0]["session_id"] == "sess-1"
    assert status_calls[0]["hunk_paths"] is None


# ---------------------------------------------------------------------------
# detail 路径:_handle_diff_detail_watch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_source_current_passes_no_session_id():
    """source="current" 时不应传 session_id 给 get_project_diff_status。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_service, status_calls = _make_fake_status_factory()

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
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

    assert len(status_calls) == 1
    # 修复后:source="current" 不传 session_id
    assert status_calls[0]["session_id"] is None
    assert status_calls[0]["hunk_paths"] == ["a.py"]
    resp = channel.responses[0]
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_detail_source_last_turn_passes_session_id():
    """source="last_turn" 时正常传 session_id,语义不变。"""
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)
    fake_service, status_calls = _make_fake_status_factory()

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
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

    assert len(status_calls) == 1
    assert status_calls[0]["session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# 关键回归:current-only 订阅在 file_ops 历史异常时仍能成功
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_current_only_subscription_survives_file_ops_failure():
    """current-only summary 订阅,file_ops 历史读取异常时不应失败。

    修复前:``get_project_diff_status(session_id="sess-1")`` 内部会调
    ``get_turn_diff_summaries`` 且异常向上抛,导致 add_watch 失败 →
    watcher 被回滚,前端拿不到 watch_id。

    修复后:include_last_turn=False 时不传 session_id,绕过 file_ops 读取,
    current-only 订阅不受影响。
    """
    channel = FakeWebChannel()
    registry = FakeRegistry()
    handler = _make_handler(channel, registry)

    def fake_get_project_diff_status(
        *, project, session_id, include_files, include_hunks, hunk_paths=None,
    ):
        if session_id is not None:
            # 模拟 file_ops 历史读取失败
            raise RuntimeError("broken file_ops / change_set")
        return SimpleNamespace(
            to_dict=lambda include_hunks=False: {
                "project_id": project.project_id,
                "session_id": None,
                "repo": {
                    "is_git": True,
                    "repo_root": "/tmp/proj",
                    "branch": "main",
                    "head": "abc123",
                    "transient": False,
                },
                "current": None,
                "last_turn": None,
            }
        )

    fake_service = SimpleNamespace(
        get_project_diff_status=fake_get_project_diff_status,
    )

    with (
        patch(
            "jiuwenswarm.gateway.channel_manager.web.git_ws_handler."
            "GitDiffWebSocketHandler._resolve_git_project",
            return_value=(_make_project(), None, None),
        ),
        _patch_get_diff_status_service(fake_service),
    ):
        await handler._handle_diff_watch(
            ws=None, req_id="r1",
            params={
                "project_id": "proj-A",
                "session_id": "sess-1",
                "include_last_turn": False,
            },
        )

    # 修复后:ok=True,前端拿到 watch_id
    resp = channel.responses[0]
    assert resp["ok"] is True
    assert resp["payload"]["watch_id"] == "wid-summary"
    # 响应里 last_turn=None(因 include_last_turn=False)
    assert resp["payload"]["snapshot"]["last_turn"] is None
