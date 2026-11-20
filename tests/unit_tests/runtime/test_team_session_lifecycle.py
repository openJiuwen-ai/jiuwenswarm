# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Team implementation of the Runtime lifecycle port."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
from jiuwenswarm.runtime import team_session_lifecycle as lifecycle_module
from jiuwenswarm.runtime.session.model import SessionLifecycleContext
from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter

test_logger = logging.getLogger("tests.team_session_lifecycle")


class _RecordingTeamManager:
    """Record the round reporter registered by the lifecycle adapter."""

    def __init__(self, *, paused: bool = True) -> None:
        self.reporter: Callable[[str, int, bool], None] | None = None
        self.paused = paused

    def set_round_state_reporter(
        self,
        reporter: Callable[[str, int, bool], None] | None,
    ) -> None:
        self.reporter = reporter

    def remove_waiter(
        self,
        session_id: str,
        request_id: str,
        *,
        generation: int | None = None,
    ) -> None:
        del session_id, request_id, generation

    async def pause_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        generation: int | None = None,
    ) -> bool:
        del session_id, reason, generation
        return self.paused

    async def stop_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        stop_runner: bool = True,
        generation: int | None = None,
    ) -> bool:
        del session_id, reason, stop_runner, generation
        return True

    def has_waiters(self, session_id: str) -> bool:
        return False

    async def broadcast_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        channel_id: str | None = None,
    ) -> bool:
        raise AssertionError("A manager without waiters should not broadcast the pause completion")

    def is_generation_running(self, session_id: str, generation: int) -> bool:
        del session_id, generation
        return True


class _RecordingCoordinator:
    """Record Team round transitions reported to the Coordinator."""

    def __init__(self) -> None:
        self.transitions: list[tuple[str, str, int]] = []

    def resume_waiting_for_input(self, session_id: str, generation: int) -> None:
        self.transitions.append(("running", session_id, generation))

    def mark_session_waiting_for_input(
        self,
        session_id: str,
        generation: int,
    ) -> None:
        self.transitions.append(("waiting", session_id, generation))


def test_adapter_registers_required_round_state_reporter() -> None:
    manager = _RecordingTeamManager()
    coordinator = _RecordingCoordinator()

    TeamSessionLifecycleAdapter(
        coordinator=coordinator,
        team_manager=manager,
    )

    assert manager.reporter is not None
    manager.reporter("session-a", 3, True)
    manager.reporter("session-a", 3, False)
    assert coordinator.transitions == [
        ("running", "session-a", 3),
        ("waiting", "session-a", 3),
    ]


def test_adapter_rejects_manager_without_round_state_reporter() -> None:
    coordinator = _RecordingCoordinator()
    incomplete_manager = cast(Any, object())

    with pytest.raises(AttributeError, match="set_round_state_reporter"):
        TeamSessionLifecycleAdapter(
            coordinator=coordinator,
            team_manager=incomplete_manager,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [True, False])
