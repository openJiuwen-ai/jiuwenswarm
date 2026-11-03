# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow Monitor Handler — bridges WorkflowProgressTeamEvent to workflow.updated events.

Consumes raw workflow_progress EventMessage objects from TeamMonitor.workflow_events(),
aggregates state in WorkflowRunState instances, and pushes incremental delta dicts onto
an asyncio.Queue for consumption by the events() async iterator.

Lifecycle mirrors TeamMonitorHandler via BaseMonitorHandler.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

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

    # Activity-only deltas (agent_activity) coalesce per run in this window:
    # the first delta of a run opens the window, later activity deltas merge
    # into the pending buffer, and a non-activity delta for that run flushes
    # it early. Keeps a burst of tool activity from spamming the frontend
    # with one workflow.updated per tool call.
    _ACTIVITY_BATCH_WINDOW_SEC = 0.3

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
        # Session-wide (leader-shared) budget snapshot, updated from each
        # progress event's ``budget`` field and persisted to session metadata.
        self._session_budget: Optional[dict] = None
        # team.task/team.member conversion dedup. Session-scoped on purpose:
        # the consumer loop is cancelled on team pause and restarted on wake,
        # and a resume relaunch REPLAYS the cached prefix (completed agents
        # re-emit started+completed) — a per-loop table would re-emit the
        # replay and double-count finished work on the task board. Public:
        # the consumer (_consume_workflow_events) owns the emission contract
        # and reads them cross-class.
        self.seen_phase: dict[str, str] = {}
        self.seen_agent: dict[str, str] = {}
        self.spawned_members: set[str] = set()
        # Mirrored activity rows per member (tool_call_id/type keys), owned by
        # the consumer for the same replay-dedup reason as seen_agent.
        self.seen_activity: dict[str, set[tuple[str, str]]] = {}
        # Activity-only delta coalescing state, keyed by run id. A run whose
        # first activity delta has been buffered sits in ``_activity_flush_tasks``
        # (a flush task is scheduled); the merged workflow delta accumulates in
        # ``_pending_activity_workflow``. A non-activity delta for the run flushes
        # the buffer immediately (merged into its own push).
        self._pending_activity_workflow: dict[str, dict[str, Any]] = {}
        self._activity_flush_tasks: dict[str, asyncio.Task] = {}

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

        # Track the session-wide budget snapshot from each event (leader-shared).
        if progress.budget is not None:
            self._session_budget = progress.budget

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

        # Activity-only deltas coalesce per run in a 300ms window: buffer the
        # delta and let the first delta's flush task push the merged result.
        # A non-activity delta for that run flushes the pending buffer early
        # (merged into its own push) so status changes are never delayed.
        if progress.kind == "agent_activity":
            self._buffer_activity_delta(run_state.id, delta)
            return
        delta = self._flush_pending_activity(run_state.id, delta)

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
    # Activity-only delta batching (per run, 300ms window)
    # ------------------------------------------------------------------

    def _buffer_activity_delta(self, run_id: str, delta: dict[str, Any]) -> None:
        """Buffer an activity-only delta for ``run_id``, scheduling the flush.

        The first buffered delta of a run opens the window (an asyncio task
        flushes 300ms later); later deltas just merge into the same buffer.
        Deltas are merged by phase id — each activity delta serializes the
        whole touched phase, so the newest phase dict is complete.
        """
        if run_id not in self._activity_flush_tasks:
            self._activity_flush_tasks[run_id] = asyncio.get_running_loop().create_task(
                self._flush_activity_delta(run_id)
            )
        self._pending_activity_workflow[run_id] = self._merge_workflow_delta(
            self._pending_activity_workflow.get(run_id), delta,
        )

    def _flush_pending_activity(self, run_id: str, delta: dict[str, Any]) -> dict[str, Any]:
        """Flush ``run_id``'s buffered activity, merged into ``delta``.

        A non-activity delta for a run with a pending window flushes early so
        status changes are not delayed by the window; the buffered activity
        rides the same push. Returns the (possibly merged) delta.
        """
        flush_task = self._activity_flush_tasks.pop(run_id, None)
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
        pending = self._pending_activity_workflow.pop(run_id, None)
        if pending is not None:
            return self._merge_workflow_delta(pending, delta)
        return delta

    @staticmethod
    def _merge_workflow_delta(
        base: Optional[dict[str, Any]], new: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge two workflow delta dicts: newer top-level fields win, phases union by id."""
        if base is None:
            return new
        phases_by_id: dict[str, dict[str, Any]] = {}
        for phase in base.get("phases", []) or []:
            if isinstance(phase, dict) and phase.get("id"):
                phases_by_id[phase["id"]] = phase
        for phase in new.get("phases", []) or []:
            if isinstance(phase, dict) and phase.get("id"):
                phases_by_id[phase["id"]] = phase
        merged = {**base, **new}
        if phases_by_id:
            merged["phases"] = list(phases_by_id.values())
        return merged

    async def _flush_activity_delta(self, run_id: str) -> None:
        """Flush ``run_id``'s buffered activity after the window closes.

        Runs as the scheduled asyncio task. Whichever of this task or a
        status delta reaches the buffer first claims it; the other finds it
        already gone and no-ops (both run on one event loop, and the buffer
        read+clear below is synchronous, so no interleaving is possible).
        """
        await asyncio.sleep(self._ACTIVITY_BATCH_WINDOW_SEC)
        self._activity_flush_tasks.pop(run_id, None)
        workflow = self._pending_activity_workflow.pop(run_id, None)
        if workflow is None:
            return
        updated_event = self._build_updated_event(workflow)
        await self._event_queue.put(updated_event)
        self._persist()

    def _flush_all_pending_activity(self) -> None:
        """Flush every pending activity buffer synchronously (handler teardown).

        Pushes buffered activity before stop() so the last window of a run is
        never lost on teardown. Used by stop(); status-delta early flushes
        already emptied the buffers, so this is normally a no-op.
        """
        for run_id, task in list(self._activity_flush_tasks.items()):
            if not task.done():
                task.cancel()
        self._activity_flush_tasks.clear()
        for run_id, workflow in list(self._pending_activity_workflow.items()):
            self._pending_activity_workflow.pop(run_id, None)
            self._event_queue.put_nowait(self._build_updated_event(workflow))
            self._persist()

    async def stop(self) -> None:
        """Stop monitoring, flushing any pending activity window first."""
        self._flush_all_pending_activity()
        await super().stop()

    # ------------------------------------------------------------------
    # Checkpoint persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        try:
            from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
                persist_workflow_runs,
            )
            # runs + session_budget must land in ONE read-modify-write: two
            # separate persists each cache_bust-read the disk before the
            # other's queued write is flushed, and the second full-file
            # replace reverts the first's workflow_runs (lost update that
            # froze the checkpoint at a stale pre-terminal state).
            persist_workflow_runs(
                self._runs, self._session_id, session_budget=self._session_budget
            )
        except Exception as e:
            logger.warning("[WorkflowMonitorHandler] checkpoint persist failed: %s", e)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def get_workflow_snapshot(self) -> list[dict[str, Any]]:
        """Return a list of all workflow run dicts for ``command.workflows``."""
        return [run.to_workflow_run_dict() for run in self._runs.values()]

    def finalize_pending_runs(self, *, disposition: Literal["stop", "pause"] = "stop") -> None:
        """Finalize every non-terminal run by real state, not one-size stopped.

        Called on non-resumable teardown. ``disposition="stop"`` (user
        termination) stamps non-terminal runs to ``stopped``; ``disposition=
        "pause"`` (disconnect/crash reclaim) parks them to ``paused`` so the
        journal cache prefix stays resumable on cold start.
        """
        changed = False
        for run in self._runs.values():
            if run.is_terminal or run.status == "paused":
                continue  # terminal / already parked → nothing to do
            if disposition == "pause":
                changed = run.pause_if_running() or changed
            else:
                changed = run.finalize_if_running("stopped") or changed
        if changed:
            self._persist()

    def get_run_states(self) -> dict[str, WorkflowRunState]:
        """Return a shallow copy of in-memory workflow run states."""
        return dict(self._runs)

    async def stop_run(self, run_id: str) -> bool:
        """Stamp a paused run ``stopped`` and push the delta like an engine stop.

        A paused run has already unwound, so no WORKFLOW_STOPPED will ever
        arrive from the engine; the tree-view stop must synthesize it here or
        the frontend keeps the paused card until a reload. Only paused runs:
        an active run's stop is announced by the engine itself while it
        unwinds, and stamping it here too would double-finalize. Returns False
        when the run is unknown or not paused.
        """
        run = self._runs.get(run_id)
        if run is None or run.status != "paused":
            return False
        delta = run.apply(WorkflowProgress(kind="workflow_stopped", run_id=run_id))
        if delta is not None:
            await self._event_queue.put(self._build_updated_event(delta))
        self._persist()
        return True

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
                agent_type=getattr(payload, "agent_type", None),
                agent_template=getattr(payload, "agent_template", None),
                display_name=getattr(payload, "display_name", None),
                answer=getattr(payload, "answer", None),
                activity_type=getattr(payload, "activity_type", None),
                tool_call_id=getattr(payload, "tool_call_id", None),
                tool_name=getattr(payload, "tool_name", None),
                content=getattr(payload, "content", None),
                tokens=getattr(payload, "tokens", None),
                budget=getattr(payload, "budget", None),
                workflow_budget=getattr(payload, "workflow_budget", None),
                budget_exhausted_scope=getattr(payload, "budget_exhausted_scope", None),
                relaunch_kind=getattr(payload, "relaunch_kind", None),
                script_path=getattr(payload, "script_path", None),
                phase_type=getattr(payload, "phase_type", None),
                nested_phase=getattr(payload, "nested_phase", None),
                parent_phase=getattr(payload, "parent_phase", None),
                phase_iteration=getattr(payload, "phase_iteration", None),
                parent_session_id=getattr(payload, "parent_session_id", None),
                member_name=getattr(payload, "member_name", None),
                verify_reviewers=getattr(payload, "verify_reviewers", None),
                verify_verdict=getattr(payload, "verify_verdict", None),
                verify_threshold=getattr(payload, "verify_threshold", None),
                verify_votes=getattr(payload, "verify_votes", None),
                verify_reviewer_labels=getattr(payload, "verify_reviewer_labels", None),
                verify_reviewer_roles=getattr(payload, "verify_reviewer_roles", None),
                verify_id=getattr(payload, "verify_id", None),
                cache_tokens=getattr(payload, "cache_tokens", None),
                token_input=getattr(payload, "token_input", None),
                token_output=getattr(payload, "token_output", None),
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
