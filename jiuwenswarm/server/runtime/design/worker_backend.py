# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Engine ``AgentBackend`` that executes each ``agent()`` call as one designer node.

The compiled prompt (upstream outputs already inlined by the compiler) is
written onto the node's ``config.prompt`` and the node runs through the
preserved ``NodeAgentHost`` — its full tool surface (``build_designer_tools``),
per-node persona templates, media materialization, and fail-closed semantics
are untouched. ``ctx.run_id`` is trigger-scoped so a rerun never reuses the
previous trigger's cached agent conversation.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from openjiuwen.agent_teams.workflow.engine.backends.base import (
    AgentBackend,
    AgentResult,
)
from openjiuwen.agent_teams.workflow.engine.errors import BackendError

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    node_uses_agent_runtime,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.node_agent import NodeAgentHost

logger = logging.getLogger(__name__)


class DesignerWorkerBackend(AgentBackend):
    """One engine ``agent()`` call = one designer node execution.

    RETIRED to FALLBACK (2026-10-12 terminal state): the live path is the
    agent-core ``TeamWorkerBackend`` (see ``team_worker.py`` — workers derive
    from the design team's teammate spec with per-call role templates from
    the designer AgentGroup package; all worker logic lives in the package).
    This class remains for contexts with no bound base spec (unit tests,
    STs with fake contexts). The follow-up crew removes it once the package
    path is the only one exercised.
    """

    #: Options accepted beyond the engine's own set. ``agent_template`` is
    #: compiled in by GraphScriptCompiler (the node's AgentGroup member
    #: template reference); this backend resolves its persona/tools from the
    #: node itself today and IGNORES the option — it is declared so the
    #: engine's option whitelist accepts it (a TeamWorkerBackend /
    #: cross-process manifest worker consumes it through ``spec_resolver``).
    KNOWN_OPTIONS: frozenset[str] = frozenset({"agent_template"})

    def __init__(
        self,
        *,
        graph: DesignerExecutionGraph,
        run_id: str,
        spawner: Any,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        media_root: "Path | None" = None,
    ) -> None:
        super().__init__()
        self._graph = graph
        self._run_id = run_id
        self._spawner = spawner
        # Team-owned asset root (the owning team's team-workspace/media/
        # designer); None keeps the legacy global designer_media layout.
        self._media_root = media_root
        # Swarmflow-layer boundary: this backend EMITS a node-result event
        # (node_id, {text, output_ref, output_refs}) the moment one node's
        # execution completes. It knows nothing about the design Store — the
        # design layer (DesignEventBridge.on_node_result) listens and writes.
        self._event_sink = event_sink
        self._host = NodeAgentHost(spawner)
        self._nodes_by_id: dict[str, dict[str, Any]] = {
            str(n.get("id") or ""): n for n in (graph.get("nodes") or [])
        }
        # Per-trigger produced outputs (node_id → {text, output_ref, output_refs})
        # read by RunCoordinator: the engine's agent() returns text only, so the
        # asset refs ride here.
        self.results: dict[str, dict[str, Any]] = {}
        # Freshest run snapshot for worker runtime reads (read_upstream /
        # reference collection). The coordinator refreshes it whenever the
        # bridge persists a node result; starts as the compile-time snapshot
        # so frozen upstream outputs are visible too.
        self._run_snapshot: dict[str, Any] | None = None
        # Concurrency observability (ST): in-flight node executions and the
        # observed maximum. A parallel wave shows max_in_flight >= 2; serial
        # compilation never exceeds 1.
        self.in_flight = 0
        self.max_in_flight = 0

    def bind_run_snapshot(self, run: dict[str, Any] | None) -> None:
        """Refresh the run state the worker's runtime tools read from.

        Called by the coordinator before the trigger (compile-time state) and
        by the node-result writeback after every persisted node — later nodes
        in the same trigger see earlier nodes' outputs through read_upstream
        exactly like the old in-run executor, without the backend ever
        touching the Store itself.
        """
        self._run_snapshot = dict(run) if isinstance(run, dict) else None

    async def run(
        self, prompt: str, opts: dict, schema_json: dict | None, *, call_key=None
    ) -> AgentResult:
        node_id = str(opts.get("label") or "")
        node = self._nodes_by_id.get(node_id)
        if node is None:
            raise BackendError(f"designer node not found: {node_id!r}")
        if not node_uses_agent_runtime(node):
            raise BackendError(
                f"designer node '{node_id}' is a handler node "
                f"(comfyui/upload); handler dispatch is not wired in v1"
            )
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return await self._run_node(prompt, node_id, node)
        finally:
            self.in_flight -= 1

    async def _run_node(
        self, prompt: str, node_id: str, node: dict[str, Any]
    ) -> AgentResult:
        # Node-input observability (user request 2026-10-11): the compiled
        # prompt (upstream outputs already inlined by the compiler) is the
        # node's actual input — log its head so the run log shows whether an
        # upstream edit propagated into this node's input without re-running
        # the upstream node.
        logger.info(
            "[design-node-input] node=%s run=%s prompt_head=%r prompt_len=%d",
            node_id,
            self._run_id,
            prompt[:160],
            len(prompt),
        )
        live = deepcopy(node)
        config = dict(live.get("config") or {})
        config["prompt"] = prompt
        live["config"] = config
        ctx = NodeExecutionContext(
            graph=self._graph,
            run_id=self._run_id,
            node_id=node_id,
            # Run snapshot for the worker's runtime data reads (read_upstream,
            # reference collection): the freshest state the design layer has
            # persisted — upstream outputs written by earlier nodes in THIS
            # trigger (event writeback) are visible to later nodes. Compiled
            # prompts stay the primary carrier; this restores the old
            # executor's in-run visibility the NodeAgentHost tools expect.
            run=self._run_snapshot,
            emit_activity=None,
            on_prompt_artifact=None,
            media_dir=self._media_root,
        )
        try:
            result = await self._host.execute(live, ctx)
        except Exception as exc:
            raise BackendError(
                f"designer node '{node_id}' execution failed: {exc}"
            ) from exc
        if result is None:
            raise BackendError(f"designer node '{node_id}' returned no result")
        structured: dict[str, Any] = {}
        if result.output_ref:
            structured["output_ref"] = dict(result.output_ref)
        if result.output_refs:
            structured["output_refs"] = [dict(r) for r in result.output_refs]
        content = _output_content_text(result)
        self.results[node_id] = {
            "text": content,
            "output_ref": dict(result.output_ref) if result.output_ref else None,
            "output_refs": [dict(r) for r in result.output_refs or []],
        }
        # Swarmflow-layer event: node result exists. The design layer's
        # listener persists it (per-node Store writeback); an emission
        # failure must not kill the run — log it so the lost write is
        # visible (the next trigger re-executes the node instead of
        # replaying a cache record the Store never saw).
        if self._event_sink is not None:
            try:
                self._event_sink(node_id, dict(self.results[node_id]))
            except Exception:
                logger.exception(
                    "[design-run] node writeback event failed: node_id=%s",
                    node_id,
                )
        return AgentResult(
            text=content,
            structured=structured or None,
            tokens=0,
        )


