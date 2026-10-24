# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""守护小艺渠道 session 复用：同一逻辑会话（conversationId）的连续 query 必须命中同一 session。

背景（真实日志）：小艺每条 message/stream 的入站 session_id 是手机侧逻辑会话 id
（conversationId，例如 5a423bc9f2ce471cab9e316d2d06edbf）。``_apply_channel_state``
按 ``xiaoyi:<conversationId>`` 查状态、查不到就清空 session_id，交给 gateway 侧
``session.create`` 分配真实会话；分配结果必须写回同一把状态键，否则下次入站又命中不到，
每发一条 query 就新建一个 ``xiaoyi_<hex>_<hex>`` session、上下文全丢。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import Message, ReqMethod
from gateway.message_handler.message_handler import MessageHandler

_CONVERSATION_ID = "5a423bc9f2ce471cab9e316d2d06edbf"
_PHYSICAL_SESSION_ID = "ef130309-65f4-4aa6-9df1-639390ad08cb"


class _FakeAgentClient:
    @staticmethod
    async def send_request(env: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True, payload={})

    @staticmethod
    async def send_request_stream(env: object):
        if False:  # pragma: no cover
            yield env


class _Harness(MessageHandler):
    @classmethod
    def create(cls) -> "_Harness":
        # 与 test_message_handler_mode 同法：清单例标记让 __init__ 跑完整初始化。
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        if "_singleton_initialized" in MessageHandler.__dict__:
            del MessageHandler._singleton_initialized
        if "_singleton_initialized" in cls.__dict__:
            del cls._singleton_initialized
        handler = cls(_FakeAgentClient())
        handler._get_config_raw = lambda: {}  # type: ignore[assignment]
        handler.allocated_session_ids: list[str] = []

        async def _fake_send(env: object) -> SimpleNamespace:
            sid = f"xiaoyi_alloc_{len(handler.allocated_session_ids)}"
            handler.allocated_session_ids.append(sid)
            return SimpleNamespace(ok=True, payload={"session_id": sid})

        handler._send_non_stream_agent_request = _fake_send  # type: ignore[assignment]
        return handler


def _xiaoyi_msg(request_id: str, *, conversation_id: str = _CONVERSATION_ID) -> Message:
    return Message(
        id=request_id,
        type="req",
        channel_id="xiaoyi",
        session_id=conversation_id,
        user_id="agent86d3225ab3ae403a8092872de86574f4",
        bot_id="agent86d3225ab3ae403a8092872de86574f4",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        timestamp=0,
        ok=True,
        params={"query": request_id},
        metadata={
            "xiaoyi_conversation_id": conversation_id,
            "xiaoyi_session_id": _PHYSICAL_SESSION_ID,
        },
    )


async def _drive(handler: _Harness, msg: Message) -> str | None:
    handler._apply_channel_state(msg)
    await handler._ensure_inbound_session(msg)
    return msg.session_id


@pytest.mark.asyncio
async def test_same_conversation_reuses_one_agent_session() -> None:
    handler = _Harness.create()

    first = await _drive(handler, _xiaoyi_msg("conv&1&aaaa&0"))
    second = await _drive(handler, _xiaoyi_msg("conv&2&bbbb&0"))
    third = await _drive(handler, _xiaoyi_msg("conv&3&cccc&0"))

    assert first, "首次入站必须分配真实会话"
    assert first == second == third
    assert handler.allocated_session_ids == [first], "同一逻辑会话只应分配一次 session"


@pytest.mark.asyncio
async def test_other_conversation_gets_its_own_session() -> None:
    handler = _Harness.create()

    a = await _drive(handler, _xiaoyi_msg("convA&1&aaaa&0", conversation_id="conv-a"))
    b = await _drive(handler, _xiaoyi_msg("convB&1&aaaa&0", conversation_id="conv-b"))

    assert a and b and a != b
    assert len(handler.allocated_session_ids) == 2


@pytest.mark.asyncio
async def test_physical_session_rotation_does_not_rotate_agent_session() -> None:
    """手机侧物理 sessionId 轮换（换设备/重连）不应让逻辑会话换 session。"""
    handler = _Harness.create()

    first = await _drive(handler, _xiaoyi_msg("conv&1&aaaa&0"))
    msg = _xiaoyi_msg("conv&2&bbbb&0")
    msg.metadata = dict(msg.metadata or {})
    msg.metadata["xiaoyi_session_id"] = "e54b2915-54f0-4f6e-a238-4f4253e0ea6a"
    second = await _drive(handler, msg)

    assert first == second
    assert len(handler.allocated_session_ids) == 1


@pytest.mark.asyncio
async def test_non_controlled_channel_is_untouched() -> None:
    """web 等非受控渠道保持原样：入站 session_id 直通，不触发分配。"""
    handler = _Harness.create()
    msg = _xiaoyi_msg("web&1&aaaa&0")
    msg.channel_id = "web"
    msg.session_id = "web_1a132ebe247_d77b4e6ece33"

    handler._apply_channel_state(msg)
    await handler._ensure_inbound_session(msg)

    assert msg.session_id == "web_1a132ebe247_d77b4e6ece33"
    assert handler.allocated_session_ids == []
