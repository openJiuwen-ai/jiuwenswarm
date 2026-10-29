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
from openjiuwen_protocol.message import EventType, Message

from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.im_platforms.feishu.feishu_connect import (
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
        "questions": [_question("执行 bash", [ALLOW_ONCE, REJECT], card_id="card-1")],
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
    assert params["answers"] == [{"selected_options": ["allow_once"], "card_id": "card-1"}]

    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )
    payload = interactive_input.user_inputs["card-1"]
    assert ConfirmPayload.model_validate(payload).approved is True


def test_permission_always_allow_sets_persist_allow(channel):
    ctx = _ctx()
    ctx.answers[0] = "always_allow"
    ctx.answer_labels[0] = "永久记住"

    params = channel._build_resume_params(ctx)
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )

    payload = ConfirmPayload.model_validate(interactive_input.user_inputs["card-1"])
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

    assert ConfirmPayload.model_validate(interactive_input.user_inputs["card-1"]).approved is False


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


def test_confirm_source_never_carries_card_id(channel):
    ctx = _ctx(
        source="confirm_interrupt",
        questions=[_question("执行计划", [{"label": "批准", "value": "approve"}], card_id="card-9")],
    )
    ctx.answers[0] = "approve"

    params = channel._build_resume_params(ctx)

    assert params["source"] == "confirm_interrupt"
    assert params["answers"] == [{"selected_options": ["approve"]}]
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )
    assert ConfirmPayload.model_validate(interactive_input.user_inputs["call_1"]).approved is True


# ---------- V-04：计划 / 演进来源与 plan 字段 ----------------------------


def test_plan_extra_is_returned_with_resume_params(channel):
    ctx = _ctx(
        source="confirm_interrupt",
        questions=[_question("执行计划", [{"label": "批准", "value": "approve"}])],
        plan_extra={
            "plan_approval_kind": "plan_approval",
            "plan_content": "计划正文",
            "plan_language": "cn",
        },
    )
    ctx.answers[0] = "approve"

    params = channel._build_resume_params(ctx)

    assert params["plan_approval_kind"] == "plan_approval"
    assert params["plan_content"] == "计划正文"
    assert params["plan_language"] == "cn"


def test_evolution_source_uses_selected_option(channel):
    ctx = _ctx(
        source="evolution_interrupt",
        questions=[_question("技能演进", [ALLOW_ONCE, ALWAYS_ALLOW, REJECT])],
    )
    ctx.answers[0] = "allow_once"

    params = channel._build_resume_params(ctx)

    assert params["source"] == "evolution_interrupt"
    assert params["answers"] == [{"selected_options": ["allow_once"]}]


# ---------- V-05：ask_user 按问题原文回传 ----------------------------


def test_ask_user_answers_are_keyed_by_question_text(channel):
    ctx = _ctx(
        source="ask_user_interrupt",
        questions=[
            _question("选颜色", [{"label": "红", "value": "red"}, {"label": "蓝", "value": "blue"}]),
            _question("选尺寸", [{"label": "大", "value": "L"}, {"label": "小", "value": "S"}]),
        ],
    )
    ctx.answers[0] = "blue"
    ctx.answers[1] = "L"

    params = channel._build_resume_params(ctx)

    assert params["source"] == "ask_user_interrupt"
    assert params["answers"] == [
        {"question": "选颜色", "selected_options": ["blue"]},
        {"question": "选尺寸", "selected_options": ["L"]},
    ]
    interactive_input = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1", params["answers"], params["source"],
    )
    assert interactive_input.user_inputs["call_1"] == {
        "answers": {"选颜色": "blue", "选尺寸": "L"},
    }


# ---------- 发卡：逐题渲染 + 上下文落盘 ----------------------------


