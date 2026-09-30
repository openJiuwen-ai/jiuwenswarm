# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer graph executor — wave scheduler or agent-owned graph."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATE_AGENT,
    CONFIG_DELEGATE_HANDLER,
    DesignerExecutionGraph,
    DesignerExecutionRun,
    DesignerGraphNode,
    DesignerNodeState,
    MAX_SHOT_CLIP_NODES,
    NODE_ROLE_BRIEF,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_STATUS_CANCELLED,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_RUNNING,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_FAILED,
    RUN_STATUS_PAUSED,
    RUN_STATUS_RUNNING,
    apply_graph_patch,
    apply_shot_generate_prompts,
    artifact_dependency_satisfied,
    clip_node_id,
    execution_predecessors,
    filter_ready_by_dependency_order,
    frame_node_id,
    graph_uses_agent_scheduler,
    initial_node_states,
    is_comfyui_node,
    is_soft_artifact_dependency,
    new_run_id,
    node_pipeline,
    node_uses_agent_runtime,
    sync_groups,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.server.runtime.designer.activity import (
    emit_run_activity,
    graph_node_states,
)
from jiuwenswarm.server.runtime.designer.a2a_collab import collaborate_ready_wave
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers import (
    NodeExecutionContext,
    get_node_handler,
)
from jiuwenswarm.server.runtime.designer.node_agent import NodeAgentHost, NodeAgentRunner
from jiuwenswarm.server.runtime.designer.user_references import (
    carry_user_references,
    ensure_user_reference_routes,
    is_uploaded_media_node,
)

logger = logging.getLogger(__name__)

GraphUpdateCallback = Callable[[DesignerExecutionGraph], None]


class RunUpdateCallback(Protocol):
    def __call__(
        self,
        run: DesignerExecutionRun,
        node_id: str | None = None,
    ) -> None: ...

_MOCK_NODE_DELAY_SECONDS = 0.35
# Ready shots start together once their inputs exist. The cap only limits how
# many node agents run at once; it is not a dependency lock.
_MAX_CONCURRENT_NODE_AGENTS = 3
_TERMINAL_NODE_STATUSES = {
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_CANCELLED,
}


def _director_text_ready(graph: DesignerExecutionGraph, node: DesignerGraphNode) -> bool:
    """True when the director already approved the brief or storyboard text."""
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    role = node_pipeline(node)
    node_id = str(node.get("id") or "")
    if role == NODE_ROLE_BRIEF or node_id == "n_brief":
        return bool(str(meta.get("approved_brief") or "").strip())
    if role == NODE_ROLE_STORYBOARD or node_id == "n_storyboard":
        return bool(str(meta.get("approved_storyboard") or "").strip())
    return False


@dataclass(frozen=True)
class NodeEvent:
    event: str
    run: DesignerExecutionRun
    node_id: str | None = None


