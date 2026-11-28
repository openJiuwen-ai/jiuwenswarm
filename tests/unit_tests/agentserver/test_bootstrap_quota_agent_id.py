# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``session.create`` quota gate must read ``agent_id`` from ``metadata.routing``.

issue #124: ``agent_id`` is carried in ``metadata.routing`` (WEB_ROUTING_ID_FIELDS),
never as a top-level metadata key. ``_reject_session_create_if_quota_blocked``
must forward the routing-sourced ``agent_id`` to ``check_workspace_write`` so
agent-scoped quota policies can match on the AgentServer side.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.common.request_identity import apply_routing_metadata
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.enterprise.capabilities.workspace import quota as quota_mod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers import bootstrap
from jiuwenswarm.server.transports.ws_sink import WSSink


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _ctx(metadata: dict[str, Any]) -> RequestContext:
    request = AgentRequest(
        request_id="req-1",
        channel_id="web",
        session_id=None,
        req_method=ReqMethod.SESSION_CREATE,
        params={},
        metadata=metadata,
    )
    ws = _FakeWS()
    return RequestContext(
        request=request,
        sink=WSSink(ws, asyncio.Lock()),
        connection_id="c",
        services=None,  # type: ignore[arg-type]
    )


@pytest.fixture
def _quota_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(quota_mod, "is_workspace_quota_enabled", lambda: True)
    captured: dict[str, Any] = {}

    def _capture(*, additional_bytes: int = 0, agent_id: str = "", **_: Any) -> None:
        captured["additional_bytes"] = additional_bytes
        captured["agent_id"] = agent_id

    monkeypatch.setattr(quota_mod, "check_workspace_write", _capture)
    yield captured


@pytest.mark.asyncio
async def test_reject_passes_agent_id_from_routing(_quota_enabled):
    """agent_id in metadata.routing reaches check_workspace_write (issue #124)."""
    meta = apply_routing_metadata(
        {}, {"user_id": "u1", "group_id": "g1", "bot_id": "b1", "agent_id": "agent-1"},
    )
    blocked = await bootstrap._reject_session_create_if_quota_blocked(_ctx(meta))
    assert blocked is False
    assert _quota_enabled["agent_id"] == "agent-1"


@pytest.mark.asyncio
async def test_reject_ignores_top_level_agent_id(_quota_enabled):
    """A top-level metadata.agent_id (outside routing) must not reach the gate."""
    blocked = await bootstrap._reject_session_create_if_quota_blocked(
        _ctx({"agent_id": "evil-1", "user_id": "u1"})
    )
    assert blocked is False
    assert _quota_enabled["agent_id"] == ""
