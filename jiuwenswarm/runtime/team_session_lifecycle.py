# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team implementation of the Runtime Session lifecycle port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from jiuwenswarm.runtime.session.model import (
    ExecutionLifecycleContext,
    SessionLifecycleContext,
)


class _TeamLifecycleManager(Protocol):
    """Define the Team runtime operations required by the lifecycle adapter."""

    def set_round_state_reporter(
        self,
        reporter: Callable[[str, int, bool], None] | None,
    ) -> None:
        """Attach the Runtime observer for active versus parked Team rounds."""
        ...

    def remove_waiter(
        self,
        session_id: str,
        request_id: str,
        *,
        generation: int | None = None,
    ) -> None:
        """Remove one response waiter from the matching Session generation."""
        ...

    async def pause_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        generation: int | None = None,
    ) -> bool:
        """Pause runtime resources owned by the matching Session generation."""
        ...

    async def stop_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        stop_runner: bool = True,
        generation: int | None = None,
    ) -> bool:
        """Stop runtime resources owned by the matching Session generation."""
        ...

    async def broadcast_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        channel_id: str | None = None,
    ) -> bool:
        """Deliver a Session event through observers or the waiter-less fallback."""
        ...

    def is_generation_running(self, session_id: str, generation: int) -> bool:
        """Return whether the matching Session generation has active work."""
        ...


class _RoundStateCoordinator(Protocol):
    """Define Coordinator state transitions reported by Team rounds."""

    def resume_waiting_for_input(self, session_id: str, generation: int) -> None:
        """Mark parked persistent executions running for a new Team round."""
        ...

    def mark_session_waiting_for_input(
        self,
        session_id: str,
        generation: int,
    ) -> None:
        """Park persistent executions after a Team round becomes idle."""
        ...


class TeamSessionLifecycleAdapter:
    """Map Runtime lifecycle orchestration to generation-aware Team resources."""

    def __init__(
        self,
        coordinator: _RoundStateCoordinator,
        team_manager: _TeamLifecycleManager | None = None,
    ) -> None:
        if team_manager is None:
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_manager = get_team_manager()
        self._team_manager = team_manager
        self._coordinator = coordinator
        self._team_manager.set_round_state_reporter(self._report_round_state)

    def _report_round_state(
        self,
        session_id: str,
        generation: int,
        running: bool,
    ) -> None:
        """Reflect Team round activity on persistent Runtime executions."""
        if running:
            self._coordinator.resume_waiting_for_input(session_id, generation)
        else:
            self._coordinator.mark_session_waiting_for_input(session_id, generation)

    async def remove_response_waiter(
        self,
        context: ExecutionLifecycleContext,
        *,
        reason: str,
    ) -> None:
        """Remove only the waiter owned by the supplied execution."""
        del reason
        self._team_manager.remove_waiter(
            context.session.session_id,
            context.request_id,
            generation=context.session.generation,
        )

    async def pause_session(
        self,
        context: SessionLifecycleContext,
        *,
        reason: str,
        channel_id: str | None = None,
    ) -> bool:
        """Return whether the matching Team generation was paused or drained."""
        del channel_id
        return await self._team_manager.pause_session_runtime(
            context.session_id,
            reason,
            generation=context.generation,
        )

    async def stop_session(
        self,
        context: SessionLifecycleContext,
        *,
        reason: str,
    ) -> None:
        """Stop the matching Team generation."""
        await self._team_manager.stop_session_runtime(
            context.session_id,
            reason,
            generation=context.generation,
        )

    async def publish_pause_completed(
        self,
        session_id: str,
        *,
        generation: int | None,
        channel_id: str | None = None,
    ) -> None:
        """Park surviving observers and publish the authoritative pause terminal."""
        if generation is not None:
            self._coordinator.mark_session_waiting_for_input(session_id, generation)
        await self._team_manager.broadcast_event(
            session_id,
            {
                "event_type": "chat.processing_status",
                "session_id": session_id,
                "is_processing": False,
                "is_complete": True,
            },
            channel_id=channel_id,
        )

    def is_running(self, context: SessionLifecycleContext) -> bool:
        """Return whether the matching Team generation has active work."""
        return self._team_manager.is_generation_running(
            context.session_id,
            context.generation,
        )


__all__ = ["TeamSessionLifecycleAdapter"]