async def test_pause_session_returns_result_without_publishing(
    paused: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _RecordingTeamManager(paused=paused)
    adapter = TeamSessionLifecycleAdapter(
        coordinator=_RecordingCoordinator(),
        team_manager=manager,
    )
    publish = AsyncMock()
    monkeypatch.setattr(adapter, "publish_pause_completed", publish)

    result = await adapter.pause_session(
        SessionLifecycleContext(session_id="session-a", generation=2),
        reason="user_pause",
        channel_id="web",
    )

    assert result is paused
    publish.assert_not_awaited()
    test_logger.info("Pause result=%s; completion delivery remains Runtime-owned", paused)


@pytest.mark.asyncio
async def test_pause_completion_pushes_member_and_session_states_when_gateway_cancels_observer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent_gateway.message_handler.message_handler import MessageHandler

    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
    from jiuwenswarm.server.runtime.session.session_history import flush_history_writes, read_team_history_records

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    manager = TeamManager()
    coordinator = _RecordingCoordinator()
    adapter = TeamSessionLifecycleAdapter(coordinator=coordinator, team_manager=manager)
    observer: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    manager.add_waiter("session-a", "observer", observer, generation=2, attach_only=True)
    gateway = object.__new__(MessageHandler)
    gateway.__init__(AsyncMock())
    sent_messages = AsyncMock()
    monkeypatch.setattr(gateway, "publish_robot_messages", sent_messages)
    gateway_observer = asyncio.get_running_loop().create_future()

    async def deliver_push(message: dict[str, Any]) -> bool:
        await gateway._handle_agent_server_push(build_server_push_wire(message))
        if sent_messages.await_count == 3:
            # The Gateway can close its consumer while AgentServer still has a waiter.
            gateway_observer.cancel()
        return True

    server = AsyncMock()
    server.send_push.side_effect = deliver_push
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", lambda: server)
    monkeypatch.setattr(
        lifecycle_module,
        "get_session_metadata_async",
        AsyncMock(return_value={"team_name": "team-a"}),
    )
    member_ids = ("writer", "reviewer", "analyst", "tester", "integrator")
    read_members = AsyncMock(
        return_value=[
            *({"member_id": member_id, "status": "paused"} for member_id in member_ids),
            {"member_id": "unused", "status": "unstarted"},
        ],
    )
    monkeypatch.setattr(TeamMonitorHandler, "get_member_list_from_db", read_members)

    await adapter.publish_pause_completed("session-a", generation=2, channel_id="web")

    read_members.assert_awaited_once_with("team-a", exclude_leader=True)
    assert gateway_observer.cancelled()
    assert observer.empty()
    assert sent_messages.await_count == 6
    messages = [call.args[0] for call in sent_messages.await_args_list]
    flush_history_writes()
    history = read_team_history_records("session-a")
    assert [record["event"] for record in history if record["event_type"] == "team.member"] == [
        message.payload["event"] for message in messages[:-1]
    ]
    for message, member_id in zip(messages[:-1], member_ids, strict=True):
        assert message.channel_id == "web"
        assert message.session_id == "session-a"
        assert message.payload == {
            "event_type": "team.member",
            "session_id": "session-a",
            "event": {
                "type": "team.member.status_changed",
                "team_id": "team-a",
                "member_id": member_id,
                "new_status": "paused",
            },
        }
    assert messages[-1].payload == {
        "event_type": "chat.processing_status",
        "session_id": "session-a",
        "is_processing": False,
        "is_complete": True,
    }
    assert coordinator.transitions == [("waiting", "session-a", 2)]
    test_logger.info("Gateway published five member states and the session terminal independently of its observer")


@pytest.mark.asyncio
async def test_pause_completion_pushes_members_when_a_history_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = TeamSessionLifecycleAdapter(coordinator=_RecordingCoordinator(), team_manager=_RecordingTeamManager())
    write_history = AsyncMock(side_effect=[OSError("history unavailable"), None])
    push_event = AsyncMock(return_value=True)
    monkeypatch.setattr(lifecycle_module, "run_history_io", write_history)
    monkeypatch.setattr(adapter, "_push_pause_event", push_event)
    monkeypatch.setattr(
        lifecycle_module,
        "get_session_metadata_async",
        AsyncMock(return_value={"team_name": "team-a"}),
    )
    members = [{"member_id": "writer", "status": "paused"}, {"member_id": "reviewer", "status": "paused"}]
    monkeypatch.setattr(TeamMonitorHandler, "get_member_list_from_db", AsyncMock(return_value=members))

    await adapter.publish_pause_completed("session-a", generation=2, channel_id="web")

    assert write_history.await_count == 2
    events = [call.args[1] for call in push_event.await_args_list]
    assert [event["event"]["member_id"] for event in events[:-1]] == ["writer", "reviewer"]
    assert events[-1]["event_type"] == "chat.processing_status"
    assert "Failed to record paused Team member: session_id=session-a member_id=writer" in caplog.text
    test_logger.info("Both paused members and the session terminal were pushed after a history write failed")


@pytest.mark.asyncio
async def test_pause_completion_survives_member_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _RecordingTeamManager()
    adapter = TeamSessionLifecycleAdapter(coordinator=_RecordingCoordinator(), team_manager=manager)
    push_event = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_push_pause_event", push_event)
    monkeypatch.setattr(
        lifecycle_module,
        "get_session_metadata_async",
        AsyncMock(return_value={"team_name": "team-a"}),
    )
    monkeypatch.setattr(TeamMonitorHandler, "get_member_list_from_db", AsyncMock(side_effect=OSError("database")))

    await adapter.publish_pause_completed("session-a", generation=2, channel_id="web")

    push_event.assert_awaited_once_with(
        "session-a",
        {
            "event_type": "chat.processing_status",
            "session_id": "session-a",
            "is_processing": False,
            "is_complete": True,
        },
        channel_id="web",
    )
    assert "Failed to synchronize paused Team members: session_id=session-a" in caplog.text
    test_logger.info("Session pause completion remained deliverable after the member lookup failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["get_instance", "send_push"])
async def test_pause_completion_falls_back_to_observer_when_push_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    manager = TeamManager()
    observer: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    manager.add_waiter("session-a", "observer", observer, generation=2, attach_only=True)
    server = AsyncMock()
    get_instance = Mock(return_value=server)
    error = RuntimeError("Gateway push unavailable")
    if failure == "get_instance":
        get_instance.side_effect = error
    else:
        server.send_push.side_effect = error
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", get_instance)
    monkeypatch.setattr(lifecycle_module, "get_session_metadata_async", AsyncMock(return_value={}))
    adapter = TeamSessionLifecycleAdapter(coordinator=_RecordingCoordinator(), team_manager=manager)

    await adapter.publish_pause_completed("session-a", generation=2, channel_id="web")

    get_instance.assert_called_once_with()
    assert observer.get_nowait() == {
        "event_type": "chat.processing_status",
        "session_id": "session-a",
        "is_processing": False,
        "is_complete": True,
    }
    assert observer.empty()
    assert "Failed to push Team pause event: session_id=session-a event_type=chat.processing_status" in caplog.text
    test_logger.info("Observer received one pause terminal after Gateway %s failed", failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_pause_completion_pushes_terminal_without_waiters(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    delivered: bool,
) -> None:
    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    manager = TeamManager()
    server = AsyncMock()
    server.send_push.return_value = delivered
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", lambda: server)
    monkeypatch.setattr(lifecycle_module, "get_session_metadata_async", AsyncMock(return_value={}))
    adapter = TeamSessionLifecycleAdapter(coordinator=_RecordingCoordinator(), team_manager=manager)

    await adapter.publish_pause_completed("session-a", generation=None, channel_id="web")

    server.send_push.assert_awaited_once_with(
        {
            "request_id": "",
            "channel_id": "web",
            "session_id": "session-a",
            "payload": {
                "event_type": "chat.processing_status",
                "session_id": "session-a",
                "is_processing": False,
                "is_complete": True,
            },
        },
    )
    if not delivered:
        assert "Team pause completion push not delivered: session_id=session-a" in caplog.text
    test_logger.info("Pause completion server push delivered=%s", delivered)