@pytest.mark.asyncio
async def test_send_card_renders_every_question_and_saves_context(channel):
    channel._create_and_send_message = AsyncMock()
    questions = [
        _question("第一题", [{"label": "甲", "value": "a"}, {"label": "乙", "value": "b"}]),
        _question("第二题", [{"label": "丙", "value": "c"}, {"label": "丁", "value": "d"}]),
    ]

    await channel._send_ask_user_question_card(
        _event_message("call_2", "ask_user_interrupt", questions)
    )

    request = channel._create_and_send_message.await_args.args[0]
    card = json.loads(request.content)
    action_blocks = [element for element in card["elements"] if element.get("tag") == "action"]
    assert len(action_blocks) == 2
    assert action_blocks[0]["actions"][0]["value"] == {
        "ref": {"request_id": "call_2", "index": 0},
        "value": "a",
    }
    assert action_blocks[1]["actions"][1]["value"] == {
        "ref": {"request_id": "call_2", "index": 1},
        "value": "d",
    }

    ctx = channel._card_contexts["call_2"]
    assert ctx.source == "ask_user_interrupt"
    assert ctx.chat_id == "oc_chat"


@pytest.mark.asyncio
async def test_other_option_is_not_rendered_as_button(channel):
    """卡片没有输入框，"Other" 只能当占位，不能渲染成按钮。"""
    channel._create_and_send_message = AsyncMock()
    questions = [
        _question("第一题", [{"label": "红", "value": "red"}, {"label": "Other", "description": "Custom input"}]),
        _question("第二题", [{"label": "Other", "description": "Custom input"}]),
    ]

    await channel._send_ask_user_question_card(
        _event_message("call_5", "ask_user_interrupt", questions)
    )

    card = json.loads(channel._create_and_send_message.await_args.args[0].content)
    action_blocks = [element for element in card["elements"] if element.get("tag") == "action"]
    assert len(action_blocks) == 1
    assert [button["value"]["value"] for button in action_blocks[0]["actions"]] == ["red"]
    rendered = json.dumps(card, ensure_ascii=False)
    assert "该问题需要文字作答，请在网页端或终端完成。" in rendered


def test_other_only_question_is_not_answerable(channel):
    ctx = _ctx(
        source="ask_user_interrupt",
        questions=[_question("自由题", [{"label": "Other", "description": "Custom input"}])],
    )

    assert ctx.options_of(0) == []
    assert ctx.answerable_indexes() == []
    assert ctx.is_answered() is False


def test_other_placeholder_click_is_rejected(channel):
    """升级前发出的卡片仍带 "Other" 按钮，点下来要拦住而不是发无效回执。"""
    ctx = _ctx(
        source="ask_user_interrupt",
        questions=[_question("Q1", [{"label": "A1", "value": "a1"}, {"label": "Other", "description": "Custom input"}])],
    )
    channel._save_card_context(ctx)

    response = channel._on_card_action_trigger_sync(_card_action(
        {"ref": {"request_id": "call_1", "index": 0}, "value": "Other"},
    ))

    assert _toast_content(response) == "该题需要在网页端或终端填写答案"
    assert channel._card_contexts["call_1"].answers == {}


@pytest.mark.asyncio
async def test_permission_card_shows_redacted_tool_preview(channel):
    channel._create_and_send_message = AsyncMock()
    questions = [
        _question(
            "执行 bash",
            [ALLOW_ONCE, REJECT],
            card_id="card-1",
            tool_payload={"command": "rm -rf /tmp/x", "api_key": "super-secret"},
        )
    ]

    await channel._send_ask_user_question_card(
        _event_message("call_3", "permission_interrupt", questions)
    )

    card = json.loads(channel._create_and_send_message.await_args.args[0].content)
    rendered = json.dumps(card, ensure_ascii=False)
    assert "参数：" in rendered
    assert "super-secret" not in rendered


@pytest.mark.asyncio
async def test_send_failure_drops_context(channel):
    channel._create_and_send_message = AsyncMock(side_effect=RuntimeError("boom"))

    await channel._send_ask_user_question_card(
        _event_message("call_4", "permission_interrupt", [_question("执行 bash", [ALLOW_ONCE, REJECT])])
    )

    assert "call_4" not in channel._card_contexts


# ---------- V-06：多问题答完前不提交 ----------------------------


