# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Designer session bindings for the standard team.design chat round.

The design chat rides the STANDARD ``chat.send(mode='team.design')``
pipeline end to end — user-history persistence, durable assistant records,
interact (ask_user) resume, streaming — exactly like a work/code team
conversation. This module owns only the design-specific seams around it:

* :class:`DesignTeamBridge.bind_round` — resolve the session's graph and
  bind the execution context (StoreNodeSpawner, worker base spec, media
  root, channel) the leader's design tools need, plus the
  conversation-round guard for the ``node_run`` tool;
* the canvas push helpers (``designer.leader.activity`` /
  ``designer.graph.updated`` / ``designer.run.updated``) the design tools
  emit on the session's websocket.

The designer AgentGroup package ships in-repo
(``resources/agent_groups/designer/``) and is installed to the user's
built-in package root on first use (idempotent).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from jiuwenswarm.common.mode_matrix import NEW_TEAM_DESIGN
from jiuwenswarm.common.schema.agent import AgentRequest

logger = logging.getLogger(__name__)

DESIGNER_AGENT_GROUP = "designer"

# The event loop keeps only weak references to tasks; a bare create_task the
# caller drops can be GC'd before it sends. Hold the push tasks until done.
_PENDING_PUSH_TASKS: set["asyncio.Task[None]"] = set()


def _spawn_push_task(coro: Any) -> None:
    task: "asyncio.Task[None]" = asyncio.get_running_loop().create_task(coro)
    _PENDING_PUSH_TASKS.add(task)
    task.add_done_callback(_PENDING_PUSH_TASKS.discard)


def ensure_designer_agent_group_installed() -> None:
    """Install/refresh the in-repo designer AgentGroup (idempotent).

    A built_in install copies the in-repo package once; when the repo copy
    is newer (role templates added/changed — the manifest-package worker
    surface lives there), re-copy so the installed tree matches HEAD.
    """
    from jiuwenswarm.server.runtime.extension_package_manager import (
        install_agent_group,
        resolve_agent_group_dir,
    )

    try:
        installed = resolve_agent_group_dir(DESIGNER_AGENT_GROUP)
    except Exception:
        installed = None
    if installed is not None:
        # Refresh when the repo source tree is newer than the installed copy
        # (any file added/changed — role templates, tools, rails — not just
        # the manifest; both live under the same built_in root).
        try:
            if "built_in" in str(installed) and _source_tree_newer(
                _designer_group_source_dir(), installed
            ):
                import shutil as _shutil

                _shutil.rmtree(installed, ignore_errors=True)
                install_agent_group({"id": DESIGNER_AGENT_GROUP})
        except Exception:  # noqa: BLE001 — refresh is best-effort
            pass
        return
    install_agent_group({"id": DESIGNER_AGENT_GROUP})


def _source_tree_newer(source: Path, installed: Path) -> bool:
    """Whether any file under source is newer than the installed copy."""
    def _newest(root: Path) -> float:
        latest = 0.0
        if not root.is_dir():
            return latest
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    latest = max(latest, path.stat().st_mtime)
                except OSError:
                    continue
        return latest

    return _newest(source) > _newest(installed) + 1.0


def _designer_group_source_dir() -> Path:
    from jiuwenswarm.server.runtime.extension_package_manager import (
        get_equipment_resources_agent_groups_dir,
    )

    root = get_equipment_resources_agent_groups_dir()
    if root is None:
        raise RuntimeError("agent_groups resources root unavailable")
    return root / DESIGNER_AGENT_GROUP


async def _push_team_event(
    channel_id: str,
    session_id: str,
    payload: dict[str, Any],
    request_id: str = "",
) -> None:
    """Forward one team stream event to the frontend via gateway push.

    ``request_id`` must be stable across the events of one turn: the web
    frontend coalesces team ``chat.delta`` frames into a single streaming
    bubble keyed by the request id (``teamStream.requestId``). A fresh id per
    frame would render one bubble per character.
    """
    try:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        # Voice tasks execute on an internal channel; their saved Design canvas
        # is still observed through the normal Web session subscription.
        event_channel = channel_id
        if channel_id == "video_tool" and str(payload.get("event_type") or "").startswith("designer."):
            event_channel = "web"
        await AgentWebSocketServer.get_instance().send_push(
            {
                "request_id": request_id or f"design-{uuid.uuid4().hex}",
                "channel_id": event_channel or "web",
                "session_id": session_id,
                "payload": payload,
                "is_complete": False,
            }
        )
    except Exception:
        logger.debug(
            "[DesignTeamBridge] event push failed: session_id=%s", session_id, exc_info=True
        )


async def push_leader_activity(
    channel_id: str, session_id: str, kind: str, text: str, tool: str = ""
) -> None:
    """Push a designer.leader.activity frame (canvas peek contract)."""
    import time as _time

    await _push_team_event(
        channel_id,
        session_id,
        {
            "event_type": "designer.leader.activity",
            "session_id": session_id,
            "activity": {
                "kind": kind,
                "text": text,
                "tool": tool,
                "at": int(_time.time() * 1000),
            },
        },
    )


