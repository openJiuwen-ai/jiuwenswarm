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
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata_async

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

    def has_waiters(self, session_id: str) -> bool:
        """Return whether any Session observers can receive an in-process event."""
        ...

    async def broadcast_event(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        channel_id: str | None = None,
    ) -> bool:
        """Deliver a Session event to registered consumers."""
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
        """Park surviving observers and publish persisted member and session pause states."""
        if generation is not None:
            self._coordinator.mark_session_waiting_for_input(session_id, generation)
        try:
            await self._publish_paused_members(session_id, channel_id=channel_id)
        except Exception:
            logger.warning("Failed to synchronize paused Team members: session_id=%s", session_id, exc_info=True)

        logger.info(
            "[TeamProcessingStatus] generated: source=pause_completed channel_id=%s "
            "session_id=%s generation=%s is_processing=False is_complete=True",
            channel_id,
            session_id,
            generation,
        )
        terminal_event = {
            "event_type": "chat.processing_status",
            "session_id": session_id,
            "is_processing": False,
            "is_complete": True,
        }
        delivered = await self._push_pause_event(session_id, terminal_event, channel_id=channel_id)
        if not delivered and self._team_manager.has_waiters(session_id):
            # In-process consumers can outlive a Team execution without a connected Gateway.
            delivered = await self._team_manager.broadcast_event(session_id, terminal_event, channel_id=channel_id)
        if not delivered:
            logger.warning("Team pause completion push not delivered: session_id=%s", session_id)

    async def _publish_paused_members(self, session_id: str, *, channel_id: str | None) -> None:
        """Record paused member states for replay and push them to live clients."""
        from openjiuwen.agent_teams.schema.status import MemberStatus

        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _persist_team_history_event

        metadata = await get_session_metadata_async(session_id, enable_writeback=False)
        team_name = str(metadata.get("team_name") or "").strip()
        if not team_name:
            return

        members = await TeamMonitorHandler.get_member_list_from_db(team_name, exclude_leader=True)
        for member in members or ():
            if member["status"] != MemberStatus.PAUSED.value:
                continue
            event = {
                "event_type": "team.member",
                "session_id": session_id,
                "event": {
                    "type": "team.member.status_changed",
                    "team_id": team_name,
                    "member_id": member["member_id"],
                    "new_status": member["status"],
                },
            }
            try:
                await run_history_io(_persist_team_history_event, channel_id, session_id, event)
            except Exception:
                logger.warning(
                    "Failed to record paused Team member: session_id=%s member_id=%s",
                    session_id,
                    member["member_id"],
                    exc_info=True,
                )
            await self._push_pause_event(session_id, event, channel_id=channel_id)

    @staticmethod
    async def _push_pause_event(
        session_id: str,
        event: dict[str, Any],
        *,
        channel_id: str | None,
    ) -> bool:
        """Send a pause event through the Gateway and return whether it was delivered."""
        try:
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

            server = AgentWebSocketServer.get_instance()
            return await server.send_push(
                {
                    "request_id": "",
                    "channel_id": (channel_id or "").strip() or "web",
                    "session_id": session_id,
                    "payload": event,
                },
            )
        except Exception:
            logger.warning(
                "Failed to push Team pause event: session_id=%s event_type=%s",
                session_id,
                event.get("event_type"),
                exc_info=True,
            )
            return False

    def is_running(self, context: SessionLifecycleContext) -> bool:
        """Return whether the matching Team generation has active work."""
        return self._team_manager.is_generation_running(
            context.session_id,
            context.generation,
        )


__all__ = ["TeamSessionLifecycleAdapter"]
