# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared IM channels hand explicit supplements to the public input path."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.im_pipeline.im_inbound import IMInboundPipeline
from jiuwenswarm.gateway.im_pipeline.im_session_input import (
    is_shared_im_channel,
    prepare_im_session_input,
    steer_busy_im_chat,
)

_CHANNELS = (
    "feishu",
    "wecom",
    "dingtalk",
    "slack",
    "discord",
    "telegram",
    "wechat",
    "whatsapp",
    "xiaoyi",
)


def _message(channel: str, text: str = "补充一下验收标准", **params) -> Message:
    return Message(
        id=f"{channel}-1",
        type="req",
        channel_id=channel,
        session_id=f"{channel}-session",
        chat_id=f"{channel}-chat",
        user_id="user-1",
        params={"query": text, "content": text, **params},
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        metadata={"im_sender_user_id": "user-1"},
    )


@pytest.mark.parametrize("channel", _CHANNELS)
def test_shared_platforms_normalize_explicit_steer(channel: str) -> None:
    msg = _message(channel, runtime_mode=" STEER ", expected_execution_id="exec-1")
    assert is_shared_im_channel(channel)
    assert prepare_im_session_input(msg) is True
    assert msg.params["input_mode"] == "steer"
    assert "runtime_mode" not in msg.params
    assert msg.params["expected_execution_id"] == "exec-1"
    assert msg.params["query"] == "补充一下验收标准"
    assert msg.session_id == f"{channel}-session"
    assert msg.user_id == "user-1"


@pytest.mark.parametrize("channel", _CHANNELS)
def test_ordinary_im_text_is_not_converted_to_steer(channel: str) -> None:
    msg = _message(channel, "今天的发布还做吗")
    assert prepare_im_session_input(msg) is False
    assert "input_mode" not in msg.params
    assert msg.params["query"] == "今天的发布还做吗"


def test_metadata_intent_is_promoted_without_platform_execution_control() -> None:
    msg = _message("feishu_enterprise:bot-a", "继续看日志")
    msg.params.pop("query")
    msg.params["content"] = "继续看日志"
    msg.metadata = {
        "input_mode": "follow_up",
        "expected_execution_id": "exec-9",
        "im_sender_user_id": "user-1",
    }
    assert prepare_im_session_input(msg) is True
    assert msg.params["input_mode"] == "follow_up"
    assert msg.params["expected_execution_id"] == "exec-9"
    assert "runtime_mode" not in msg.params


def test_conflicting_modes_remain_for_the_public_entry_to_reject() -> None:
    msg = _message("slack", input_mode="steer", runtime_mode="follow_up")
    assert prepare_im_session_input(msg) is False
    assert msg.params["input_mode"] == "steer"
    assert msg.params["runtime_mode"] == "follow_up"


def test_interaction_answers_stay_on_the_resume_path() -> None:
    msg = _message("dingtalk", input_mode="steer", answers=[{"text": "同意"}])
    assert prepare_im_session_input(msg) is False
    assert msg.params["input_mode"] == "steer"
    assert msg.params["answers"] == [{"text": "同意"}]


@pytest.mark.parametrize("channel", _CHANNELS)
def test_busy_session_chat_send_becomes_steer(channel: str) -> None:
    msg = _message(channel, "主题改成市场")
    assert steer_busy_im_chat(msg) is True
    assert msg.params["input_mode"] == "steer"
    assert msg.params["query"] == "主题改成市场"
    assert msg.session_id == f"{channel}-session"


def test_busy_steer_skips_resume_answers_and_attachments() -> None:
    resume = _message("feishu", "这是回答")
    resume.metadata = {"is_resume_message": True}
    assert steer_busy_im_chat(resume) is False
    assert "input_mode" not in resume.params

    answer = _message("wecom", "同意", answers=[{"text": "同意"}])
    assert steer_busy_im_chat(answer) is False

    attachment = _message("slack", "看这个文件", files=[{"name": "a.txt"}])
    assert steer_busy_im_chat(attachment) is False
    assert "input_mode" not in attachment.params

    team = _message("dingtalk", "继续", mode="team.work.normal")
    assert steer_busy_im_chat(team) is False


def test_non_im_channel_is_not_rewritten_here() -> None:
    msg = _message("web", runtime_mode="steer")
    assert prepare_im_session_input(msg) is False
    assert msg.params["runtime_mode"] == "steer"
    assert "input_mode" not in msg.params


@dataclass
class _Adapter:
    channel_id: str = "telegram"
    platform_name: str = "Telegram"
    names: dict[str, str] = field(default_factory=lambda: {"user-1": "Ada"})

    def resolve_user_display_name(self, user_id: str) -> str:
        return self.names.get(user_id, "")


def _parse_visible_identity(query: str) -> tuple[dict, str]:
    prefix = "<im_sender_identity>"
    suffix = "</im_sender_identity>\n<im_message>\n"
    assert query.startswith(prefix)
    identity_raw, message = query[len(prefix):].split(suffix, 1)
    assert message.endswith("\n</im_message>")
    return json.loads(identity_raw), message[: -len("\n</im_message>")]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", _CHANNELS)
async def test_pipeline_forwards_explicit_input_for_every_shared_platform(channel: str) -> None:
    processor = AsyncMock()
    pipeline = IMInboundPipeline(processor=processor)
    msg = _message(channel, runtime_mode="steer")
    assert await pipeline.apply(msg) is True
    assert msg.params["input_mode"] == "steer"
    assert msg.params["query"] == "补充一下验收标准"
    processor.process.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_steer_keeps_sender_and_does_not_consume_pending_question(monkeypatch) -> None:
    processor = AsyncMock()
    adapter = _Adapter(channel_id="feishu", platform_name="飞书")
    pipeline = IMInboundPipeline(processor=processor, adapters={"feishu": adapter})
    pending = Mock(side_effect=AssertionError("steer must not consume the pending answer"))
    monkeypatch.setattr(pipeline, "_peek_pending", pending)
    msg = _message("feishu", "请补充测试结果")
    msg.group_digital_avatar = True
    msg.metadata = {
        "chat_type": "group",
        "im_chat_type": "group",
        "im_sender_user_id": "user-1",
        "is_resume_message": True,
        "dm_pending_interaction_id": "still-pending",
        "runtime_mode": "steer",
    }
    assert await pipeline.apply(msg) is True
    identity, text = _parse_visible_identity(msg.params["query"])
    assert identity["display_name"] == "Ada"
    assert identity["user_id"] == "user-1"
    assert identity["channel"] == "飞书"
    assert text == "请补充测试结果"
    assert msg.params["input_mode"] == "steer"
    pending.assert_not_called()
    processor.process.assert_not_awaited()
