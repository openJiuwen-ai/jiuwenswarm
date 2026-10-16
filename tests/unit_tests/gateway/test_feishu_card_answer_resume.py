# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the Feishu ask_user card answer routing (issue #5335).

A card action callback only carries the operator open_id, so the answer must
travel with the originating chat_id / session_id inside the button value and
re-enter the original session as a canonical interrupt-resume chat.send.
"""

import asyncio
import types
from types import SimpleNamespace

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
)


def _make_channel(captured: list) -> FeishuChannel:
    channel = object.__new__(FeishuChannel)
    channel._channel_id = "feishu"
    channel.config = SimpleNamespace(
        app_id="cli_test_app",
        enable_streaming=True,
        enable_memory=True,
    )
    channel._message_callback = captured.append
    return channel


def test_card_answer_routes_resume_chat_send_to_origin_session() -> None:
    captured: list = []
    channel = _make_channel(captured)

    action_value = {
        "label": "北京长城（慕田峪/八达岭）",
        "request_id": "call_abc123",
        "session_id": "feishu_1a101da3efc_ba5d1e223f93",
        "chat_id": "oc_bbbc3173029bc05410120cf5b083599d",
        "question": "去哪个长城？（你在杭州，两者行程差异很大）",
    }
    asyncio.run(
        channel._handle_card_callback_async("token_1", action_value, "ou_operator")
    )

    assert len(captured) == 1
    msg = captured[0]
    assert msg.req_method == ReqMethod.CHAT_SEND
    assert msg.session_id == "feishu_1a101da3efc_ba5d1e223f93"
    assert msg.chat_id == "oc_bbbc3173029bc05410120cf5b083599d"
    assert msg.params["source"] == "ask_user_interrupt"
    assert msg.params["request_id"] == "call_abc123"
    assert msg.params["query"] == ""
    assert msg.params["supports_user_interaction"] is True
    assert msg.params["answers"] == [
        {
            "question": "去哪个长城？（你在杭州，两者行程差异很大）",
            "selected_options": ["北京长城（慕田峪/八达岭）"],
        }
    ]
    # identity 四元组齐全，网关 SessionMap 反查才能命中原会话
    assert msg.provider == "feishu"
    assert msg.bot_id == "cli_test_app"
    assert msg.user_id == "ou_operator"
    assert msg.metadata["feishu_chat_id"] == "oc_bbbc3173029bc05410120cf5b083599d"


def test_card_answer_without_chat_id_keeps_legacy_answer_path() -> None:
    captured: list = []
    channel = _make_channel(captured)

    asyncio.run(
        channel._handle_card_callback_async(
            "token_2", {"label": "自驾", "request_id": "call_def456"}, "ou_operator"
        )
    )

    assert len(captured) == 1
    msg = captured[0]
    assert msg.req_method == ReqMethod.CHAT_ANSWER
    assert msg.session_id == "ou_operator"
    assert msg.chat_id == ""
    assert msg.params["request_id"] == "call_def456"
    assert msg.params["answers"] == [{"selected_options": ["自驾"]}]


def test_card_answer_button_value_carries_routing_context() -> None:
    """发卡侧：按钮 value 必须带回 chat_id/session_id/question 供回调路由。"""

    channel = _make_channel([])
    sent: list = []
    channel._user_question_card = {}

    async def _fake_send(request) -> None:
        sent.append(request)

    channel._create_and_send_message = _fake_send
    channel._extract_receive_info = lambda msg: ("oc_bbbc3173029bc05410120cf5b083599d", "chat_id")
    channel._build_feishu_card_elements = lambda text: []

    out_msg = types.SimpleNamespace(
        session_id="feishu_1a101da3efc_ba5d1e223f93",
        metadata={"feishu_chat_id": "oc_bbbc3173029bc05410120cf5b083599d"},
        chat_id="oc_bbbc3173029bc05410120cf5b083599d",
    )
    payload = {
        "request_id": "call_abc123",
        "questions": [
            {
                "question": "去哪个长城？（你在杭州，两者行程差异很大）",
                "header": "目的地",
                "options": [
                    {"label": "北京长城（慕田峪/八达岭）", "description": "两天一夜"},
                    {"label": "江南长城（临海）", "description": "当天往返"},
                ],
            }
        ],
    }
    asyncio.run(channel._send_ask_user_question_card(_with_payload(out_msg, payload)))

    assert len(sent) == 1
    card = sent[0].content
    assert "oc_bbbc3173029bc05410120cf5b083599d" in card
    assert "feishu_1a101da3efc_ba5d1e223f93" in card


def _with_payload(msg, payload: dict):
    msg.payload = payload
    return msg
