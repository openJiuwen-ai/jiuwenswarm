# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``subagents.list`` reports the roster mounted on a channel's main agent."""

import asyncio
from types import SimpleNamespace

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server


def _server(monkeypatch, sent, get_agent):
    async def send(_ws, response):
        sent.append(response)

    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send)
    monkeypatch.setattr(
        agent_ws_server, "encode_agent_response_for_wire", lambda response, **_: response
    )
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._agent_manager = SimpleNamespace(get_agent=get_agent)
    return server


def _request() -> AgentRequest:
    return AgentRequest(
        request_id="subagents-list",
        channel_id="channel-one",
        req_method=ReqMethod.SUBAGENTS_LIST,
    )


def _agent_with(specs):
    async def ensure_instance():
        return SimpleNamespace(deep_config=SimpleNamespace(subagents=specs))

    async def get_agent(**_kwargs):
        return SimpleNamespace(ensure_instance=ensure_instance)

    return get_agent


async def test_mounted_names_are_reported_once_for_the_requested_channel(monkeypatch):
    sent, calls = [], []

    async def ensure_instance():
        return SimpleNamespace(deep_config=SimpleNamespace(subagents=[
            SimpleNamespace(agent_card=SimpleNamespace(name="research_agent")),
            SimpleNamespace(agent_card=SimpleNamespace(name="research_agent")),
            SimpleNamespace(card=SimpleNamespace(name="general-purpose")),
        ]))

    async def get_agent(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ensure_instance=ensure_instance)

    server = _server(monkeypatch, sent, get_agent)

    await server._handle_subagents_list(None, _request(), asyncio.Lock())

    assert calls == [{"channel_id": "channel-one", "mode": "agent"}]
    assert sent[0].ok is True
    assert sent[0].payload == {"subagents": ["research_agent", "general-purpose"]}


async def test_a_channel_with_no_subagents_reports_an_empty_roster(monkeypatch):
    sent = []
    server = _server(monkeypatch, sent, _agent_with([]))

    await server._handle_subagents_list(None, _request(), asyncio.Lock())

    assert sent[0].ok is True
    assert sent[0].payload == {"subagents": []}


async def test_an_unavailable_main_agent_is_reported_as_a_failure(monkeypatch):
    sent = []

    async def get_agent(**_kwargs):
        return None

    server = _server(monkeypatch, sent, get_agent)

    await server._handle_subagents_list(None, _request(), asyncio.Lock())

    assert sent[0].ok is False
    assert "main agent is unavailable" in sent[0].payload["error"]
