# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for handle_team_snapshot.

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

_SNAPSHOT_VIA_WS = True


@pytest.fixture(params=[False, True], ids=["handler", "ws"], autouse=True)
def _snapshot_entrypoint(request, monkeypatch):
    """Run the same snapshot contracts through both management entry points."""
    monkeypatch.setitem(globals(), "_SNAPSHOT_VIA_WS", request.param)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeMonitorHandler:
    def __init__(self, snapshot: dict[str, Any] | None, *, running: bool = True) -> None:
        self._snapshot = snapshot
        self.is_running = running
        self.team_id = snapshot.get("team_id") if snapshot is not None else None

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
    db_members: list[dict[str, Any]] | None = None,
    db_members_error: bool = False,
    active_team_name: str | None = "team-sess-1",
    metadata_team_name: str | None = None,
    requested_team_name: str | None = None,
    via_ws: bool | None = None,
):
    from e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.server import agent_ws_server
    from jiuwenswarm.server.management import teams as teams_owner

    ws = _FakeWS()
    lock = asyncio.Lock()
    request = _make_request()
    if requested_team_name:
        request.params["team_name"] = requested_team_name
    team_manager = _FakeTeamManager(monitor, active_team_name=active_team_name)
    db_calls: list[tuple[str, str]] = []
    member_calls: list[tuple[str, bool]] = []

    async def _db(session_id: str, team_name: str):
        db_calls.append((session_id, team_name))
        return db_snapshot

    async def _db_members(team_name: str, *, exclude_leader: bool = False):
        member_calls.append((team_name, exclude_leader))
        if db_members_error:
            raise RuntimeError("team.db is locked")
        return db_members

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
            "jiuwenswarm.agents.harness.team.handlers.team_monitor_handler"
            ".TeamMonitorHandler.get_member_list_from_db",
            side_effect=_db_members,
        ),
        mock.patch.object(
            teams_owner,
            "get_session_metadata",
            return_value=(
                {"team_name": metadata_team_name} if metadata_team_name else {}
            ),
        ),
        mock.patch.object(
            agent_ws_server,
            "get_session_metadata",
            return_value=(
                {"team_name": metadata_team_name} if metadata_team_name else {}
            ),
        ),
    ):
        from jiuwenswarm.server.context import build_ws_request_context
        from jiuwenswarm.server.handlers.team import handle_team_snapshot

        entrypoint_via_ws = _SNAPSHOT_VIA_WS if via_ws is None else via_ws
        if entrypoint_via_ws:
            # Frontend WebClient always dispatches through the WS copy, so the
            # regression must be covered on this entry point too.
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

            await AgentWebSocketServer._handle_team_snapshot(
                AgentWebSocketServer.__new__(AgentWebSocketServer), ws, request, lock
            )
        else:
            await handle_team_snapshot(build_ws_request_context(object(), ws, request, lock))

    assert len(ws.sent) == 1
    resp = parse_agent_server_wire_unary(json.loads(ws.sent[0]))
    return resp, db_calls, member_calls


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
    resp, db_calls, member_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=db,
    )

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.ok is True
    assert len(resp.payload["tasks"]) == 1
    assert resp.payload["tasks"][0]["title"] == "实现冒泡排序"
    assert resp.payload["members_source"] == "db"


