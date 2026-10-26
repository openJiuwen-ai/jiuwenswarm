# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""sync_chat_request_metadata 的 attach-only 订阅守护测试.

attach-only ``chat.send`` 是被动事件订阅（Web 端进入 Team 会话时挂接
persistent waiter），不是真实用户输入：它不得刷新会话 metadata 的
``last_message_at``（侧边栏排序时间）与 ``last_user_message_at``，
否则仅仅打开或刷新页面就会把旧会话置顶。
"""

from __future__ import annotations

import time

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.request import sync_chat_request_metadata


@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: d,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        _METADATA_CACHE,
        _METADATA_QUEUE,
        _SESSION_REBIND_GEN,
    )

    _METADATA_QUEUE.join()
    _METADATA_CACHE.clear()
    _SESSION_REBIND_GEN.clear()
    return d


def _init_session(session_id: str, *, project_dir: str = "") -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata,
    )

    init_session_metadata(
        session_id=session_id,
        channel_id="web",
        user_id="u1",
        title="team session",
        mode="team.work.normal",
        project_dir=project_dir,
    )


def _read_metadata(session_id: str) -> dict:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
    )

    return get_session_metadata(session_id, cache_bust=True, enable_writeback=False)


def _drain_writes() -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_QUEUE

    _METADATA_QUEUE.join()


def _chat_request(request_id: str, session_id: str, params: dict) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=params,
        is_stream=True,
    )


def test_attach_only_subscription_does_not_touch_session_metadata(sessions_dir) -> None:
    _init_session("s-attach")
    _drain_writes()
    before = _read_metadata("s-attach")
    time.sleep(0.05)

    request = _chat_request(
        "r-attach",
        "s-attach",
        {"attach_only": True, "session_id": "s-attach", "mode": "team.work.normal"},
    )
    result = sync_chat_request_metadata(request, None, "team.work.normal")

    _drain_writes()
    after = _read_metadata("s-attach")
    assert result is None  # no locked project_dir: the caller candidate passes through
    assert after["last_message_at"] == before["last_message_at"]
    assert after["last_user_message_at"] == before["last_user_message_at"]


def test_attach_only_subscription_returns_locked_project_dir(sessions_dir) -> None:
    _init_session("s-lock", project_dir="/proj/locked")
    _drain_writes()

    # Web attach sends no project_dir candidate at all.
    request = _chat_request(
        "r-lock-1",
        "s-lock",
        {"attach_only": True, "session_id": "s-lock", "mode": "team.work.normal"},
    )
    assert sync_chat_request_metadata(request, None, "team.work.normal") == "/proj/locked"

    # A client-supplied candidate must not bypass the session lock.
    request = _chat_request(
        "r-lock-2",
        "s-lock",
        {
            "attach_only": True,
            "session_id": "s-lock",
            "mode": "team.work.normal",
            "project_dir": "/proj/client",
        },
    )
    assert (
        sync_chat_request_metadata(request, "/proj/client", "team.work.normal")
        == "/proj/locked"
    )


def test_regular_chat_send_refreshes_activity_timestamps(sessions_dir) -> None:
    _init_session("s-chat")
    _drain_writes()
    before = _read_metadata("s-chat")
    time.sleep(0.05)

    request = _chat_request(
        "r-chat",
        "s-chat",
        {"session_id": "s-chat", "mode": "team.work.normal"},
    )
    sync_chat_request_metadata(request, None, "team.work.normal")

    _drain_writes()
    after = _read_metadata("s-chat")
    assert after["last_message_at"] > before["last_message_at"]
    assert after["last_user_message_at"] > before["last_user_message_at"]
