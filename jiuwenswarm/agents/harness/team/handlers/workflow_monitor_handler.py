# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow Monitor Handler — bridges WorkflowProgressTeamEvent to workflow.updated events.

Consumes raw workflow_progress EventMessage objects from TeamMonitor.workflow_events(),
aggregates state in WorkflowRunState instances, and pushes incremental delta dicts onto
an asyncio.Queue for consumption by the events() async iterator.

Lifecycle mirrors TeamMonitorHandler via BaseMonitorHandler.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from openjiuwen.agent_teams.monitor.team_monitor import TeamMonitor
from jiuwenswarm.agents.harness.team.handlers.base_monitor_handler import BaseMonitorHandler
from jiuwenswarm.agents.harness.team.handlers.workflow_state import (
    WorkflowProgress,
    WorkflowRunState,
)

logger = logging.getLogger(__name__)


class WorkflowMonitorHandler(BaseMonitorHandler):
    """Handler that bridges workflow progress events to the TUI frontend.

    Wraps a TeamMonitor and consumes from monitor.workflow_events(). For each
    raw EventMessage, extracts a WorkflowProgress payload, updates WorkflowRunState
    instances, and emits incremental ``workflow.updated`` event dicts onto an internal
    asyncio.Queue.

    The ``events()`` async iterator consumes from that queue and is intended
    to be read by ``_consume_workflow_events()`` in the gateway channel.
    """

    def __init__(
            self,
            monitor: TeamMonitor,
            session_id: str,
            channel_id: Optional[str] = None,
            initial_runs: Optional[dict[str, WorkflowRunState]] = None,
    ) -> None:
        super().__init__(monitor, session_id)
        self._channel_id = channel_id
        self._runs: dict[str, WorkflowRunState] = dict(initial_runs or {})

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def channel_id(self) -> Optional[str]:
        return self._channel_id

    # ------------------------------------------------------------------
    # Collect loop — consumes monitor.workflow_events()
    # ------------------------------------------------------------------

    async def _collect_events(self) -> None:
        """Drain monitor.workflow_events() and process each raw EventMessage.

        Each event is processed under its own try/except so a single malformed
        event cannot terminate the whole collection loop — otherwise one bad
        event would silently drop all subsequent workflow updates. The outer
        try only guards the monitor stream iteration itself.
        """
        try:
            async for raw_event in self._monitor.workflow_events():
                if not self._running:
                    break
                try:
                    await self._process_event(raw_event)
                except Exception as e:
                    logger.error(
                        "[WorkflowMonitorHandler] 单事件处理失败（已跳过该事件）: "
                        "session_id=%s, error=%s",
                        self._session_id,
                        e,
                        exc_info=True,
                    )
        except Exception as e:
            logger.error(
                "[WorkflowMonitorHandler] 事件收集失败: session_id=%s, error=%s",
                self._session_id,
                e,
            )

    async def _process_event(self, event: Any) -> None:
        """Process one raw EventMessage: extract progress, update state, push delta."""
        progress = self._extract_progress(event)
        if progress is None:
            return

        logger.info(
            "[WF_DBG WorkflowMonitorHandler] received progress: "
            "session_id=%s kind=%s run_id=%s workflow_name=%s "
            "phase=%s label=%s agent_id=%s outcome_len=%s",
            self._session_id,
            progress.kind,
            progress.run_id,
            progress.workflow_name,
            progress.phase,
            progress.label,
            progress.agent_id,
            len(progress.outcome) if isinstance(progress.outcome, str) else 0,
        )

        run_state = self._get_or_create_run(progress)
        if run_state is None:
            logger.warning(
                "[WF_DBG WorkflowMonitorHandler] progress event missing run_id, "
                "session_id=%s kind=%s — ignored",
                self._session_id,
                progress.kind,
            )
            return

        delta = run_state.apply(progress)
        if delta is None:
            logger.debug(
                "[WF_DBG WorkflowMonitorHandler] kind=%s produced no delta (ignored), "
                "session_id=%s workflow_id=%s",
                progress.kind,
                self._session_id,
                run_state.id or "(pending)",
            )
            return

        updated_event = self._build_updated_event(delta)

        wf_delta = updated_event.get("workflow", {})
        logger.info(
            "[WF_DBG WorkflowMonitorHandler] _process_event → queue: "
            "session_id=%s kind=%s workflow_id=%s workflow_name=%s status=%s "
            "phases_count=%d agent_count=%d completed_agent_count=%d",
            self._session_id,
            progress.kind,
            wf_delta.get("id", ""),
            wf_delta.get("name", ""),
            wf_delta.get("status", ""),
            len(wf_delta.get("phases", [])),
            wf_delta.get("agent_count", 0),
            wf_delta.get("completed_agent_count", 0),
        )

        await self._event_queue.put(updated_event)
        self._persist()

    # ------------------------------------------------------------------
    # Checkpoint persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        try:
            from jiuwenswarm.server.runtime.agent_adapter.team_helpers import persist_workflow_runs
            persist_workflow_runs(self._runs, self._session_id)
        except Exception as e:
            logger.warning("[WorkflowMonitorHandler] checkpoint persist failed: %s", e)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def get_workflow_snapshot(self) -> list[dict[str, Any]]:
        """Return a list of all workflow run dicts for ``command.workflows``."""
        return [run.to_workflow_run_dict() for run in self._runs.values()]

    def finalize_pending_runs(self, terminal_status: str = "stopped") -> None:
        """Mark every non-terminal run as terminal and persist the result.

        Called on non-resumable teardown (session cancel / stop / destroy) so a
        torn-down runtime never leaves a workflow stuck in ``running`` on the
        checkpoint — once the runtime is gone no further ``workflow.updated``
        events can arrive, so a restored snapshot must show a terminal status.
        """
        changed = False
        for run in self._runs.values():
            if run.finalize_if_running(terminal_status):
                changed = True
        if changed:
            self._persist()

    def get_run_states(self) -> dict[str, WorkflowRunState]:
        """Return a shallow copy of in-memory workflow run states."""
        return dict(self._runs)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_progress(self, event: Any) -> Optional[WorkflowProgress]:
        """Extract a WorkflowProgress payload from a raw EventMessage."""
        if hasattr(event, "get_payload") and callable(event.get_payload):
            payload = event.get_payload()
        elif hasattr(event, "payload"):
            payload = event.payload
        else:
            return None

        if payload is None:
            return None

        if isinstance(payload, dict):
            return WorkflowProgress(**payload)

        if hasattr(payload, "model_dump") and callable(payload.model_dump):
            try:
                return WorkflowProgress(**payload.model_dump())
            except Exception:
                logger.warning(
                    "[WorkflowMonitorHandler] Failed to convert team event payload via model_dump()",
                    exc_info=True,
                )
                return None

        try:
            return WorkflowProgress(
                kind=payload.kind if hasattr(payload, "kind") else "unknown",
                run_id=getattr(payload, "run_id", None),
                workflow_name=getattr(payload, "workflow_name", None),
                description=getattr(payload, "description", None),
                phase=getattr(payload, "phase", None),
                label=getattr(payload, "label", None),
                prompt=getattr(payload, "prompt", None),
                model=getattr(payload, "model", None),
                outcome=getattr(payload, "outcome", None),
                text=getattr(payload, "text", None),
                phases=getattr(payload, "phases", None),
                correlation_id=getattr(payload, "correlation_id", None),
                node_type=getattr(payload, "node_type", None),
                agent_id=getattr(payload, "agent_id", None),
                answer=getattr(payload, "answer", None),
                tokens=getattr(payload, "tokens", None),
                budget=getattr(payload, "budget", None),
                phase_type=getattr(payload, "phase_type", None),
                nested_phase=getattr(payload, "nested_phase", None),
                parent_phase=getattr(payload, "parent_phase", None),
            )
        except Exception:
            logger.warning("[WorkflowMonitorHandler] Failed to extract progress from event")
            return None

    def _get_or_create_run(
            self,
            progress: WorkflowProgress,
    ) -> WorkflowRunState | None:
        """Find an existing WorkflowRunState by run_id, or create a new one.

        ``run_id`` is required — every progress event carries one, set by
        SwarmflowTool at launch. Returns ``None`` if ``run_id`` is missing.
        """
        run_id = progress.run_id
        if not run_id:
            return None

        # Direct lookup by run_id
        if run_id in self._runs:
            return self._runs[run_id]

        # New run — register under its run_id
        new_run = WorkflowRunState()
        new_run.id = run_id
        self._runs[run_id] = new_run
        return new_run

    def _build_updated_event(
            self,
            delta: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "event_type": "workflow.updated",
            "session_id": self._session_id,
            "workflow": delta,
        }
