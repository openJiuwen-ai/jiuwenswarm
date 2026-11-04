# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One graph, one run_id, on-demand triggers — the team.design run surface.

Semantics (user-ruled):

* the first full run mints the graph's run_id; every later trigger reuses it
  and merges into the same run record — the Store-level identity is lifelong,
  and every trigger's compiled script (full ABC, single B, cascade from C)
  points at the graph's one journal file;
* a trigger compiles a script under the frozen-once-produced rule — completed
  nodes inline as literals, only the requested nodes execute, downstream is
  never in the script, so no operation can re-execute it;
* ONE eternal swarmflow run id (user-ruled 2026-10-10): every trigger runs
  under the graph's lifelong run id and appends into the same journal + WAL
  pair. A trigger that forces nodes to re-execute (single / cascade) first
  drops those nodes' cache records — "who re-runs, whose cache is dropped" —
  so a same-prompt rerun always executes live (gacha). Downstream nodes
  need no invalidation: their sigs embed the upstream output text and miss
  naturally once it changes;
* node results are written back to the Store per node the moment they
  complete (the original designer executor's contract). Crash / cancel
  recovery is therefore Store-driven: the next trigger simply compiles with
  the completed nodes frozen. No resume bookkeeping, no trigger sequence;
* every trigger (single node / full / cascade) writes its result into the
  node's current slots the same way: ``history`` is the old-version log (the
  previous current output is archived before each overwrite, so
  ``history[-1]`` is the true previous version) and ``text``/``output_ref``/
  ``output_refs`` always hold the latest accepted output;
* the run record's ``node_states`` is the cache/history store — per-node
  ``text`` snapshots feed compile-time inlining and ``history`` keeps one
  entry per trigger.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Any, Callable

from openjiuwen.agent_teams.paths import (
    workflow_run_dir,
    workflow_run_journal_path,
    workflow_run_wal_path,
)
from openjiuwen.agent_teams.workflow.engine import run_workflow
from openjiuwen.agent_teams.workflow.engine.errors import (
    WorkflowAborted as _WorkflowAborted,
)
from openjiuwen.agent_teams.workflow.engine.runtime import AbortSignal as _AbortSignal

from jiuwenswarm.common.schema.designer_graph import (
    RUN_SCHEMA_VERSION,
    RUN_TRIGGER_SOURCE_CANVAS,
    DesignerExecutionGraph,
)
from jiuwenswarm.server.runtime.design.context import (
    get_design_context_registry,
)
from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge
from jiuwenswarm.server.runtime.design.graph_compiler import (
    GraphScriptCompiler,
    _descendants,
)
from jiuwenswarm.server.runtime.design.team_worker import (
    build_worker_backend as _build_team_worker,
)

logger = logging.getLogger(__name__)

RunUpdateCallback = Callable[[dict[str, Any]], None]


