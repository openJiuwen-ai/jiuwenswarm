# pylint: disable=protected-access
"""A Feishu ask_user card answer names the interrupt it answers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen_protocol.message import EventType, Message

from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel, FeishuConfig


def _question(payload: dict) -> Message:
    return Message(
        id="req_001",
        type="event",
        channel_id="feishu",
        session_id="sess_001",
        params={},
        timestamp=1000.0,
        ok=True,
        payload={"event_type": "chat.ask_user_question", **payload},
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata={"feishu_chat_id": "oc_test123"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_id", ["intr-1", ""])
async def test_card_answer_echoes_the_interrupt_id_of_the_question(interrupt_id):
    channel = FeishuChannel(FeishuConfig(enabled=True, app_id="app", app_secret="secret"), MagicMock(spec=RobotMessageRouter))
    channel._create_and_send_message = AsyncMock()
    sent: list[Message] = []
    channel._message_callback = sent.append
    payload = {
        "request_id": "call-1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "Which?", "options": [{"label": "A"}]}],
    }
    if interrupt_id:
        payload["interrupt_id"] = interrupt_id

    await channel._send_ask_user_question_card(_question(payload))
    # 新卡片回执按 (request_id, 题号, 取值) 定位上下文；选项无 value 时以 label 为取值。
    await channel._handle_card_callback_async("token-1", "call-1", 0, "A", "A", "ou_user")

    params = sent[-1].params
    assert params["source"] == "ask_user_interrupt"
    assert params["request_id"] == "call-1"
    assert params.get("interrupt_id", "") == interrupt_id
    assert ("interrupt_id" in params) is bool(interrupt_id)