@pytest.mark.anyio
async def test_live_snapshot_with_tasks_and_members_skips_db() -> None:
    """Live board already has tasks and members → do not hit DB at all."""
    live = {
        "members": [{"member_id": "w1", "name": "W", "status": "idle"}],
        "tasks": [{"task_id": "uuid-live", "title": "from-live", "status": "pending"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls, member_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot={
            "members": [],
            "tasks": [{"task_id": "uuid-db", "title": "from-db", "status": "completed"}],
            "team_id": "team-sess-1",
        },
    )

    assert db_calls == []
    assert member_calls == []
    assert resp.payload["tasks"][0]["task_id"] == "uuid-live"
    assert resp.payload["members"][0]["member_id"] == "w1"
    assert resp.payload["members_source"] == "live"


@pytest.mark.anyio
async def test_live_tasks_without_members_tops_up_roster_from_db() -> None:
    """Roster rows land before team.runtime_ready: live may have tasks only.

    The member top-up must run independently of the task fallback, otherwise
    the right-side panel stays empty until a page refresh.
    """
    live = {
        "members": [],
        "tasks": [{"task_id": "uuid-live", "title": "from-live", "status": "pending"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls, member_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=None,
        db_members=[{"member_id": "w1", "name": "W", "status": "unstarted"}],
    )

    assert db_calls == []
    assert member_calls == [("team-sess-1", True)]
    assert resp.payload["members"][0]["member_id"] == "w1"
    assert resp.payload["tasks"][0]["task_id"] == "uuid-live"
    assert resp.payload["members_source"] == "live+db"


@pytest.mark.anyio
async def test_ws_dispatch_tops_up_roster_from_db() -> None:
    """The WS entry point must share the HTTP roster top-up (issue #92 path)."""
    live = {
        "members": [],
        "tasks": [{"task_id": "uuid-live", "title": "from-live", "status": "pending"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls, member_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=None,
        db_members=[{"member_id": "w1", "name": "W", "status": "unstarted"}],
        via_ws=True,
    )

    assert db_calls == []
    assert member_calls == [("team-sess-1", True)]
    assert resp.payload["members"][0]["member_id"] == "w1"
    assert resp.payload["tasks"][0]["task_id"] == "uuid-live"
    assert resp.payload["members_source"] == "live+db"
    assert resp.payload["members_live"] is False


@pytest.mark.anyio
async def test_ws_dispatch_db_tasks_do_not_drop_live_members() -> None:
    """WS fallback must not overwrite a live board that still has members."""
    live = {
        "members": [{"member_id": "live-1", "name": "L", "status": "idle"}],
        "tasks": [],
        "team_id": "team-sess-1",
    }
    db = {
        "members": [],
        "tasks": [{"task_id": "uuid-db", "title": "t", "status": "completed"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls, _ = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=db,
        via_ws=True,
    )

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.payload["tasks"][0]["task_id"] == "uuid-db"
    assert resp.payload["members"][0]["member_id"] == "live-1"
    assert resp.payload["members_source"] == "db"


@pytest.mark.anyio
async def test_member_top_up_failure_keeps_live_board() -> None:
    """A broken team runtime DB must not make the snapshot RPC fail."""
    live = {
        "members": [],
        "tasks": [{"task_id": "uuid-live", "title": "from-live", "status": "pending"}],
        "team_id": "team-sess-1",
    }
    resp, _, member_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=None,
        db_members_error=True,
    )

    assert member_calls == [("team-sess-1", True)]
    assert resp.ok is True
    assert resp.payload["members"] == []
    assert resp.payload["members_source"] == "live"


@pytest.mark.anyio
async def test_monitor_down_uses_db() -> None:
    """No running monitor → DB fallback."""
    db = {
        "members": [{"member_id": "w1", "name": "W", "status": "idle"}],
        "tasks": [{"task_id": "uuid-1", "title": "t", "status": "completed"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls, member_calls = await _invoke(monitor=None, db_snapshot=db)

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
    resp, db_calls, member_calls = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot={"members": [], "tasks": [], "team_id": "team-sess-1"},
    )

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.payload["members"][0]["member_id"] == "w1"
    assert resp.payload["tasks"] == []


@pytest.mark.anyio
async def test_db_tasks_do_not_drop_live_members() -> None:
    """A DB board without members must not erase members the live board has."""
    live = {
        "members": [{"member_id": "live-1", "name": "L", "status": "idle"}],
        "tasks": [],
        "team_id": "team-sess-1",
    }
    db = {
        "members": [],
        "tasks": [{"task_id": "uuid-db", "title": "t", "status": "completed"}],
        "team_id": "team-sess-1",
    }
    resp, db_calls, _ = await _invoke(
        monitor=_FakeMonitorHandler(live),
        db_snapshot=db,
    )

    assert db_calls == [("sess-1", "team-sess-1")]
    assert resp.payload["tasks"][0]["task_id"] == "uuid-db"
    assert resp.payload["members"][0]["member_id"] == "live-1"
    assert resp.payload["members_source"] == "db"


@pytest.mark.anyio
async def test_db_running_rows_do_not_claim_live_execution() -> None:
    """After worker replacement, persisted busy members are not live proof."""
    db = {
        "members": [{"member_id": "w1", "status": "busy", "execution_status": "running"}],
        "tasks": [{"task_id": "t1", "status": "in_progress"}],
        "team_id": "team-sess-1",
    }
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    with mock.patch.object(GLOBAL_RUNNER, "get_agent_team_monitor", new=mock.AsyncMock(return_value=None)):
        resp, _, _ = await _invoke(monitor=None, db_snapshot=db)
    assert resp.payload["members_source"] == "db"
    assert resp.payload["members_live"] is False
    assert resp.payload["runtime_available"] is False
    assert resp.payload["recovery_required"] is True
    assert resp.payload["members"] == db["members"]


@pytest.mark.anyio
async def test_db_fallback_does_not_require_recovery_of_a_live_runtime() -> None:
    live = {"members": [], "tasks": [], "team_id": "team-sess-1"}
    db = {"members": [], "tasks": [{"task_id": "t1", "status": "pending"}], "team_id": "team-sess-1"}
    resp, _, _ = await _invoke(monitor=_FakeMonitorHandler(live), db_snapshot=db)
    assert resp.payload["members_source"] == "db"
    assert resp.payload["runtime_available"] is True
    assert resp.payload["members_live"] is False
    assert resp.payload["recovery_required"] is False


@pytest.mark.anyio
async def test_runtime_probe_error_is_unknown_not_stopped() -> None:
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    with mock.patch.object(GLOBAL_RUNNER, "get_agent_team_monitor", new=mock.AsyncMock(side_effect=OSError("offline"))):
        resp, _, _ = await _invoke(monitor=None, db_snapshot={
            "members": [], "tasks": [{"task_id": "t1", "status": "pending"}], "team_id": "team-sess-1",
        })
    assert resp.payload["runtime_available"] is None
    assert resp.payload["recovery_required"] is None


@pytest.mark.anyio
async def test_other_teams_live_monitor_cannot_prove_requested_runtime_alive() -> None:
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    live = {"members": [], "tasks": [], "team_id": "team-other"}
    db = {
        "members": [{"member_id": "old-worker", "status": "busy"}],
        "tasks": [{"task_id": "unfinished", "status": "pending"}],
        "team_id": "requested-team",
    }
    probe = mock.AsyncMock(return_value=None)
    with mock.patch.object(GLOBAL_RUNNER, "get_agent_team_monitor", new=probe):
        resp, calls, _ = await _invoke(
            monitor=_FakeMonitorHandler(live),
            db_snapshot=db,
            active_team_name="team-other",
            requested_team_name="requested-team",
        )
    probe.assert_awaited_once_with(team_name="requested-team", session_id="sess-1")
    assert calls == [("sess-1", "requested-team")]
    assert resp.payload["team_id"] == "requested-team"
    assert resp.payload["runtime_available"] is False
    assert resp.payload["recovery_required"] is True


@pytest.mark.anyio
async def test_completed_historical_board_does_not_require_recovery() -> None:
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    with mock.patch.object(GLOBAL_RUNNER, "get_agent_team_monitor", new=mock.AsyncMock(return_value=None)):
        resp, _, _ = await _invoke(monitor=None, db_snapshot={
            "members": [], "tasks": [{"task_id": "done", "status": "completed"}], "team_id": "team-sess-1",
        })
    assert resp.payload["runtime_available"] is False
    assert resp.payload["members_live"] is False
    assert resp.payload["recovery_required"] is False


@pytest.mark.anyio
async def test_missing_database_board_does_not_claim_no_recovery_needed() -> None:
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    with mock.patch.object(GLOBAL_RUNNER, "get_agent_team_monitor", new=mock.AsyncMock(return_value=None)):
        resp, _, _ = await _invoke(monitor=None, db_snapshot=None)
    assert resp.payload["runtime_available"] is False
    assert resp.payload["recovery_required"] is None


@pytest.mark.anyio
async def test_scoped_probe_miss_keeps_a_running_session_monitor_available() -> None:
    """A running monitor without a board is still a live runtime, not a stopped one."""
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER

    probe = mock.AsyncMock(return_value=None)
    with mock.patch.object(GLOBAL_RUNNER, "get_agent_team_monitor", new=probe):
        resp, _, _ = await _invoke(
            monitor=_FakeMonitorHandler(None),
            db_snapshot={"members": [], "tasks": [{"task_id": "t1", "status": "pending"}], "team_id": "team-sess-1"},
        )
    probe.assert_awaited_once()
    assert resp.payload["runtime_available"] is True
    assert resp.payload["recovery_required"] is False
