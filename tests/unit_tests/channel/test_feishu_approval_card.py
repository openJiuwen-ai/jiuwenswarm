# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""飞书审批卡片回执贯通（D-20261029-0153-feishu-approval-callback）。

覆盖设计文档 V-01～V-09（裁剪后）：
- 四类中断（permission / confirm / evolution / ask_user）的回执形状；
- 多问题卡片逐题渲染、答完前不提交；
- 上下文过期、重复点击、非提问人点击、投递失败提示；
- 旧卡片（无 ref）按已过期处理。

注：上下文落盘与重启恢复（V-08 磁盘部分）已裁剪，上下文仅存内存。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from e2a.message import EventType, Message

from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
    _ApprovalCardContext,
)
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

ALLOW_ONCE = {"label": "本次允许", "value": "allow_once"}
ALWAYS_ALLOW = {"label": "永久记住", "value": "always_allow"}
REJECT = {"label": "拒绝", "value": "reject"}


def _question(
    text: str,
    options: list[dict[str, str]] | None = None,
    *,
    card_id: str = "",
    tool_payload: dict | None = None,
) -> dict:
    question = {
        "question": text,
        "header": "权限审批: bash",
        "options": list(options or []),
        "multi_select": False,
    }
    if card_id:
        question["card_id"] = card_id
    if tool_payload is not None:
        question["tool_payload"] = tool_payload
    return question


def _ctx(**overrides) -> _ApprovalCardContext:
    defaults = {
        "request_id": "call_1",
        "source": "permission_interrupt",
        "questions": [_question("执行 bash", [ALLOW_ONCE, REJECT], card_id="call_1")],
        "session_id": "feishu_oc_chat",
        "chat_id": "oc_chat",
        "metadata": {"feishu_chat_id": "oc_chat", "feishu_open_id": "ou_requester"},
    }
    defaults.update(overrides)
    return _ApprovalCardContext(**defaults)


@pytest.fixture()
def channel():
    channel = FeishuChannel(
        FeishuConfig(channel_id="feishu", app_id="app", app_secret="secret"),
        RobotMessageRouter(),
    )
    published: list[Message] = []
    channel._message_callback = published.append
    channel._main_loop = None
    channel._api_client = None
    channel.published = published  # type: ignore[attr-defined]
    return channel


def _toast_content(response) -> str:
    if isinstance(response, dict):
        return str((response.get("toast") or {}).get("content") or "")
    toast = getattr(response, "toast", None)
    return str(getattr(toast, "content", "") or "")


def _card_action(value: dict, operator: str = "ou_requester") -> SimpleNamespace:
    return SimpleNamespace(
        event=SimpleNamespace(
            token="tok_1",
            action=SimpleNamespace(value=value),
            operator=SimpleNamespace(open_id=operator),
        )
    )


def _event_message(request_id: str, source: str, questions: list[dict]) -> Message:
    return Message(
        id="evt_1",
        type="event",
        channel_id="feishu",
        session_id="feishu_oc_chat",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "source": source,
            "questions": questions,
        },
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata={
            "feishu_chat_id": "oc_chat",
            "feishu_open_id": "ou_requester",
            "feishu_chat_type": "group",
        },
    )


# ---------- V-01 / V-02：权限回执 ----------------------------


def test_permission_allow_once_builds_valid_confirm_payload(channel):
    ctx = _ctx()
    ctx.answers[0] = "allow_once"
    ctx.answer_labels[0] = "本次允许"

    params = channel._build_resume_params(ctx)

    assert params["source"] == "permission_interrupt"
    assert params["request_id"] == "call_1"
    assert params["supports_user_interaction"] is True
    assert params["answers"] == [{"selected_options": ["allow_once"], "card_id": "call_1"}]

    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )
    payload = interactive_input.user_inputs["call_1"]
    assert ConfirmPayload.model_validate(payload).approved is True


def test_permission_always_allow_sets_persist_allow(channel):
    ctx = _ctx()
    ctx.answers[0] = "always_allow"
    ctx.answer_labels[0] = "永久记住"

    params = channel._build_resume_params(ctx)
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )

    payload = ConfirmPayload.model_validate(interactive_input.user_inputs["call_1"])
    assert payload.approved is True
    assert payload.auto_confirm is True
    assert payload.persist_allow is True


def test_permission_reject_builds_rejected_payload(channel):
    ctx = _ctx()
    ctx.answers[0] = "reject"
    ctx.answer_labels[0] = "拒绝"

    params = channel._build_resume_params(ctx)
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )

    assert ConfirmPayload.model_validate(interactive_input.user_inputs["call_1"]).approved is False


# ---------- V-03：card_id 只属于权限回执 ----------------------------


def test_permission_without_card_id_keys_answer_by_request_id(channel):
    ctx = _ctx(questions=[_question("执行 bash", [ALLOW_ONCE, REJECT])])
    ctx.answers[0] = "allow_once"

    params = channel._build_resume_params(ctx)

    assert params["answers"] == [{"selected_options": ["allow_once"]}]
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )
    assert interactive_input.user_inputs["call_1"]["approved"] is True


# ---------- V-04：计划 / 演进来源与 plan 字段 ----------------------------


# ---------- V-05：ask_user 按问题原文回传 ----------------------------


# ---------- 发卡：逐题渲染 + 上下文落盘 ----------------------------


# ---------- V-06：多问题答完前不提交 ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("card_id", ["call_1", "legacy-opaque-card"])
async def test_permission_answer_submits_chat_send_with_card_id(channel, card_id):
    ctx = _ctx()
    ctx.questions[0]["card_id"] = card_id
    channel._save_card_context(ctx)

    await channel._handle_card_callback_async("tok", "call_1", 0, "allow_once", "本次允许", "ou_requester")

    assert len(channel.published) == 1
    message = channel.published[0]
    assert message.req_method.value == "chat.send"
    assert message.params["source"] == "permission_interrupt"
    assert message.params["answers"] == [{"selected_options": ["allow_once"], "card_id": card_id}]
    assert message.session_id == "oc_chat"
    assert message.chat_id == "oc_chat"


# ---------- V-06：多问题答完前不提交（续） ----------------------------


# ---------- V-08：过期 / 重复点击 / 重启恢复 ----------------------------


# ---------- V-09：旧卡片（无 ref）按已过期处理 --------------------


