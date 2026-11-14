# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``mcp.connection_changed`` frame survives the pinned Gateway hop.

The unit tests around the timeout patch only assert the dict handed to
``send_push``. The browser sees something else: the Gateway decodes that wire
into a chunk, turns it into a ``Message`` and resolves the event name for the
web frame. Two properties matter there and are easy to lose:

* the session id must survive decoding — ``WebChannel._send_message`` drops
  session-less frames instead of broadcasting them;
* the event name must come back as ``mcp.connection_changed`` — the Gateway
  does not know that name, so ``msg.event_type`` stays ``None`` and only the
  ``payload.event_type`` fallback keeps it.

These helpers live in agent-gateway, a pinned dependency this repo does not
modify; the test pins the contract we rely on (if the Gateway changes it, this
fails loudly instead of the UI silently never refreshing).
"""

from __future__ import annotations

from agent_gateway.channel_manager.web.web_connect import WebChannel
from agent_gateway.message_handler.message_handler import MessageHandler
from e2a.wire_codec import parse_agent_server_wire_chunk

from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.server.runtime.mcp.connection_events import (
    MCP_CONNECTION_CHANGED_EVENT,
)


def _frame_message():
    wire = build_server_push_wire({
        "request_id": "req-frame",
        "channel_id": "web",
        "session_id": "sess-1",
        "payload": {"event_type": MCP_CONNECTION_CHANGED_EVENT},
        "is_complete": False,
    })
    chunk = parse_agent_server_wire_chunk(wire)
    session_id = wire.get("session_id")
    return MessageHandler._chunk_to_message(chunk, session_id=session_id)


def test_gateway_keeps_the_session_id_for_routing() -> None:
    assert _frame_message().session_id == "sess-1"


def test_gateway_resolves_the_event_name_from_the_payload() -> None:
    message = _frame_message()

    # The Gateway has no EventType for our name, so only the payload carries it.
    assert message.event_type is None
    assert WebChannel._resolve_event_name(message) == MCP_CONNECTION_CHANGED_EVENT
