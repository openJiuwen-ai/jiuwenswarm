# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""飞书 ask_user 卡片回调应把答案按「中断恢复」语义路由回提问时的会话。

两个历史回归场景：
1. 卡片回调原先用 operator open_id 作为 session_id、chat_id 留空，gateway 以
   (provider, chat_id, bot_id, user_id) 查 SessionMap 找不到已有会话，于是新建
   一个会话并把答案投进去，正在等待 ask_user 的原会话永远收不到答案。
2. 即便路由修对，答案若走 chat.user_answer，agent 的 handle_user_answer 只消费
   symphony/evolution/permission 等审批类 request_id，ask_user 的 call_xxx 会
   直接返回空响应——原会话依旧等不到答案，飞书侧一直显示"正在回复"。
   Web/CLI 的正确做法是 chat.send + source=ask_user_interrupt（流式），由
   Runtime 注入 InteractiveInput 恢复被中断的回合（deliver_control_input）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.common.schema.message import Message, ReqMethod
from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
)

APP_ID = "cli_test_app"
ORIGIN_SESSION = "feishu_1a15738ae2e_d8d8b6b8d5f7"
ORIGIN_CHAT = "oc_f12c9a9b815b8c9f0c90dfd465168510"
OPERATOR = "ou_7df1452a94a81bcdb1ad057d3eea5a71"
REQUEST_ID = "call_00_kB2EMvyvV7z3EBUPhlUr6520"


def _make_channel() -> FeishuChannel:
    config = FeishuConfig(app_id=APP_ID, app_secret="secret", enabled=True)
    router = SimpleNamespace()
    return FeishuChannel(config, router)


def _ask_user_outbound(metadata: dict | None) -> Message:
    return Message(
        id=f"feishu:{REQUEST_ID}",
        type="event",
        channel_id="feishu",
        session_id=ORIGIN_SESSION,
        params={},
        timestamp=time.time(),
        ok=True,
        provider="feishu",
        chat_id=ORIGIN_CHAT,
        user_id=OPERATOR,
        bot_id=APP_ID,
        app_id=APP_ID,
        payload={
            "request_id": REQUEST_ID,
            "source": "ask_user_interrupt",
            "questions": [
                {
                    "question": "晚上想吃哪一类？",
                    "options": [{"label": "中餐"}, {"label": "西餐"}],
                }
            ],
        },
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_card_callback_resumes_paused_turn_via_chat_send() -> None:
    """ask_user 答案必须走 chat.send + source=ask_user_interrupt（流式）。"""
    channel = _make_channel()
    captured: list[Message] = []
    channel.on_message(captured.append)

    with patch.object(channel, "_create_and_send_message", new=AsyncMock()):
        await channel._send_ask_user_question_card(
            _ask_user_outbound({"feishu_chat_id": ORIGIN_CHAT, "feishu_open_id": OPERATOR})
        )

    assert channel._load_card_context(REQUEST_ID) is not None

    await channel._handle_card_callback_async(
        token="card-token",
        request_id=REQUEST_ID, index=0, value="中餐", label="中餐",
        operator_id=OPERATOR,
    )

    assert len(captured) == 1
    answer = captured[0]
    # 中断恢复语义：chat.send（流式）+ source=ask_user_interrupt
    assert answer.req_method == ReqMethod.CHAT_SEND
    assert answer.is_stream is True
    assert answer.params["source"] == "ask_user_interrupt"
    assert answer.params["request_id"] == REQUEST_ID
    assert answer.params["answers"] == [{"question": "晚上想吃哪一类？", "selected_options": ["中餐"]}]
    assert answer.params["supports_user_interaction"] is True
    # session_id 用平台侧 chat_id（与普通入站一致），由 gateway 的 SessionMap
    # 按身份四元组解析回内部会话；chat_id 必须是提问时的值而非空串。
    assert answer.session_id == ORIGIN_CHAT
    assert answer.chat_id == ORIGIN_CHAT
    # SessionMap 身份四元组 (provider, chat_id, bot_id, user_id) 必须完整
    assert answer.provider == "feishu"
    assert answer.bot_id == APP_ID
    assert answer.user_id == OPERATOR
    assert answer.metadata["feishu_chat_id"] == ORIGIN_CHAT
    assert answer.metadata["feishu_open_id"] == OPERATOR
    # 回调消费后应清理上下文，避免泄漏
    assert channel._load_card_context(REQUEST_ID) is None


@pytest.mark.asyncio
async def test_card_callback_without_context_never_resumes_an_unrelated_turn() -> None:
    channel = _make_channel()
    captured: list[Message] = []
    channel.on_message(captured.append)
    with patch.object(channel, "_notify_approval_operator", new=AsyncMock()) as notify:
        await channel._handle_card_callback_async(
            token="card-token", request_id=REQUEST_ID, index=0, value="中餐", label="中餐", operator_id=OPERATOR,
        )
        notify.assert_awaited_once()
    assert not captured


@pytest.mark.asyncio
async def test_card_callback_uses_msg_chat_id_when_metadata_lacks_feishu_chat_id() -> None:
    channel = _make_channel()
    captured: list[Message] = []
    channel.on_message(captured.append)

    with patch.object(channel, "_create_and_send_message", new=AsyncMock()):
        await channel._send_ask_user_question_card(_ask_user_outbound(None))

    await channel._handle_card_callback_async(
        token="card-token",
        request_id=REQUEST_ID, index=0, value="西餐", label="西餐",
        operator_id=OPERATOR,
    )

    answer = captured[0]
    assert answer.req_method == ReqMethod.CHAT_SEND
    assert answer.session_id == ORIGIN_CHAT
    assert answer.chat_id == ORIGIN_CHAT


@pytest.mark.asyncio
async def test_unanswerable_card_has_no_active_approval_context() -> None:
    channel = _make_channel()
    message = _ask_user_outbound({"feishu_chat_id": ORIGIN_CHAT})
    message.payload["questions"] = []
    with patch.object(channel, "_create_and_send_message", new=AsyncMock()):
        await channel._send_ask_user_question_card(message)
    assert channel._load_card_context(REQUEST_ID) is None