def push_graph_update(channel_id: str, session_id: str, graph: dict[str, Any]) -> None:
    """Push designer.graph.updated the moment a tool edit lands in the store.

    Called synchronously from ``StoreNodeSpawner.apply_agent_graph_patch`` and
    from the authoring tool (after the Director chain saves the composed
    graph); schedules the async gateway push on the running loop (the leader
    round is always inside one) so the canvas reflects mid-round graph edits
    without waiting for the RPC response.
    """
    import asyncio as _asyncio

    async def _emit() -> None:
        await _push_team_event(
            channel_id,
            session_id,
            {"event_type": "designer.graph.updated", "session_id": session_id, "graph": graph},
        )

    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        return
    _spawn_push_task(_emit())


def _push_run_update(
    channel_id: str,
    session_id: str,
    run: dict[str, Any],
    node_id: str | None = None,
) -> None:
    """Push designer.run.updated while a leader-triggered run executes.

    The ``designer_run_graph`` tool call awaits the whole trigger, so the
    canvas needs the run event stream (not just the final reply) to show
    live progress — same payloads as the canvas run button path.
    """
    import asyncio as _asyncio

    async def _emit() -> None:
        await _push_team_event(
            channel_id,
            session_id,
            {"event_type": "designer.run.updated", "session_id": session_id, "run": run},
        )
        if node_id:
            await _push_team_event(
                channel_id,
                session_id,
                {
                    "event_type": "designer.node.updated",
                    "session_id": session_id,
                    "run": run,
                    "node_id": node_id,
                },
            )

    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        return
    _spawn_push_task(_emit())


def activity_text(event: dict[str, Any]) -> str:
    tool = str(event.get("tool_name") or event.get("tool") or "")
    if tool:
        return f"调用 {tool}"
    return str(event.get("content") or "")[:120]


class _DesignRoundBindings:
    """Round-scoped design context bindings (see :meth:`DesignTeamBridge.bind_round`)."""

    def __init__(self, context: Any, channel_id: str, session_id: str) -> None:
        self._context = context
        self._channel_id = channel_id
        self._session_id = session_id

    def cleanup(self) -> None:
        """Release the conversation-round guard; the context stays bound."""
        self._context.spawner.conversation_round_active = False

    def push_stage(self, kind: str, text: str, tool: str = "") -> None:
        """Round-boundary canvas peek lines (the old chat path's progress pair)."""
        import asyncio as _asyncio

        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            push_leader_activity(self._channel_id, self._session_id, kind, text, tool)
        )


class DesignTeamBridge:
    """Bind a designer session's execution context around a standard team round.

    The design chat rides the STANDARD ``chat.send(mode='team.design')``
    pipeline (user history, durable assistant persistence, interact, streaming
    — all the standard machinery). This class only owns what that pipeline
    lacks: resolving the session's graph, binding the design context
    (StoreNodeSpawner / worker base spec / media root) the leader's tools
    need, and the conversation-round guard for the node_run tool.
    """

    async def bind_round(
        self, *, request: AgentRequest, session_id: str
    ) -> _DesignRoundBindings | None:
        from jiuwenswarm.server.runtime.design.context import (
            StoreNodeSpawner,
            get_design_context_registry,
            get_graph_coordinator,
        )
        from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore

        ensure_designer_agent_group_installed()

        store = DesignerGraphStore()
        registry = get_design_context_registry()
        context = registry.get(session_id)
        graph_id = str(context.graph_id) if context is not None else ""
        if not graph_id:
            graph = store.get_latest_graph_for_session(session_id)
            graph_id = str(graph.get("graph_id") or "") if graph else ""
        if not graph_id:
            logger.warning(
                "[DesignTeamBridge] session has no designer graph; design tools "
                "not mounted: session_id=%s",
                session_id,
            )
            return None
        context = registry.bind(session_id, graph_id)
        channel_id = request.channel_id or "web"
        if context.spawner is None:
            context.spawner = StoreNodeSpawner(
                store,
                graph_id,
                lambda: get_graph_coordinator(store, graph_id),
                on_graph_update=lambda saved: push_graph_update(
                    channel_id, session_id, saved
                ),
                on_run_update=lambda run, node_id=None: _push_run_update(
                    channel_id, session_id, run, node_id
                ),
            )
        else:
            # The spawner can outlive rounds (created by the tool provider or
            # an earlier bind) — its push callbacks were bound to that older
            # round's channel; rebind every round so graph/run pushes always
            # ride the current websocket. Without this, designer.graph.updated
            # frames silently stop reaching the canvas (the canvas never
            # expands after patch/autorun).
            context.spawner.set_push_callbacks(
                on_graph_update=lambda saved: push_graph_update(
                    channel_id, session_id, saved
                ),
                on_run_update=lambda run, node_id=None: _push_run_update(
                    channel_id, session_id, run, node_id
                ),
            )
        # Worker media assets land where the canvas keeps its outputs: the
        # project directory, or the per-session workspace without a project.
        coordinator = get_graph_coordinator(store, graph_id)
        if coordinator.media_root is None:
            from jiuwenswarm.server.runtime.designer.handlers.common import canvas_output_dir

            coordinator.media_root = canvas_output_dir(store.get_graph(graph_id))
        # Worker base for the TeamWorkerBackend: derive from the design team's
        # teammate spec (same enriched-spec source the team chain uses;
        # idempotent — same session/mode/agent_group inputs), cached on the
        # design context for every later trigger.
        await ensure_worker_base_spec(context, session_id)
        # Channel binding for tool-emitted canvas events: the authoring tool
        # (designer_graph_autorun) pushes its stage/graph frames on this
        # channel so they ride the same websocket as the chat stream.
        context.channel_id = channel_id
        # Conversation-round guard for the leader's node_run tool: while this
        # turn is in flight, node_run answers with a "canvas run button"
        # message instead of embedding a real node execution inside the
        # leader's turn (which would hold the chat stream hostage).
        context.spawner.conversation_round_active = True
        # A user message ends any report-only round left by a finished run.
        context.spawner.report_pending = False
        return _DesignRoundBindings(context, channel_id, session_id)


