# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expert routing must not execute or cancel the owner's foreground turn."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.session import session_metadata


def request(method=ReqMethod.CHAT_SEND, team="expert"):
    return AgentRequest(
        request_id="request",
        channel_id="web",
        session_id="session",
        req_method=method,
        params={"mode": "team", "target_team_id": team},
    )


def backend(org):
    return SimpleNamespace(org_task_manager=SimpleNamespace(organization_id=org))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "organization,allowed", [("org", True), ("foreign", False), (None, False)]
)
async def test_cancel_isolated_from_owner(monkeypatch, organization, allowed):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(
        session_metadata, "get_session_metadata", lambda _: {"team_name": "owner"}
    )
    monkeypatch.setattr(
        server,
        "_resolve_team_backend",
        lambda sid, team: backend("org" if team == "owner" else organization),
    )
    started = asyncio.Event()

    async def running():
        started.set()
        await asyncio.Event().wait()

    expert = asyncio.create_task(running())
    owner = asyncio.create_task(running())
    await started.wait()
    server._session_stream_tasks = {
        "session": {owner: asyncio.Event()},
        "session::organization::expert": {expert: asyncio.Event()},
    }
    try:
        response = await server._cancel_targeted_expert(request(ReqMethod.CHAT_CANCEL))
        assert response.ok is allowed
        assert expert.cancelled() is allowed
        assert not owner.done()
    finally:
        owner.cancel()
        expert.cancel()
        await asyncio.gather(owner, expert, return_exceptions=True)


@pytest.mark.asyncio
async def test_expert_stream_uses_runtime_event_adapter(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    closed = []

    async def stream(req):
        try:
            yield AgentResponseChunk(
                request_id=req.request_id,
                channel_id="web",
                payload={"event_type": "chat.final", "team_id": "expert"},
                is_complete=True,
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(server, "_targeted_expert_response_stream", stream)
    events = [
        event async for event in server._targeted_expert_runtime_stream(request())
    ]
    assert len(events) == 1
    assert events[0].session_id == "session"
    assert events[0].payload["team_id"] == "expert"
    assert events[0].is_complete
    assert closed == [True]


def test_regular_stream_key_is_unchanged():
    assert AgentWebSocketServer._organization_stream_key(request(team="")) == "session"
    assert (
        AgentWebSocketServer._organization_stream_key(request())
        == "session::organization::expert"
    )


@pytest.mark.asyncio
async def test_team_list_defaults_to_owner_even_when_expert_is_running(monkeypatch):
    from jiuwenswarm.server import agent_ws_server

    server = object.__new__(AgentWebSocketServer)
    sent = []

    async def entries(session_id, channel_id):
        assert (session_id, channel_id) == ("session", "web")
        return [
            {"team_id": "expert", "state": "running", "is_owner": False},
            {"team_id": "owner", "state": "paused", "is_owner": True},
        ]

    async def send(ws, payload):
        sent.append(payload)

    monkeypatch.setattr(server, "_team_list_entries", entries)
    monkeypatch.setattr(
        agent_ws_server,
        "encode_agent_response_for_wire",
        lambda response, **kwargs: response,
    )
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send)
    await server._handle_team_list(object(), request(), asyncio.Lock())
    assert sent[0].payload["default_team_id"] == "owner"
