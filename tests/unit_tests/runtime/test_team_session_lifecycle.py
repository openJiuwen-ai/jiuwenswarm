# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Team implementation of the Runtime lifecycle port."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime.session.model import SessionLifecycleContext
from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter

test_logger = logging.getLogger("tests.team_session_lifecycle")


class _RecordingTeamManager:
    """Record the round reporter registered by the lifecycle adapter."""

    def __init__(self, *, paused: bool = True, broadcast_error: Exception | None = None) -> None:
        self.reporter: Callable[[str, int, bool], None] | None = None
        self.paused = paused
        self.broadcast_error = broadcast_error
        self.broadcast_events: list[tuple[str, dict[str, Any], str | None]] = []

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

    async def broadcast_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        channel_id: str | None = None,
    ) -> bool:
        self.broadcast_events.append((session_id, event, channel_id))
        if self.broadcast_error is not None:
            raise self.broadcast_error
        return True

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
async def test_pause_session_returns_result_without_broadcasting(paused: bool) -> None:
    manager = _RecordingTeamManager(paused=paused)
    adapter = TeamSessionLifecycleAdapter(
        coordinator=_RecordingCoordinator(),
        team_manager=manager,
    )

    result = await adapter.pause_session(
        SessionLifecycleContext(session_id="session-a", generation=2),
        reason="user_pause",
        channel_id="web",
    )

    assert result is paused
    assert manager.broadcast_events == []
    test_logger.info("Pause result=%s; completion delivery remains Runtime-owned", paused)


@pytest.mark.asyncio
async def test_pause_completion_parks_observers_and_broadcasts_terminal() -> None:
    manager = _RecordingTeamManager()
    coordinator = _RecordingCoordinator()
    adapter = TeamSessionLifecycleAdapter(coordinator=coordinator, team_manager=manager)

    await adapter.publish_pause_completed("session-a", generation=2, channel_id="web")

    assert coordinator.transitions == [("waiting", "session-a", 2)]
    assert manager.broadcast_events == [
        (
            "session-a",
            {
                "event_type": "chat.processing_status",
                "session_id": "session-a",
                "is_processing": False,
                "is_complete": True,
            },
            "web",
        ),
    ]
    test_logger.info("Pause completion parked observers and used the existing Team broadcast")


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_pause_completion_uses_existing_push_fallback_without_waiters(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    delivered: bool,
) -> None:
    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.agents.harness.team import team_manager as team_manager_module
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    manager = TeamManager()
    server = AsyncMock()
    server.send_push.return_value = delivered
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", lambda: server)
    monkeypatch.setattr(team_manager_module.logger, "handlers", [caplog.handler])
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
            "is_complete": False,
        },
    )
    if not delivered:
        assert "waiter-less terminal event not delivered: session_id=session-a" in caplog.text
    test_logger.info("Existing waiter-less push fallback delivered=%s", delivered)
