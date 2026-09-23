import json
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    build_status_update_response,
    should_send_as_status_update,
)
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


def _build_channel(*, enable_streaming: bool = True) -> tuple[XiaoyiChannel, list[dict[str, Any]]]:
    channel = XiaoyiChannel(
        XiaoyiChannelConfig(
            agent_id="agent-1",
            enable_streaming=enable_streaming,
        ),
        RobotMessageRouter(),
    )
    sent: list[dict[str, Any]] = []

    async def fake_safe_ws_send(url_key: str, payload: dict[str, Any]) -> None:
        assert url_key == "ws_url1"
        sent.append(payload)

    channel._ws_connections = {"ws_url1": object()}
    channel._safe_ws_send = fake_safe_ws_send
    return channel, sent


def _message(event_type: EventType, payload: dict[str, Any]) -> Message:
    return Message(
        id="request-1",
        type="event",
        channel_id="xiaoyi",
        session_id="jiuwen-session-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload=payload,
        event_type=event_type,
        metadata={
            "xiaoyi_session_id": "xiaoyi-session-1",
            "xiaoyi_task_id": "xiaoyi-task-1",
        },
    )


def _result(wrapper: dict[str, Any]) -> dict[str, Any]:
    return json.loads(wrapper["msgDetail"])["result"]


def test_processing_status_is_a_status_update() -> None:
    assert should_send_as_status_update(EventType.CHAT_PROCESSING_STATUS)
    assert build_status_update_response("task-1", "working", "working")["final"] is False
    assert build_status_update_response("task-1", "done", "completed")["final"] is True
    assert build_status_update_response("task-1", "failed", "failed")["final"] is True
    assert build_status_update_response("task-1", "cancelled", "canceled")["final"] is True


@pytest.mark.asyncio
async def test_streaming_final_and_gateway_notice_use_expected_terminal_frames() -> None:
    channel, sent = _build_channel()
    summary = "抖音已经帮你打开了"
    channel._mark_session_active("xiaoyi-session-1")

    await channel.send(
        _message(
            EventType.CHAT_FINAL,
            {"event_type": "chat.final", "content": summary},
        )
    )
    await channel.send(
        _message(
            EventType.CHAT_PROCESSING_STATUS,
            {
                "event_type": "chat.processing_status",
                "is_processing": False,
                "is_complete": True,
            },
        )
    )

    assert len(sent) == 2
    artifact = _result(sent[0])
    assert artifact["kind"] == "artifact-update"
    assert artifact["append"] is False
    assert artifact["lastChunk"] is True
    assert artifact["final"] is False
    assert artifact["artifact"]["parts"] == [{"kind": "text", "text": summary}]

    status = _result(sent[1])
    assert status["taskId"] == artifact["taskId"]
    assert status["kind"] == "status-update"
    assert status["status"]["state"] == "completed"
    assert status["status"]["message"]["parts"] == [
        {"kind": "text", "text": "任务处理已完成~"}
    ]
    assert status["final"] is True

    notices: list[Message] = []
    handler = object.__new__(MessageHandler)

    async def capture_notice(msg: Message) -> None:
        notices.append(msg)

    handler.publish_robot_messages = capture_notice
    await handler._send_channel_notice(
        {
            "id": "request-1",
            "meta_data": {
                "xiaoyi_session_id": "xiaoyi-session-1",
                "xiaoyi_task_id": "xiaoyi-task-2",
            },
        },
        "xiaoyi",
        "jiuwen-session-1",
        "mode 已变更为 team",
        mode="team",
        reset_team_session=True,
    )

    assert notices[0].metadata["terminal_notice"] is True
    sent.clear()
    await channel.send(notices[0])

    assert len(sent) == 1
    notice = _result(sent[0])
    assert notice["kind"] == "artifact-update"
    assert notice["append"] is False
    assert notice["lastChunk"] is True
    assert notice["final"] is True