async def ensure_worker_base_spec(context: Any, session_id: str) -> None:
    """Derive the TeamWorkerBackend base spec onto a bound design context.

    Idempotent — contexts that already carry a spec are left alone. Unit/ST
    environments without a team manager simply keep the spec unset (the
    worker path stays unavailable for that round).
    """
    if context.worker_base_spec is not None:
        return
    try:
        from jiuwenswarm.agents.harness.team import get_team_manager

        team_manager = get_team_manager()
        worker_spec = await team_manager.get_swarm_enriched_team_spec(
            session_id=session_id,
            mode=NEW_TEAM_DESIGN,
            agent_group_name=DESIGNER_AGENT_GROUP,
        )
        context.worker_base_spec = (
            worker_spec.agents.get("teammate") or worker_spec.agents.get("leader")
        )
        context.worker_build_context = worker_spec.build_context
        context.team_name = worker_spec.team_name
        # SDD-0030 expert table (2026-10-14): derived once from the
        # same enriched spec — bare member names resolve against it
        # in the engine (fail-closed), replacing the per-call
        # spec_resolver path (dropped).
        try:
            from jiuwenswarm.server.runtime.design.team_worker import (
                derive_design_expert_specs,
            )

            context.expert_specs = derive_design_expert_specs(worker_spec)
        except Exception:
            # Without the group package the table stays empty: plain
            # workers keep running; agent_template names fail closed.
            logger.debug(
                "[DesignTeamBridge] expert table derivation failed: session_id=%s",
                session_id,
                exc_info=True,
            )
            context.expert_specs = {}
    except Exception:
        # Unit/ST environments run without a team manager; the worker
        # path is simply unavailable for this round.
        logger.debug(
            "[DesignTeamBridge] worker base spec unavailable: session_id=%s",
            session_id,
            exc_info=True,
        )
        context.worker_base_spec = None


async def ensure_design_context(graph: Any) -> None:
    """Bind a graph's design context when the in-process registry is cold.

    The chat round's ``bind_round`` is the normal binder and the binding
    lives for the whole process; a canvas rerun right after a server
    restart reaches the run RPC with no chat turn behind it, so the
    trigger would find no worker base spec. Bind from the graph's own
    metadata (a graph serves one session) and derive the same spec
    ``bind_round`` would have. Best-effort: a real setup failure still
    surfaces as the coordinator's "not bound" RuntimeError.
    """
    from jiuwenswarm.server.runtime.design.context import (
        get_design_context_registry,
        get_graph_coordinator,
    )
    from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore

    graph_id = str((graph or {}).get("graph_id") or "").strip()
    session_id = str(((graph or {}).get("metadata") or {}).get("session_id") or "").strip()
    if not graph_id or not session_id:
        return
    ensure_designer_agent_group_installed()
    store = DesignerGraphStore()
    registry = get_design_context_registry()
    context = registry.for_graph(graph_id)
    if context is None:
        context = registry.bind(session_id, graph_id)
    # Worker media assets land where the canvas keeps its outputs: the
    # project directory, or the per-session workspace without a project.
    coordinator = get_graph_coordinator(store, graph_id)
    if coordinator.media_root is None:
        from jiuwenswarm.server.runtime.designer.handlers.common import canvas_output_dir

        coordinator.media_root = canvas_output_dir(graph)
    await ensure_worker_base_spec(context, session_id)


__all__ = [
    "DESIGNER_AGENT_GROUP",
    "activity_text",
    "DesignTeamBridge",
    "ensure_design_context",
    "ensure_designer_agent_group_installed",
    "push_graph_update",
    "push_leader_activity",
]
