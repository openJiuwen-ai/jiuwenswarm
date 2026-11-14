# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway delivery of client tool requests and of changes to the client attachments."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from e2a.message import EventType, Message
from agent_gateway.channel_manager.web.web_connect import WebChannel
from agent_gateway.message_handler import MessageHandler

from e2a.wire_codec import encode_agent_response_for_wire, parse_agent_server_wire_chunk
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
from tests.unit_tests.gateway.test_message_handler_stream_cancel import _drain_robot_messages, _TestMessageHandler

REQUEST = {
    "event_type": "chat.client_tool_request",
    "request_id": "call-1",
    "interrupt_id": "intr_1",
    "source": "client_tool_interrupt",
    "tool_name": "read_presentation_context",
    "client_tool": {"tool_name": "read_presentation_context", "input": {"call_goal": "inspect"},
                    "assets": {"uploads": {}, "downloads": {}}, "origin": {"kind": "user"}},
}


def test_client_tool_request_is_a_registered_gateway_event() -> None:
    message = MessageHandler._response_to_message(
        SimpleNamespace(request_id="req-1", channel_id="web", payload=REQUEST,
                        metadata={}, agent_ref=None, ok=True),
        "session-1",
        app_id="office_powerpoint",
    )

    assert message.type == "event"
    assert message.event_type is EventType.CHAT_CLIENT_TOOL_REQUEST
    assert message.payload["client_tool"]["input"] == {"call_goal": "inspect"}


class _RefusingAgentServer:
    @staticmethod
    async def send_request_stream(env):
        refusal = AgentResponse(request_id=env.request_id, channel_id="web", ok=False, payload={
            "error": "no paused execution waits on these client tool results", "code": ClientToolAnswerMismatch.code})
        yield parse_agent_server_wire_chunk(encode_agent_response_for_wire(refusal, response_id=env.request_id))


@pytest.mark.asyncio
async def test_agent_server_s_refusal_of_a_client_resume_reaches_the_client_naming_that_resume() -> None:
    handler = _TestMessageHandler.create_with_client(_RefusingAgentServer())
    resume = SimpleNamespace(request_id="office_resume_1", channel="web",
                             params={"source": "client_tool_interrupt", "answers": [{"request_id": "call-1"}]})

    await handler.process_stream(resume, session_id="session-1", request_metadata={})
    error, *after = await _drain_robot_messages(handler)

    payload = WebChannel._build_event_payload(error, error.event_type.value)
    assert error.event_type is EventType.CHAT_ERROR
    assert (payload["request_id"], payload["code"]) == ("office_resume_1", "CLIENT_TOOL_ANSWER_MISMATCH")
    assert any(message.payload.get("event_type") == "chat.processing_status"
               and message.payload.get("is_processing") is False for message in after)


def test_web_channel_preserves_the_whole_client_tool_request() -> None:
    msg = Message(
        id="req-1", type="event", channel_id="web", session_id="session-1", params={},
        timestamp=0.0, ok=True, payload=REQUEST, event_type=EventType.CHAT_CLIENT_TOOL_REQUEST,
    )

    payload = WebChannel._build_event_payload(msg, "chat.client_tool_request")

    assert payload["request_id"] == "call-1"
    assert payload["interrupt_id"] == "intr_1"
    assert payload["source"] == "client_tool_interrupt"
    assert payload["client_tool"] == REQUEST["client_tool"]


def test_web_channel_preserves_the_attachments_change_notice() -> None:
    msg = Message(
        id="client-directory-1", type="event", channel_id="web", session_id="session-1", params={},
        timestamp=0.0, ok=True,
        payload={"event_type": "client.attachments_changed", "request_id": "client_attachments_changed_1"},
        event_type=EventType.CLIENT_ATTACHMENTS_CHANGED,
    )

    payload = WebChannel._build_event_payload(msg, "client.attachments_changed")

    assert payload["request_id"] == "client_attachments_changed_1"