@pytest.mark.asyncio
async def test_processing_started_is_non_terminal_status_update() -> None:
    channel, sent = _build_channel()

    await channel.send(
        _message(
            EventType.CHAT_PROCESSING_STATUS,
            {
                "event_type": "chat.processing_status",
                "is_processing": True,
                "is_complete": False,
            },
        )
    )

    assert len(sent) == 1
    status = _result(sent[0])
    assert status["kind"] == "status-update"
    assert status["status"]["state"] == "working"
    assert status["final"] is False


@pytest.mark.asyncio
async def test_completed_status_is_terminal_without_final_text() -> None:
    channel, sent = _build_channel()
    channel._mark_session_active("xiaoyi-session-1")

    await channel.send(
        _message(
            EventType.CHAT_PROCESSING_STATUS,
            {
                "event_type": "chat.processing_status",
                "is_processing": False,
                "is_complete": True,
            },
        )
    )

    assert len(sent) == 1
    status = _result(sent[0])
    assert status["kind"] == "status-update"
    assert status["status"]["state"] == "completed"
    assert status["final"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        EventType.CHAT_USAGE_METADATA,
        EventType.CHAT_USAGE_SUMMARY,
        EventType.CONTEXT_USAGE,
    ],
)
async def test_internal_events_do_not_create_blank_artifacts(
    event_type: EventType,
) -> None:
    channel, sent = _build_channel()

    await channel.send(
        _message(
            event_type,
            {"event_type": event_type.value, "usage": {"total_tokens": 10}},
        )
    )

    assert sent == []


@pytest.mark.asyncio
async def test_non_streaming_final_text_remains_terminal_artifact() -> None:
    channel, sent = _build_channel(enable_streaming=False)

    await channel.send(
        _message(
            EventType.CHAT_FINAL,
            {"event_type": "chat.final", "content": "已完成"},
        )
    )

    assert len(sent) == 1
    artifact = _result(sent[0])
    assert artifact["kind"] == "artifact-update"
    assert artifact["append"] is False
    assert artifact["lastChunk"] is True
    assert artifact["final"] is True


