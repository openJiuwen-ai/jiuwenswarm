# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session message status stays within the owning TUI user."""

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.common.session_message import (
    SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY,
    SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY,
)
from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel
from jiuwenswarm.gateway.routing.keys import RoutingKey


class _Client:
    def __init__(self, user_id):
        self._gateway_user_id = user_id
        self.closed = False
        self.frames = []

    async def send(self, data):
        self.frames.append(json.loads(data))


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_user_id", ["owner", "", "local"])
async def test_tui_status_routes_to_owner_only(owner_user_id):
    channel = TuiChannel()
    owner = _Client(owner_user_id)
    other = _Client("other")
    for client in (owner, other):
        await channel.register_ws(client, RoutingKey(
            channel_id="tui",
            app_id="default",
            user_id=client._gateway_user_id or "local-peer",
            session_id="target-1",
            agent_ref=None,
        ))
    try:
        message = Message(
            id="sm-1",
            type="event",
            channel_id="tui",
            session_id="target-1",
            params={},
            timestamp=0.0,
            ok=True,
            payload={"event_type": "session.message.updated", "message": {"status": "queued"}},
            metadata={
                SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY: owner_user_id or "local",
                SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY: not bool(owner_user_id),
            },
        )
        await channel.send(message)
        for _ in range(20):
            if owner.frames:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.01)
        assert len(owner.frames) == 1
        assert other.frames == []

        message.metadata = {}
        await channel.send(message)
        await asyncio.sleep(0.01)
        assert len(owner.frames) == 1
        assert other.frames == []
    finally:
        await channel.unregister_ws(owner)
        await channel.unregister_ws(other)
