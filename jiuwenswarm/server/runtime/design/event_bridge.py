# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Engine progress events → designer run record mutation + update push.

The bridge keeps the run record's live fields (``status`` / ``current_node_ids``)
in sync while a trigger executes and forwards every mutation to the caller's
``on_update`` callback so the gateway adapter can push
``DESIGNER_RUN_UPDATED`` with the same payload shape the frontend consumes
today. Final, authoritative state is written by :class:`RunCoordinator` after
the engine call returns.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from openjiuwen.agent_teams.workflow.engine.progress import (
    ProgressKind,
    WorkflowProgressEvent,
)

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
)
from jiuwenswarm.server.runtime.designer.activity import (
    emit_run_activity,
    stage_text_for_node,
)

logger = logging.getLogger(__name__)


class DesignEventBridge:
    """Design-layer listener over the swarmflow execution surface.

    Two inbound channels, one outbound push:

    * ``emit`` — engine ``WorkflowProgressEvent`` stream: live status fields
      (``status`` / ``current_node_ids``) folded into the run record.
    * ``on_node_result`` — backend-emitted node-result events (the swarmflow
      layer's only write channel): the design layer listens and persists each
      node's output to the Store the moment it exists (the old executor's
      per-node writeback contract — a crash/cancel never loses a completed
      node). Every trigger type (single / full / cascade) writes the same
      slots: the previous current output is archived into ``history`` (the
      old-version log) and then ``text``/``output_ref``/``output_refs`` are
      overwritten atomically — there is no candidate stage anymore.

    Both mutations forward to the caller's ``on_update`` so the gateway
    adapter pushes ``DESIGNER_RUN_UPDATED`` / ``DESIGNER_NODE_UPDATED`` with
    the payload shape the frontend consumes.
    """

    def __init__(
        self,
        run: dict[str, Any],
        on_update: Callable[..., None] | None,
        *,
        store: Any = None,
        graph: dict[str, Any] | None = None,
    ) -> None:
        self._run = run
        self._on_update = on_update
        self._store = store
        # Node metadata (label/pipeline) for the node-opening activity pair.
        self._graph = graph if isinstance(graph, dict) else {}
        # This trigger's node results, keyed by node id (the media guarantee
        # builds a member's receipt from it).
        self._produced: dict[str, dict[str, Any]] = {}
        # Last member-tool failure per node. The worker's own end-of-turn
        # error only says no receipt arrived; this keeps the cause.
        self._member_errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # swarmflow-layer event: a node's result exists
    # ------------------------------------------------------------------
    def on_node_result(self, node_id: str, produced: dict[str, Any]) -> None:
        """Persist one node's result (per-node Store writeback).

        Old-version log: the node's CURRENT output is archived into
        ``history`` before the overwrite, so ``history[-1]`` is always the
        true previous version. A single-artifact submit also lands in
        ``output_refs`` — list-first downstream readers must never see a
        stale set from an earlier version.
        """
        state = (self._run.setdefault("node_states", {}).setdefault(
            node_id, {"status": "pending", "started_at": None, "completed_at": None}
        ))
        state["status"] = "completed"
        state["completed_at"] = _now_ms()
        # A successful rerun invalidates a previous failure's reason — error
        # is write-only on the failed paths otherwise and would stick forever.
        state.pop("error", None)
        self._produced[node_id] = dict(produced)
        text = str(produced.get("text") or "")
        ref = produced.get("output_ref")
        refs = produced.get("output_refs")
        if text or ref or refs:
            old_text = state.get("text")
            old_ref = state.get("output_ref")
            if old_text or old_ref:
                entry: dict[str, Any] = {"ts": _now_ms(), "text": str(old_text or "")}
                if old_ref:
                    entry["output_ref"] = old_ref
                state.setdefault("history", []).append(entry)
            if text:
                state["text"] = text
            if ref:
                state["output_ref"] = ref
                if not refs:
                    refs = [ref]
            if refs:
                state["output_refs"] = list(refs)
        self._persist_run()
        self._push(node_id)

    def produced_for(self, node_id: str) -> dict[str, Any] | None:
        """The result a node wrote during THIS trigger, or None."""
        produced = self._produced.get(node_id)
        return dict(produced) if produced is not None else None

    def note_member_error(self, node_id: str, error: str | None) -> None:
        """Record a member tool's failure for a node; ``None`` clears it."""
        if error:
            self._member_errors[node_id] = str(error)[:2000]
        else:
            self._member_errors.pop(node_id, None)

    def member_error_for(self, node_id: str) -> str | None:
        """The last member-tool failure recorded for a node during THIS trigger."""
        return self._member_errors.get(node_id)

    # ------------------------------------------------------------------
    # engine progress events
    # ------------------------------------------------------------------
    def emit(self, event: WorkflowProgressEvent) -> None:
        node_id = str(getattr(event, "label", "") or "")
        states = self._run.setdefault("node_states", {})
        if event.kind == ProgressKind.AGENT_STARTED and node_id:
            state = states.setdefault(node_id, _pending_state())
            state["status"] = "running"
            state["started_at"] = _now_ms()
            # Restart clears the previous failure's reason so it does not
            # display alongside the new run in progress.
            state.pop("error", None)
            # Parallel waves keep every started node visible (the old
            # executor published the full ready-set; overwriting to the last
            # starter hides the wave).
            current = self._run.setdefault("current_node_ids", [])
            if node_id not in current:
                current.append(node_id)
            self._run["status"] = "running"
            # Original executor contract (52469f4c executor.py:2250): every
            # node opens its run-record activity with the starting pair —
            # thinking "starting <label>" then the pipeline stage line — so
            # the node card shows what it is about to do.
            self._emit_start_activity(node_id)
            self._persist_run()
            self._push(node_id)
        elif event.kind == ProgressKind.AGENT_COMPLETED and node_id:
            state = states.setdefault(node_id, _pending_state())
            state["status"] = "completed"
            state["completed_at"] = _now_ms()
            # The engine-side completion path clears stale error too (same
            # contract as on_node_result: success invalidates old failures).
            state.pop("error", None)
            self._detach_current(node_id)
            self._persist_run()
            self._push(node_id)
        elif event.kind == ProgressKind.AGENT_FAILED and node_id:
            state = states.setdefault(node_id, _pending_state())
            state["status"] = "failed"
            state["completed_at"] = _now_ms()
            message = str(getattr(event, "message", "") or "")
            # Keep a more specific error already on record (e.g. the host
            # media-guarantee reason written before the engine's generic
            # "backend declined" frame lands).
            if message and not state.get("error"):
                state["error"] = message[:2000]
            self._detach_current(node_id)
            self._persist_run()
            self._push(node_id)
        elif event.kind == ProgressKind.AGENT_ACTIVITY and node_id:
            # Live worker activity (agent-core SwarmflowActivityRail, one
            # frame per worker tool call, message="tool: <name>"). The old
            # executor wrote these into run.node_states[n].activity_tail —
            # the DesignerActivityPeek surface; the migration dropped the
            # branch and the frames died here.
            message = str(getattr(event, "message", "") or "")
            if message:
                tool = (
                    message[len("tool: "):].strip()
                    if message.startswith("tool: ")
                    else message
                )
                self._emit_activity(
                    node_id,
                    kind=ACTIVITY_KIND_TOOL_CALL,
                    text=tool,
                    tool=tool,
                )

    def run_snapshot(self) -> dict[str, Any]:
        """The current run record (shared reference the coordinator owns)."""
        return self._run

    def node_activity_sink(self, node_id: str) -> Callable[[str, str, str], None] | None:
        """Worker-tool activity emitter for one node (the old ctx.emit_activity).

        Manifest-package tools resolve this through the design context and
        push handler-level stage lines ("calling image model") — they have no
        engine event of their own, so the write goes straight through the
        bridge into the run record + throttled push.
        """

        def emit(kind: str, text: str, tool: str = "") -> None:
            self._emit_activity(node_id, kind=kind, text=text, tool=tool)

        return emit

    def mark_node_failed(self, node_id: str, error: str) -> None:
        """Host-side guarantee failure: record + push, no engine event involved.

        Used by the media-guarantee backend when a node's product does not
        satisfy its required media family (the old executor's FAIL path).
        """
        state = (self._run.setdefault("node_states", {}).setdefault(
            node_id, {"status": "pending", "started_at": None, "completed_at": None}
        ))
        state["status"] = "failed"
        state["completed_at"] = _now_ms()
        state["error"] = str(error or "node failed")[:2000]
        self._persist_run()
        self._push(node_id)

    def _emit_start_activity(self, node_id: str) -> None:
        """The original executor's node-opening activity pair (52469f4c :2250)."""
        node = self._node_by_id(node_id)
        self._emit_activity(
            node_id,
            kind=ACTIVITY_KIND_THINKING,
            text=f"starting {self._node_label(node, node_id)}",
            force=True,
        )
        self._emit_activity(
            node_id,
            kind=ACTIVITY_KIND_TOOL_CALL,
            text=stage_text_for_node(node),
            tool="worker",
            force=True,
        )

    def _emit_activity(
        self,
        node_id: str,
        *,
        kind: str,
        text: str,
        tool: str = "",
        force: bool = False,
    ) -> None:
        """apply + persist + throttled publish via the preserved activity helpers."""
        emit_run_activity(
            self._run,
            node_id,
            kind=kind,
            text=text,
            tool=tool,
            on_update=self._on_update,
            force=force,
        )
        self._persist_run()

    def _detach_current(self, node_id: str) -> None:
        current = self._run.setdefault("current_node_ids", [])
        if node_id in current:
            current.remove(node_id)

    def _node_by_id(self, node_id: str) -> dict[str, Any] | None:
        nodes = (self._graph or {}).get("nodes") or []
        for node in nodes:
            if str(node.get("id") or "") == node_id:
                return node
        return None

    @staticmethod
    def _node_label(node: dict[str, Any] | None, node_id: str) -> str:
        if isinstance(node, dict):
            return str(node.get("label") or node_id)
        return node_id

    def _persist_run(self) -> None:
        """Persist the run record after every mutation.

        The canvas polls ``designer.run.get`` (a Store read) every 400ms while
        a run is active; a pushed-but-unsaved state is overwritten by the
        next poll (e.g. a node flashing running then back to pending).
        """
        if self._store is None:
            return
        try:
            self._store.save_run(self._run)
        except Exception:
            logger.debug(
                "[design] per-node run save failed: run_id=%s",
                self._run.get("run_id"),
                exc_info=True,
            )

    def _push(self, node_id: str | None = None) -> None:
        if self._on_update is None:
            return
        try:
            # The adapter callback signature is (run, node_id=None); passing
            # the node id makes it emit the DESIGNER_NODE_UPDATED frame the
            # frontend subscribes to alongside the run snapshot.
            self._on_update(self._run, node_id)
        except Exception:
            logger.debug("[design] event bridge push failed", exc_info=True)


def _pending_state() -> dict[str, Any]:
    return {"status": "pending", "started_at": None, "completed_at": None}


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


__all__ = ["DesignEventBridge"]
