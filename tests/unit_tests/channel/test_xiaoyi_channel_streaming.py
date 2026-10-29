import json
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.common.xiaoyi_reference import build_a2a_reference_items
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
async def test_reference_response_uses_huawei_reference_data_object() -> None:
    channel, sent = _build_channel()
    items = build_a2a_reference_items(
        [
            {
                "title": "标题",
                "url": "https://example.com/a",
                "source": "web_search",
                "name": "示例站",
            }
        ]
    )

    await channel._send_reference_response(
        "xiaoyi-session-1",
        "xiaoyi-task-1",
        "request-1",
        items,
        "ws_url1",
    )

    assert sent[0]["msgType"] == "agent_response"
    assert sent[0]["sessionId"] == "xiaoyi-session-1"
    assert sent[0]["taskId"] == "xiaoyi-task-1"
    result = _result(sent[0])
    assert result["taskId"] == "xiaoyi-task-1"
    assert result["kind"] == "artifact-update"
    assert result["append"] is False
    assert result["lastChunk"] is True
    assert result["final"] is False
    assert result["artifact"]["parts"] == [
        {
            "kind": "data",
            "data": {"reference": {"items": items}},
        }
    ]


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
    # 终帧按「扣住末片、段末补换行」语义作为增量收尾帧下发：append=True、
    # 末片带 \n、lastChunk=True（语义约定见 test_xiaoyi_text_segment_newline.py）
    assert artifact["append"] is True
    assert artifact["lastChunk"] is True
    assert artifact["final"] is False
    assert artifact["artifact"]["parts"] == [{"kind": "text", "text": f"{summary}\n"}]

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
    await handler.send_channel_notice(
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
    # 通知与正文终帧统一走「扣住末片」拆帧路径，append=True（上游 630374805 起的行为）
    assert notice["append"] is True
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


def test_artifact_delivery_task_id_falls_back_when_sticky_completed() -> None:
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
    assert channel._artifact_delivery_task_id(session_id, task_id, msg) == "pc-turn-uuid"


@pytest.mark.asyncio
async def test_file_on_completed_sticky_uses_message_id() -> None:
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
    assert captured == [("xiaoyi-session-1", "pc-turn-uuid")]


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
async def test_html_card_on_completed_sticky_uses_message_id() -> None:
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
    assert captured == [("xiaoyi-session-1", "pc-turn-uuid", "pc-turn-uuid")]


def _text_frames(sent: list[dict[str, Any]]) -> list[tuple[str, str]]:
    frames: list[tuple[str, str]] = []
    for wrapper in sent:
        if wrapper.get("msgType") != "agent_response":
            continue
        result = _result(wrapper)
        for part in result.get("artifact", {}).get("parts") or []:
            if part.get("kind") == "text":
                frames.append((str(result.get("taskId") or ""), str(part.get("text") or "")))
    return frames


def _track_text_then_artifact(channel: XiaoyiChannel, order: list[str]) -> None:
    inner = channel._safe_ws_send

    async def tracking_send(url_key: str, payload: dict[str, Any]) -> None:
        await inner(url_key, payload)
        result = _result(payload)
        if result.get("kind") != "artifact-update":
            return
        parts = result.get("artifact", {}).get("parts") or []
        if parts and parts[0].get("kind") == "text":
            order.append("text")

    channel._safe_ws_send = tracking_send


def _seed_held_text(channel: XiaoyiChannel, *, completed: bool = False) -> None:
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    if completed:
        channel._mark_session_completed("xiaoyi-session-1", "xiaoyi-task-1")
    channel._text_stream_pending[("xiaoyi-session-1", "xiaoyi-task-1")] = ("用户。", "text")


async def _send_tool_phase(channel: XiaoyiChannel) -> None:
    await channel.send(
        _artifact_message(
            EventType.CHAT_TOOL_CALL,
            {
                "event_type": "chat.tool_call",
                "tool_call": {"name": "send_file_to_user"},
            },
        )
    )


@pytest.mark.asyncio
async def test_file_flushes_held_text_before_artifact() -> None:
    channel, sent = _build_channel()
    order: list[str] = []
    _track_text_then_artifact(channel, order)

    async def fake_send_file(session_id, task_id, file_info, url_key):
        order.append("file")

    channel._send_file_response = fake_send_file
    _seed_held_text(channel)
    await channel.send(
        _artifact_message(
            EventType.CHAT_FILE,
            {
                "event_type": "chat.file",
                "files": [{"path": "/tmp/a.jpg", "name": "a.jpg"}],
            },
        )
    )
    assert order == ["text", "file"]
    assert _text_frames(sent) == [("xiaoyi-task-1", "用户。\n")]
    await _send_tool_phase(channel)
    assert _text_frames(sent) == [("xiaoyi-task-1", "用户。\n")]


def test_extract_uses_message_id_when_task_missing() -> None:
    channel, _ = _build_channel()
    msg = _artifact_message(
        EventType.CHAT_FILE,
        {"event_type": "chat.file", "files": []},
        message_id="pc-turn-uuid",
    )
    msg.metadata = {"xiaoyi_session_id": "xiaoyi-session-1"}
    session_id, task_id = channel._extract_platform_receive_info(msg)
    assert session_id == "xiaoyi-session-1"
    assert task_id == "pc-turn-uuid"


@pytest.mark.asyncio
async def test_file_flush_keeps_sticky_task_id_when_completed() -> None:
    """sticky 已收尾时，扣住的正文与文件走同一个 delivery id。"""
    channel, sent = _build_channel()
    captured: list[str] = []

    async def fake_send_file(session_id, task_id, file_info, url_key):
        captured.append(task_id)

    channel._send_file_response = fake_send_file
    _seed_held_text(channel, completed=True)
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
    assert captured == ["pc-turn-uuid"]
    assert _text_frames(sent) == [("pc-turn-uuid", "用户。\n")]
    assert _result(sent[0])["lastChunk"] is False


@pytest.mark.asyncio
async def test_file_flush_pending_on_message_id_before_file() -> None:
    """sticky 已结束、pending 在 msg.id 上时，尾巴早于文件且 taskId 相同。"""
    channel, sent = _build_channel()
    order: list[str] = []
    _track_text_then_artifact(channel, order)

    async def fake_send_file(session_id, task_id, file_info, url_key):
        order.append("file")

    channel._send_file_response = fake_send_file
    channel._mark_session_active("xiaoyi-session-1", "xiaoyi-task-1")
    channel._mark_session_completed("xiaoyi-session-1", "xiaoyi-task-1")
    channel._text_stream_pending[("xiaoyi-session-1", "pc-turn-uuid")] = ("给你。", "text")
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
    assert order == ["text", "file"]
    assert _text_frames(sent) == [("pc-turn-uuid", "给你。\n")]
    assert _result(sent[0])["lastChunk"] is False


@pytest.mark.asyncio
async def test_reference_flushes_held_text_before_artifact() -> None:
    channel, sent = _build_channel()
    order: list[str] = []
    _track_text_then_artifact(channel, order)

    async def fake_send_reference(session_id, task_id, message_id, items, url_key):
        order.append("reference")

    channel._send_reference_response = fake_send_reference
    _seed_held_text(channel)
    await channel.send(
        _artifact_message(
            EventType.CHAT_REFERENCE,
            {
                "event_type": "chat.reference",
                "references": [
                    {
                        "title": "今日新闻",
                        "url": "https://example.com/news",
                        "source": "web",
                        "name": "news",
                    }
                ],
            },
        )
    )
    assert order == ["text", "reference"]
    assert _text_frames(sent) == [("xiaoyi-task-1", "用户。\n")]
    await _send_tool_phase(channel)
    assert _text_frames(sent) == [("xiaoyi-task-1", "用户。\n")]


@pytest.mark.asyncio
async def test_html_card_flushes_held_text_before_artifact() -> None:
    channel, sent = _build_channel()
    order: list[str] = []
    _track_text_then_artifact(channel, order)

    async def fake_send_html(session_id, task_id, message_id, cards_info, url_key):
        order.append("html_card")

    channel._send_html_card_response = fake_send_html
    _seed_held_text(channel)
    await channel.send(
        _artifact_message(
            EventType.CHAT_HTML_CARD,
            {"event_type": "chat.html_card", "url": "https://example.com/card.html"},
        )
    )
    assert order == ["text", "html_card"]
    assert _text_frames(sent) == [("xiaoyi-task-1", "用户。\n")]
    await _send_tool_phase(channel)
    assert _text_frames(sent) == [("xiaoyi-task-1", "用户。\n")]

