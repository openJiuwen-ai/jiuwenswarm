# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Forward-layer tests for chat.swarmflow_reply.

The reply rides the standard 3-layer chain: TUI empty ack -> message_handler
forward -> process_message -> handle_swarmflow_reply. These tests pin the two
gateway-side invariants:

1. A swarmflow reply must NOT cancel an in-flight leader stream (the cancel gate
   only fires for CHAT_SEND).
2. A swarmflow reply is forwarded to the AgentServer as a non-stream request
   (the agent_ws_server then dispatches it to handle_swarmflow_reply).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema import Message
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _FakeAgentClient:
    """Captures the forwarded env (proves the reply reached the AgentServer)."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_request(self, env: object) -> SimpleNamespace:
        self.sent.append(env)
        return SimpleNamespace(
            request_id="r1",
            channel_id="tui",
            ok=True,
            payload={"ok": True},
            metadata=None,
        )

    @staticmethod
    async def send_request_stream(env: object):  # pragma: no cover
        if False:
            yield env


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        return cls(_FakeAgentClient())

    async def cancel_stream_tasks_for_channel(self, msg: Message) -> int:
        return await getattr(self, "_cancel_stream_tasks_for_channel")(msg)


def _swarmflow_reply_message(*, session_id: str = "sess-1") -> Message:
    return Message(
        id="r-sw",
        type="req",
        channel_id="tui",
        session_id=session_id,
        params={
            "session_id": session_id,
            "run_id": "run-1",
            "correlation_id": "review:host:0",
            "answer": "ok",
        },
        timestamp=0.0,
        ok=True,
        req_method=ReqMethod.CHAT_SWARMFLOW_REPLY,
        is_stream=False,
    )


@pytest.mark.asyncio
async def test_swarmflow_reply_does_not_cancel_existing_stream() -> None:
    """The cancel gate is CHAT_SEND-only; a swarmflow reply leaves streams alive.

    ``_cancel_stream_tasks_for_channel`` is a generic by-channel tool; the real
    gate is ``_should_cancel_existing_stream_before_chat_send`` (called inside
    ``_forward_loop`` before cancelling). Pinning the gate here proves a
    swarmflow reply never triggers cancellation of an in-flight leader stream.
    """
    assert MessageHandler._should_cancel_existing_stream_before_chat_send(
        _swarmflow_reply_message()
    ) is False
    # Contrast: a plain chat.send on the same channel WOULD cancel.
    chat_send = Message(
        id="r-cs", type="req", channel_id="tui", session_id="sess-1",
        params={"mode": "agent.plan", "query": "hi"}, timestamp=0.0, ok=True,
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
    )
    assert MessageHandler._should_cancel_existing_stream_before_chat_send(chat_send) is True


@pytest.mark.asyncio
async def test_swarmflow_reply_forwarded_to_agent_server_non_stream() -> None:
    """_process_non_stream_request forwards the reply env to the AgentServer client."""
    handler = _TestMessageHandler.create()
    msg = _swarmflow_reply_message()
    env = e2a_from_agent_fields(
        request_id=msg.id,
        channel_id=msg.channel_id,
        session_id=msg.session_id,
        req_method=ReqMethod.CHAT_SWARMFLOW_REPLY,
        params=msg.params,
        is_stream=False,
        timestamp=0.0,
    )
    await handler._process_non_stream_request(msg, env)  # pylint: disable=protected-access

    client = handler.agent_client  # type: ignore[attr-defined]
    assert len(client.sent) == 1
    forwarded = client.sent[0]
    # The forwarded envelope preserves the swarmflow reply method + params.
    assert forwarded.method == ReqMethod.CHAT_SWARMFLOW_REPLY.value
    assert forwarded.params["correlation_id"] == "review:host:0"
    assert forwarded.params["answer"] == "ok"
