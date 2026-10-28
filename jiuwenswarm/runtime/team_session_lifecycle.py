# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team implementation of the Runtime Session lifecycle port."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from jiuwenswarm.runtime.session.model import (
    ExecutionLifecycleContext,
    SessionLifecycleContext,
)

logger = logging.getLogger(__name__)


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

    async def broadcast_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        channel_id: str | None = None,
    ) -> bool:
        """Deliver one Team event to the Session's waiters or server push."""
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
    ) -> None:
        """Pause the matching Team generation."""
        paused = await self._team_manager.pause_session_runtime(
            context.session_id,
            reason,
            generation=context.generation,
        )
        if paused:
            # This pause tears the runtime down first, so the later agent-level
            # team interrupt finds nothing to pause and skips its terminal
            # frame.  Emit it here, otherwise a round with no surviving
            # foreground stream (e.g. an ask_user resume) never reports
            # is_processing=false and the session keeps spinning.
            # Delivery failure must not abort the caller's remaining
            # interrupt and coordinator cleanup.
            try:
                await self._team_manager.broadcast_event(
                    context.session_id,
                    {
                        "event_type": "chat.processing_status",
                        "session_id": context.session_id,
                        "is_processing": False,
                        "is_complete": True,
                    },
                    channel_id=channel_id,
                )
            except Exception as exc:
                logger.warning(
                    "[TeamSessionLifecycle] pause terminal frame delivery failed: "
                    "session_id=%s error=%s",
                    context.session_id,
                    exc,
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

    def is_running(self, context: SessionLifecycleContext) -> bool:
        """Return whether the matching Team generation has active work."""
        return self._team_manager.is_generation_running(
            context.session_id,
            context.generation,
        )


__all__ = ["TeamSessionLifecycleAdapter"]
