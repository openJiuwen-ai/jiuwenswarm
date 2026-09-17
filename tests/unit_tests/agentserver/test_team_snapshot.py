# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for AgentWebSocketServer._handle_team_snapshot.

History restore must not keep a truthy-but-empty live monitor board
(``{tasks: [], members: [], team_id: ...}``) when ``team.db`` still has the
completed task rows with title/content.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeMonitorHandler:
    def __init__(self, snapshot: dict[str, Any] | None, *, running: bool = True) -> None:
        self._snapshot = snapshot
        self.is_running = running

    async def get_team_snapshot(self) -> dict[str, Any] | None:
        return self._snapshot


class _FakeTeamManager:
    def __init__(
        self,
        monitor: _FakeMonitorHandler | None,
        *,
        active_team_name: str | None = None,
    ) -> None:
        self._monitor = monitor
        self._active_team_name = active_team_name

    def get_monitor_handler(self, _session_id: str) -> _FakeMonitorHandler | None:
        return self._monitor

    def get_active_team_name(self, _session_id: str) -> str | None:
        return self._active_team_name


def _make_request(session_id: str = "sess-1") -> AgentRequest:
    return AgentRequest(
        request_id="req-snap-1",
        session_id=session_id,
        channel_id="web",
        req_method=ReqMethod.TEAM_SNAPSHOT,
        params={"session_id": session_id},
    )


async def _invoke(
    *,
    monitor: _FakeMonitorHandler | None,
    db_snapshot: dict[str, Any] | None,
    active_team_name: str | None = "team-sess-1",
    metadata_team_name: str | None = None,
):
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.server import agent_ws_server

    ws = _FakeWS()
    lock = asyncio.Lock()
    request = _make_request()
    team_manager = _FakeTeamManager(monitor, active_team_name=active_team_name)
    db_calls: list[tuple[str, str]] = []

    async def _db(session_id: str, team_name: str):
        db_calls.append((session_id, team_name))
        return db_snapshot

    with (
        mock.patch(
            "jiuwenswarm.agents.harness.team.get_team_manager",
            return_value=team_manager,
        ),
        mock.patch(
            "jiuwenswarm.agents.harness.team.handlers.team_monitor_handler"
            ".TeamMonitorHandler.get_team_snapshot_from_db",
            side_effect=_db,
        ),
        mock.patch(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            return_value=(
                {"team_name": metadata_team_name} if metadata_team_name else {}
            ),
        ),
    ):
        await agent_ws_server.AgentWebSocketServer._handle_team_snapshot(
            None, ws, request, lock
        )

    assert len(ws.sent) == 1
    resp = parse_agent_server_wire_unary(json.loads(ws.sent[0]))
    return resp, db_calls


@pytest.mark.anyio
async def test_empty_live_snapshot_falls_back_to_db_tasks() -> None:
    """Live monitor returns a truthy empty board → still read team.db."""
    live = {"members": [], "tasks": [], "team_id": "team-sess-1"}
    db = {
        "members": [],
        "tasks": [
            {
                "task_id": "uuid-1",
                "title": "实现冒泡排序",
                "content": "用 Python 实现",
                "status": "completed",
            }
        ],
        "team_id": "team-sess-1",
    }
    resp, db_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=db,
    )

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.ok is True
    assert len(resp.payload["tasks"]) == 1
    assert resp.payload["tasks"][0]["title"] == "实现冒泡排序"


@pytest.mark.anyio
async def test_live_snapshot_with_tasks_skips_db() -> None:
    """Live board already has tasks → do not hit DB."""
    live = {
        "members": [],
        "tasks": [{"task_id": "uuid-live", "title": "from-live", "status": "pending"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot={
            "members": [],
            "tasks": [{"task_id": "uuid-db", "title": "from-db", "status": "completed"}],
            "team_id": "team-sess-1",
        },
    )

    assert db_calls == []
    assert resp.payload["tasks"][0]["task_id"] == "uuid-live"


@pytest.mark.anyio
async def test_monitor_down_uses_db() -> None:
    """No running monitor → DB fallback."""
    db = {
        "members": [{"member_id": "w1", "name": "W", "status": "idle"}],
        "tasks": [{"task_id": "uuid-1", "title": "t", "status": "completed"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls = await _invoke(monitor=None, db_snapshot=db)

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.payload["tasks"][0]["task_id"] == "uuid-1"
    assert resp.payload["members"][0]["member_id"] == "w1"


@pytest.mark.anyio
async def test_empty_live_keeps_live_when_db_also_empty() -> None:
    """DB has no tasks either → keep the live members board (do not wipe)."""
    live = {
        "members": [{"member_id": "w1", "name": "W", "status": "idle"}],
        "tasks": [],
        "team_id": "team-sess-1",
    }
    resp, db_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot={"members": [], "tasks": [], "team_id": "team-sess-1"},
    )

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.payload["members"][0]["member_id"] == "w1"
    assert resp.payload["tasks"] == []
