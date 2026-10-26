# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IM streaming adapters must not emit external updates for formatting-only deltas."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.dingtalk_connect import (
    DingTalkChannel,
    DingTalkConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.wecom.wecom_connect import (
    WecomChannel,
    WecomConfig,
)


def _stream_message(
    channel_id: str,
    content: str,
    *,
    metadata: dict[str, Any],
    event_type: EventType = EventType.CHAT_DELTA,
) -> Message:
    return Message(
        id="request-1",
        type="event",
        channel_id=channel_id,
        session_id="session-1",
        params={},
        payload={"event_type": event_type.value, "content": content},
        metadata=metadata,
        timestamp=0,
        ok=True,
        event_type=event_type,
    )


@pytest.mark.asyncio
async def test_dingtalk_skips_whitespace_only_stream_delta() -> None:
    channel = DingTalkChannel(
        DingTalkConfig(enabled=True, client_id="client", client_secret="secret"),
        RobotMessageRouter(),
    )
    channel._get_access_token = AsyncMock(return_value="token")
    channel._send_http_request = AsyncMock()
    msg = _stream_message(
        "dingtalk",
        " \n",
        metadata={
            "dingtalk_sender_id": "user-1",
            "conversation_type": "1",
        },
    )

    await channel.send(msg)

    channel._get_access_token.assert_not_awaited()
    channel._send_http_request.assert_not_awaited()


class _FakeWecomClient:
    is_connected = True

    def __init__(self) -> None:
        self.reply_calls: list[tuple[Any, str, str, bool]] = []

    async def reply_stream(
        self,
        frame: Any,
        stream_id: str,
        content: str,
        *,
        finish: bool,
    ) -> None:
        self.reply_calls.append((frame, stream_id, content, finish))


@pytest.mark.asyncio
async def test_wecom_does_not_resend_unchanged_content_for_whitespace_delta() -> None:
    channel = WecomChannel(
        WecomConfig(enabled=True, enable_streaming=True),
        RobotMessageRouter(),
    )
    client = _FakeWecomClient()
    channel._ws_client = client
    channel._pending_streams["request-1"] = {
        "frame": {"headers": {}},
        "stream_id": "stream-1",
        "accumulated": "",
    }
    metadata = {"wecom_req_id": "request-1"}

    await channel.send(_stream_message("wecom", "Hello", metadata=metadata))
    await channel.send(_stream_message("wecom", " ", metadata=metadata))

    assert client.reply_calls == [
        ({"headers": {}}, "stream-1", "Hello", False),
    ]


@pytest.mark.asyncio
async def test_wecom_final_content_replaces_stream_accumulation_without_duplication() -> None:
    channel = WecomChannel(
        WecomConfig(enabled=True, enable_streaming=True),
        RobotMessageRouter(),
    )
    client = _FakeWecomClient()
    channel._ws_client = client
    channel._pending_streams["request-1"] = {
        "frame": {"headers": {}},
        "stream_id": "stream-1",
        "accumulated": "",
    }
    metadata = {"wecom_req_id": "request-1"}

    await channel.send(_stream_message("wecom", "Hello", metadata=metadata))
    await channel.send(
        _stream_message(
            "wecom",
            "Hello",
            metadata=metadata,
            event_type=EventType.CHAT_FINAL,
        )
    )

    assert client.reply_calls == [
        ({"headers": {}}, "stream-1", "Hello", False),
        ({"headers": {}}, "stream-1", "Hello", True),
    ]


@pytest.mark.asyncio
async def test_wecom_whitespace_final_still_finishes_visible_stream() -> None:
    channel = WecomChannel(
        WecomConfig(enabled=True, enable_streaming=True),
        RobotMessageRouter(),
    )
    client = _FakeWecomClient()
    channel._ws_client = client
    channel._pending_streams["request-1"] = {
        "frame": {"headers": {}},
        "stream_id": "stream-1",
        "accumulated": "",
    }
    metadata = {"wecom_req_id": "request-1"}

    await channel.send(_stream_message("wecom", "Hello", metadata=metadata))
    await channel.send(
        _stream_message(
            "wecom",
            " ",
            metadata=metadata,
            event_type=EventType.CHAT_FINAL,
        )
    )

    assert client.reply_calls == [
        ({"headers": {}}, "stream-1", "Hello", False),
        ({"headers": {}}, "stream-1", "Hello", True),
    ]
