# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""飞书通道只投递用户可见结果，不把遥测事件的内容当成消息发出去。

``chat.llm_call_end`` 携带本次模型调用的完整回复正文（给前端「思考中」指示用）。
它不属于 EventType 枚举，事件名只存在于 payload；若走通用内容兜底，会先于
``chat.final`` 发一条，用户在飞书里看到同一段文字重复两次。
"""

import time
from unittest.mock import AsyncMock

import pytest
from e2a.message import Message

from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
)

REPLY_TEXT = "已批准，先读取已有文件再写入："


def _event(event_type: str, content: str = "") -> Message:
    """构造事件名只存在于 payload 的流事件（与 llm_call_end 的真实形态一致）。"""
    return Message(
        id="req-1",
        type="event",
        channel_id="feishu",
        session_id="feishu_oc_chat",
        params={},
        payload={"event_type": event_type, "content": content},
        timestamp=time.time(),
        ok=True,
        event_type=None,
        metadata={"feishu_chat_id": "oc_chat"},
    )


def _channel(*, enable_streaming: bool) -> FeishuChannel:
    channel = FeishuChannel(
        FeishuConfig(
            channel_id="feishu",
            app_id="app",
            app_secret="secret",
            enable_streaming=enable_streaming,
        ),
        RobotMessageRouter(),
    )
    channel._api_client = object()
    channel._create_and_send_message = AsyncMock()
    return channel


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_streaming", [True, False])
async def test_llm_call_end_content_is_not_delivered(enable_streaming):
    channel = _channel(enable_streaming=enable_streaming)

    await channel.send(_event("chat.llm_call_end", REPLY_TEXT))

    channel._create_and_send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_call_end_does_not_duplicate_the_final_text():
    channel = _channel(enable_streaming=False)

    await channel.send(_event("chat.llm_call_end", REPLY_TEXT))
    await channel.send(_event("chat.final", REPLY_TEXT))

    assert channel._create_and_send_message.await_count == 1
    sent = channel._create_and_send_message.await_args.args[0]
    assert REPLY_TEXT in sent.content
