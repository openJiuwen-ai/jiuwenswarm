# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared setup of the client attachment tests: sessions and client apps in ``tmp_path``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.client_attachments import (
    attach,
    close_client_attachments,
    directory,
    registry,
    set_conversation_access,
    watch,
)
from jiuwenswarm.server.runtime.session import session_metadata

RESOURCE = {"key": "office-deck", "display_name": "Roadmap.pptx",
            "kind": "presentation", "host_vendor": "microsoft"}
CLIENT_APP = "office-powerpoint"
OTHER_APP = "wps-presentation"
OWNERS = {"session-1": "user-1", "session-2": "user-1", "session-3": "user-1", "foreign": "user-2"}
# What each conversation's paused agent still waits on (AgentRuntime.pending_interactions).
PAUSED: dict[str, list[dict]] = {}


async def read_paused(session_id: str) -> list[dict]:
    return list(PAUSED.get(session_id, []))


def give_access(pending=read_paused, client_gone=None, push=None) -> None:
    """AgentServer's conversation access: the turns of ``PAUSED`` wait, each runs under its owner."""
    set_conversation_access(
        pending, lambda: tuple(session_id for session_id, events in PAUSED.items() if events),
        lambda session_id: ("web", OWNERS.get(session_id, "")),
        client_gone or _nobody_answers_for_gone_clients, push or push_nowhere,
    )


class Clock:
    """The clock leases and the sweep read; it moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


async def sweep(clock: Clock) -> None:
    """One sweep at the clock's time, and the client_gone answers it starts."""
    await watch.sweep_once(clock.now)
    await asyncio.gather(*watch._answers)


class Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def write_app(root: Path, directory: str, tools: list[str]) -> Path:
    """An app's contract directory: the tools its client may declare."""
    contract = root / directory
    contract.mkdir(parents=True)
    (contract / "tools.json").write_text(json.dumps([{"name": name, "description": name} for name in tools]))
    return contract


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Sessions and the client apps JiuwenSwarm ships stay inside ``tmp_path``.

    ``docapps/apps`` is an importable package directory holding the contracts
    of two apps. Every conversation was created by the PowerPoint task pane.
    """
    sessions = tmp_path / "sessions"
    for session_id, owner in OWNERS.items():
        (sessions / session_id).mkdir(parents=True)
        (sessions / session_id / "metadata.json").write_text(json.dumps(new_conversation(session_id, owner)))
    monkeypatch.setattr(attach, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    session_metadata.flush_pending_writes()
    session_metadata._METADATA_CACHE.clear()
    monkeypatch.setattr(attach, "encode_agent_response_for_wire",
                        lambda response, response_id: {"ok": response.ok, "payload": response.payload})
    monkeypatch.setattr(session_metadata, "build_server_push_message",
                        lambda **kw: {"session_id": kw["session_id"], "payload": kw["payload"]})

    importable = tmp_path / "importable"
    (importable / "docapps").mkdir(parents=True)
    (importable / "docapps" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(importable))
    apps = importable / "docapps" / "apps"
    write_app(apps, "powerpoint", ["read_presentation_context", "presentation_apply_patch"])
    write_app(apps, "wps", ["read_deck"])
    monkeypatch.setattr(registry, "APPS", {CLIENT_APP: ("docapps/apps/powerpoint", frozenset()),
                                         OTHER_APP: ("docapps/apps/wps", frozenset())})
    registry.client_apps.cache_clear()
    clock = Clock()
    monkeypatch.setattr(directory, "clock", clock)
    # An AgentServer built by another test installs its access process-wide.
    give_access()
    close_client_attachments()
    yield
    close_client_attachments()
    registry.client_apps.cache_clear()
    PAUSED.clear()
    session_metadata.flush_pending_writes()
    session_metadata._METADATA_CACHE.clear()


def new_conversation(session_id: str, owner: str, client_app: str = CLIENT_APP) -> dict:
    """The metadata of a conversation created in a task pane, nobody having written in it yet."""
    return {"session_id": session_id, "user_id": owner, "channel_id": "web", "title": "", "message_count": 0,
            "mode": "agent.code.normal", "created_at": 100.0, "last_message_at": 100.0, "client_app": client_app}


def created_for(session_id: str, client_app: str) -> None:
    """The conversation was created by another app's client (``client_app``), or in the web UI (``""``)."""
    metadata = stored_metadata(session_id)
    (session_metadata.get_agent_sessions_dir() / session_id / "metadata.json").write_text(
        json.dumps(new_conversation(session_id, metadata["user_id"], client_app)))
    session_metadata._METADATA_CACHE.clear()


def stored_metadata(session_id: str) -> dict:
    """What ``session.list``, the Heartbeat and the mailbox read back for the conversation."""
    session_metadata.flush_pending_writes()
    return json.loads((session_metadata.get_agent_sessions_dir() / session_id / "metadata.json").read_text())


async def _nobody_answers_for_gone_clients(session_id, channel_id, owner_id, pending) -> None:
    pass


async def push_nowhere(message) -> None:
    pass


def request(method=ReqMethod.CLIENT_ATTACH, *, session="session-1", owner="user-1", socket="ws-1", **params):
    if method == ReqMethod.CLIENT_ATTACH:
        params = {"session_id": session, "resource": RESOURCE,
                  "capabilities": {"supported_tools": ["read_presentation_context"]}, **params}
    return AgentRequest(request_id="req-1", channel_id="web", session_id=session, req_method=method,
                        user_id=owner, params=params, metadata={"ws_id": socket})


def renew(attachment_id: str, *, session: str = "session-1", socket: str = "ws-1") -> AgentRequest:
    return request(ReqMethod.CLIENT_RENEW, session=session, socket=socket,
                   session_id=session, attachment_id=attachment_id)


def answer(agent_request: AgentRequest, pushed: list | None = None, push=None) -> dict:
    return asyncio.run(answer_async(agent_request, pushed, push))


async def answer_async(agent_request: AgentRequest, pushed: list | None = None, push=None) -> dict:
    socket = Socket()

    async def collect(message) -> None:
        if pushed is not None:
            pushed.append(message)

    await attach.handle_client_attach_request(socket, agent_request, asyncio.Lock(), push or collect)
    return socket.sent[-1]


def pending_call(session_id: str, request_id: str) -> dict:
    """A client tool call the conversation's pause waits on."""
    payload = {"event_type": "chat.client_tool_request", "request_id": request_id,
               "interrupt_id": f"interrupt-{request_id}", "source": "client_tool_interrupt",
               "tool_name": "read_presentation_context", "client_tool": {"input": {}}}
    PAUSED.setdefault(session_id, []).append(payload)
    return payload


def pending_question(session_id: str, request_id: str) -> dict:
    """A question the conversation's pause waits on."""
    payload = {"event_type": "chat.ask_user_question", "request_id": request_id,
               "interrupt_id": f"interrupt-{request_id}", "source": "permission_interrupt",
               "questions": [{"question": "Allow?"}]}
    PAUSED.setdefault(session_id, []).append(payload)
    return payload


def answered(session_id: str, request_id: str) -> None:
    """The pause no longer waits on ``request_id``."""
    PAUSED[session_id] = [payload for payload in PAUSED.get(session_id, []) if payload["request_id"] != request_id]
