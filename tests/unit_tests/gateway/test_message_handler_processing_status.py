# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""_send_processing_status must carry inbound metadata (ws_id) for request-scoped routing."""

from __future__ import annotations

import pytest

from gateway.message_handler.message_handler import MessageHandler
from openjiuwen_protocol.message import EventType


def _bare_handler() -> MessageHandler:
    handler = object.__new__(MessageHandler)
    handler._stream_modes = {}
    handler._stream_emits_processing_status = {}
    handler._active_chat_tasks = {}
    handler.published = []

    async def _publish(msg) -> None:
        handler.published.append(msg)

    async def _broadcast() -> None:
        return None

    handler.publish_robot_messages = _publish  # type: ignore[method-assign]
    handler._broadcast_task_global_running = _broadcast  # type: ignore[method-assign]
    return handler


@pytest.mark.asyncio
async def test_send_processing_status_forwards_ws_id_and_merges_mode() -> None:
    handler = _bare_handler()
    handler._stream_modes["rid-1"] = "agent.work.normal"

    await handler._send_processing_status(
        "rid-1",
        "sess-1",
        "web",
        is_processing=False,
        app_id="default",
        metadata={"ws_id": "http-outbound-old", "user_id": "u1"},
    )

    assert len(handler.published) == 1
    msg = handler.published[0]
    assert msg.event_type == EventType.CHAT_PROCESSING_STATUS
    assert msg.payload["is_processing"] is False
    assert msg.payload["request_id"] == "rid-1"
    assert msg.metadata["ws_id"] == "http-outbound-old"
    assert msg.metadata["user_id"] == "u1"
    assert msg.metadata["mode"] == "agent.work.normal"


@pytest.mark.asyncio
async def test_send_processing_status_without_metadata_keeps_mode_only() -> None:
    handler = _bare_handler()
    handler._stream_modes["rid-2"] = "agent.plan"

    await handler._send_processing_status(
        "rid-2",
        "sess-2",
        "web",
        is_processing=True,
    )

    msg = handler.published[0]
    assert msg.metadata == {"mode": "agent.plan"}
    assert msg.payload["request_id"] == "rid-2"
    assert msg.payload["is_processing"] is True