@pytest.mark.asyncio
async def test_direct_gui_response_is_not_forwarded_as_user_message() -> None:
    channel, _ = _build_channel()
    gui_events: list[dict[str, Any]] = []
    user_messages: list[Message] = []
    channel.register_gui_agent_handler(gui_events.append)
    channel.on_message(user_messages.append)
    raw = {
        "jsonrpc": "2.0",
        "id": "xiaoyi-task-1",
        "method": "message/stream",
        "sessionId": "xiaoyi-session-1",
        "params": {
            "id": "xiaoyi-task-1",
            "sessionId": "params-session-1",
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "xiaoyi-task-1",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "events": [
                                {
                                    "header": {
                                        "namespace": "ClawAgent",
                                        "name": "InvokeJarvisGUIAgentResponse",
                                    },
                                    "payload": {
                                        "isFinal": True,
                                        "streamInfo": {
                                            "streamContent": "layout analysis failed"
                                        },
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        },
    }

    await channel._handle_raw_message(json.dumps(raw))

    assert len(gui_events) == 1
    assert user_messages == []


def _artifact_message(
    event_type: EventType,
    payload: dict[str, Any],
    *,
    message_id: str = "request-1",
    sticky_task: str = "xiaoyi-task-1",
) -> Message:
    return Message(
        id=message_id,
        type="event",
        channel_id="xiaoyi",
        session_id="jiuwen-session-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload=payload,
        event_type=event_type,
        metadata={
            "xiaoyi_session_id": "xiaoyi-session-1",
            "xiaoyi_task_id": sticky_task,
        },
    )


def test_artifact_delivery_task_id_keeps_active_sticky() -> None:
    channel, _ = _build_channel()
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    msg = _artifact_message(EventType.CHAT_FILE, {"event_type": "chat.file", "files": []})
    session_id, task_id = channel._extract_platform_receive_info(msg)
    assert session_id == "xiaoyi-session-1"
    assert channel._artifact_delivery_task_id(session_id, task_id, msg) == "xiaoyi-task-1"


def test_artifact_delivery_task_id_keeps_task_id_when_sticky_completed() -> None:
    """Sticky 已收尾（final 已发、active 已清）的同轮产物仍沿用原 taskId。

    切换 taskId 会被端侧判为新流（streamType=start）并重置渲染，把已
    流式输出的正文截断——文件上传/派发晚于 chat.final 时必然触发。
    """
    channel, _ = _build_channel()
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    channel._mark_session_completed("xiaoyi-session-1", "xiaoyi-task-1")
    msg = _artifact_message(
        EventType.CHAT_FILE,
        {"event_type": "chat.file", "files": []},
        message_id="pc-turn-uuid",
    )
    session_id, task_id = channel._extract_platform_receive_info(msg)
    assert session_id == "xiaoyi-session-1"
    assert task_id == "xiaoyi-task-1"
    assert channel._artifact_delivery_task_id(session_id, task_id, msg) == "xiaoyi-task-1"


def test_artifact_delivery_task_id_falls_back_for_stale_cached_task() -> None:
    """消息携带的 xiaoyi_task_id 与会话最新登记任务不一致（工具单例 stale
    缓存，产物挂到旧回复的场景）→ 仍回退本轮 msg.id（39d752995 语义）。"""
    channel, _ = _build_channel()
    channel._remember_active_platform_task("xiaoyi-session-1", "xiaoyi-task-2")
    msg = Message(
        id="pc-turn-uuid",
        type="event",
        channel_id="xiaoyi",
        session_id="jiuwen-session-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "chat.file", "files": []},
        event_type=EventType.CHAT_FILE,
        metadata={
            "xiaoyi_session_id": "xiaoyi-session-1",
            "xiaoyi_task_id": "xiaoyi-task-1",
        },
    )
    session_id, task_id = channel._extract_platform_receive_info(msg)
    assert task_id == "xiaoyi-task-1"
    assert channel._artifact_delivery_task_id(session_id, task_id, msg) == "pc-turn-uuid"


@pytest.mark.asyncio
async def test_file_on_completed_sticky_keeps_task_id() -> None:
    channel, _ = _build_channel()
    captured: list[tuple[str, str]] = []

    async def fake_send_file(session_id, task_id, file_info, url_key):
        captured.append((session_id, task_id))

    channel._send_file_response = fake_send_file
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    channel._mark_session_completed("xiaoyi-session-1", "xiaoyi-task-1")
    await channel.send(
        _artifact_message(
            EventType.CHAT_FILE,
            {
                "event_type": "chat.file",
                "files": [{"path": "/tmp/a.jpg", "name": "a.jpg"}],
            },
            message_id="pc-turn-uuid",
        )
    )
    assert captured == [("xiaoyi-session-1", "xiaoyi-task-1")]


@pytest.mark.asyncio
async def test_file_on_active_sticky_keeps_task_id() -> None:
    channel, _ = _build_channel()
    captured: list[tuple[str, str]] = []

    async def fake_send_file(session_id, task_id, file_info, url_key):
        captured.append((session_id, task_id))

    channel._send_file_response = fake_send_file
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    await channel.send(
        _artifact_message(
            EventType.CHAT_FILE,
            {
                "event_type": "chat.file",
                "files": [{"path": "/tmp/a.jpg", "name": "a.jpg"}],
            },
            message_id="pc-turn-uuid",
        )
    )
    assert captured == [("xiaoyi-session-1", "xiaoyi-task-1")]


@pytest.mark.asyncio
async def test_html_card_on_completed_sticky_keeps_task_id() -> None:
    channel, _ = _build_channel()
    captured: list[tuple[str, str, str]] = []

    async def fake_send_html(session_id, task_id, message_id, cards_info, url_key):
        captured.append((session_id, task_id, message_id))

    channel._send_html_card_response = fake_send_html
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    channel._mark_session_completed("xiaoyi-session-1", "xiaoyi-task-1")
    await channel.send(
        _artifact_message(
            EventType.CHAT_HTML_CARD,
            {"event_type": "chat.html_card", "url": "https://example.com/card.html"},
            message_id="pc-turn-uuid",
        )
    )
    assert captured == [("xiaoyi-session-1", "xiaoyi-task-1", "pc-turn-uuid")]
