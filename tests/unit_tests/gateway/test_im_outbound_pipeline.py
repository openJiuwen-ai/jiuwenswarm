# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IM outbound routing must classify terminal business replies only."""

from dataclasses import dataclass

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.im_pipeline.im_outbound import IMOutboundPipeline


@dataclass
class _FakeAdapter:
    platform_name: str = "飞书"
    reply_user_id_key: str = "reply_feishu_open_id"
    use_keyword_override: bool = False

    @staticmethod
    def get_candidate_user_id(metadata: dict) -> str:
        return str(metadata.get("reply_candidate_user_id") or "")


def _message(
    event_type: str,
    *,
    message_event_type: EventType | None = None,
) -> Message:
    payload = {"content": "我会提醒你"}
    if event_type:
        payload["event_type"] = event_type
    return Message(
        id="msg-1",
        type="event",
        channel_id="feishu",
        session_id="session-1",
        params={},
        timestamp=0,
        ok=True,
        payload=payload,
        metadata={
            "chat_type": "group",
            "reply_candidate_user_id": "ou_target",
        },
        group_digital_avatar=True,
        event_type=message_event_type,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "chat.delta",
        "chat.reasoning",
        "chat.tool_call",
        "chat.tool_result",
        "chat.processing_status",
        "chat.interrupt_result",
        "chat.media",
        "chat.error",
        "todo.updated",
        "stream.done",
    ],
)
async def test_non_terminal_event_never_enters_personal_action_classifier(
    event_type: str,
) -> None:
    pipeline = IMOutboundPipeline()
    pipeline.register_adapter("feishu", _FakeAdapter())

    async def unexpected_classifier(*_args):
        raise AssertionError("non-terminal events must not be classified")

    pipeline._classify_personal_action = unexpected_classifier

    await pipeline.apply(_message(event_type))


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["chat.final", ""])
async def test_terminal_or_legacy_reply_can_route_to_dm(event_type: str) -> None:
    pipeline = IMOutboundPipeline()
    pipeline.register_adapter("feishu", _FakeAdapter())

    async def classify_as_personal(*_args):
        return True, "DM"

    pipeline._classify_personal_action = classify_as_personal
    msg = _message(event_type)

    await pipeline.apply(msg)

    assert msg.metadata is not None
    assert msg.metadata["reply_scope"] == "dm"
    assert msg.metadata["reply_feishu_open_id"] == "ou_target"
    assert msg.metadata["reply_personal_action"] is True


@pytest.mark.asyncio
async def test_message_level_non_terminal_event_does_not_enter_classifier() -> None:
    pipeline = IMOutboundPipeline()
    pipeline.register_adapter("feishu", _FakeAdapter())

    async def unexpected_classifier(*_args):
        raise AssertionError("message-level chat.delta must not be classified")

    pipeline._classify_personal_action = unexpected_classifier

    await pipeline.apply(
        _message("", message_event_type=EventType.CHAT_DELTA)
    )
