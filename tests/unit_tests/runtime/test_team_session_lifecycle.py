# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Team implementation of the Runtime lifecycle port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from jiuwenswarm.runtime.session.model import SessionLifecycleContext
from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter


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
async def test_pause_session_broadcasts_terminal_frame_after_successful_pause() -> None:
    manager = _RecordingTeamManager(paused=True)
    adapter = TeamSessionLifecycleAdapter(
        coordinator=_RecordingCoordinator(),
        team_manager=manager,
    )

    await adapter.pause_session(
        SessionLifecycleContext(session_id="session-a", generation=2),
        reason="user_pause",
        channel_id="web",
    )

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


@pytest.mark.asyncio
async def test_pause_session_skips_terminal_frame_when_nothing_paused() -> None:
    manager = _RecordingTeamManager(paused=False)
    adapter = TeamSessionLifecycleAdapter(
        coordinator=_RecordingCoordinator(),
        team_manager=manager,
    )

    await adapter.pause_session(
        SessionLifecycleContext(session_id="session-a", generation=2),
        reason="user_pause",
    )

    assert manager.broadcast_events == []


@pytest.mark.asyncio
async def test_pause_session_swallows_terminal_frame_delivery_failure() -> None:
    manager = _RecordingTeamManager(paused=True, broadcast_error=RuntimeError("push failed"))
    adapter = TeamSessionLifecycleAdapter(
        coordinator=_RecordingCoordinator(),
        team_manager=manager,
    )

    await adapter.pause_session(
        SessionLifecycleContext(session_id="session-a", generation=2),
        reason="user_pause",
        channel_id="web",
    )

    assert len(manager.broadcast_events) == 1