class RunCoordinator:
    """Own one graph's run lifecycle (one run_id for its whole life)."""

    def __init__(
        self,
        store: Any,
        graph_id: str,
        media_root: "Path | None" = None,
    ) -> None:
        self._store = store
        self._graph_id = graph_id
        # Team-owned media root: set by the bridge binding (the owning team's
        # team-workspace) so worker assets live under the team. None keeps the
        # legacy global designer_media layout.
        self.media_root = media_root
        self._compiler = GraphScriptCompiler()
        self._lock = asyncio.Lock()
        self._abort: _AbortSignal | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Last trigger's backend (concurrency observability: max_in_flight).
        self.last_backend: Any = None

    # ------------------------------------------------------------------
    # trigger control (frontend buttons are the only run-control surface)
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """Whether a trigger is executing right now."""
        return self._lock.locked()

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Abort the in-flight trigger; terminal for that trigger."""
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if not self.is_active():
            # No trigger is executing, so a 'running' record is a leftover of
            # a process that died mid-trigger; nothing else would ever end it,
            # and the canvas stays read-only while the run looks active.
            if run.get("status") == "running":
                run["status"] = "cancelled"
                run["current_node_ids"] = []
                for state in (run.get("node_states") or {}).values():
                    if isinstance(state, dict) and state.get("status") == "running":
                        state["status"] = "failed"
                        state["error"] = "interrupted by process restart"
                self._store.save_run(run)
            return dict(run)
        if self._abort is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: self._abort.set("stop"))
        return dict(run)

    # ------------------------------------------------------------------
    # run_id identity
    # ------------------------------------------------------------------

    def _ensure_run_id(self, graph: DesignerExecutionGraph) -> tuple[str, bool]:
        meta = dict(graph.get("metadata") or {})
        run_id = str(meta.get("team_design_run_id") or "").strip()
        if run_id:
            return run_id, False
        run_id = f"run_{secrets.token_hex(6)}"
        meta["team_design_run_id"] = run_id
        graph["metadata"] = meta
        self._store.save_graph(graph)
        return run_id, True

    def _load_run(
        self, run_id: str, graph: DesignerExecutionGraph
    ) -> dict[str, Any]:
        existing = self._store.get_run(run_id)
        if existing is not None:
            return dict(existing)
        return {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "graph_id": str(graph.get("graph_id") or self._graph_id),
            "project_id": str(graph.get("project_id") or ""),
            "status": "draft",
            "node_states": {},
            "current_node_ids": [],
            "created_at": _now_ms(),
            "updated_at": _now_ms(),
        }

    # ------------------------------------------------------------------
    # triggers
    # ------------------------------------------------------------------

    async def start(
        self,
        graph: DesignerExecutionGraph,
        *,
        node_ids: list[str] | None = None,
        cascade: bool = False,
        on_update: RunUpdateCallback | None = None,
        source: str = RUN_TRIGGER_SOURCE_CANVAS,
    ) -> dict[str, Any]:
        """Execute one trigger.

        Full run when ``node_ids`` is None; single node when ``node_ids=[X]``
        (fresh output lands in the node's current slots — same as every
        other trigger type); "rerun from X" when ``cascade=True`` (X plus its
        downstream closure re-execute, outputs overwrite directly — the
        refresh the user explicitly asked for).
        """
        async with self._lock:
            self.heal_zombie_runs(graph)
            return await self._execute(
                graph,
                node_ids=node_ids,
                cascade=cascade,
                on_update=on_update,
                source=source,
            )

    def heal_zombie_runs(self, graph: DesignerExecutionGraph) -> None:
        """Mark crashed 'running' runs as failed (startup self-heal).

        A process crash mid-trigger leaves the Store's run record in
        'running' with no live trigger — the frontend polls that zombie
        forever (F04 §5). Any run still 'running' when a NEW trigger starts
        on this coordinator (which holds the lock, so no trigger is actually
        live) must be a leftover: mark it failed with a restart note; its
        completed nodes are already durable (per-node writeback) and the
        failed ones re-execute on this trigger.
        """
        try:
            run_id = str(
                (graph.get("metadata") or {}).get("team_design_run_id") or ""
            ).strip()
            if not run_id:
                return
            run = self._store.get_run(run_id)
            if not run or run.get("status") != "running":
                return
            run["status"] = "failed"
            run["current_node_ids"] = []
            run["error"] = "interrupted by process restart (zombie self-heal)"
            for state in (run.get("node_states") or {}).values():
                if isinstance(state, dict) and state.get("status") == "running":
                    state["status"] = "failed"
                    state["error"] = "interrupted by process restart"
            self._store.save_run(run)
            logger.warning(
                "[design-run] healed zombie run: run_id=%s graph=%s",
                run_id,
                self._graph_id,
            )
        except Exception:  # noqa: BLE001 — healing must never block a trigger
            logger.debug(
                "[design-run] zombie heal failed: graph=%s", self._graph_id, exc_info=True
            )

    async def _execute(
        self,
        graph: DesignerExecutionGraph,
        *,
        node_ids: list[str] | None,
        cascade: bool,
        on_update: RunUpdateCallback | None,
        source: str = RUN_TRIGGER_SOURCE_CANVAS,
    ) -> dict[str, Any]:
        run_id, minted = self._ensure_run_id(graph)
        run = self._load_run(run_id, graph)
        # One eternal swarmflow run id (user-ruled): every trigger executes
        # under the graph's lifelong run id and appends into the same
        # journal + WAL pair. Cross-trigger cache behaviour is carried by
        # the node VERSION, not by the run id and not by deleting records:
        # the coordinator bumps the version of every node this trigger
        # EXECUTES before compiling, and the compiler encodes it into the
        # agent() ``phase`` ("v{n}") which rides the call signature's
        # identity opts. A rerun bumps the version → new sig → WAL miss →
        # live execution (the gacha); untouched nodes keep their version →
        # stable sig → cross-trigger WAL hit → free replay. Downstream
        # nodes need no invalidation — their sigs embed the upstream output
        # text and miss naturally once it changes. The WAL keeps its
        # append-only invariant (no record deletion).
        executing_ids = self._executing_nodes(graph, node_ids=node_ids, cascade=cascade)
        states = run.setdefault("node_states", {})
        for node_id in executing_ids:
            state = states.setdefault(
                node_id, {"status": "pending", "started_at": None, "completed_at": None}
            )
            current = state.get("version")
            state["version"] = (
                current if isinstance(current, int) and current > 0 else 0
            ) + 1
        # Per-trigger scope for the worker side only (conversation cache
        # key, workspace file stems, media dir, trajectory) — the role the
        # old per-rerun run id played. The swarmflow run id stays eternal.
        trigger_scope = f"{run_id}-t{secrets.token_hex(4)}"
        run["status"] = "running"
        run["current_node_ids"] = []
        run["metadata"] = {**(run.get("metadata") or {}), "trigger_source": source}
        # A new trigger invalidates the previous trigger's run-level error;
        # leaving it would keep showing a stale refusal on every later run.
        run.pop("error", None)
        self._store.save_run(run)
        if on_update is not None:
            on_update(dict(run))

        # From here on the record is armed as 'running': a compile refusal
        # (single-node rerun whose upstream produced nothing) or a setup
        # crash must write the failure back — otherwise the record stays
        # 'running' with no live trigger and the frontend polls it forever.
        context: Any = None
        try:
            # Layered writeback: the swarmflow layer (backend) only EMITS a
            # node-result event; the design layer (bridge) listens and persists
            # to the Store per node (the old executor's contract). The bridge is
            # the single writer — engine status events and node results both
            # flow through it.
            bridge = DesignEventBridge(
                run, on_update, store=self._store, graph=graph
            )
            script = self._compiler.compile(
                graph,
                run.get("node_states") or {},
                node_ids=node_ids,
                cascade=cascade,
                run_id=run_id,
            )
            # Compile observability (user request 2026-10-11): one line per
            # scheduled agent() call so the run log answers "did the edited
            # upstream reach this node's compiled prompt" without opening the
            # script file. Full prompts ride the backend's node-input log.
            import re as _re

            for match in _re.finditer(
                r"label=([\"'])(?P<label>.*?)\1, phase=([\"'])(?P<phase>.*?)\3",
                script,
            ):
                logger.info(
                    "[design-compile] run=%s node=%s phase=%s",
                    run_id,
                    match.group("label"),
                    match.group("phase"),
                )
            # Worker backend (terminal state 2026-10-12): the agent-core
            # TeamWorkerBackend — every worker derives from the design team's
            # teammate base spec, per-call role templates ride
            # ``options={'agent_template': role}`` through the spec resolver
            # (persona + package tools live in the designer AgentGroup package;
            # the claw repo only maps graph nodes to role names). The base spec
            # is bound by the bridge from the live design team (context.worker_
            # base_spec); an unbound context is a setup error, not a mode to
            # silently route around with a legacy executor.
            context = get_design_context_registry().for_graph(
                str(graph.get("graph_id") or self._graph_id)
            )
            if context is None or context.worker_base_spec is None:
                raise RuntimeError(
                    "design context not bound for this graph: the session bridge "
                    "must bind_round (worker base spec) before a trigger starts"
                )
            build_ctx = context.worker_build_context
            expert_specs = context.expert_specs
            team_name = str(context.team_name or "").strip()
            session_id = str((graph.get("metadata") or {}).get("session_id") or "").strip()
            if not team_name or not session_id:
                raise RuntimeError(
                    "design trigger needs its owning team: team_name (bound by "
                    "the bridge) and the graph's metadata.session_id are both "
                    f"required, got team_name={team_name!r} session_id={session_id!r}"
                )
            # Native swarmflow layout (user ruling 2026-10-19): journal / WAL /
            # compiled scripts live under the owning team session exactly like
            # the leader's swarmflow tool —
            # {team_home}/sessions/{sid}/workflows/{graph_id}/. The workflow
            # name is the compiled script's META.name, which the compiler sets
            # to the graph id.
            workflow_name = str(graph.get("graph_id") or self._graph_id)
            run_dir = workflow_run_dir(team_name, session_id, workflow_name)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "wal").mkdir(exist_ok=True)
            journal_path = str(
                workflow_run_journal_path(team_name, session_id, workflow_name, run_id)
            )
            wal_path = str(
                workflow_run_wal_path(team_name, session_id, workflow_name, run_id)
            )
            script_path = _write_script(run_dir, trigger_scope, script)
            backend = _build_team_worker(
                team_name=team_name,
                worker_base_spec=context.worker_base_spec,
                build_context=build_ctx,
                # Worker names (hence their checkpointer keys under the shared
                # session) derive from this id; the lifelong run id would mint
                # the same name on every rerun and restore the last worker's
                # context. The engine's journal keeps the lifelong run id.
                run_id=trigger_scope,
                session_id=session_id,
                expert_specs=expert_specs,
            )
            # Host guarantees (plan A, user ruling 2026-10-12): re-arm the old
            # executor's post-turn contract on every worker call — text products
            # get a workspace file ref, media nodes get handler materialization
            # when the worker's result lacks the required family, and a node that
            # still has no required media FAILS (never completed-with-stub).
            from jiuwenswarm.server.runtime.design.media_guarantee import (
                MediaGuaranteeBackend,
            )

            backend = MediaGuaranteeBackend(
                backend,
                graph=graph,
                run=run,
                bridge=bridge,
                media_root=self.media_root,
            )
            # Node-result writeback: the manifest package's submit tool resolves
            # the active sink through the design context (graph-keyed); the
            # TeamWorkerBackend path has no event_sink of its own, so register
            # the bridge handler here and clear it when the trigger ends.
            context.node_result_sink = bridge.on_node_result
            # Worker-tool live activity (handler-level stage lines): resolved
            # by manifest tools through the design context like the result sink.
            context.node_activity_sink = bridge.node_activity_sink
            # Member tools (the designer bundle's tools/node_tools.py) resolve
            # their shot tickets against this trigger's graph / run / bridge.
            context.node_run_env = {
                "graph": graph,
                "run": run,
                "bridge": bridge,
                "media_root": self.media_root,
                "generations": {},
            }
            # Concurrency observability: keep the last trigger's backend alive on
            # the coordinator so STs / diagnostics can read max_in_flight (a
            # parallel wave compiles to engine parallel(); the backend sees the
            # real overlap, unlike wall-clock probes at the media layer).
            self.last_backend = backend
            self._loop = asyncio.get_running_loop()
            self._abort = _AbortSignal(reason="stop")
            abort_signal = self._abort
            result = await run_workflow(
                str(script_path),
                args=None,
                backend=backend,
                progress_sink=bridge.emit,
                log_sink=lambda message: logger.info("[design-run] %s", message),
                run_id=run_id,
                journal_path=journal_path,
                resume=journal_path,
                wal_path=wal_path,
                abort_event=abort_signal,
            )
        except (_WorkflowAborted, asyncio.CancelledError) as exc:
            # CancelledError: the awaiting task died (a leader trigger when the
            # leader is paused). Unrecorded, the run stays 'running' and the
            # canvas stays read-only with no live trigger behind it.
            run["status"] = "cancelled"
            run["current_node_ids"] = []
            for state in (run.get("node_states") or {}).values():
                if state.get("status") == "running":
                    state["status"] = "cancelled"
            self._store.save_run(run)
            if on_update is not None:
                on_update(dict(run))
            if isinstance(exc, asyncio.CancelledError):
                raise
            return run
        except Exception as exc:
            logger.exception("[design-run] trigger failed: run_id=%s", run_id)
            run["status"] = "failed"
            run["current_node_ids"] = []
            message = f"{type(exc).__name__}: {exc}"
            for node_id, state in (run.get("node_states") or {}).items():
                if state.get("status") == "running":
                    state["status"] = "failed"
                    state["error"] = message[:2000]
            run["error"] = message[:2000]
            self._store.save_run(run)
            if on_update is not None:
                on_update(dict(run))
            return run
        finally:
            # Trigger ended: drop the context's node-result sink so package
            # tools outside a trigger submit to a clear "no active trigger"
            # answer instead of a stale bridge.
            if context is not None:
                context.node_result_sink = None
                context.node_activity_sink = None
                context.node_run_env = None
        # The engine's failure semantics are soft: a node whose backend call
        # exhausted its retries returns None (AGENT_FAILED event, no
        # exception) and the workflow keeps running. A trigger whose result
        # contains None-valued nodes therefore FAILED from the design
        # layer's point of view — the failed node re-executes on the next
        # trigger, while the completed ones are already durable in the
        # Store (per-node writeback) and compile-time frozen.
        outputs = result if isinstance(result, dict) else {}
        failed_nodes = [nid for nid, text in outputs.items() if text is None]
        if failed_nodes:
            run["status"] = "failed"
            failed_set = set(failed_nodes)
            for node_id in failed_nodes:
                state = (run.setdefault("node_states", {}).setdefault(
                    node_id, {"status": "pending", "started_at": None, "completed_at": None}
                ))
                state["status"] = "failed"
                # Keep a more specific error already on record (the media
                # guarantee's precise reason, an engine attempt error) instead
                # of the generic gate text.
                if not state.get("error"):
                    # A node gated off by the compiled script (its upstream
                    # failed this trigger) never ran — say which upstream
                    # failed instead of the misleading backend-retry text.
                    failed_upstream: list[str] = []
                    for edge in graph.get("edges") or []:
                        source = str(edge.get("source") or "")
                        if str(edge.get("target") or "") != node_id:
                            continue
                        if source not in failed_set:
                            continue
                        failed_upstream.append(source)
                    if failed_upstream:
                        state["error"] = (
                            "upstream node failed: "
                            + ", ".join(failed_upstream)
                            + " — node not executed"
                        )
                    else:
                        state["error"] = (
                            "node execution failed (backend retries exhausted)"
                        )
            run["current_node_ids"] = []
            self._store.save_run(run)
            if on_update is not None:
                on_update(dict(run))
            return run

        # Manifest-package workers submit through submit_node_output (the
        # package tool writes via the design-context sink). A worker that
        # answered in free text WITHOUT submitting has no Store output —
        # fall back to the engine result text so the node's product still
        # lands (the old backend's per-node event carried this; the
        # TeamWorkerBackend path surfaces it only here, at trigger end).
        states = run.setdefault("node_states", {})
        for node_id, text in outputs.items():
            if not isinstance(text, str) or not text.strip():
                continue
            state = states.get(node_id) or {}
            already = bool(state.get("text") or state.get("output_ref"))
            if already:
                continue
            bridge.on_node_result(node_id, {"text": text})

        # Successful nodes were persisted per node as they completed (the
        # persist callback); stamp the canvas-facing output refs for every
        # trigger type — a single run's fresh output must reach the graph
        # node too, or the canvas keeps showing the previous version.
        self._write_outputs_to_graph(graph, run)

        run["status"] = "completed"
        run["current_node_ids"] = []
        self._store.save_run(run)
        if on_update is not None:
            on_update(dict(run))
        if minted:
            logger.info("[design-run] minted run_id=%s for graph=%s", run_id, self._graph_id)
        return run

    @staticmethod
    def _executing_nodes(
        graph: DesignerExecutionGraph,
        *,
        node_ids: list[str] | None,
        cascade: bool,
    ) -> list[str]:
        """Nodes this trigger will EXECUTE live (version-bump targets).

        Mirrors the compiler's scope: a full run executes every node the
        script will call (the compiler freezes completed ones, so only
        first-time nodes actually run — but their version bumps are harmless:
        they had none); a single-node trigger executes its target; a cascade
        executes the target plus its downstream closure.

        Graph order, not a set: the caller pre-seeds run.node_states from
        this sequence, and that dict's key order lands in the persisted run
        JSON — a set here leaks a per-process hash order into durable data.
        """
        nodes = [str(n.get("id") or "") for n in (graph.get("nodes") or [])]
        if node_ids is None:
            return [nid for nid in nodes if nid]
        targets = {nid for nid in node_ids if nid}
        if cascade:
            for target in list(targets):
                targets.update(_descendants(graph, target))
        return [nid for nid in nodes if nid in targets]

    def _write_outputs_to_graph(
        self, graph: DesignerExecutionGraph, run: dict[str, Any]
    ) -> None:
        """Persist node output refs onto the graph (canvas display)."""
        states = run.get("node_states") or {}
        changed = False
        for node in graph.get("nodes") or []:
            node_id = str(node.get("id") or "")
            state = states.get(node_id) or {}
            output_ref = state.get("output_ref")
            if output_ref and node.get("output_ref") != output_ref:
                node["output_ref"] = output_ref
                changed = True
        if changed:
            self._store.save_graph(graph)


def _write_script(run_dir: Path, trigger_scope: str, source: str) -> Path:
    """Archive one trigger's compiled script next to its journal (native layout)."""
    path = run_dir / "scripts" / f"{trigger_scope}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    logger.info("[design-run] compiled script: %s", path)
    return path


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


__all__ = ["RunCoordinator"]
