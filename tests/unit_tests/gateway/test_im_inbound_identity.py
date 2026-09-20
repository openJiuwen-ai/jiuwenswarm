# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A shared IM group session must preserve the current human's identity for the Agent."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock
from dataclasses import dataclass, field

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a_or_fallback
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.im_pipeline.im_inbound import (
    IMConversationProcessor,
    IMInboundPipeline,
)


@dataclass
class _FakeAdapter:
    channel_id: str = "feishu"
    platform_name: str = "飞书"
    reply_user_id_key: str = "reply_feishu_open_id"
    use_keyword_override: bool = False
    names: dict[str, str] = field(
        default_factory=lambda: {"ou_zhang": "张三", "ou_li": "李四"}
    )

    def get_principal_user_id(self) -> str:
        return "ou_owner"

    def get_principal_display_name(self) -> str:
        return "王经理"

    def resolve_user_display_name(self, user_id: str) -> str:
        return self.names.get(user_id, "")

    def get_bot_mention_tokens(self) -> list[str]:
        return ["@WorkSwarm"]

    def load_recent_messages(self, _thread_id: str, limit: int = 500) -> list:
        del limit
        return []

    def build_relevance_metadata(
        self,
        metadata: dict,
        *,
        sender_user_id: str,
        relevant: bool,
    ) -> dict:
        del metadata
        return {
            "reply_candidate_feishu_open_id": sender_user_id,
            "relevant": relevant,
        }


class _RewritingProcessor(IMConversationProcessor):
    async def _rewrite_query(self, prompt: str, principal_name: str, adapter) -> str:
        del prompt, principal_name, adapter
        return "整理后的发布请求"


def _group_message(user_id: str, text: str, *, mentions: list[str] | None = None) -> Message:
    return Message(
        id=f"msg-{user_id}",
        type="req",
        channel_id="feishu",
        session_id="shared-work-session",
        chat_id="oc_group",
        user_id=user_id,
        bot_id="cli_bot",
        provider="feishu",
        params={"query": text, "content": text},
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        metadata={
            "chat_type": "group",
            "im_chat_type": "group",
            "im_sender_user_id": user_id,
            "im_thread_id": "oc_group",
            "mentioned_open_ids": mentions or [],
            "timestamp_ms": 0,
        },
        group_digital_avatar=True,
    )


def _parse_visible_identity(query: str) -> tuple[dict, str]:
    prefix = "<im_sender_identity>"
    suffix = "</im_sender_identity>\n<im_message>\n"
    assert query.startswith(prefix)
    identity_raw, message = query[len(prefix) :].split(suffix, 1)
    assert message.endswith("\n</im_message>")
    return json.loads(identity_raw), message[: -len("\n</im_message>")]


@pytest.mark.asyncio
async def test_explicit_supplement_keeps_sender_without_consuming_pending_question(monkeypatch):
    processor = AsyncMock()
    pipeline = IMInboundPipeline(processor=processor, adapters={"feishu": _FakeAdapter()})
    pending = Mock(side_effect=AssertionError("supplement must not consume pending answer"))
    monkeypatch.setattr(pipeline, "_peek_pending", pending)
    msg = _group_message("ou_li", "请补充测试结果")
    msg.params["input_mode"] = "steer"
    msg.metadata["is_resume_message"] = True
    msg.metadata["dm_pending_interaction_id"] = "still-pending"
    assert await pipeline.apply(msg)
    identity, text = _parse_visible_identity(msg.params["query"])
    assert identity["user_id"] == "ou_li"
    assert text == "请补充测试结果"
    pending.assert_not_called()
    processor.process.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_group_message_keeps_sender_identity_for_agent() -> None:
    adapter = _FakeAdapter()
    pipeline = IMInboundPipeline(
        processor=_RewritingProcessor(),
        adapters={"feishu": adapter},
    )
    msg = _group_message(
        "ou_zhang",
        "@WorkSwarm 帮我确认发布计划",
        mentions=["ou_owner"],
    )

    assert await pipeline.apply(msg) is True
    request = e2a_to_agent_request(message_to_e2a_or_fallback(msg))
    identity, visible_message = _parse_visible_identity(request.params["query"])

    assert identity == {
        "channel": "飞书",
        "display_name": "张三",
        "user_id": "ou_zhang",
    }
    assert visible_message == "@WorkSwarm 帮我确认发布计划"
    assert request.user_id == "ou_zhang"
    assert request.metadata["im_sender_display_name"] == "张三"


@pytest.mark.asyncio
async def test_rewriter_cannot_remove_sender_identity() -> None:
    adapter = _FakeAdapter()
    pipeline = IMInboundPipeline(
        processor=_RewritingProcessor(),
        adapters={"feishu": adapter},
    )
    msg = _group_message("ou_li", "那个计划我觉得可以发了")

    assert await pipeline.apply(msg) is True
    request = e2a_to_agent_request(message_to_e2a_or_fallback(msg))
    identity, visible_message = _parse_visible_identity(request.params["query"])

    assert identity["display_name"] == "李四"
    assert identity["user_id"] == "ou_li"
    assert visible_message == "整理后的发布请求"
    assert request.user_id == "ou_li"


@pytest.mark.asyncio
async def test_two_humans_share_session_but_have_distinct_visible_identities() -> None:
    adapter = _FakeAdapter()
    pipeline = IMInboundPipeline(
        processor=_RewritingProcessor(),
        adapters={"feishu": adapter},
    )
    messages = [
        _group_message("ou_zhang", "张三的任务"),
        _group_message("ou_li", "李四的任务"),
    ]

    requests = []
    for msg in messages:
        assert await pipeline.apply(msg) is True
        requests.append(e2a_to_agent_request(message_to_e2a_or_fallback(msg)))

    assert {request.session_id for request in requests} == {"shared-work-session"}
    identities = [_parse_visible_identity(request.params["query"])[0] for request in requests]
    assert [(item["display_name"], item["user_id"]) for item in identities] == [
        ("张三", "ou_zhang"),
        ("李四", "ou_li"),
    ]


@pytest.mark.asyncio
async def test_non_avatar_message_is_not_rewritten() -> None:
    adapter = _FakeAdapter()
    pipeline = IMInboundPipeline(
        processor=_RewritingProcessor(),
        adapters={"feishu": adapter},
    )
    msg = _group_message("ou_zhang", "普通群消息")
    msg.group_digital_avatar = False

    assert await pipeline.apply(msg) is True
    assert msg.params["query"] == "普通群消息"
    assert "agent_sender_identity_injected" not in msg.metadata


@pytest.mark.asyncio
async def test_persist_first_task_bypasses_rewrite_but_keeps_sender_identity() -> None:
    adapter = _FakeAdapter()
    pipeline = IMInboundPipeline(
        processor=_RewritingProcessor(),
        adapters={"feishu": adapter},
    )
    msg = _group_message("ou_zhang", "跟进本周发布")
    msg.metadata["persist_session_first_task"] = True

    assert await pipeline.apply(msg) is True
    request = e2a_to_agent_request(message_to_e2a_or_fallback(msg))
    identity, visible_message = _parse_visible_identity(request.params["query"])

    assert identity["display_name"] == "张三"
    assert identity["user_id"] == "ou_zhang"
    assert visible_message == "跟进本周发布"