@pytest.mark.asyncio
async def test_multi_question_partial_answer_does_not_submit(channel):
    ctx = _ctx(
        source="ask_user_interrupt",
        questions=[
            _question("Q1", [{"label": "A1", "value": "a1"}]),
            _question("Q2", [{"label": "A2", "value": "a2"}]),
        ],
    )
    channel._save_card_context(ctx)

    await channel._handle_card_callback_async("tok", "call_1", 0, "a1", "A1", "ou_requester")

    assert channel.published == []
    saved = channel._card_contexts["call_1"]
    assert saved.answers == {0: "a1"}

    await channel._handle_card_callback_async("tok", "call_1", 1, "a2", "A2", "ou_requester")

    assert len(channel.published) == 1
    message = channel.published[0]
    assert message.req_method.value == "chat.send"
    assert message.params["source"] == "ask_user_interrupt"
    assert message.params["answers"] == [
        {"question": "Q1", "selected_options": ["a1"]},
        {"question": "Q2", "selected_options": ["a2"]},
    ]
    # 一次性：提交后上下文清理
    assert "call_1" not in channel._card_contexts


@pytest.mark.asyncio
async def test_permission_answer_submits_chat_send_with_card_id(channel):
    channel._save_card_context(_ctx())

    await channel._handle_card_callback_async("tok", "call_1", 0, "allow_once", "本次允许", "ou_requester")

    assert len(channel.published) == 1
    message = channel.published[0]
    assert message.req_method.value == "chat.send"
    assert message.params["source"] == "permission_interrupt"
    assert message.params["answers"] == [{"selected_options": ["allow_once"], "card_id": "card-1"}]
    assert message.session_id == "oc_chat"
    assert message.chat_id == "oc_chat"


# ---------- V-06：多问题答完前不提交（续） ----------------------------


def test_multi_question_click_shows_remaining_hint(channel):
    channel._save_card_context(_ctx(
        source="ask_user_interrupt",
        questions=[
            _question("Q1", [{"label": "A1", "value": "a1"}]),
            _question("Q2", [{"label": "A2", "value": "a2"}]),
        ],
    ))

    response = channel._on_card_action_trigger_sync(_card_action(
        {"ref": {"request_id": "call_1", "index": 0}, "value": "a1"},
    ))

    assert _toast_content(response) == "已选择，请继续回答剩余问题"


# ---------- V-08：过期 / 重复点击 / 重启恢复 ----------------------------


@pytest.mark.asyncio
async def test_expired_context_is_rejected_and_removed(channel):
    ctx = _ctx(created_at=time.time() - 25 * 60 * 60)
    channel._save_card_context(ctx)
    notify = AsyncMock()
    channel._notify_approval_operator = notify

    await channel._handle_card_callback_async("tok", "call_1", 0, "allow_once", "本次允许", "ou_requester")

    assert channel.published == []
    notify.assert_awaited_once()
    assert "过期" in notify.await_args.args[1]
    assert "call_1" not in channel._card_contexts


@pytest.mark.asyncio
async def test_duplicate_click_after_submit_is_rejected(channel):
    channel._save_card_context(_ctx())
    await channel._handle_card_callback_async("tok", "call_1", 0, "allow_once", "本次允许", "ou_requester")
    assert len(channel.published) == 1

    notify = AsyncMock()
    channel._notify_approval_operator = notify
    await channel._handle_card_callback_async("tok", "call_1", 0, "allow_once", "本次允许", "ou_requester")

    assert len(channel.published) == 1
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivery_failure_reports_invalid_card_without_retry_hint(channel):
    """投递失败后卡片已无按钮，提示不得引导用户重试。"""
    channel._save_card_context(_ctx())

    def _raise(_msg):
        raise RuntimeError("bus down")

    channel._message_callback = _raise
    notify = AsyncMock()
    channel._notify_approval_operator = notify

    await channel._handle_card_callback_async("tok", "call_1", 0, "allow_once", "本次允许", "ou_requester")

    assert "call_1" not in channel._card_contexts
    notify.assert_awaited_once()
    text = notify.await_args.args[1]
    assert "请重试" not in text
    assert "重新发起审批" in text


# ---------- V-09：旧卡片（无 ref）按已过期处理 --------------------


def test_legacy_card_without_ref_reports_expired(channel):
    response = channel._on_card_action_trigger_sync(_card_action({
        "label": "本次允许",
        "request_id": "legacy_call",
    }))

    assert _toast_content(response) == "审批已过期或已处理"


def test_unknown_request_id_reports_expired(channel):
    response = channel._on_card_action_trigger_sync(_card_action(
        {"ref": {"request_id": "missing_call", "index": 0}, "value": "allow_once"},
    ))

    assert _toast_content(response) == "审批已过期或已处理"
