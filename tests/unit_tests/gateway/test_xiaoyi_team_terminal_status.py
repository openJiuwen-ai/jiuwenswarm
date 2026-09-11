"""回归测试：xiaoyi 团队会话的终态帧投递。

修复前：团队会话的 chat.processing_status(is_processing=False) 一律 DEFERRED
（终态收尾交给 team.completed），但 leader 死亡探针/停摆看门狗补发的失败终态
（processing_status 带 error）也被一并拦下——team.completed 不会来，手机端
任务永远停在「正在处理中」。

修复后：无 error 的收尾帧照常 DEFERRED；带 error 的终态帧放行为 failed
status-update 并 finalize 会话任务。
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)


def _config() -> XiaoyiChannelConfig:
    return XiaoyiChannelConfig(
        enabled=True,
        mode="xiaoyi_claw",
        ak="dummy-ak",
        sk="dummy-sk",
        agent_id="dummy-agent",
        ws_url1="wss://dummy/xiaoyi1",
        ws_url2="wss://dummy/xiaoyi2",
        enable_streaming=True,
        uid="dummy-uid",
        api_key="dummy-api-key",
        api_id="dummy-api-id",
        push_id="dummy-push-id",
        push_url="https://dummy/push",
        file_upload_url="https://dummy/upload",
        task_timeout_ms=3600000,
    )


def _make_channel():
    channel = XiaoyiChannel(config=_config(), router=None)
    channel._ws_connections["ws1"] = object()
    channel._running = True

    sent_frames: list[dict] = []

    async def _capture(session_id, task_id, response, url_key):
        sent_frames.append({"session_id": session_id, "task_id": task_id, "response": response})

    channel._send_agent_response = _capture  # type: ignore[assignment]
    return channel, sent_frames


def _team_session_setup(channel: XiaoyiChannel, session_id: str, task_id: str) -> None:
    """模拟已注册的团队会话 + 活跃中的平台任务。"""
    channel._team_sessions.add(session_id)
    channel._team_tasks.add((session_id, task_id))
    channel._active_tasks.add((session_id, task_id))
    channel._session_active.add(session_id)


def _processing_status_msg(session_id: str, task_id: str, error: str = "") -> Message:
    payload = {
        "event_type": "chat.processing_status",
        "session_id": session_id,
        "is_processing": False,
        "is_complete": True,
    }
    if error:
        payload["error"] = error
    return Message(
        id="req-term-1",
        type="event",
        channel_id="xiaoyi",
        session_id=session_id,
        params={},
        timestamp=1.0,
        ok=True,
        payload=payload,
        event_type=EventType.CHAT_PROCESSING_STATUS,
        metadata={"xiaoyi_session_id": session_id, "xiaoyi_task_id": task_id},
    )


@pytest.mark.asyncio
async def test_team_terminal_without_error_stays_deferred() -> None:
    """无 error 的回合收尾帧照常 DEFERRED（终态由 team.completed 承担）。"""
    channel, sent = _make_channel()
    _team_session_setup(channel, "desktop_s1", "task-1")

    await channel.send(_processing_status_msg("desktop_s1", "task-1"))

    assert sent == []


@pytest.mark.asyncio
async def test_team_terminal_with_error_is_forwarded_as_failed() -> None:
    """带 error 的终态帧（探针/看门狗补发的回合失败）放行：failed + finalize。"""
    channel, sent = _make_channel()
    _team_session_setup(channel, "desktop_s1", "task-2")

    await channel.send(_processing_status_msg("desktop_s1", "task-2", error="团队任务停摆：2 个任务已下发但无成员执行"))

    assert len(sent) == 1
    frame = json.dumps(sent[0]["response"], ensure_ascii=False)
    assert "failed" in frame
    assert "团队任务停摆" in frame
