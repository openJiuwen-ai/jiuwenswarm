# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session-scoped bindings for the team.design execution layer.

The registry maps a designer team session to its graph and run coordinator so
capability providers (leader graph tools, worker ``node_run``) and the gateway
adapter resolve the same execution surface without global singletons per
graph. The store-backed spawner adapts the picked ``DesignerNodeSpawner``
protocol onto ``DesignerGraphStore`` plus the coordinator's single-node run.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    apply_graph_patch,
)

logger = logging.getLogger(__name__)


class StoreNodeSpawner:
    """``DesignerNodeSpawner`` over ``DesignerGraphStore`` + RunCoordinator.

    ``spawn_node_agent`` routes to the coordinator's single-node execution so
    the preserved ``node_run`` tool keeps its "run that node now" semantics
    under the new engine. The coordinator is provided lazily (the coordinator
    itself takes this spawner at construction).

    Guards against nested execution (both reject fast with an actionable
    message instead of executing):

    - Trigger re-entry: a node agent running inside a trigger owns the
      coordinator lock, so a worker's ``node_run`` would deadlock on
      ``asyncio.Lock`` re-entry.
    - Conversation round: the design leader's ``node_run`` inside a
      bootstrap/chat round would embed a real node execution (full worker
      LLM run) inside the leader's turn, holding the RPC response hostage —
      the canvas page would sit on the input screen until the node finished.
      The round boundary stays "graph edits + reply"; execution is the
      canvas run button's job.
    """

    def __init__(
        self,
        store: Any,
        graph_id: str,
        coordinator_provider: Any,
        on_graph_update: Any = None,
        on_run_update: Any = None,
    ) -> None:
        self._store = store
        self._graph_id = graph_id
        self._coordinator_provider = coordinator_provider
        # Optional push hook: called with the saved graph after every
        # leader/worker ``designer_graph_patch`` lands so the canvas reflects
        # mid-round edits without waiting for the RPC response.
        self._on_graph_update = on_graph_update
        # Optional push hook: forwards run snapshots as DESIGNER_RUN_UPDATED
        # while a leader-triggered run executes (the tool call awaits the
        # trigger, so the canvas needs the event stream, not just the reply).
        self._on_run_update = on_run_update
        # Set while a leader conversation round is in flight (DesignTeamBridge
        # turn). Purely advisory state; see ``conversation_round_active``.
        self._conversation_round_active = False
        # Set when a leader-triggered run finishes and its summary is fed
        # back to the leader; cleared by the next user chat round. While set,
        # the leader may only report: run/rebuild tools refuse.
        self._report_pending = False

    @property
    def report_pending(self) -> bool:
        return self._report_pending

    @report_pending.setter
    def report_pending(self, value: bool) -> None:
        self._report_pending = bool(value)

    @property
    def conversation_round_active(self) -> bool:
        return self._conversation_round_active

    @conversation_round_active.setter
    def conversation_round_active(self, value: bool) -> None:
        self._conversation_round_active = bool(value)

    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        if self._conversation_round_active:
            return (
                "node_run is not available during a conversation round: this "
                "turn is for editing the canvas graph. Finish your reply "
                "(the graph is saved already); the user will run nodes from "
                "the canvas run button when ready."
            )
        coordinator = self._coordinator_provider()
        if coordinator.is_active():
            return (
                "node_run is not available while a trigger is executing: the "
                "canvas scheduler runs one trigger at a time. Finish the "
                "current node (designer_node_complete), then run the new node "
                "from the canvas run button."
            )
        graph = self._store.get_graph(self._graph_id)
        result = await coordinator.start(graph, node_ids=[node_id])
        state = (result.get("node_states") or {}).get(node_id) or {}
        return str(state.get("text") or state.get("error") or "")

    async def run_graph(
        self,
        *,
        node_ids: list[str] | None = None,
        cascade: bool = False,
    ) -> dict[str, Any]:
        """Leader execution entry: trigger the graph through its coordinator.

        Unlike ``spawn_node_agent`` (worker self-expansion, rejected inside a
        conversation round), this is the leader's "run it now" tool — the
        conversation round is exactly where it is used. Only trigger re-entry
        is rejected: the coordinator lock is held by the in-flight trigger,
        and awaiting it from the same round would deadlock.
        """
        coordinator = self._coordinator_provider()
        if coordinator.is_active():
            raise RuntimeError(
                "a trigger is already executing on this graph: wait for it to "
                "finish (the canvas shows its progress) before starting another"
            )
        graph = self._store.get_graph(self._graph_id)
        return await coordinator.start(
            graph,
            node_ids=node_ids,
            cascade=cascade,
            on_update=self._on_run_update,
        )

    def set_push_callbacks(
        self, *, on_graph_update: Any = None, on_run_update: Any = None
    ) -> None:
        """Rebind the canvas push hooks (each round may ride a fresh channel)."""
        self._on_graph_update = on_graph_update
        self._on_run_update = on_run_update

    def apply_agent_graph_patch(
        self, graph_id: str, patch: dict[str, Any]
    ) -> DesignerExecutionGraph:
        if self._coordinator_provider().is_active():
            raise RuntimeError(
                "a trigger is executing on this graph: wait for it to finish "
                "before editing the canvas"
            )
        graph = self._store.get_graph(graph_id)
        if graph is None:
            raise ValueError(f"designer graph not found: {graph_id!r}")
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
            mark_brief_prompt_edits,
        )

        updated = apply_graph_patch(graph, patch)
        mark_brief_prompt_edits(graph, updated)
        saved = self._store.save_graph(updated)
        if self._on_graph_update is not None:
            try:
                self._on_graph_update(dict(saved))
            except Exception:
                logger.debug(
                    "[design] graph update push failed: graph_id=%s", graph_id
                )
        return saved

    def load_graph_snapshot(
        self, graph_id: str, run_id: str
    ) -> dict[str, Any]:
        graph = self._store.get_graph(graph_id)
        if graph is None:
            raise ValueError(f"designer graph not found: {graph_id!r}")
        return dict(graph)


