# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Team implementation of the Runtime lifecycle port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter


class _RecordingTeamManager:
    """Record the round reporter registered by the lifecycle adapter."""

    def __init__(self) -> None:
        self.reporter: Callable[[str, int, bool], None] | None = None

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
