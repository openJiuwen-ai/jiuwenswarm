# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""sync_chat_request_metadata 的 attach-only 订阅守护测试.

attach-only ``chat.send`` 是被动事件订阅（Web 端进入 Team 会话时挂接
persistent waiter），不是真实用户输入：它不得刷新会话 metadata 的
``last_message_at``（侧边栏排序时间）与 ``last_user_message_at``，
否则仅仅打开或刷新页面就会把旧会话置顶。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.request import sync_chat_request_metadata


@pytest.mark.parametrize("project_id, expected_dir", [("default_code", None), ("proj_bound", "/bound")])
@pytest.mark.parametrize("sync_metadata", [False, True])
async def test_prepare_chat_turn_discards_conflicting_project_context(
    monkeypatch, project_id, expected_dir, sync_metadata,
):
    from jiuwenswarm.runtime.request import prepare_chat_turn
    from jiuwenswarm.server.runtime.session import project_store

    binding = {
        "project_id": project_id,
        "project_dir": "/stale",
        "work_mode": "code",
        "mode": "agent.code.normal",
    }
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: binding.copy(),
    )
    project = project_store.Project(project_id="proj_bound", name="Bound", project_dir="/bound")
    monkeypatch.setattr(
        project_store, "get_project_by_id", lambda pid, cache_bust=False: project if pid == "proj_bound" else None,
    )
    request = _chat_request("conflict", "bound", {"project_id": "proj_other", "project_dir": "/unrelated"})
    request.metadata = {"project_id": "proj_other", "project_dir": "/unrelated"}
    agent = object()

    async def get_agent_for_request(request, **kwargs):
        assert kwargs["admit_request"]() == expected_dir
        return agent

    manager = SimpleNamespace(wait_for_session_prewarm=AsyncMock(), get_agent_for_request=get_agent_for_request)
    result = await prepare_chat_turn(
        manager, request, "web", sync_metadata=sync_metadata,
        metadata_sync=lambda *args, **kwargs: "/unrelated",
    )
    assert result[2] is agent
    assert request.params["project_id"] == project_id
    assert request.metadata["project_id"] == project_id
    assert request.params.get("project_dir") == expected_dir
    assert request.metadata.get("project_dir") == expected_dir


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


async def test_prepare_chat_turn_keeps_legacy_agentos_workspace(sessions_dir, monkeypatch):
    from jiuwenswarm.runtime.request import prepare_chat_turn
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    session_metadata.init_session_metadata(
        session_id="cloud", channel_id="web", project_id="default_code",
        work_mode="code", mode="agent.code.normal",
    )
    path = sessions_dir / "cloud" / "metadata.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["project_dir"] = "/home/agentos/workspace"
    path.write_text(json.dumps(stored), encoding="utf-8")
    request = _chat_request("legacy-cloud", "cloud", {"project_id": "default_code"})
    agent = object()

    async def get_agent_for_request(request, **kwargs):
        assert kwargs["admit_request"]() == "/home/agentos/workspace"
        return agent

    manager = SimpleNamespace(wait_for_session_prewarm=AsyncMock(), get_agent_for_request=get_agent_for_request)
    result = await prepare_chat_turn(manager, request, "web")
    assert result[2] is agent
    assert request.params["project_dir"] == "/home/agentos/workspace"
    assert request.metadata["project_dir"] == "/home/agentos/workspace"
    session_metadata._METADATA_QUEUE.join()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["project_id"] == "default_code"
    assert persisted["project_dir"] == ""


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


def _voice_request(session_id: str, mode: str = "agent.code.normal") -> AgentRequest:
    return AgentRequest(
        request_id="task-1",
        channel_id="video_tool",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode},
        is_stream=True,
    )


def test_voice_task_in_a_saved_chat_runs_on_the_chat_channel(sessions_dir):
    """#5487: a second video_tool agent kept its own copy of the chat's session,
    so a voice turn resumed approval cards the chat had already answered."""
    from jiuwenswarm.runtime.request import resolve_request_owner_channel

    _init_session("chat-1")
    _drain_writes()

    assert resolve_request_owner_channel(_voice_request("chat-1")) == "web"


def test_voice_task_without_a_saved_chat_keeps_its_own_channel(sessions_dir):
    from jiuwenswarm.runtime.request import resolve_request_owner_channel

    assert resolve_request_owner_channel(_voice_request("managed-task-1")) == "video_tool"


def test_design_voice_task_and_other_channels_keep_their_routing(sessions_dir):
    from jiuwenswarm.runtime.request import resolve_request_owner_channel

    _init_session("chat-2")
    _drain_writes()

    # Design tasks are bound by the Team stream on their own channel.
    assert resolve_request_owner_channel(_voice_request("chat-2", "team.design")) == "video_tool"
    assert resolve_request_owner_channel(_chat_request("r2", "chat-2", {"mode": "agent"})) == "web"