class _DesignContext:
    """One designer team session's execution bindings."""

    def __init__(self, graph_id: str) -> None:
        self.graph_id = graph_id
        self.coordinator: Any = None
        self.spawner: StoreNodeSpawner | None = None
        # Active trigger's node-result sink (set by RunCoordinator while a
        # trigger executes; manifest-package worker tools submit through it).
        self.node_result_sink: Any = None
        # Active trigger's execution environment (graph / run / bridge) the
        # designer member tools resolve their shot tickets against (same
        # lifecycle as the sinks).
        self.node_run_env: Any = None
        # Active trigger's live-activity sink (same lifecycle): worker tools
        # push "calling image model" style stage lines through it so the node
        # card's activity peek shows handler-level progress (the old
        # executor's ctx.emit_activity contract).
        self.node_activity_sink: Any = None
        # Worker base for the TeamWorkerBackend (terminal state 2026-10-12):
        # the design team's teammate spec + build context, bound by the
        # bridge; role templates ride agent(options={'agent_template'}).
        self.worker_base_spec: Any = None
        self.worker_build_context: Any = None
        # Swarmflow expert table (SDD-0030 adaptation 2026-10-14): member
        # name → SwarmflowExpertEntry, derived ONCE from the same enriched
        # spec by the bridge (derive_design_expert_specs) — never per call.
        self.expert_specs: dict[str, Any] | None = None
        self.team_name: str = ""
        # Channel the design session's chat runs on (bound by DesignTeamBridge
        # each round). Design tools that push canvas events (the authoring
        # rail's stage/graph updates) read it so their frames land on the same
        # websocket the chat stream uses.
        self.channel_id: str = ""


class DesignContextRegistry:
    """Thread-safe session_id → design context bindings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, _DesignContext] = {}

    def bind(self, session_id: str, graph_id: str) -> _DesignContext:
        with self._lock:
            context = self._contexts.get(session_id)
            if context is None or context.graph_id != graph_id:
                context = _DesignContext(graph_id)
                self._contexts[session_id] = context
            return context

    def get(self, session_id: str) -> _DesignContext | None:
        with self._lock:
            return self._contexts.get(session_id)

    def for_graph(self, graph_id: str) -> _DesignContext | None:
        """Latest context bound to a graph (a graph serves one session)."""
        with self._lock:
            for context in reversed(list(self._contexts.values())):
                if context.graph_id == graph_id:
                    return context
            return None

    def node_result_sink_for(self, graph_id: str):
        """The active trigger's node-result sink for a graph, or None.

        Manifest-package worker tools (submit_node_output) resolve their
        writeback channel through this: the coordinator registers its
        backend's event sink on the context while a trigger executes.
        """
        context = self.for_graph(graph_id)
        if context is None:
            return None
        return context.node_result_sink

    def node_activity_sink_for(self, graph_id: str):
        """The active trigger's live-activity sink for a graph, or None.

        Worker tools resolve their "calling image model" stage emitter
        through this (same lifecycle as the node-result sink).
        """
        context = self.for_graph(graph_id)
        if context is None:
            return None
        return context.node_activity_sink

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._contexts.pop(session_id, None)


_REGISTRY = DesignContextRegistry()

# Process-wide RunCoordinator per graph: the adapter (run.start/cancel),
# DesignTeamBridge (leader node_run) and the capability provider must observe
# the same in-flight trigger — is_active()/cancel() would silently miss runs
# started through another entry point otherwise.
_GRAPH_COORDINATORS: dict[str, Any] = {}


def get_design_context_registry() -> DesignContextRegistry:
    return _REGISTRY


def get_graph_coordinator(store: Any, graph_id: str) -> Any:
    """One RunCoordinator per graph for the whole process."""
    coordinator = _GRAPH_COORDINATORS.get(graph_id)
    if coordinator is None:
        from jiuwenswarm.server.runtime.design.run_coordinator import RunCoordinator

        coordinator = RunCoordinator(store, graph_id)
        _GRAPH_COORDINATORS[graph_id] = coordinator
    return coordinator