_OUTPUT_TEXT_MAX_CHARS = 20000


def _file_uri_to_path(uri: str) -> Path:
    """Convert a ``file:`` uri to a local Path (Windows drive-letter aware).

    ``urlparse("file:///C:/x.md").path`` yields ``/C:/x.md``; on Windows the
    leading slash makes ``Path`` resolve a root-relative path that never
    exists. Strip it so the drive letter resolves.
    """
    raw = unquote(urlparse(uri).path)
    windows_drive = len(raw) > 2 and raw[0] == "/" and raw[2] == ":"
    if windows_drive and raw[1].isalpha():
        raw = raw[1:]
    return Path(raw)


def _output_content_text(result: Any) -> str:
    """Resolve the node's real content: the output_ref file when readable.

    ``NodeResult.message`` is a stub in both completion paths ("node
    completed" / "node agent text fallback"); the creative body lives in the
    file behind ``output_ref.uri`` (``node_complete(text=...)`` writes it).
    Falls back to the message when no readable file ref exists.
    """
    ref = getattr(result, "output_ref", None)
    if isinstance(ref, dict):
        uri = str(ref.get("uri") or "")
        if uri.startswith("file:"):
            try:
                text = _file_uri_to_path(uri).read_text(encoding="utf-8")
            except (OSError, ValueError):
                # Binary media (png/mp4) as primary output has no text body —
                # fall through to the message stub instead of raising out of
                # backend.run (a media-primary node would otherwise fail all
                # engine retries on a decode error, not a real failure).
                text = ""
            if text.strip():
                return text[:_OUTPUT_TEXT_MAX_CHARS]
    return str(getattr(result, "message", "") or "")


__all__ = ["DesignerWorkerBackend"]
