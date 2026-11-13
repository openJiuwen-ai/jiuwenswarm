"""Silent lifecycle joins reach the Swarm host without creating chat tasks.

SDK ACK/timeout coverage belongs to agent-gateway lifecycle transport tests.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lark_oapi.api.im.v1 import P2ImChatMemberBotAddedV1

from agent_gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition


def channel(callback):
    result = object.__new__(FeishuChannel)
    result.config = SimpleNamespace(app_id="app", agent_id="agent")
    result._main_loop = asyncio.get_running_loop()
    result._persistent_group_fanout_callback = callback
    return result


def joined():
    return P2ImChatMemberBotAddedV1({
        "schema": "2.0",
        "header": {"event_id": "join-event", "create_time": str(int(time.time() * 1000)),
                   "event_type": "im.chat.member.bot.added_v1"},
        "event": {"chat_id": "silent-chat", "name": "Silent group"},
    })


@pytest.mark.asyncio
async def test_silent_joins_are_acknowledged_without_registering_groups_or_requests(tmp_path, monkeypatch):
    from jiuwenswarm.server import agent_ws_server as module
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from agent_gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import FeishuBinding
    from tests.unit_tests.gateway.test_persistent_group_fanout import _Host, NS

    host = _Host(tmp_path)
    agent = host.colleague("Silent group tester")
    binding = FeishuBinding(binding_id="binding", platform="feishu", app_id="app", namespace_id=NS, agent_id=agent)
    fanout = host.fanout([binding])
    server = object.__new__(module.AgentWebSocketServer)
    server._ensure_persistent_group_fanout = AsyncMock(return_value=fanout)
    wire = AsyncMock()
    monkeypatch.setattr(module, "send_wire_payload", wire)
    handler = object.__new__(MessageHandler)

    async def send(envelope):
        await server.dispatch_parsed_request(object(), e2a_to_agent_request(envelope), asyncio.Lock())
        return SimpleNamespace(ok=True, payload={"disposition": "handled"})

    handler.agent_client = SimpleNamespace(send_request=send)
    c = channel(handler.handle_persistent_feishu_event)
    c.config.agent_id = agent
    try:
        for index in (1, 2):
            data = P2ImChatMemberBotAddedV1({
                "schema": "2.0", "header": {"event_id": f"join-{index}",
                    "create_time": str(int(time.time() * 1000)), "event_type": "im.chat.member.bot.added_v1"},
                "event": {"chat_id": f"silent-{index}", "name": f"Silent {index}"},
            })
            await c._on_persistent_change(data, "joined")
        rows = host.groups.directory(NS, agent)
        assert rows == ()
        assert host.groups.active_accesses(NS, agent) == ()
        assert host.row_counts() == (0, 0, 0)
        assert wire.await_count == 2
    finally:
        await host.close()