class GraphExecutor:
    """Wave scheduler for handler graphs; agent scheduler for node Agents."""

    def __init__(
        self,
        store: DesignerGraphStore | None = None,
        *,
        runner: NodeAgentRunner | None = None,
    ) -> None:
        self._store = store or DesignerGraphStore()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._graph_tasks: dict[asyncio.Task[None], tuple[str, str]] = {}
        self._node_workers: dict[str, dict[str, asyncio.Task[None]]] = {}
        self._pause_flags: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._on_updates: dict[str, RunUpdateCallback] = {}
        self._on_graph_updates: dict[str, GraphUpdateCallback] = {}
        self._live_runs: dict[str, DesignerExecutionRun] = {}
        self._host = NodeAgentHost(self, runner=runner)
        self._handoff_wake: dict[str, asyncio.Event] = {}

    def has_active_tasks(self, graph_id: str) -> bool:
        """Include workers that are still exiting after pause/cancel/cleanup."""
        return any(owner[0] == graph_id and not task.done() for task, owner in self._graph_tasks.items())

    def _track_graph_task(self, graph_id: str, task: asyncio.Task[None], *, run_id: str) -> None:
        self._graph_tasks[task] = (graph_id, run_id)

        def finished(done: asyncio.Task[None]) -> None:
            self._graph_tasks.pop(done, None)
            self._cleanup_run(run_id)

        task.add_done_callback(finished)

    def _get_handoff_wake(self, run_id: str) -> asyncio.Event:
        ev = self._handoff_wake.get(run_id)
        if ev is None:
            ev = asyncio.Event()
            self._handoff_wake[run_id] = ev
        return ev

    def _signal_handoff_wake(self, run_id: str) -> None:
        ev = self._handoff_wake.get(run_id)
        if ev is not None:
            ev.set()

    def _publish_prompt_artifact_early(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        node: DesignerGraphNode,
        *,
        prompt: str = "",
        on_update: RunUpdateCallback | None = None,
    ) -> None:
        """Publish finalized leaf prompt so soft dependents can start before media finishes."""
        node_id = str(node.get("id") or "")
        if not node_id:
            return
        cfg = dict(node.get("config") or {})
        text = str(
            prompt
            or cfg.get("last_approved_prompt")
            or cfg.get("last_wan_prompt")
            or (cfg.get("generate") or {}).get("prompt")
            or cfg.get("prompt")
            or ""
        ).strip()
        if not text:
            return
        role = node_pipeline(node)
        shot_index = int(cfg.get("shot_index") or 0)
        live = self._require_graph(str(run.get("graph_id") or graph.get("graph_id") or ""))
        for n in live.get("nodes") or []:
            if str(n.get("id") or "") != node_id:
                continue
            c = dict(n.get("config") or {})
            c["handoff_artifact_ready"] = True
            c["last_approved_prompt"] = text[:4000]
            if role in {NODE_ROLE_CLIP, "clip", "video"}:
                c["last_wan_prompt"] = text[:4000]
                c["clip_prompt_preview"] = text[:1200]
            if role in {NODE_ROLE_FRAME, "frame", "keyframe"} and bool(c.get("is_scene_master")):
                from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import (
                    architecture_clause_from_bible,
                )

                specs_m = c.get("scene_specs") if isinstance(c.get("scene_specs"), dict) else None
                arch_m = architecture_clause_from_bible(specs_m)
                c["scene_architecture_clause"] = arch_m or text[:900]
                c["scene_master_prompt"] = (arch_m or text)[:900]
            if role in {NODE_ROLE_SCENE, "scene"} or (
                node_id.startswith("n_scene_") and bool(c.get("is_scene_master"))
            ):
                from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import (
                    architecture_clause_from_bible,
                )

                specs_m = c.get("scene_specs") if isinstance(c.get("scene_specs"), dict) else None
                arch_m = architecture_clause_from_bible(specs_m)
                c["scene_architecture_clause"] = arch_m or text[:900]
                c["scene_master_prompt"] = (arch_m or text)[:900]
                c["handoff_artifact_ready"] = True
            n["config"] = c
            node = n
            break
        if role in {NODE_ROLE_CLIP, "clip", "video"} and shot_index >= 1:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
                stamp_wan_prompt_handoff,
            )

            stamp_wan_prompt_handoff(
                live,
                shot_index=shot_index,
                prompt=text,
                node_id=node_id,
                shot_action=str(cfg.get("shot_action") or ""),
                speech_line=str(cfg.get("speech_line") or ""),
            )
        if role in {NODE_ROLE_FRAME, "frame", "keyframe"} and shot_index >= 1:
            from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import (
                architecture_clause_from_bible,
            )

            specs = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else None
            if not specs:
                meta0 = live.get("metadata") if isinstance(live.get("metadata"), dict) else {}
                locks0 = meta0.get("scene_locks") if isinstance(meta0.get("scene_locks"), dict) else {}
                sid = str(cfg.get("setting_id") or "").strip()
                if sid and isinstance(locks0.get(sid), dict):
                    specs = locks0[sid]
            arch = architecture_clause_from_bible(specs)
            next_ids = {f"n_frame_{shot_index + 1}", f"n_clip_{shot_index + 1}"}
            for n in live.get("nodes") or []:
                if not isinstance(n, dict):
                    continue
                nid = str(n.get("id") or "")
                if not nid or nid == node_id:
                    continue
                c = dict(n.get("config") or {})
                needs = {
                    str(c.get("scene_prompt_handoff_from") or "").strip(),
                    str(c.get("scene_master_frame_id") or "").strip(),
                    str(c.get("continuity_frame_node_id") or "").strip(),
                }
                if node_id not in needs and nid not in next_ids:
                    continue
                changed = False
                if arch and not str(c.get("scene_architecture_clause") or "").strip():
                    c["scene_architecture_clause"] = arch
                    changed = True
                if node_id in needs and arch and not str(c.get("scene_master_prompt") or "").strip():
                    c["scene_master_prompt"] = arch[:900]
                    changed = True
                # Consecutive prior stamp — always overwrite for N→N+1 (fixes parallel KF race).
                if nid in next_ids or str(c.get("continuity_frame_node_id") or "") == node_id:
                    action_snip = str(
                        cfg.get("shot_action") or cfg.get("character_action") or ""
                    ).strip()
                    if not action_snip:
                        action_snip = text[:220]
                    c["previous_keyframe_action"] = action_snip[:300]
                    c["previous_keyframe_node_id"] = node_id
                    # Soft-dep readiness; keep short so handlers don't dump full prior.
                    c["previous_keyframe_prompt"] = text[:800]
                    changed = True
                if changed:
                    n["config"] = c
        self._store.save_graph(live)
        # Keep caller's graph object in sync when possible.
        graph["nodes"] = live.get("nodes")
        graph["edges"] = live.get("edges")
        graph["metadata"] = live.get("metadata")
        self._signal_handoff_wake(str(run.get("run_id") or ""))
        if on_update is not None:
            try:
                on_update(run, node_id)
            except Exception:  # noqa: BLE001
                logger.debug("handoff wake publish failed", exc_info=True)

    def create_run(self, graph: DesignerExecutionGraph) -> DesignerExecutionRun:
        now = utc_now_ms()
        run: DesignerExecutionRun = {
            "schema_version": "designer-execution-run.v1",
            "run_id": new_run_id(),
            "graph_id": graph["graph_id"],
            "project_id": graph["project_id"],
            "status": RUN_STATUS_DRAFT,
            "node_states": initial_node_states(graph),
            "current_node_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        return self._store.save_run(run)

    def create_rerun(
        self,
        graph: DesignerExecutionGraph,
        *,
        source_run: DesignerExecutionRun | None,
        node_id: str,
    ) -> DesignerExecutionRun:
        """Reuse available outputs and execute only the selected node."""
        node_ids = {node["id"] for node in graph.get("nodes", [])}
        if node_id not in node_ids:
            raise KeyError(f"node not found: {node_id}")
        if is_comfyui_node(_node_by_id(graph, node_id)):
            return self._create_scoped_rerun(graph, source_run=source_run, node_id=node_id)
        if source_run is None:
            raise ValueError("no previous run to rerun from")
        incoming = execution_predecessors(graph)
        groups = sync_groups(graph)
        source_states = source_run.get("node_states") or {}
        for pred in incoming.get(node_id, []):
            members = groups.get(pred, frozenset({pred}))
            for member in members:
                if (source_states.get(member) or {}).get("status") != NODE_STATUS_COMPLETED:
                    raise ValueError(f"upstream not ready: {member}")
        now = utc_now_ms()
        states = deepcopy(source_states)
        for node in graph.get("nodes", []):
            states.setdefault(node["id"], {"status": NODE_STATUS_PENDING})
        for nid, state in list(states.items()):
            if nid == node_id or not isinstance(state, dict):
                continue
            if state.get("status") != NODE_STATUS_RUNNING:
                continue
            # Orphaned running snapshots are not scheduled again (_is_ready
            # only accepts pending). Park them so Continue can resume.
            states[nid] = _parked_node_state(state)
        previous = states.get(node_id) or {}
        kept_ref = previous.get("output_ref") if _usable_ref(previous.get("output_ref")) else None
        kept_refs = [
            ref for ref in (previous.get("output_refs") or []) if _usable_ref(ref)
        ]
        if kept_ref is not None and not kept_refs:
            kept_refs = [kept_ref]
        target_node = _node_by_id(graph, node_id)
        target_type = str(target_node.get("type") or "")
        if (
            target_type in {NODE_TYPE_IMAGE, NODE_TYPE_VIDEO}
            and _is_fallback_text_ref(kept_ref)
        ) or node_pipeline(target_node) == NODE_ROLE_COMPOSE:
            kept_ref = None
            kept_refs = []
        states[node_id] = {
            "status": NODE_STATUS_PENDING,
            "started_at": None,
            "completed_at": None,
            "output_ref": kept_ref,
            "output_refs": kept_refs,
            "error": None,
            "blocked_by": [],
        }
        run: DesignerExecutionRun = {
            "schema_version": "designer-execution-run.v1",
            "run_id": new_run_id(),
            "graph_id": graph["graph_id"],
            "project_id": graph["project_id"],
            "status": RUN_STATUS_DRAFT,
            "node_states": states,
            "current_node_ids": [],
            "created_at": now,
            "updated_at": now,
            "metadata": {"target_node_id": node_id},
        }
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
                capture_regenerate_packet,
            )

            target = _node_by_id(graph, node_id)
            cfg = dict(target.get("config") or {})
            cfg["regenerate_packet"] = capture_regenerate_packet(
                target,
                graph,
                states,
            )
            target["config"] = cfg
        except Exception:  # noqa: BLE001
            logger.debug("regenerate packet capture failed", exc_info=True)
        self._store.save_graph(graph)
        return self._store.save_run(run)

    def _create_scoped_rerun(
        self,
        graph: DesignerExecutionGraph,
        *,
        source_run: DesignerExecutionRun | None,
        node_id: str,
    ) -> DesignerExecutionRun:
        """A run that executes only ``node_id`` on its handler.

        No Director phase, no other node, no ratings. Inputs count as ready
        when the user uploaded a file over them or a previous run produced one,
        so a freshly imported workflow can generate before any full Play.
        """
        states = deepcopy((source_run or {}).get("node_states") or {})
        for node in graph.get("nodes", []):
            states.setdefault(node["id"], {"status": NODE_STATUS_PENDING})
        for nid, state in list(states.items()):
            if isinstance(state, dict) and state.get("status") == NODE_STATUS_RUNNING:
                states[nid] = _parked_node_state(state)
        from jiuwenswarm.server.runtime.designer.handlers.common import uploaded_output_ref

        for pred in execution_predecessors(graph).get(node_id, []):
            uploaded = uploaded_output_ref(graph, pred)
            if uploaded is not None:
                states[pred] = {
                    **(states.get(pred) or {}),
                    "status": NODE_STATUS_COMPLETED,
                    "output_ref": uploaded,
                    "output_refs": [uploaded],
                    "error": None,
                    "blocked_by": [],
                }
                continue
            state = states.get(pred) or {}
            if state.get("status") != NODE_STATUS_COMPLETED or not _usable_ref(
                state.get("output_ref")
            ):
                raise ValueError(f"upstream not ready: {pred}")
        previous = states.get(node_id) or {}
        states[node_id] = {
            **_parked_node_state(previous),
            "output_ref": previous.get("output_ref") if _usable_ref(previous.get("output_ref")) else None,
            "output_refs": [ref for ref in (previous.get("output_refs") or []) if _usable_ref(ref)],
        }
        now = utc_now_ms()
        run: DesignerExecutionRun = {
            "schema_version": "designer-execution-run.v1",
            "run_id": new_run_id(),
            "graph_id": graph["graph_id"],
            "project_id": graph["project_id"],
            "status": RUN_STATUS_DRAFT,
            "node_states": states,
            "current_node_ids": [],
            "created_at": now,
            "updated_at": now,
            "metadata": {"single_node_rerun": True, "scope_node_ids": [node_id]},
        }
        return self._store.save_run(run)

    async def start_run(
        self,
        run_id: str,
        *,
        on_update: RunUpdateCallback | None = None,
        on_graph_update: GraphUpdateCallback | None = None,
    ) -> DesignerExecutionRun:
        run = self._require_run(run_id)
        if run["status"] == RUN_STATUS_RUNNING:
            return run
        if self._is_cancelled(run_id) and self.has_active_tasks(run["graph_id"]):
            raise ValueError("任务正在停止，请稍后重试")
        target_node_id = _target_node_id(run)
        # Completed node runs must not pick up unrelated pending work on restart.
        if run["status"] == RUN_STATUS_COMPLETED:
            pending = any(
                (state or {}).get("status") == NODE_STATUS_PENDING
                for node_id, state in graph_node_states(run).items()
                if target_node_id is None or node_id == target_node_id
            )
            if not pending:
                return run
        if run["status"] == RUN_STATUS_FAILED:
            # Continue after a failure (e.g. out of credit) retries the failed
            # nodes too; the scheduler only picks up pending ones.
            states = run.setdefault("node_states", {})
            for node_id, state in graph_node_states(run).items():
                if isinstance(state, dict) and state.get("status") == NODE_STATUS_FAILED:
                    states[node_id] = _parked_node_state(state)
        graph = self._require_graph(run["graph_id"])
        from jiuwenswarm.server.runtime.designer.model_tools import (
            require_llm,
            require_media_models,
        )

        scope = _scope_node_ids(run)
        if scope and all(
            (graph_node_states(run).get(node_id) or {}).get("status") in _TERMINAL_NODE_STATUSES
            for node_id in scope
        ):
            # Continue after a ComfyUI generate means "run the rest of the canvas".
            meta = dict(run.get("metadata") or {})
            meta.pop("scope_node_ids", None)
            run["metadata"] = meta
            scope = []
        scoped = bool(scope)
        if scoped:
            limit: set[str] | None = set(scope)
        elif target_node_id is not None:
            limit = {target_node_id}
        else:
            limit = None
        _reject_pending_audio_generation(graph, run, node_ids=limit)
        # One local credential gate at Play entry (Work/Code checks before
        # spawning agents). Billing/API failures still surface on the model call.
        # Scoped runs only drive ComfyUI nodes: no LLM, and their own vLLM-Omni URL.
        if not scoped:
            require_llm()
            # Incomplete image/video config blocks only when a yet-to-run leaf needs it.
            # Runtime failures (credit, bad endpoint) still surface on the model call.
            require_media_models(**_pending_media_modalities(graph, run, node_ids=limit))
        run.pop("error", None)
        for node in graph.get("nodes") or []:
            if target_node_id is not None and node["id"] != target_node_id:
                continue
            cfg = node.setdefault("config", {})
            if not isinstance(cfg, dict):
                continue
            if is_uploaded_media_node(node) or is_comfyui_node(node) or cfg.get("force_handler"):
                cfg["delegate"] = CONFIG_DELEGATE_HANDLER
                cfg["force_handler"] = True
                cfg["skip_llm"] = True
                if is_uploaded_media_node(node):
                    cfg["read_only"] = True
                    cfg["immutable_source"] = True
                continue
            cfg.pop("force_handler", None)
            cfg.pop("skip_llm", None)
            cfg["delegate"] = CONFIG_DELEGATE_AGENT
            cfg["kind"] = "agent"
        # Single-node runs must not repair routes or change workflow metadata.
        if not scoped and target_node_id is None:
            await ensure_user_reference_routes(graph)
            meta = dict(graph.get("metadata") or {})
            # Lock before nodes run so a saved graph with the lock off cannot
            # drop image nodes when the storyboard finishes.
            meta["freeze_shot_topology"] = True
            graph["metadata"] = meta
            self._store.save_graph(graph)
        run["status"] = RUN_STATUS_RUNNING
        run["updated_at"] = utc_now_ms()
        run = self._store.save_run(run)
        self._live_runs[run_id] = run
        self._pause_flags[run_id] = asyncio.Event()
        self._pause_flags[run_id].set()
        self._cancel_flags[run_id] = asyncio.Event()
        self._state_locks[run_id] = asyncio.Lock()
        self._node_workers[run_id] = {}
        if on_update is not None:
            self._on_updates[run_id] = on_update
        if on_graph_update is not None:
            self._on_graph_updates[run_id] = on_graph_update
        task = asyncio.create_task(
            self._execute_run(graph, run, on_update=on_update),
            name=f"designer-run-{run_id}",
        )
        self._tasks[run_id] = task
        self._track_graph_task(graph["graph_id"], task, run_id=run_id)
        return run

    async def run(self, graph: DesignerExecutionGraph, run_id: str) -> AsyncIterator[NodeEvent]:
        """Drive a run and yield node/run events as they happen."""
        queue: asyncio.Queue[NodeEvent] = asyncio.Queue()

        def on_update(updated: DesignerExecutionRun, node_id: str | None = None) -> None:
            event = (
                EventType.DESIGNER_NODE_UPDATED.value
                if node_id
                else EventType.DESIGNER_RUN_UPDATED.value
            )
            queue.put_nowait(
                NodeEvent(
                    event=event,
                    run=deepcopy(updated),
                    node_id=node_id,
                )
            )

        existing = self._tasks.get(run_id)
        if existing is None or existing.done():
            started = await self.start_run(run_id, on_update=on_update)
            queue.put_nowait(
                NodeEvent(event=EventType.DESIGNER_RUN_UPDATED.value, run=deepcopy(started))
            )
        task = self._tasks.get(run_id)
        if task is None:
            return
        while True:
            if task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield event
        await task

    def pause_run(self, run_id: str) -> DesignerExecutionRun:
        run = self._require_run(run_id)
        pause_flag = self._pause_flags.get(run_id)
        if pause_flag is not None:
            pause_flag.clear()
        run["status"] = RUN_STATUS_PAUSED
        run["updated_at"] = utc_now_ms()
        return self._store.save_run(run)

    def choose_output(
        self,
        run_id: str,
        node_id: str,
        choice: str,
    ) -> DesignerExecutionRun:
        """Keep the original output or promote the regenerated candidate."""
        run = self._require_run(run_id)
        states = run.setdefault("node_states", {})
        state = states.get(node_id)
        if not isinstance(state, dict):
            raise KeyError(f"node not found: {node_id}")
        if state.get("status") == NODE_STATUS_RUNNING:
            raise ValueError("node is still running")
        decided = str(choice or "").strip()
        if decided not in {"original", "new"}:
            raise ValueError("choice must be original or new")
        candidate = state.get("candidate_output_ref")
        if not _usable_ref(candidate):
            raise ValueError("no pending revision")
        if decided == "new":
            refs = [ref for ref in (state.get("candidate_output_refs") or []) if _usable_ref(ref)]
            state["output_ref"] = candidate
            state["output_refs"] = refs or [candidate]
        state["candidate_output_ref"] = None
        state["candidate_output_refs"] = []
        run["updated_at"] = utc_now_ms()
        return self._store.save_run(run)

    def cancel_run(self, run_id: str) -> DesignerExecutionRun:
        run = self._require_run(run_id)
        self._cancel_flags.setdefault(run_id, asyncio.Event()).set()
        for task, (_, owner_run) in self._graph_tasks.items():
            if owner_run == run_id and not task.done() and not task.cancelling():
                task.cancel()
        target_node_id = _target_node_id(run)
        for node_id, state in run.get("node_states", {}).items():
            if target_node_id is not None and node_id != target_node_id:
                continue
            if state.get("status") in {NODE_STATUS_PENDING, NODE_STATUS_RUNNING}:
                state["status"] = NODE_STATUS_CANCELLED
        run["status"] = RUN_STATUS_CANCELLED
        run["current_node_ids"] = []
        run["updated_at"] = utc_now_ms()
        saved = self._store.save_run(run)
        self._cleanup_run(run_id)
        return saved

    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        """Start one node's Agent.

        Compose/ffmpeg is never allowed early — all shots (+ separate audio) must
        have completed first. Other nodes may still be spawned by agents.
        """
        target = str(node_id or "").strip()
        if not target:
            return "node_id is required"
        if self._is_cancelled(run_id):
            return "run cancelled"
        run = self._require_run(run_id)
        if run.get("status") not in {RUN_STATUS_RUNNING, RUN_STATUS_PAUSED}:
            return f"run is {run.get('status')}"
        target_node_id = _target_node_id(run)
        if target_node_id is not None and target != target_node_id:
            return f"this run only executes {target_node_id}"
        graph = self._require_graph(str(run.get("graph_id") or ""))
        try:
            target_node = _node_by_id(graph, target)
        except KeyError:
            return f"node not found: {target}"
        from jiuwenswarm.common.schema.designer_graph import is_compose_sink_node

        if is_compose_sink_node(target_node):
            preds = execution_predecessors(graph)
            groups = sync_groups(graph)
            if not _is_ready(target, run, preds, groups, graph):
                return (
                    f"compose blocked: waiting for all shots"
                    f"{' and audio nodes' if any(k.startswith('n_speech') or k.startswith('n_music') or 'speech' in k or 'music' in k for k in preds.get(target, [])) else ''}"
                    " to finish before ffmpeg"
                )
        workers = self._node_workers.setdefault(run_id, {})
        existing = workers.get(target)
        if existing is not None and not existing.done():
            return f"already running: {target}"
        running_count = sum(1 for item in workers.values() if not item.done())
        if running_count >= _MAX_CONCURRENT_NODE_AGENTS:
            return "too many concurrent node agents"
        lock = self._state_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self._require_run(run_id)
            states = run.setdefault("node_states", {})
            current = dict(states.get(target) or {"status": NODE_STATUS_PENDING})
            if current.get("status") == NODE_STATUS_RUNNING:
                return f"already running: {target}"
            if current.get("status") == NODE_STATUS_FAILED:
                return f"node failed: {target}; regenerate that node instead of spawning another"
            if current.get("status") in _TERMINAL_NODE_STATUSES:
                return f"already finished: {target}"
            states[target] = current
            current_ids = list(run.get("current_node_ids") or [])
            if target not in current_ids:
                current_ids.append(target)
                run["current_node_ids"] = current_ids
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
        on_update = self._on_updates.get(run_id)
        task = asyncio.create_task(
            self._run_spawned_node(run_id, target, on_update=on_update),
            name=f"designer-node-{run_id}-{target}",
        )
        workers[target] = task
        self._track_graph_task(run["graph_id"], task, run_id=run_id)
        return f"started {target}"

    def apply_agent_graph_patch(
        self,
        graph_id: str,
        patch: dict[str, Any],
    ) -> DesignerExecutionGraph:
        graph = self._require_graph(graph_id)
        if any(
            run["graph_id"] == graph_id and _target_node_id(run) is not None
            for run in self._live_runs.values()
        ):
            raise ValueError("Graph edits are unavailable during single-node generation")
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        locked = bool(meta.get("freeze_shot_topology"))
        failed = False
        for run in self._live_runs.values():
            if str(run.get("graph_id") or "") != str(graph_id):
                continue
            for state in (run.get("node_states") or {}).values():
                if (state or {}).get("status") == NODE_STATUS_FAILED:
                    failed = True
                    break
        if locked or failed:
            existing = {str(node.get("id") or "") for node in graph.get("nodes") or []}
            patch = dict(patch or {})
            upsert = patch.get("upsert_nodes") or []
            if isinstance(upsert, list):
                patch["upsert_nodes"] = [
                    item
                    for item in upsert
                    if isinstance(item, dict) and str(item.get("id") or "") in existing
                ]
            patch["remove_node_ids"] = []
        saved = self._store.save_graph(apply_graph_patch(graph, patch))
        node_ids = {node["id"] for node in saved.get("nodes") or []}
        for run_id, run in list(self._live_runs.items()):
            if run.get("graph_id") != graph_id:
                continue
            states = run.setdefault("node_states", {})
            for node_id in node_ids:
                states.setdefault(node_id, {"status": NODE_STATUS_PENDING})
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
            callback = self._on_graph_updates.get(run_id)
            if callback is not None:
                callback(deepcopy(saved))
        return saved

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict[str, Any]:
        graph = self._require_graph(graph_id)
        run = self._store.get_run(run_id)
        return {
            "graph": deepcopy(graph),
            "run": deepcopy(run) if run else None,
        }

    def _require_run(self, run_id: str) -> DesignerExecutionRun:
        live = self._live_runs.get(run_id)
        if live is not None:
            return live
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(f"designer run not found: {run_id}")
        return run

    def _require_graph(self, graph_id: str) -> DesignerExecutionGraph:
        graph = self._store.get_graph(graph_id)
        if graph is None:
            raise KeyError(f"designer graph not found: {graph_id}")
        return graph

    async def _execute_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        target_node_id = _target_node_id(run)
        if target_node_id is not None:
            await self._execute_node_run(graph, run, target_node_id, on_update=on_update)
            return
        # Always use the continuous wave scheduler. The agent-spawn scheduler
        # dropped ready nodes past the concurrency cap (spawn returned
        # "too many concurrent" and was ignored), which left the run stalled
        # with pending nodes — UI showed Continue. Leaf agents still run via
        # config.delegate=agent inside _run_single_node.
        _ = graph_uses_agent_scheduler  # retained import for callers/tests
        if _scope_node_ids(run):
            await self._execute_scoped_run(graph, run, on_update=on_update)
            return
        await self._execute_wave_run(graph, run, on_update=on_update)

    async def _execute_node_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        node_id: str,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        """Execute saved node content without whole-workflow planning or expansion."""
        from jiuwenswarm.server.runtime.designer.trajectory import begin_trajectory, end_trajectory

        run_id = run["run_id"]
        begin_trajectory(graph["graph_id"], run_id, meta={"target_node_id": node_id})
        try:
            await self._await_pause(run_id)
            if self._is_cancelled(run_id):
                return
            node = _node_by_id(graph, node_id)
            if not _is_ready(node_id, run, execution_predecessors(graph), sync_groups(graph), graph):
                raise ValueError(f"node is not ready: {node_id}")
            run["current_node_ids"] = [node_id]
            self._publish(run, on_update)
            await self._run_single_node(graph, run, node, on_update=on_update)
            if self._is_cancelled(run_id):
                return
            state = run["node_states"][node_id]
            run["status"] = (
                RUN_STATUS_COMPLETED if state["status"] == NODE_STATUS_COMPLETED else RUN_STATUS_FAILED
            )
        except asyncio.CancelledError:
            run = self.cancel_run(run_id)
            raise
        except Exception as exc:
            logger.exception("Designer node run %s failed", run_id)
            self._set_node_state(run, node_id, {"status": NODE_STATUS_FAILED, "error": _exception_text(exc)})
            _stamp_run_failure(run, exc)
        finally:
            run["current_node_ids"] = []
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
            self._publish(run, on_update)
            end_trajectory(run_id)
            self._cleanup_run(run_id)

    async def _execute_scoped_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        """Run only the scoped nodes, in order, with no Director phase or ratings."""
        run_id = run["run_id"]
        scope = _scope_node_ids(run)
        try:
            for node_id in scope:
                await self._await_pause(run_id)
                if self._is_cancelled(run_id):
                    return
                run["current_node_ids"] = [node_id]
                await self._run_single_node(
                    graph,
                    run,
                    _node_by_id(graph, node_id),
                    on_update=on_update,
                )
                if (run.get("node_states") or {}).get(node_id, {}).get(
                    "status"
                ) == NODE_STATUS_FAILED:
                    break
            if self._is_cancelled(run_id):
                return
            failed = any(
                (run.get("node_states") or {}).get(node_id, {}).get("status")
                != NODE_STATUS_COMPLETED
                for node_id in scope
            )
            run["status"] = RUN_STATUS_FAILED if failed else RUN_STATUS_COMPLETED
            if failed and not str(run.get("error") or "").strip():
                run["error"] = f"node did not finish: {', '.join(scope)}"
            run["current_node_ids"] = []
            run["updated_at"] = utc_now_ms()
            self._publish(run, on_update)
            self._store.save_run(run)
        except asyncio.CancelledError:
            run = self.cancel_run(run_id)
            self._publish(run, on_update)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Designer scoped run %s failed: %s", run_id, exc)
            _stamp_run_failure(run, exc)
            run["current_node_ids"] = []
            self._publish(run, on_update)
            self._store.save_run(run)
        finally:
            self._cleanup_run(run_id)

    async def _execute_agent_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        """Run all ready waves to completion (same one-pass contract as wave executor)."""
        run_id = run["run_id"]
        try:
            while True:
                await self._await_pause(run_id)
                if self._is_cancelled(run_id):
                    return
                run = self._require_run(run_id)
                graph = self._require_graph(str(run.get("graph_id") or graph.get("graph_id") or ""))
                incoming = execution_predecessors(graph)
                groups = sync_groups(graph)
                ready_ids = [
                    node["id"]
                    for node in graph.get("nodes") or []
                    if _is_ready(node["id"], run, incoming, groups, graph)
                ]
                ready_ids = filter_ready_by_dependency_order(
                    ready_ids,
                    preds=incoming,
                    in_flight=set(),
                    graph=graph,
                    run=run,
                )
                if not ready_ids:
                    pending = any(
                        (run.get("node_states") or {}).get(node["id"], {}).get("status")
                        == NODE_STATUS_PENDING
                        for node in graph.get("nodes") or []
                    )
                    run["status"] = RUN_STATUS_FAILED if pending else RUN_STATUS_COMPLETED
                    run["current_node_ids"] = []
                    run["updated_at"] = utc_now_ms()
                    self._publish(run, on_update)
                    self._store.save_run(run)
                    return
                run["current_node_ids"] = list(ready_ids)
                run["updated_at"] = utc_now_ms()
                self._publish(run, on_update)
                self._store.save_run(run)
                for node_id in ready_ids:
                    await self.spawn_node_agent(run_id, node_id)
                await self._wait_agent_workers(run_id)
                if self._is_cancelled(run_id):
                    return
                run = self._require_run(run_id)
                if NODE_STATUS_FAILED in {
                    state.get("status") for state in graph_node_states(run).values()
                }:
                    run["status"] = RUN_STATUS_FAILED
                    run["current_node_ids"] = []
                    run["updated_at"] = utc_now_ms()
                    self._publish(run, on_update)
                    self._store.save_run(run)
                    return
        except asyncio.CancelledError:
            run = self.cancel_run(run_id)
            self._publish(run, on_update)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Designer agent run %s failed: %s", run_id, exc)
            _stamp_run_failure(run, exc)
            self._publish(run, on_update)
            self._store.save_run(run)
        finally:
            self._cleanup_run(run_id)

    async def _execute_wave_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        run_id = run["run_id"]
        graph_id = str(graph.get("graph_id") or "")
        from jiuwenswarm.server.runtime.designer.feedback import load_latest_feedback
        from jiuwenswarm.server.runtime.designer.orchestration import (
            Director,
            write_run_feedback,
        )
        from jiuwenswarm.server.runtime.designer.trajectory import (
            begin_trajectory,
            end_trajectory,
            get_trajectory,
        )

        optimize_for = str((graph.get("metadata") or {}).get("optimize_for") or "quality")
        # One-pass: never consume prior ratings/feedback unless this is an explicit Run again.
        use_prior = bool(
            (graph.get("metadata") or {}).get("use_prior_feedback")
            or (run.get("metadata") or {}).get("use_prior_feedback")
        )
        single_node_rerun = bool((run.get("metadata") or {}).get("single_node_rerun"))
        prior: dict[str, Any] | None = None
        if use_prior:
            prior = load_latest_feedback(graph_id)
            if prior:
                meta = dict(graph.get("metadata") or {})
                meta["prior_feedback"] = prior
                meta["last_improvement_plan"] = str(
                    ((prior.get("final") or {}).get("improvement_plan"))
                    or meta.get("last_improvement_plan")
                    or ""
                )
                graph["metadata"] = meta
        else:
            # Strip stale prior so node agents do not re-apply old constraints mid-pass.
            meta = dict(graph.get("metadata") or {})
            meta.pop("prior_feedback", None)
            graph["metadata"] = meta
        traj = begin_trajectory(
            graph_id,
            run_id,
            project_id=str(graph.get("project_id") or ""),
            meta={
                "scenario": (graph.get("metadata") or {}).get("scenario"),
                "optimize_for": optimize_for,
                "skill_guided": bool((graph.get("metadata") or {}).get("skill_guided")),
                "audio_intent": (graph.get("metadata") or {}).get("audio_intent"),
                "use_prior_feedback": use_prior,
            },
        )
        agent_feedback: dict[str, dict[str, Any]] = {}
        try:
            with traj.span(
                agent_id="director",
                action="decide_capabilities",
                phase="orchestration",
                role="director",
                tool="structural",
            ):
                cap_plan = Director().decide_capabilities(graph)
                traj.record(
                    agent_id="director",
                    action="decide_capabilities_result",
                    phase="orchestration",
                    role="director",
                    detail={
                        "rating_modality": cap_plan.get("global_rating_modality"),
                        "can_vision": cap_plan.get("can_vision"),
                        "can_video": cap_plan.get("can_video"),
                        "reason": str(cap_plan.get("reason") or "")[:400],
                    },
                )
                graph = self._store.save_graph(graph)

            # Quality path: Brief+Storyboard redesign ONLY when Enter did not compose
            # or user explicitly asked Run again with prior feedback.
            scenario0 = str((graph.get("metadata") or {}).get("scenario") or "")
            meta_play = dict(graph.get("metadata") or {})
            already_composed = bool(meta_play.get("director_composed_on_bootstrap"))
            run_enter_redesign = (
                scenario0 == "video"
                and not single_node_rerun
                and (use_prior or not already_composed)
            )
            if run_enter_redesign:
                with traj.span(
                    agent_id="director",
                    action="author_creative_brief",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    brief_ack = await Director().author_creative_brief(
                        graph
                    )
                    traj.record(
                        agent_id="director",
                        action="author_creative_brief_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "source": brief_ack.get("source"),
                            "chars": brief_ack.get("chars"),
                            "notes": str(brief_ack.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                with traj.span(
                    agent_id="director",
                    action="review_brief",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    mgr_brief = await Director().review_brief(graph)
                    traj.record(
                        agent_id="director",
                        action="review_brief_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "source": mgr_brief.get("source"),
                            "patched": list(mgr_brief.get("patched") or [])[:20],
                            "notes": str(mgr_brief.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                with traj.span(
                    agent_id="director",
                    action="author_storyboard",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    sb_ack = await Director().author_storyboard(
                        graph
                    )
                    traj.record(
                        agent_id="director",
                        action="author_storyboard_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "source": sb_ack.get("source"),
                            "shot_count": sb_ack.get("shot_count"),
                            "notes": str(sb_ack.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                with traj.span(
                    agent_id="director",
                    action="review_storyboard_pre",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    mgr_sb = await Director().review_storyboard(
                        graph
                    )
                    traj.record(
                        agent_id="director",
                        action="review_storyboard_pre_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "source": mgr_sb.get("source"),
                            "patched": list(mgr_sb.get("patched") or [])[:20],
                            "notes": str(mgr_sb.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                # Director designs flexible multi-shot graph from locked Brief+Storyboard.
                with traj.span(
                    agent_id="director",
                    action="design_execution_graph",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    graph_ack = await Director().design_execution_graph(
                        graph,
                        optimize_for=optimize_for,
                    )
                    graph = self._store.save_graph(graph)
                    # Topology changed — resync run states and push canvas update now.
                    self._resync_run_after_graph_redesign(run, graph)
                    self._publish(run, on_update)
                    self._publish_graph(run, graph)
                    traj.record(
                        agent_id="director",
                        action="design_execution_graph_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "source": graph_ack.get("source"),
                            "shot_count": graph_ack.get("shot_count"),
                            "frame_nodes": graph_ack.get("frame_nodes"),
                            "node_count": len(graph.get("nodes") or []),
                            "notes": str(graph_ack.get("notes") or "")[:400],
                        },
                    )
            elif scenario0 == "video":
                traj.record(
                    agent_id="director",
                    action="skip_enter_redesign",
                    phase="orchestration",
                    role="director",
                    detail={
                        "reason": "director_composed_on_bootstrap",
                        "use_prior_feedback": use_prior,
                    },
                )

            if single_node_rerun:
                traj.record(
                    agent_id="director",
                    action="skip_plan_single_node_rerun",
                    phase="orchestration",
                    role="director",
                    detail={"reason": "single_node_rerun"},
                )
            else:
                with traj.span(
                    agent_id="director",
                    action="plan",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                    detail={"optimize_for": optimize_for, "has_prior_feedback": bool(prior)},
                ):
                    director_skill = str(
                        (graph.get("metadata") or {}).get("director_skill_excerpt") or ""
                    )
                    if director_skill:
                        meta = dict(graph.get("metadata") or {})
                        meta["active_director_skill"] = director_skill[:3000]
                        graph["metadata"] = meta
                    plan = await Director().plan(
                        graph,
                        optimize_for=optimize_for,
                        prior_feedback=prior,
                    )
                    traj.record(
                        agent_id="director",
                        action="plan_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "notes": str((plan or {}).get("notes") or "")[:500],
                            "rating_modality": (plan or {}).get("rating_modality"),
                        },
                    )
                    self._store.save_graph(graph)

                with traj.span(
                    agent_id="director",
                    action="validate_plan",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    director_ack = await Director().validate_plan(
                        graph
                    )
                    traj.record(
                        agent_id="director",
                        action="validate_plan_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "patched": list(director_ack.get("patched") or [])[:20],
                            "rating_modality": director_ack.get("rating_modality"),
                            "can_vision": director_ack.get("can_vision"),
                        },
                    )
                    graph = self._store.save_graph(graph)
                    self._resync_run_after_graph_redesign(run, graph)
                    self._publish(run, on_update)
                    self._publish_graph(run, graph)

            remaining = {
                node["id"]
                for node in graph.get("nodes", [])
                if (run.get("node_states") or {}).get(node["id"], {}).get("status")
                not in _TERMINAL_NODE_STATUSES
            }
            incoming = execution_predecessors(graph)
            groups = sync_groups(graph)
            graph, remaining, incoming, groups = self._expand_shots_if_needed(
                graph, run, remaining, on_update=on_update
            )
            # Continuous scheduling: start each node as soon as graph deps are met
            # (no wave barrier). Cap concurrency like the agent-scheduler path.
            in_flight: dict[str, asyncio.Task[None]] = {}
            sem = asyncio.Semaphore(_MAX_CONCURRENT_NODE_AGENTS)

            async def _run_guarded(node_id: str) -> None:
                async with sem:
                    await self._run_single_node(
                        graph,
                        run,
                        _node_by_id(graph, node_id),
                        on_update=on_update,
                        agent_feedback=agent_feedback,
                    )

            def _maybe_adjust_shots() -> None:
                nonlocal graph
                meta_live = dict(graph.get("metadata") or {})
                if meta_live.get("shots_adjusted_after_keyframes"):
                    return
                frame_nodes = [
                    n
                    for n in (graph.get("nodes") or [])
                    if node_pipeline(n) == NODE_ROLE_FRAME
                ]
                scene_nodes = [
                    n
                    for n in (graph.get("nodes") or [])
                    if node_pipeline(n) == NODE_ROLE_SCENE
                ]
                shot_pending = [
                    n
                    for n in (graph.get("nodes") or [])
                    if node_pipeline(n) == NODE_ROLE_CLIP
                    and (run.get("node_states") or {}).get(str(n.get("id") or ""), {}).get(
                        "status"
                    )
                    not in _TERMINAL_NODE_STATUSES
                ]
                frames_done = bool(frame_nodes) and all(
                    (run.get("node_states") or {}).get(str(n.get("id") or ""), {}).get("status")
                    in _TERMINAL_NODE_STATUSES
                    for n in frame_nodes
                )
                scenes_done = bool(scene_nodes) and all(
                    (run.get("node_states") or {}).get(str(n.get("id") or ""), {}).get("status")
                    in _TERMINAL_NODE_STATUSES
                    for n in scene_nodes
                )
                # Scene-specs path: adjust once all scene specs finish (no n_frame_*).
                # Legacy path: adjust once all keyframes finish.
                ready_gate = (
                    (scenes_done and not frame_nodes)
                    or frames_done
                )
                if not (ready_gate and shot_pending):
                    return
                with traj.span(
                    agent_id="director",
                    action="adjust_after_keyframes",
                    phase="orchestration",
                    role="director",
                    tool="structural",
                ):
                    adj_notes = Director().adjust_clips_after_keyframes(
                        graph,
                        node_states=run.get("node_states"),
                        agent_feedback=agent_feedback,
                    )
                    traj.record(
                        agent_id="director",
                        action="adjust_after_keyframes_result",
                        phase="orchestration",
                        role="director",
                        detail={"notes": adj_notes[:20], "rating_modality": "text_only"},
                    )
                with traj.span(
                    agent_id="director",
                    action="ack_keyframe_adjustment",
                    phase="orchestration",
                    role="director",
                    tool="structural",
                ):
                    Director().ack_keyframe_adjustment(graph, adj_notes)
                graph = self._store.save_graph(graph)

            async def _maybe_review_storyboard() -> None:
                nonlocal graph
                meta_live = dict(graph.get("metadata") or {})
                if meta_live.get("storyboard_reviewed"):
                    return
                sb_state = (run.get("node_states") or {}).get("n_storyboard") or {}
                if sb_state.get("status") not in _TERMINAL_NODE_STATUSES:
                    return
                with traj.span(
                    agent_id="director",
                    action="review_storyboard",
                    phase="orchestration",
                    role="director",
                    tool="llm",
                ):
                    sb_ack = await Director().review_storyboard_once(
                        graph,
                        node_states=run.get("node_states"),
                    )
                    traj.record(
                        agent_id="director",
                        action="review_storyboard_result",
                        phase="orchestration",
                        role="director",
                        detail={
                            "patched": list(sb_ack.get("patched") or [])[:20],
                            "source": sb_ack.get("source"),
                        },
                    )
                graph = self._store.save_graph(graph)

            while remaining or in_flight:
                await self._await_pause(run_id)
                if self._is_cancelled(run_id):
                    for task in in_flight.values():
                        task.cancel()
                    break

                # Nodes a node agent started via node_run finish outside in_flight.
                states_now = run.get("node_states") or {}
                remaining.difference_update(
                    {
                        nid
                        for nid in remaining
                        if (states_now.get(nid) or {}).get("status") in _TERMINAL_NODE_STATUSES
                    }
                )
                agent_started = [
                    task
                    for task in (self._node_workers.get(run_id) or {}).values()
                    if not task.done()
                ]

                newly_ready = [
                    node_id
                    for node_id in list(remaining)
                    if node_id not in in_flight
                    and _is_ready(node_id, run, incoming, groups, graph)
                ]
                newly_ready = filter_ready_by_dependency_order(
                    newly_ready,
                    preds=incoming,
                    in_flight=set(in_flight.keys()),
                    graph=graph,
                    run=run,
                )
                if newly_ready:
                    enable_a2a = bool((graph.get("metadata") or {}).get("enable_a2a_collab"))
                    if enable_a2a:
                        with traj.span(
                            agent_id="a2a",
                            action="collaborate_ready_wave",
                            phase="collaboration",
                            role="peer",
                            detail={"ready_ids": list(newly_ready)},
                        ):
                            await collaborate_ready_wave(graph, run, newly_ready)
                    for node_id in newly_ready:
                        remaining.discard(node_id)
                        in_flight[node_id] = asyncio.create_task(
                            _run_guarded(node_id),
                            name=f"designer-node-{node_id}",
                        )
                        self._track_graph_task(graph["graph_id"], in_flight[node_id], run_id=run_id)
                    run["current_node_ids"] = list(in_flight.keys())
                    self._publish(run, on_update)

                if not in_flight and not agent_started:
                    if remaining:
                        run["status"] = RUN_STATUS_FAILED
                        if not str(run.get("error") or "").strip():
                            run["error"] = (
                                "Nodes could not start because their inputs are not ready: "
                                + ", ".join(sorted(remaining))
                            )[:500]
                        run["updated_at"] = utc_now_ms()
                        self._publish(run, on_update)
                        self._store.save_run(run)
                        return
                    break

                # Wake early when an in-flight node publishes a prompt artifact so
                # soft dependents can join (still capped by the concurrency semaphore).
                wake = self._get_handoff_wake(run_id)
                wake.clear()
                wake_task = asyncio.create_task(wake.wait(), name=f"handoff-wake-{run_id}")
                wait_set: set[asyncio.Task[Any]] = set(in_flight.values())
                wait_set.update(agent_started)
                wait_set.add(wake_task)
                done, _pending = await asyncio.wait(
                    wait_set,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not wake_task.done():
                    wake_task.cancel()
                    try:
                        await wake_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                finished_ids: list[str] = []
                for task in done:
                    if task is wake_task:
                        continue
                    for nid, t in list(in_flight.items()):
                        if t is task:
                            finished_ids.append(nid)
                            in_flight.pop(nid, None)
                            break
                    exc = task.exception() if not task.cancelled() else None
                    if exc is not None:
                        logger.info("Node task error: %s", exc, exc_info=exc)

                for node_id in finished_ids:
                    state = (run.get("node_states") or {}).get(node_id) or {}
                    traj.record(
                        agent_id="director",
                        action="node_report",
                        phase="orchestration",
                        role="director",
                        detail={
                            "node_id": node_id,
                            "status": state.get("status"),
                            "has_output": bool(state.get("output_ref")),
                        },
                    )
                    if state.get("status") == NODE_STATUS_COMPLETED:
                        self._handoff_scene_prompt_after_frame(graph, node_id)

                run["current_node_ids"] = list(in_flight.keys())
                await _maybe_review_storyboard()
                _maybe_adjust_shots()
                graph, remaining, incoming, groups = self._expand_shots_if_needed(
                    graph, run, remaining, on_update=on_update
                )
                # Re-queue any newly expanded shot ids that are not in-flight.
                for node in graph.get("nodes") or []:
                    nid = str(node.get("id") or "")
                    if not nid or nid in in_flight:
                        continue
                    st = (run.get("node_states") or {}).get(nid) or {}
                    if st.get("status") not in _TERMINAL_NODE_STATUSES:
                        remaining.add(nid)
                if run.get("status") == RUN_STATUS_FAILED or self._is_cancelled(run_id):
                    if run.get("status") == RUN_STATUS_FAILED and in_flight:
                        for task in in_flight.values():
                            task.cancel()
                        await asyncio.gather(*in_flight.values(), return_exceptions=True)
                        in_flight.clear()
                        self._park_running_nodes(run, on_update)
                    break
            if self._is_cancelled(run_id):
                return
            if run.get("status") == RUN_STATUS_FAILED:
                self._park_running_nodes(run, on_update)
            statuses = {state.get("status") for state in graph_node_states(run).values()}
            if NODE_STATUS_FAILED in statuses or remaining:
                run["status"] = RUN_STATUS_FAILED
            else:
                run["status"] = RUN_STATUS_COMPLETED
            run["current_node_ids"] = []
            run["updated_at"] = utc_now_ms()

            # Write-only reports: director rates nodes → director rates all + director.
            # Never feed ratings back into this run (no loop).
            director_plan = (graph.get("metadata") or {}).get("director_plan") or {}
            with traj.span(
                agent_id="director",
                action="finalize",
                phase="orchestration",
                role="director",
                tool="llm",
            ):
                director_final = await Director().finalize(
                    graph,
                    agent_feedback=agent_feedback,
                    director_review={},
                    optimize_for=optimize_for,
                    node_states=run.get("node_states"),
                )
            with traj.span(
                agent_id="director",
                action="review",
                phase="orchestration",
                role="director",
                tool="llm",
            ):
                director_review = await Director().review(
                    graph,
                    agent_feedback=agent_feedback,
                    director_plan=director_plan if isinstance(director_plan, dict) else {},
                    prior_feedback=None,
                    optimize_for=optimize_for,
                    director_report=director_final,
                    node_states=run.get("node_states"),
                )
            with traj.span(
                agent_id="director",
                action="dual_rate_final",
                phase="orchestration",
                role="director",
                tool="llm",
            ):
                director_review = await Director().assign_dual_raters(
                    graph,
                    agent_feedback=agent_feedback,
                    director_final=director_final,
                    director_review=director_review,
                    node_states=run.get("node_states"),
                )
                traj.record(
                    agent_id="director",
                    action="dual_rate_final_result",
                    phase="orchestration",
                    role="director",
                    detail={
                        "aggregated_overall": director_review.get("aggregated_overall"),
                        "raters": len((director_review.get("dual_raters") or {}).get("raters") or []),
                        "recommendations": len(
                            director_review.get("aggregated_recommendations") or []
                        ),
                    },
                )
            feedback_path = await write_run_feedback(
                graph=graph,
                run_id=run_id,
                agent_feedback=agent_feedback,
                director_plan=director_plan if isinstance(director_plan, dict) else {},
                director_review=director_review,
                director_final=director_final,
                optimize_for=optimize_for,
            )
            traj.set_feedback(
                {
                    "agents": agent_feedback,
                    "director": {
                        "plan": director_plan,
                        "scores": director_final.get("scores"),
                        "node_reports": director_final.get("node_reports"),
                        "summary": director_final.get("summary"),
                        "suggestions": director_final.get("suggestions"),
                        "review": director_review,
                    },
                    "final": {
                        "improvement_plan": director_final.get("improvement_plan"),
                        "aggregated_score": director_final.get("aggregated_score"),
                        "summary": director_final.get("summary"),
                        "apply_on": "run_again_only",
                    },
                    "feedback_path": str(feedback_path) if feedback_path else None,
                }
            )
            meta = dict(graph.get("metadata") or {})
            meta["last_feedback_run_id"] = run_id
            meta["last_trajectory_run_id"] = run_id
            meta["last_aggregated_score"] = director_final.get("aggregated_score")
            meta["last_improvement_plan"] = director_final.get("improvement_plan")
            meta["use_prior_feedback"] = False
            graph["metadata"] = meta
            self._store.save_graph(graph)
            self._publish(run, on_update)
            self._store.save_run(run)
        except asyncio.CancelledError:
            run = self.cancel_run(run_id)
            self._publish(run, on_update)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Designer run %s failed: %s", run_id, exc)
            _stamp_run_failure(run, exc)
            self._publish(run, on_update)
            self._store.save_run(run)
            rec = get_trajectory(run_id)
            if rec is not None:
                rec.record(
                    agent_id="executor",
                    action="run_failed",
                    phase="system",
                    status="error",
                    detail={"error": str(run.get("error") or exc)},
                )
        finally:
            end_trajectory(run_id)
            self._cleanup_run(run_id)

    def reconcile_loaded_graph(self, graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
        """Expand per-shot keyframe nodes when a completed storyboard already exists."""
        graph_id = str(graph.get("graph_id") or "")
        run = None
        for live in self._live_runs.values():
            if str(live.get("graph_id") or "") == graph_id:
                run = live
                break
        if run is None:
            run = self._store.get_latest_run_for_graph(graph_id)
        if run is None:
            return graph
        expanded, _, _, _ = self._expand_shots_if_needed(
            graph, run, set(), on_update=None
        )
        return expanded

    def _expand_shots_if_needed(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        remaining: set[str],
        *,
        on_update: RunUpdateCallback | None,
    ) -> tuple[
        DesignerExecutionGraph,
        set[str],
        dict[str, list[str]],
        dict[str, frozenset[str]],
    ]:
        shot_rows = self._completed_storyboard_shots(graph, run)
        if shot_rows is None:
            return graph, remaining, execution_predecessors(graph), sync_groups(graph)
        shot_count = max(1, min(len(shot_rows) or 1, MAX_SHOT_CLIP_NODES))
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import shot_generate_prompt

        prompts = [shot_generate_prompt(shot) for shot in shot_rows]
        has_shot_pipeline = any(
            node_pipeline(node) in {NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}
            or str(node.get("id") or "") in {"n_clip", "n_compose"}
            or str(node.get("id") or "").startswith("n_clip_")
            for node in graph.get("nodes") or []
        )
        if not has_shot_pipeline:
            return graph, remaining, execution_predecessors(graph), sync_groups(graph)
        # Flexible Director graphs: if storyboard shot count differs from frame
        # nodes, rebuild from analysis (do NOT use expand_shot_nodes — that dumps
        # all cast into every frame and breaks identity wiring).
        meta = graph.get("metadata") or {}
        current_frame_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if str(node.get("id") or "").startswith("n_frame_")
            or node_pipeline(node) == NODE_ROLE_FRAME
        }
        uses_scene_card_plus_clip_shots = (
            str(meta.get("scene_continuity_mode") or "") == "scene_card_plus_clip_shots"
        )
        if uses_scene_card_plus_clip_shots and not current_frame_ids:
            meta = dict(meta)
            meta["freeze_shot_topology"] = True
            graph["metadata"] = meta
        if len(current_frame_ids) != shot_count and not bool(meta.get("freeze_shot_topology")):
            from jiuwenswarm.server.runtime.designer.smart_graph import (
                apply_runtime_delegate,
                build_smart_video_graph,
            )

            analysis = dict(meta.get("script_analysis") or {})
            analysis["shots"] = [
                {
                    "shot_index": i,
                    "timeline": getattr(row, "timeline", None)
                    or (row.get("timeline") if isinstance(row, dict) else "")
                    or f"{(i - 1) * 5:.1f}-{i * 5:.1f}s",
                    "camera": getattr(row, "camera", None)
                    or (row.get("camera") if isinstance(row, dict) else "")
                    or "medium / eye-level",
                    "action": getattr(row, "character_action", None)
                    or getattr(row, "action", None)
                    or (row.get("action") if isinstance(row, dict) else "")
                    or "",
                    "character_ids": list(
                        getattr(row, "character_ids", None)
                        or (row.get("character_ids") if isinstance(row, dict) else [])
                        or []
                    ),
                    "keyframe_prompt": getattr(row, "keyframe_prompt", None)
                    or (row.get("keyframe_prompt") if isinstance(row, dict) else "")
                    or "",
                    "setting_id": (row.get("setting_id") if isinstance(row, dict) else None)
                    or "set_1",
                }
                for i, row in enumerate(shot_rows, start=1)
            ]
            analysis["target_shot_count"] = shot_count
            rebuilt = build_smart_video_graph(
                project_id=str(graph.get("project_id") or "project"),
                prompt=str(graph.get("description") or ""),
                analysis=analysis,
                title=str(graph.get("title") or "") or None,
                optimize_for=str(meta.get("optimize_for") or "quality"),
            )
            rebuilt["graph_id"] = graph.get("graph_id") or rebuilt.get("graph_id")
            rebuilt["project_id"] = graph.get("project_id") or rebuilt.get("project_id")
            rmeta = dict(rebuilt.get("metadata") or {})
            for key in (
                "approved_brief",
                "approved_storyboard",
                "user_prompt",
                "director_lock_ack",
            ):
                if key in meta and meta.get(key) is not None:
                    rmeta[key] = meta.get(key)
            rmeta["freeze_shot_topology"] = True
            rmeta["script_analysis"] = analysis
            rebuilt["metadata"] = rmeta
            carry_user_references(meta, rebuilt)
            saved = self._store.save_graph(apply_runtime_delegate(rebuilt))
            live_ids = {str(node.get("id") or "") for node in saved.get("nodes") or []}
            states = run.setdefault("node_states", {})
            for node_id in live_ids:
                states.setdefault(node_id, {"status": NODE_STATUS_PENDING})
            remaining = {
                node_id
                for node_id in live_ids
                if (states.get(node_id) or {}).get("status") not in _TERMINAL_NODE_STATUSES
            }
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
            self._publish(run, on_update)
            callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
            if callback is not None:
                callback(deepcopy(saved))
            return saved, remaining, execution_predecessors(saved), sync_groups(saved)

        if bool(meta.get("freeze_shot_topology")):
            synced = apply_shot_generate_prompts(graph, prompts)
            if synced is graph:
                return graph, remaining, execution_predecessors(graph), sync_groups(graph)
            saved = self._store.save_graph(synced)
            callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
            if callback is not None:
                callback(deepcopy(saved))
            return saved, remaining, execution_predecessors(saved), sync_groups(saved)
        current_shot_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if node_pipeline(node) == NODE_ROLE_CLIP
        }
        current_frame_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if node_pipeline(node) == NODE_ROLE_FRAME
        }
        wanted_shot_ids = {clip_node_id(index) for index in range(1, shot_count + 1)}
        wanted_frame_ids = {frame_node_id(index) for index in range(1, shot_count + 1)}
        has_compose = any(
            node_pipeline(node) == NODE_ROLE_COMPOSE or str(node.get("id") or "") == "n_compose"
            for node in graph.get("nodes") or []
        )
        topology_matches = (
            current_shot_ids == wanted_shot_ids
            and current_frame_ids == wanted_frame_ids
            and has_compose
        )
        bundled_frames = any(
            len(_image_output_refs((run.get("node_states") or {}).get(node_id) or {})) > 1
            for node_id in (current_frame_ids | {"n_frame"})
        )
        if topology_matches and not bundled_frames:
            synced = apply_shot_generate_prompts(graph, prompts)
            if synced is graph:
                return graph, remaining, execution_predecessors(graph), sync_groups(graph)
            saved = self._store.save_graph(synced)
            callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
            if callback is not None:
                callback(deepcopy(saved))
            return saved, remaining, execution_predecessors(saved), sync_groups(saved)

        # Prefer Director-style rebuild over expand_shot_nodes (identity-safe).
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            apply_runtime_delegate,
            build_smart_video_graph,
        )

        analysis = dict(meta.get("script_analysis") or {})
        analysis["target_shot_count"] = shot_count
        rebuilt = build_smart_video_graph(
            project_id=str(graph.get("project_id") or "project"),
            prompt=str(graph.get("description") or ""),
            analysis=analysis,
            title=str(graph.get("title") or "") or None,
            optimize_for=str(meta.get("optimize_for") or "quality"),
        )
        rebuilt["graph_id"] = graph.get("graph_id") or rebuilt.get("graph_id")
        rebuilt["project_id"] = graph.get("project_id") or rebuilt.get("project_id")
        rmeta = dict(rebuilt.get("metadata") or {})
        for key in (
            "approved_brief",
            "approved_storyboard",
            "user_prompt",
            "director_lock_ack",
        ):
            if key in meta and meta.get(key) is not None:
                rmeta[key] = meta.get(key)
        rmeta["freeze_shot_topology"] = True
        rebuilt["metadata"] = rmeta
        carry_user_references(meta, rebuilt)
        saved = self._store.save_graph(
            apply_shot_generate_prompts(apply_runtime_delegate(rebuilt), prompts)
        )
        live_ids = {str(node.get("id") or "") for node in saved.get("nodes") or []}
        states = run.setdefault("node_states", {})
        for node_id in live_ids:
            states.setdefault(node_id, {"status": NODE_STATUS_PENDING})
        redistribute_frame_node_states(run, shot_count)
        remaining = {
            node_id
            for node_id in live_ids
            if (states.get(node_id) or {}).get("status") not in _TERMINAL_NODE_STATUSES
        }
        run["updated_at"] = utc_now_ms()
        self._store.save_run(run)
        self._publish(run, on_update)
        callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
        if callback is not None:
            callback(deepcopy(saved))
        return saved, remaining, execution_predecessors(saved), sync_groups(saved)

    def _handoff_scene_prompt_after_frame(
        self,
        graph: DesignerExecutionGraph,
        completed_id: str,
    ) -> None:
        """Forward scene specs / master prompt after a scene specs or scene-master frame completes."""
        by_id = {
            str(n.get("id") or ""): n
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and n.get("id")
        }
        src_id = str(completed_id or "").strip()
        src = by_id.get(src_id)
        if not src:
            return
        role = node_pipeline(src)
        is_scene = role == NODE_ROLE_SCENE or src_id.startswith("n_scene_")
        is_frame = role == NODE_ROLE_FRAME
        if not is_scene and not is_frame:
            return
        scfg = src.get("config") if isinstance(src.get("config"), dict) else {}
        if is_frame and not bool(scfg.get("is_scene_master")):
            return
        setting_id = str(scfg.get("setting_id") or "").strip()
        if not setting_id:
            return
        specs = scfg.get("scene_specs") if isinstance(scfg.get("scene_specs"), dict) else None
        if not specs:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            locks = meta.get("scene_locks") if isinstance(meta.get("scene_locks"), dict) else {}
            maybe = locks.get(setting_id) if isinstance(locks, dict) else None
            if isinstance(maybe, dict):
                specs = maybe
        from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import (
            architecture_clause_from_bible,
        )

        arch = architecture_clause_from_bible(specs)
        gen_src = dict(scfg.get("generate") or {}) if isinstance(scfg.get("generate"), dict) else {}
        master_prompt = str(
            scfg.get("scene_master_prompt") or gen_src.get("prompt") or arch or ""
        ).strip()[:900]
        if not arch and not specs and not master_prompt:
            return
        changed = False
        target_role = NODE_ROLE_CLIP if is_scene else NODE_ROLE_FRAME
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            nid = str(node.get("id") or "")
            if nid == src_id or node_pipeline(node) != target_role:
                continue
            cfg = dict(node.get("config") or {})
            if str(cfg.get("setting_id") or "").strip() != setting_id:
                continue
            if target_role == NODE_ROLE_FRAME and bool(cfg.get("is_scene_master")):
                continue
            cfg["scene_prompt_handoff_from"] = src_id
            if is_scene:
                cfg["master_scene_node_id"] = src_id
                cfg["scene_node_id"] = cfg.get("scene_node_id") or src_id
            if specs:
                cfg["scene_specs"] = dict(specs)
            if arch or master_prompt:
                cfg["scene_architecture_clause"] = arch or master_prompt[:900]
                cfg["scene_master_prompt"] = master_prompt or (arch[:900] if arch else "")
            gen2 = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            prompt = str(gen2.get("prompt") or "")
            marker = "SCENE PROMPT HANDOFF"
            handoff_arch = arch or master_prompt
            if marker not in prompt and handoff_arch:
                handoff_bit = (
                    f"{marker} from {src_id} (same setting `{setting_id}`): "
                    f"{handoff_arch} Change ONLY camera view + on-screen cast/actions for This shot."
                )
                gen2["prompt"] = (prompt + "\n" + handoff_bit).strip()[:2200]
                cfg["generate"] = gen2
            irefs = dict(cfg.get("identity_refs") or {})
            irefs["scene_prompt_handoff_from"] = src_id
            if is_scene:
                irefs["master_scene_node_id"] = src_id
                irefs["scene_node_id"] = irefs.get("scene_node_id") or src_id
                if specs:
                    irefs["scene_specs"] = dict(specs)
            else:
                irefs["keyframe_strategy"] = "compose_from_solo_refs"
                cfg["keyframe_strategy"] = "compose_from_solo_refs"
            cfg["identity_refs"] = irefs
            node["config"] = cfg
            changed = True
        if changed:
            self._store.save_graph(graph)

    def _completed_storyboard_shots(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
    ) -> list | None:
        from jiuwenswarm.server.runtime.designer.handlers.common import role_output_text
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
            parse_storyboard_shots,
        )
        from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

        storyboard = next(
            (
                node
                for node in graph.get("nodes") or []
                if node_pipeline(node) == NODE_ROLE_STORYBOARD
            ),
            None,
        )
        if storyboard is None:
            return []
        node_id = str(storyboard.get("id") or "")
        status = ((run.get("node_states") or {}).get(node_id) or {}).get("status")
        if status != NODE_STATUS_COMPLETED:
            return None
        ctx = NodeExecutionContext(
            graph=graph,
            run_id=str(run.get("run_id") or ""),
            node_id=node_id,
            run=run,
        )
        text = role_output_text(ctx, NODE_ROLE_STORYBOARD)
        shots = parse_storyboard_shots(text)
        return shots[:MAX_SHOT_CLIP_NODES]

    def _completed_storyboard_shot_count(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
    ) -> int | None:
        shots = self._completed_storyboard_shots(graph, run)
        if shots is None:
            return None
        return max(1, min(len(shots) or 1, MAX_SHOT_CLIP_NODES))

    async def _wait_agent_workers(self, run_id: str) -> None:
        while True:
            if self._is_cancelled(run_id):
                return
            await self._await_pause(run_id)
            workers = self._node_workers.get(run_id) or {}
            pending = [task for task in workers.values() if not task.done()]
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _run_spawned_node(
        self,
        run_id: str,
        node_id: str,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        try:
            await self._await_pause(run_id)
            if self._is_cancelled(run_id):
                return
            run = self._require_run(run_id)
            graph = self._require_graph(str(run.get("graph_id") or ""))
            node = _node_by_id(graph, node_id)
            await self._run_single_node(graph, run, node, on_update=on_update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Designer spawned node %s failed in run %s", node_id, run_id)
        finally:
            workers = self._node_workers.get(run_id)
            if workers is not None:
                workers.pop(node_id, None)

    def _cleanup_run(self, run_id: str) -> None:
        if any(owner[1] == run_id and not task.done() for task, owner in self._graph_tasks.items()):
            return
        self._tasks.pop(run_id, None)
        self._node_workers.pop(run_id, None)
        self._pause_flags.pop(run_id, None)
        self._cancel_flags.pop(run_id, None)
        self._state_locks.pop(run_id, None)
        self._on_updates.pop(run_id, None)
        self._on_graph_updates.pop(run_id, None)
        self._live_runs.pop(run_id, None)
        self._handoff_wake.pop(run_id, None)
        self._host.drop_run(run_id)

    async def _run_single_node(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        node: DesignerGraphNode,
        *,
        on_update: RunUpdateCallback | None,
        agent_feedback: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        node_id = node["id"]
        started_at = utc_now_ms()
        blocked_by = list(sync_groups(graph).get(node_id, frozenset()) - {node_id})
        lock = self._state_locks.setdefault(run["run_id"], asyncio.Lock())
        from jiuwenswarm.common.schema.designer_graph import (
            node_pipeline as _node_pipeline_fn,
            node_role as _node_role_fn,
        )
        from jiuwenswarm.server.runtime.designer.skills_loader import (
            load_agent_skill,
            load_subject_skill,
        )
        from jiuwenswarm.server.runtime.designer.trajectory import get_trajectory

        role = str(
            _node_pipeline_fn(node)
            or (node.get("config") or {}).get("pipeline")
            or _node_role_fn(node)
            or node_id
        )
        traj = get_trajectory(run["run_id"])
        # Leaf nodes: task skill only (already on config). Do not dump scenario/subjects.
        skill = str((node.get("config") or {}).get("skill_excerpt") or "").strip()
        if not skill:
            stamped_skill = str((node.get("config") or {}).get("skill_id") or "").strip()
            skill = (
                (load_agent_skill(stamped_skill) if stamped_skill else "")
                or load_agent_skill(role)
                or load_agent_skill(node_id)
                or ""
            )[:1200]
            if skill:
                cfg = dict(node.get("config") or {})
                cfg["skill_excerpt"] = skill
                node["config"] = cfg
        # Optional tiny subject hint for character/scene only (not full encyclopedia).
        if role in {"character", "character_design", "scene"}:
            subjects = list((graph.get("metadata") or {}).get("subject_keys") or [])[:1]
            if subjects:
                bit = load_subject_skill(subjects[0])
                if bit:
                    cfg = dict(node.get("config") or {})
                    cfg["skill_excerpt"] = (
                        str(cfg.get("skill_excerpt") or "") + "\n\n" + bit[:600]
                    ).strip()[:1800]
                    node["config"] = cfg
        if _target_node_id(run) is None and (graph.get("metadata") or {}).get("use_prior_feedback"):
            cfg = dict(node.get("config") or {})
            prior_plan = str((graph.get("metadata") or {}).get("last_improvement_plan") or "")
            from jiuwenswarm.server.runtime.designer.feedback import (
                director_node_suggestions,
            )

            node_suggestion = director_node_suggestions(
                (graph.get("metadata") or {}).get("prior_feedback")
            ).get(node_id)
            if node_suggestion:
                cfg["rerun_suggestion"] = str(node_suggestion)
            if prior_plan and not cfg.get("rerun_suggestion"):
                cfg["rerun_suggestion"] = prior_plan[:1500]
            node["config"] = cfg

        # Director leaf prompt gate + prior-shot handoff for frame/shot media.
        role_for_gate = str(
            _node_pipeline_fn(node)
            or (node.get("config") or {}).get("role")
            or ""
        ).lower()
        if not is_comfyui_node(node) and role_for_gate in {
            "frame",
            "keyframe",
            "clip",
            "character",
            "character_design",
            "scene",
        }:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            live_graph = self._require_graph(
                str(run.get("graph_id") or graph.get("graph_id") or "")
            )
            gate = Director().review_leaf_media_prompt(live_graph, node)
            # Always persist Director lock-gate stamps (even when prompt text unchanged).
            graph = live_graph
            self._store.save_graph(graph)
            for n in graph.get("nodes") or []:
                if str(n.get("id") or "") == node_id:
                    node = n
                    break
            _ = gate
            # Early unlock: publish approved prompt so soft dependents can start while
            # this node still calls image/video tools (concurrency cap unchanged).
            self._publish_prompt_artifact_early(
                graph, run, node, on_update=on_update
            )

        async with lock:
            self._set_node_state(
                run,
                node_id,
                {
                    "status": NODE_STATUS_RUNNING,
                    "started_at": started_at,
                    "error": None,
                    "blocked_by": blocked_by,
                },
            )
            self._store.save_run(run)
            self._publish(run, on_update, node_id)
        tool_name = (
            "handler"
            if _director_text_ready(graph, node) or not node_uses_agent_runtime(node)
            else "node_agent"
        )
        span_cm = (
            traj.span(
                agent_id=node_id,
                action="execute",
                phase="node",
                role=role,
                tool=tool_name,
                detail={
                    "label": node.get("label"),
                    "type": node.get("type"),
                    "has_skill": bool((node.get("config") or {}).get("skill_excerpt")),
                },
            )
            if traj is not None
            else nullcontext()
        )
        try:
            with span_cm as span_detail:
                def emit_activity(
                    kind: str,
                    text: str,
                    tool: str = "",
                    *,
                    force: bool = False,
                ) -> None:
                    emit_run_activity(
                        run,
                        node_id,
                        kind=kind,
                        text=text,
                        tool=tool,
                        on_update=on_update,
                        force=force,
                    )

                from jiuwenswarm.common.schema.designer_graph import (
                    ACTIVITY_KIND_STAGE,
                    ACTIVITY_KIND_TOOL_CALL,
                    ACTIVITY_KIND_THINKING,
                )
                from jiuwenswarm.server.runtime.designer.activity import stage_text_for_node
                # Regenerating this node itself retires the uploaded stand-in, so the
                # new file can become the output. Downstream reads keep the stand-in
                # until that happens.
                live_cfg = node.get("config") if isinstance(node.get("config"), dict) else None
                if isinstance(live_cfg, dict) and live_cfg.pop("user_replaced_output", None):
                    node["config"] = live_cfg
                    self._store.save_graph(graph)
                # User uploads are immutable source assets. Even if an older saved
                # graph incorrectly says delegate=agent, never regenerate them.
                def _on_prompt_artifact(text: str) -> None:
                    self._publish_prompt_artifact_early(
                        graph,
                        run,
                        node,
                        prompt=str(text or ""),
                        on_update=on_update,
                    )

                ctx = NodeExecutionContext(
                    graph=graph,
                    run_id=run["run_id"],
                    node_id=node_id,
                    run=run,
                    emit_activity=emit_activity,
                    on_prompt_artifact=_on_prompt_artifact,
                )
                from jiuwenswarm.server.runtime.designer.graph_store import reload_graph_inplace

                if reload_graph_inplace(ctx.graph):
                    for fresh_node in ctx.graph.get("nodes") or []:
                        if isinstance(fresh_node, dict) and str(fresh_node.get("id") or "") == node_id:
                            node = fresh_node
                            break
                # User uploads stay on the handler. Director markdown for brief
                # and storyboard is the node file, so those nodes use the handler
                # too and do not call the leaf model again.
                uses_agent = (
                    node_uses_agent_runtime(node)
                    and not is_uploaded_media_node(node)
                    and not is_comfyui_node(node)
                    and not _director_text_ready(ctx.graph, node)
                )
                emit_activity(
                    ACTIVITY_KIND_THINKING,
                    f"starting {node.get('label') or node_id}",
                    force=True,
                )
                emit_activity(
                    ACTIVITY_KIND_TOOL_CALL if uses_agent else ACTIVITY_KIND_STAGE,
                    stage_text_for_node(node),
                    tool="node_agent" if uses_agent else "handler",
                    force=True,
                )
                if uses_agent:
                    result = await self._host.execute(node, ctx)
                    handler_name = "NodeAgentHost"
                else:
                    handler = get_node_handler(node)
                    result = await asyncio.wait_for(
                        handler.execute(node, ctx),
                        timeout=_node_execute_timeout_sec(node),
                    )
                    handler_name = type(handler).__name__
                    if _MOCK_NODE_DELAY_SECONDS:
                        await asyncio.sleep(_MOCK_NODE_DELAY_SECONDS)
                if isinstance(span_detail, dict):
                    span_detail["message"] = str(getattr(result, "message", "") or "")[:300]
                    span_detail["handler"] = handler_name
                if agent_feedback is not None:
                    agent_feedback[node_id] = {
                        "agent_id": (node.get("config") or {}).get("agent_id") or node_id,
                        "agent_name": (node.get("config") or {}).get("agent_name")
                        or node.get("label")
                        or node_id,
                        "role": role,
                        "message": str(getattr(result, "message", "") or ""),
                        "self_score": 7,
                        "notes": str(getattr(result, "message", "") or "")[:500],
                        "suggestion_for_next": str(
                            (node.get("config") or {}).get("rerun_suggestion") or ""
                        ),
                        "tool": tool_name,
                    }
            if self._is_cancelled(run["run_id"]):
                return
            refs = [ref for ref in (result.output_refs or []) if ref]
            primary = result.output_ref or (refs[0] if refs else None)
            if primary is not None:
                # Always keep primary media first even when sidecars fill output_refs.
                refs = [primary, *[r for r in refs if r is not primary]]
                # Dedup by uri.
                seen: set[str] = set()
                deduped = []
                for ref in refs:
                    uri = str((ref or {}).get("uri") or "")
                    if not uri or uri in seen:
                        continue
                    seen.add(uri)
                    deduped.append(ref)
                refs = deduped
            if node_pipeline(node) == NODE_ROLE_FRAME:
                image_refs = [ref for ref in refs if _ref_kind(ref) == "image"]
                if image_refs:
                    primary = image_refs[0]
                    refs = [image_refs[0]]
            # Always auto-accept new outputs (no one-by-one approval pause).
            async with lock:
                self._set_node_state(
                    run,
                    node_id,
                    {
                        "status": NODE_STATUS_COMPLETED,
                        "started_at": started_at,
                        "completed_at": utc_now_ms(),
                        "output_ref": primary,
                        "output_refs": refs,
                        "candidate_output_ref": None,
                        "candidate_output_refs": [],
                        "error": None,
                        "blocked_by": [],
                    },
                )
            if node_pipeline(node) == NODE_ROLE_STORYBOARD:
                live_graph = self._require_graph(str(run.get("graph_id") or graph.get("graph_id") or ""))
                try:
                    from jiuwenswarm.server.runtime.designer.handlers.common import (
                        role_output_text as _role_text,
                    )
                    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
                        sync_shot_nodes_from_storyboard_markdown,
                    )
                    from jiuwenswarm.server.runtime.designer.handlers.types import (
                        NodeExecutionContext as _Ctx,
                    )

                    sb_ctx = _Ctx(
                        graph=live_graph,
                        run_id=str(run.get("run_id") or ""),
                        node_id=node_id,
                        run=run,
                    )
                    sb_text = _role_text(sb_ctx, NODE_ROLE_STORYBOARD)
                    if sb_text:
                        notes = sync_shot_nodes_from_storyboard_markdown(live_graph, sb_text)
                        if notes:
                            meta = dict(live_graph.get("metadata") or {})
                            meta["storyboard_shot_sync"] = notes[:40]
                            live_graph["metadata"] = meta
                            self._store.save_graph(live_graph)
                except Exception:  # noqa: BLE001
                    logger.debug("storyboard→shot sync failed", exc_info=True)
                self._expand_shots_if_needed(live_graph, run, set(), on_update=on_update)
            # Stamp prior-shot prompt handoff after frame/shot media completes.
            if node_pipeline(node) in {NODE_ROLE_FRAME, NODE_ROLE_CLIP}:
                live_graph = self._require_graph(
                    str(run.get("graph_id") or graph.get("graph_id") or "")
                )
                cfg_done = dict(node.get("config") or {})
                # Prefer the prompt actually sent to video gen — last_approved / generate
                # can be empty on the executor snapshot while last_wan_prompt was set.
                approved = str(
                    cfg_done.get("last_wan_prompt")
                    or cfg_done.get("last_approved_prompt")
                    or cfg_done.get("prompt")
                    or (cfg_done.get("generate") or {}).get("prompt")
                    or ""
                ).strip()
                shot_index = int(cfg_done.get("shot_index") or 0)
                if approved:
                    for n in live_graph.get("nodes") or []:
                        if str(n.get("id") or "") != node_id:
                            continue
                        c = dict(n.get("config") or {})
                        c["last_approved_prompt"] = approved[:4000]
                        if node_pipeline(node) == NODE_ROLE_CLIP:
                            c["last_wan_prompt"] = approved[:4000]
                            c["clip_prompt_preview"] = approved[:1200]
                        n["config"] = c
                        break
                    if node_pipeline(node) == NODE_ROLE_FRAME and shot_index >= 1:
                        next_frame = f"n_frame_{shot_index + 1}"
                        next_shot = f"n_clip_{shot_index + 1}"
                        action_snip = str(
                            cfg_done.get("shot_action")
                            or cfg_done.get("character_action")
                            or ""
                        ).strip() or approved[:220]
                        for n in live_graph.get("nodes") or []:
                            nid = str(n.get("id") or "")
                            if nid not in {next_frame, next_shot}:
                                continue
                            c = dict(n.get("config") or {})
                            # Always overwrite consecutive stamp (parallel KF race fix).
                            c["previous_keyframe_action"] = action_snip[:300]
                            c["previous_keyframe_prompt"] = approved[:800]
                            c["previous_keyframe_node_id"] = node_id
                            n["config"] = c
                    if node_pipeline(node) == NODE_ROLE_CLIP and shot_index >= 1:
                        from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
                            stamp_wan_prompt_handoff,
                        )

                        stamp_wan_prompt_handoff(
                            live_graph,
                            shot_index=shot_index,
                            prompt=approved,
                            node_id=node_id,
                        )
                    graph = self._store.save_graph(live_graph)
        except Exception as exc:  # noqa: BLE001
            async with lock:
                self._set_node_state(
                    run,
                    node_id,
                    {
                        "status": NODE_STATUS_FAILED,
                        "started_at": started_at,
                        "completed_at": utc_now_ms(),
                        "error": _exception_text(exc),
                    },
                )
                _stamp_run_failure(run, exc)
            if traj is not None:
                traj.record(
                    agent_id=node_id,
                    action="execute_failed",
                    phase="node",
                    role=role,
                    tool=tool_name,
                    status="error",
                    detail={"error": _exception_text(exc)},
                )
            if agent_feedback is not None:
                agent_feedback[node_id] = {
                    "agent_id": node_id,
                    "role": role,
                    "self_score": 2,
                    "notes": _exception_text(exc),
                    "suggestion_for_next": "Retry with adjusted params from director plan",
                    "tool": tool_name,
                }
        finally:
            async with lock:
                run["updated_at"] = utc_now_ms()
                self._publish(run, on_update, node_id)
                self._store.save_run(run)

    async def _await_pause(self, run_id: str) -> None:
        pause_flag = self._pause_flags.get(run_id)
        if pause_flag is None:
            return
        await pause_flag.wait()

    def _is_cancelled(self, run_id: str) -> bool:
        cancel_flag = self._cancel_flags.get(run_id)
        return cancel_flag is not None and cancel_flag.is_set()

    def _park_running_nodes(
        self,
        run: DesignerExecutionRun,
        on_update: RunUpdateCallback | None,
    ) -> None:
        """Return in-flight siblings to pending so Continue can resume them.

        A sibling failure used to leave other leaves ``running``. Those snapshots
        are never scheduled again, so the canvas spinner never clears.
        """
        from jiuwenswarm.common.schema.designer_graph import is_leader_node_id

        states = run.setdefault("node_states", {})
        changed = False
        for node_id, raw in list(states.items()):
            if is_leader_node_id(node_id) or not isinstance(raw, dict):
                continue
            if raw.get("status") != NODE_STATUS_RUNNING:
                continue
            states[node_id] = {
                "status": NODE_STATUS_PENDING,
                "started_at": None,
                "completed_at": None,
                "output_ref": raw.get("output_ref"),
                "output_refs": list(raw.get("output_refs") or []),
                "error": None,
                "blocked_by": list(raw.get("blocked_by") or []),
            }
            changed = True
        if not changed:
            return
        run["updated_at"] = utc_now_ms()
        self._publish(run, on_update)
        self._store.save_run(run)

    def _resync_run_after_graph_redesign(
        self,
        run: DesignerExecutionRun,
        graph: DesignerExecutionGraph,
    ) -> None:
        """Keep run.node_states aligned after Director redesigns topology."""
        live_ids = {
            str(node.get("id") or "")
            for node in (graph.get("nodes") or [])
            if isinstance(node, dict) and node.get("id")
        }
        states = dict(run.get("node_states") or {})
        # Drop states for removed nodes; seed pending for new ones.
        states = {nid: st for nid, st in states.items() if nid in live_ids}
        for nid in live_ids:
            if nid not in states:
                states[nid] = {"status": NODE_STATUS_PENDING}
        run["node_states"] = states
        run["updated_at"] = utc_now_ms()
        self._store.save_run(run)
        self._live_runs[str(run.get("run_id") or "")] = run

    def _publish_graph(
        self,
        run: DesignerExecutionRun,
        graph: DesignerExecutionGraph,
    ) -> None:
        callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
        if callback is not None:
            callback(deepcopy(graph))

    @staticmethod
    def _set_node_state(
        run: DesignerExecutionRun,
        node_id: str,
        patch: DesignerNodeState,
    ) -> None:
        states = run.setdefault("node_states", {})
        current = dict(states.get(node_id) or {"status": NODE_STATUS_PENDING})
        current.update(patch)
        states[node_id] = current

    @staticmethod
    def _publish(
        run: DesignerExecutionRun,
        on_update: RunUpdateCallback | None,
        node_id: str | None = None,
    ) -> None:
        if on_update is not None:
            on_update(run, node_id)


def _image_output_refs(state: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_refs = list(state.get("output_refs") or [])
    primary = state.get("output_ref")
    if primary is not None:
        raw_refs = [primary, *raw_refs]
    for ref in raw_refs:
        if not _usable_ref(ref) or _ref_kind(ref) != "image":
            continue
        uri = str((ref or {}).get("uri") or "")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        refs.append(ref)
    return refs


def redistribute_frame_node_states(run: DesignerExecutionRun, shot_count: int) -> None:
    """Split a bundled keyframe node (many PNGs) into one image per n_frame_i."""
    count = max(1, min(int(shot_count or 1), MAX_SHOT_CLIP_NODES))
    states = run.setdefault("node_states", {})
    bundled: list[dict[str, Any]] = []
    source_status = NODE_STATUS_PENDING
    for node_id in ("n_frame", *[frame_node_id(index) for index in range(1, count + 1)]):
        state = states.get(node_id) or {}
        images = _image_output_refs(state)
        if len(images) > len(bundled):
            bundled = images
            source_status = str(state.get("status") or NODE_STATUS_PENDING)
    for index in range(1, count + 1):
        node_id = frame_node_id(index)
        state = dict(states.get(node_id) or {"status": NODE_STATUS_PENDING})
        images = _image_output_refs(state)
        if index <= len(bundled):
            ref = bundled[index - 1]
            state["output_ref"] = ref
            state["output_refs"] = [ref]
            if source_status == NODE_STATUS_COMPLETED and state.get("status") == NODE_STATUS_PENDING:
                state["status"] = NODE_STATUS_COMPLETED
                state["error"] = None
        elif len(images) > 1:
            state["output_ref"] = images[0]
            state["output_refs"] = [images[0]]
        elif images:
            state["output_ref"] = images[0]
            state["output_refs"] = [images[0]]
        states[node_id] = state


def _reject_pending_audio_generation(
    graph: DesignerExecutionGraph,
    run: DesignerExecutionRun,
    *,
    node_ids: set[str] | None = None,
) -> None:
    """Fail Play before any work starts when an audio node needs generation."""
    states = run.get("node_states") or {}
    if not isinstance(states, dict):
        states = {}
    terminal = {
        NODE_STATUS_COMPLETED,
        NODE_STATUS_FAILED,
        NODE_STATUS_CANCELLED,
    }
    unsupported: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or str(node.get("type") or "") != NODE_TYPE_AUDIO:
            continue
        node_id = str(node.get("id") or "")
        if node_ids is not None and node_id not in node_ids:
            continue
        status = str((states.get(node_id) or {}).get("status") or NODE_STATUS_PENDING)
        if status in terminal or is_uploaded_media_node(node):
            continue
        unsupported.append(str(node.get("label") or node_id or "Audio"))
    if unsupported:
        raise NotImplementedError(
            "Audio generation is not implemented. Upload audio content before running: "
            + ", ".join(unsupported)
        )


def _pending_media_modalities(
    graph: DesignerExecutionGraph,
    run: DesignerExecutionRun,
    *,
    node_ids: set[str] | None = None,
) -> dict[str, bool]:
    """Which generation backends a Play needs, from yet-to-run node modalities.

    Compose is video-typed but local ffmpeg — it does not need ``video_gen``.
    ComfyUI nodes call the vLLM-Omni URL from their own workflow instead.
    """
    from jiuwenswarm.common.schema.designer_graph import is_compose_sink_node

    states = run.get("node_states") or {}
    if not isinstance(states, dict):
        states = {}
    terminal = {
        NODE_STATUS_COMPLETED,
        NODE_STATUS_FAILED,
        NODE_STATUS_CANCELLED,
    }
    needs_image = False
    needs_video = False
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if node_ids is not None and node_id not in node_ids:
            continue
        status = str((states.get(node_id) or {}).get("status") or NODE_STATUS_PENDING)
        if status in terminal:
            continue
        if is_compose_sink_node(node) or is_comfyui_node(node):
            continue
        modality = str(node.get("type") or "").strip().lower()
        if modality == NODE_TYPE_IMAGE:
            needs_image = True
        elif modality == NODE_TYPE_VIDEO:
            needs_video = True
    return {"image": needs_image, "video": needs_video}


def _parked_node_state(state: DesignerNodeState | dict[str, Any]) -> DesignerNodeState:
    """Pending again, keeping the last output so the canvas does not blank."""
    return {
        "status": NODE_STATUS_PENDING,
        "started_at": None,
        "completed_at": None,
        "output_ref": state.get("output_ref"),
        "output_refs": list(state.get("output_refs") or []),
        "error": None,
        "blocked_by": [],
    }


def _scope_node_ids(run: DesignerExecutionRun) -> list[str]:
    """Nodes a scoped run is limited to; empty for a regular Play or rerun."""
    meta = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    raw = meta.get("scope_node_ids")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def _usable_ref(ref: object) -> bool:
    if not isinstance(ref, dict):
        return False
    uri = str(ref.get("uri") or "").strip()
    return bool(uri) and not uri.startswith("designer://")


_TEXT_FALLBACK_KINDS = {"text", "table"}
_MEDIA_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
)


def _ref_kind(ref: object) -> str:
    if not isinstance(ref, dict):
        return ""
    kind = str(ref.get("kind") or "").strip().lower()
    if kind:
        return kind
    mime = str(ref.get("mime_type") or "").strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/"):
        return "text"
    label = f"{ref.get('label') or ''} {ref.get('uri') or ''}".lower()
    if ".md" in label:
        return "text"
    if any(ext in label for ext in _MEDIA_SUFFIXES):
        return "video" if any(ext in label for ext in (".mp4", ".webm", ".mov", ".m4v")) else "image"
    return ""


def _is_fallback_text_ref(ref: object) -> bool:
    return _ref_kind(ref) in _TEXT_FALLBACK_KINDS


def _published_media_output(state: object) -> bool:
    """True once a still-running node has already written the file a dependent needs."""
    if not isinstance(state, dict):
        return False
    ref = state.get("output_ref")
    return _usable_ref(ref) and not _is_fallback_text_ref(ref)




def _node_by_id(graph: DesignerExecutionGraph, node_id: str) -> DesignerGraphNode:
    for node in graph.get("nodes", []):
        if node.get("id") == node_id:
            return node
    raise KeyError(f"node not found: {node_id}")


def _exception_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return "timed out"
    return type(exc).__name__


def _target_node_id(run: DesignerExecutionRun) -> str | None:
    return (run.get("metadata") or {}).get("target_node_id")


def _stamp_run_failure(run: DesignerExecutionRun, exc: BaseException) -> None:
    """Mark the run failed and attach a user-visible error for the Design UI toast."""
    from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError

    run["status"] = RUN_STATUS_FAILED
    run["updated_at"] = utc_now_ms()
    if isinstance(exc, DesignerLlmError):
        message = exc.user_message
    else:
        message = _exception_text(exc)
    if message and not str(run.get("error") or "").strip():
        run["error"] = message[:500]


def _node_execute_timeout_sec(node: DesignerGraphNode) -> float:
    """Hard cap so one leaf cannot hang the ready-queue forever.

    Agent + media materialization share this budget (DeepAgent turns then
    image/video backends). Default floor is 20 minutes for media nodes.
    """
    pipeline = node_pipeline(node)
    if pipeline in {NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}:
        return 1800.0
    if pipeline in {NODE_ROLE_FRAME, "character", "character_design", "scene"}:
        return 1200.0
    if pipeline in {"speech", "music"}:
        return 1200.0
    return 1200.0


def _is_ready(
    node_id: str,
    run: DesignerExecutionRun,
    incoming: dict[str, list[str]],
    groups: dict[str, frozenset[str]],
    graph: DesignerExecutionGraph | None = None,
) -> bool:
    state = run.get("node_states", {}).get(node_id) or {}
    if state.get("status") != NODE_STATUS_PENDING:
        return False
    preds = list(incoming.get(node_id, []))
    # Compose/ffmpeg must wait for EVERY shot (+ separate speech/music), even if
    # edges drifted or only the last shot was wired.
    if graph is not None:
        from jiuwenswarm.common.schema.designer_graph import (
            compose_required_predecessor_ids,
            is_compose_sink_node,
        )

        try:
            node = _node_by_id(graph, node_id)
        except KeyError:
            node = None
        if node is not None and is_compose_sink_node(node):
            compose_wait = True
            required = compose_required_predecessor_ids(graph)
            if required:
                preds = list(dict.fromkeys([*preds, *required]))
        else:
            compose_wait = False
    else:
        compose_wait = False
    for pred in preds:
        group = groups.get(pred, frozenset({pred}))
        for member in group:
            # Sync groups often include this node (Align edges). Requiring it
            # completed before it can start deadlocks storyboard forever.
            if member == node_id:
                continue
            member_state = run.get("node_states", {}).get(member) or {}
            member_status = member_state.get("status")
            # Composer is the only hard barrier: every dependency must finish
            # and hand over a real output before ffmpeg starts.
            if compose_wait:
                if member_status != NODE_STATUS_COMPLETED:
                    return False
                # Require a real on-disk shot/audio file — not just COMPLETED + URI.
                try:
                    from jiuwenswarm.server.runtime.designer.handlers.compose import (
                        compose_predecessor_media_ready,
                    )

                    if not compose_predecessor_media_ready(graph, run, member):
                        return False
                except Exception:  # noqa: BLE001
                    ref = member_state.get("output_ref")
                    if ref is None or not _usable_ref(ref) or _is_fallback_text_ref(ref):
                        return False
                continue
            if member_status == NODE_STATUS_COMPLETED:
                continue
            # Soft artifact deps (another shot's beat): unlock while that shot
            # is still generating, once the storyboard shot is already known.
            if graph is not None and is_soft_artifact_dependency(graph, member, node_id):
                if artifact_dependency_satisfied(graph, node_id, member):
                    continue
                return False
            # Hard inputs (character sheet, scene specs): proceed as soon
            # as the file exists, even if that node is still finishing.
            if member_status == NODE_STATUS_RUNNING and _published_media_output(member_state):
                continue
            if member_status != NODE_STATUS_COMPLETED:
                return False
    return True
