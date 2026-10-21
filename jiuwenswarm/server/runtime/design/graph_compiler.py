# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic designer graph → swarmflow script compilation.

One rule governs every compile path ("frozen once produced"):

* a node with a completed output in the run record contributes its text as a
  literal — no ``agent()`` call is ever emitted for it;
* a node without an output compiles to ``await agent(prompt, label=node_id,
  model=node_type)`` in topological order;
* a single-node run (``node_ids=[X]``) forces ``agent()`` for X, inlines the
  transitive ancestors' frozen texts into X's prompt, and excludes every
  other node — downstream can never re-execute because it is not in the
  script at all;
* a cascade run (``node_ids=[X]`` + ``cascade=True``, "rerun from X") forces
  ``agent()`` for X and X's whole downstream closure (fresh outputs, direct
  overwrite), keeps the ancestors frozen at their current values — the user
  decides where the refresh starts, upstream is never re-executed.

Upstream values are inlined as literals at compile time, so the script never
depends on journal replay, position keys, or runtime upstream reads.

Scheduling: nodes execute in topological **waves** — a wave is the set of
nodes whose in-scope upstream is entirely settled (frozen or executed in an
earlier wave). A wave with one executing node emits a plain ``await``; a
wave with several emits one ``parallel([...])`` block so independent
branches run concurrently (engine barrier semantics; a branch error maps to
None exactly like the serial gate). This mirrors the old executor's wave
scheduler for fan-out graphs (outline → two storyboard branches) while
keeping single-node chains byte-identical to the serial form.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    NODE_TYPE_TEXT,
    DesignerExecutionGraph,
    node_agent_template,
    node_pipeline,
    node_role,
    node_uses_agent_runtime,
)
from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri

_FROZEN_TEXT_MAX_CHARS = 20000

_ASSET_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "uri": {"type": "string"},
        "mime_type": {"type": "string"},
        "label": {"type": "string"},
    },
    "required": ["kind", "uri"],
}

#: The receipt a member returns through ``structured_output``: the
#: ``output_ref`` its ``submit_node_output`` call wrote.
NODE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_ref": _ASSET_REF_SCHEMA,
        "output_refs": {"type": "array", "items": _ASSET_REF_SCHEMA},
        "message": {"type": "string"},
    },
    "required": ["output_ref"],
}
_UPSTREAM_HEADER = "\n\n## Upstream node inputs\n"
_NO_OUTPUT_PLACEHOLDER = "(Upstream node {node_id} has no output yet.)"
_UPSTREAM_REFS_HEADER = "\nUpstream artifacts (reference these files by uri in media tools):"
_REF_LINE = "- [{label}] {kind} {uri}"


def _topo_order(graph: DesignerExecutionGraph) -> list[str]:
    """Kahn topological order over data/sync edges (source before target)."""
    nodes = [str(n.get("id") or "") for n in (graph.get("nodes") or [])]
    node_set = set(nodes)
    incoming: dict[str, set[str]] = {node: set() for node in nodes}
    for edge in graph.get("edges") or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in node_set and target in node_set:
            incoming[target].add(source)
    ordered: list[str] = []
    ready = [node for node in nodes if not incoming[node]]
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for other, preds in incoming.items():
            if node in preds:
                preds.discard(node)
                if not preds and other not in ordered and other not in ready:
                    ready.append(other)
    # Cycle leftovers keep their original relative order at the end.
    for node in nodes:
        if node not in ordered:
            ordered.append(node)
    return ordered


def _ancestors(graph: DesignerExecutionGraph, node_id: str) -> set[str]:
    """Transitive upstream node ids (data/sync edges)."""
    parents: dict[str, set[str]] = {}
    for edge in graph.get("edges") or []:
        parents.setdefault(str(edge.get("target") or ""), set()).add(
            str(edge.get("source") or "")
        )
    seen: set[str] = set()
    frontier = list(parents.get(node_id, ()))
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(parents.get(current, ()))
    return seen


def _node_label(node: dict[str, Any]) -> str:
    return str(node.get("label") or node.get("id") or "")


def _node_prompt(node: dict[str, Any]) -> str:
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    prompt = str(config.get("prompt") or "").strip()
    if prompt:
        return prompt
    return str(node.get("description") or node.get("title") or _node_label(node))


def _text_file_body(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    mime = str(ref.get("mime_type") or "")
    if ref.get("kind") != "text" and not mime.startswith("text/"):
        return ""
    path = path_from_uri(str(ref.get("uri") or ""))
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")[:_FROZEN_TEXT_MAX_CHARS]
    except (OSError, ValueError):
        return ""


def _frozen_text(state: dict[str, Any] | None) -> str | None:
    """A produced node's current output text.

    The output file wins over ``state["text"]``: the canvas text editor saves
    user edits straight to that file (``/file-api/file-content``) and never
    touches the run record, so the stored text can be stale.
    """
    if not isinstance(state, dict):
        return None
    text = _text_file_body(state.get("output_ref")).strip()
    if not text:
        text = str(state.get("text") or "").strip()
    return text or None


def _descendants(graph: DesignerExecutionGraph, node_id: str) -> set[str]:
    """Transitive downstream node ids (data/sync edges)."""
    children: dict[str, set[str]] = {}
    for edge in graph.get("edges") or []:
        children.setdefault(str(edge.get("source") or ""), set()).add(
            str(edge.get("target") or "")
        )
    seen: set[str] = set()
    frontier = list(children.get(node_id, ()))
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(children.get(current, ()))
    return seen


def _waves(
    graph: DesignerExecutionGraph,
    order: list[str],
    scope: set[str],
    frozen_ids: set[str],
) -> list[list[str]]:
    """Partition in-scope nodes into topological waves (Kahn levels).

    A wave is every node whose in-scope, non-frozen upstream lies entirely in
    earlier waves; frozen nodes bind literals and never hold a wave back
    (their value exists at compile time). The result preserves ``order``
    within each wave for deterministic output.
    """
    upstream_in_scope: dict[str, set[str]] = {nid: set() for nid in scope}
    for edge in graph.get("edges") or []:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if src in scope and tgt in scope and src not in frozen_ids:
            upstream_in_scope[tgt].add(src)
    remaining = {nid: set(preds) for nid, preds in upstream_in_scope.items()}
    waves: list[list[str]] = []
    settled: set[str] = set()
    while remaining:
        ready = [nid for nid in order if nid in remaining and not remaining[nid]]
        if not ready:  # cycle leftovers: flush in original order
            ready = [nid for nid in order if nid in remaining]
        for nid in ready:
            remaining.pop(nid, None)
        waves.append(ready)
        settled.update(ready)
        for preds in remaining.values():
            preds.difference_update(settled)
    return waves


# Per-wave concurrency cap: one parallel([...]) block per batch of at most
# this many nodes (old executor ran with asyncio.Semaphore(3) for the same
# reason — LLM/media API quota protection). Batches within a wave chain
# sequentially: [A,B,C] with cap 2 → parallel([A,B]) then parallel([C]).
_MAX_WAVE_CONCURRENCY = 3


@dataclass
class _NodeEmit:
    """Mutable compile buffers shared while emitting one node's script."""

    lines: list[str]
    graph: DesignerExecutionGraph
    nodes_by_id: dict[str, dict[str, Any]]
    bindings: dict[str, str]
    run_outputs: dict[str, Any] | None
    failed: list[str]
    executed: list[str]


class GraphScriptCompiler:
    """Compile a designer graph into a swarmflow script source."""

    def __init__(self) -> None:
        # Ticket-line state (per compile call): set in compile() so every
        # _prompt_expr call can open with the node's shot ticket.
        self._run_id = ""
        self._graph_id = ""

    def compile(
        self,
        graph: DesignerExecutionGraph,
        run_outputs: dict[str, Any],
        *,
        node_ids: list[str] | None = None,
        cascade: bool = False,
        run_id: str = "",
    ) -> str:
        """Emit the script source for this trigger.

        Args:
            graph: The designer execution graph (single source of truth).
            run_outputs: ``{node_id: node_state}`` from the graph's run
                record; a state with ``text`` marks the node frozen.
            node_ids: ``None`` = full run (every unfrozen node executes);
                ``[X]`` = single-node run (only X executes, ancestors inline).
            cascade: With ``node_ids=[X]``: "rerun from X" — X plus its whole
                downstream closure re-execute (fresh outputs overwrite),
                ancestors stay frozen at their current values. The user picks
                where the refresh starts; upstream is never re-executed.
            run_id: The graph's lifelong run id; when set each agent() prompt
                opens with the shot ticket the member tools resolve
                (``shot_ticket: design:{graph}:{run}:{node}``).
        """
        self._run_id = str(run_id or "")
        self._graph_id = str(graph.get("graph_id") or "")
        nodes_by_id = {
            str(n.get("id") or ""): n for n in (graph.get("nodes") or [])
        }
        order = [nid for nid in _topo_order(graph) if nid in nodes_by_id]
        scope = set(order)
        if node_ids is not None:
            targets = [nid for nid in node_ids if nid in nodes_by_id]
            if not targets:
                raise ValueError(
                    f"no runnable nodes in graph for node_ids={node_ids!r}"
                )
            if cascade:
                # The forced-execution set grows to the downstream closure;
                # every other node in the script (the ancestors) is ordinary
                # scope — frozen when produced, executed when not.
                forced = set(targets)
                for target in targets:
                    forced.update(_descendants(graph, target))
                node_ids = sorted(forced)
                scope = set(node_ids)
                for target in targets:
                    scope.update(_ancestors(graph, target))
            else:
                forced = set(targets)
                scope = set(targets)
                for target in targets:
                    scope.update(_ancestors(graph, target))
            # Old-executor fail-fast, shared by single and cascade: the
            # trigger inlines its out-of-scope ancestors' outputs into the
            # prompts ("upstream is never re-executed"); an ancestor without
            # a produced output has nothing to inline, so refuse instead of
            # silently rerunning the ancestor chain. Uploaded handler nodes
            # are always inline-able literals.
            not_ready: list[str] = []
            for nid in scope.difference(forced):
                if (
                    node_uses_agent_runtime(nodes_by_id[nid])
                    and _frozen_text(run_outputs.get(nid)) is None
                ):
                    not_ready.append(nid)
            not_ready.sort()
            if not_ready:
                raise ValueError(
                    "upstream not ready: " + ", ".join(not_ready)
                )
        lines: list[str] = [
            "META = "
            + json.dumps(
                {
                    "name": str(graph.get("graph_id") or "designer_graph"),
                    "description": "team.design compiled graph run",
                },
                ensure_ascii=False,
            ),
            "",
            "from swarmflow import agent, parallel",
            "",
            # Member receipt (output_ref) every AgentGroup member submits via
            # structured_output; Python literal, not JSON (true/false/null).
            f"NODE_OUTPUT_SCHEMA = {NODE_OUTPUT_SCHEMA!r}",
            "",
            "",
            "async def run(args):",
            "    # Nodes gated off because an upstream agent failed this",
            "    # trigger (old-executor semantics: wait, never run on",
            "    # garbage input).",
            "    __failed: list[str] = []",
        ]
        executed: list[str] = []
        failed: list[str] = []
        bindings: dict[str, str] = {}
        # Frozen literals bind first (value known at compile time); executing
        # nodes then run in topological waves, a multi-node wave compiling to
        # one parallel([...]) block.
        frozen_ids: set[str] = set()
        for nid in order:
            if nid not in scope:
                continue
            node = nodes_by_id[nid]
            # Handler nodes (user-uploaded reference media: n_ref_*, uploads,
            # comfyui imports) never execute an agent — their "output" is the
            # uploaded file itself, known at compile time. Materialize them
            # as literals exactly like frozen nodes (F14: the handler branch
            # of the retired executor, at compile time).
            if not node_uses_agent_runtime(node):
                literal = self._handler_literal(node)
                if literal is not None:
                    # The return dict reads r_<id> for every executed node.
                    lines.append(f"    r_{_safe_ident(nid)} = {literal}")
                    bindings[nid] = literal
                    frozen_ids.add(nid)
                    executed.append(nid)
                    continue
            frozen = _frozen_text(run_outputs.get(nid))
            is_target = node_ids is not None and nid in node_ids
            if frozen is not None and not is_target:
                bindings[nid] = json.dumps(frozen, ensure_ascii=False)
                frozen_ids.add(nid)
        waves = _waves(graph, order, scope, frozen_ids)
        emit = _NodeEmit(
            lines=lines,
            graph=graph,
            nodes_by_id=nodes_by_id,
            bindings=bindings,
            run_outputs=run_outputs,
            failed=failed,
            executed=executed,
        )
        for wave in waves:
            executing = [
                nid
                for nid in wave
                if nid in scope and nid not in frozen_ids
            ]
            if not executing:
                continue
            if len(executing) == 1:
                for nid in executing:
                    self._emit_serial_node(emit, nid, nodes_by_id[nid])
                continue
            # Multi-node wave: batches of at most _MAX_WAVE_CONCURRENCY nodes,
            # each batch one parallel([...]) block (concurrency cap mirrors
            # the old executor's Semaphore(3) — quota protection). One thunk
            # per node, named locals defined before use (a list literal cannot
            # carry def statements). Every thunk keeps the node's own gate (a
            # branch whose upstream failed returns None — barrier semantics
            # identical to the serial gate).
            for batch_start in range(0, len(executing), _MAX_WAVE_CONCURRENCY):
                batch = executing[batch_start:batch_start + _MAX_WAVE_CONCURRENCY]
                thunk_names: list[str] = []
                for nid in batch:
                    thunk_src = self._emit_thunk(emit, nid, nodes_by_id[nid])
                    lines.append(thunk_src)
                    thunk_names.append(f"_thunk_{_safe_ident(nid)}")
                lines.append(
                    "    _wave = await parallel(["
                    + ", ".join(thunk_names)
                    + "])"
                )
                for index, nid in enumerate(batch):
                    var = "r_" + _safe_ident(nid)
                    lines.append(f"    {var} = _wave[{index}]")
                    bindings[nid] = var
                    executed.append(nid)
                    # A None result here means either the node failed outright
                    # or its gate blocked it — both failure-shaped downstream.
                    lines.append(
                        f"    if {var} is None:"
                        f" __failed.append({json.dumps(nid, ensure_ascii=False)})"
                    )
                    failed.append(nid)
        if not executed:
            lines.append("    pass")
            lines.append("    return {}")
        else:
            lines.append(
                "    return {"
                + ", ".join(
                    f"{json.dumps(nid, ensure_ascii=False)}: r_{_safe_ident(nid)}"
                    for nid in executed
                )
                + "}"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _gate_expr(
        nid: str,
        graph: DesignerExecutionGraph,
        bindings: dict[str, str],
        failed: list[str],
    ) -> str | None:
        """Upstream failure gate for one node (old-executor semantics).

        A predecessor that failed blocks its dependents — they wait, they
        never run on garbage input. The engine's failure mode is soft (a
        failed agent yields None and the workflow keeps going), so the
        compiled script enforces the gate itself: None-valued live upstream
        results / already-failed upstream ids gate this node off. Returns
        None when no live upstream can fail (frozen literals never do).
        """
        gate_parts: list[str] = []
        for edge in graph.get("edges") or []:
            src = str(edge.get("source") or "")
            if str(edge.get("target") or "") != nid:
                continue
            bound = bindings.get(src)
            if bound is None:
                continue  # frozen literal or out of scope: never fails
            if bound.startswith('"'):
                continue  # frozen text literal: cannot be None
            gate_parts.append(bound + " is None")
            if src in failed:
                gate_parts.append(
                    json.dumps(src, ensure_ascii=False) + " in __failed"
                )
        if not gate_parts:
            return None
        return " or ".join(dict.fromkeys(gate_parts))

    def _emit_serial_node(self, emit: _NodeEmit, nid: str, node: dict[str, Any]) -> None:
        """Emit one node as a plain serial await (single-node wave)."""
        gate = self._gate_expr(nid, emit.graph, emit.bindings, emit.failed)
        var = "r_" + _safe_ident(nid)
        if gate is not None:
            # Gated node: bind unconditionally, await only when the gate
            # passes (downstream gates reference the variable regardless).
            emit.lines.append("    " + var + " = None")
            emit.lines.append("    if " + gate + ":")
            emit.lines.append(
                "        __failed.append("
                + json.dumps(nid, ensure_ascii=False) + ")"
            )
            emit.lines.append("    else:")
            emit.lines.append(self._agent_call_line(emit, node, var, indent="        "))
            emit.bindings[nid] = var
            emit.failed.append(nid)
            emit.executed.append(nid)
        else:
            emit.lines.append(self._agent_call_line(emit, node, var))
            emit.lines.append(
                f"    if {var} is None:"
                f" __failed.append({json.dumps(nid, ensure_ascii=False)})"
            )
            emit.bindings[nid] = var
            emit.executed.append(nid)

    def _emit_thunk(self, emit: _NodeEmit, nid: str, node: dict[str, Any]) -> str:
        """Emit one node as a parallel-branch thunk (multi-node wave).

        The thunk closes over nothing node-specific: the gate reads the
        shared ``__failed`` list (single-threaded asyncio — no races), and a
        blocked/failed branch returns None exactly like the serial gate.
        """
        gate = self._gate_expr(nid, emit.graph, emit.bindings, emit.failed)
        prompt_expr = self._prompt_expr(
            node, emit.graph, emit.nodes_by_id, emit.bindings, emit.run_outputs,
        )
        call = self._agent_call_expr(prompt_expr, node, nid, emit.run_outputs)
        thunk_name = f"_thunk_{_safe_ident(nid)}"
        body: list[str] = [f"    async def {thunk_name}():"]
        if gate is not None:
            body.append(
                f"        if {gate}:"
                f" __failed.append({json.dumps(nid, ensure_ascii=False)}); return None"
            )
        body.append(f"        return await agent({call})")
        return "\n".join(body)

    def _agent_call_expr(
        self,
        prompt_expr: str,
        node: dict[str, Any],
        node_id: str,
        run_outputs: dict[str, Any] | None,
    ) -> str:
        """The agent(...) argument list shared by serial lines and thunks.

        ``options.agent_template`` carries the node's AgentGroup member name
        (bare, SDD-0030 — the engine gate rejects "group/member" refs); the
        engine resolves it against the design session's expert table. A
        member call also requests the ``NODE_OUTPUT_SCHEMA`` receipt.

        Raises:
            ValueError: A text node with no pipeline — no member runs it.
        """
        version = self._node_version(node_id, run_outputs)
        model = json.dumps(str(node.get("type") or "text"), ensure_ascii=False)
        options = {"model": model}
        template_ref = str(node_agent_template(node) or "").strip()
        if not template_ref and not node_pipeline(node) and node_role(node) == NODE_TYPE_TEXT:
            raise ValueError(
                f"text node {node_id!r} has no pipeline; no designer member runs "
                "generic text nodes"
            )
        if template_ref:
            options["agent_template"] = json.dumps(template_ref, ensure_ascii=False)
        # Keys AND values through json.dumps — uniform double quoting, the
        # same style swarmflow's own docs/examples use
        # (options={"agent_template": "<member>"}); {key!r} would mix
        # single-quoted keys with double-quoted values.
        options_expr = ", ".join(
            f"{json.dumps(key)}: {value}" for key, value in options.items()
        )
        return (
            f"{prompt_expr}, label={json.dumps(node_id, ensure_ascii=False)}, "
            f"phase={json.dumps(f'v{version}', ensure_ascii=False)}, "
            + ("schema=NODE_OUTPUT_SCHEMA, " if template_ref else "")
            + f"options={{{options_expr}}}"
        )

    @staticmethod
    def _handler_literal(node: dict[str, Any]) -> str | None:
        """Compile-time literal for a handler (non-agent) node.

        User-uploaded reference media are passthrough assets: the file is the
        output, so the node contributes a short literal describing the asset
        (role label + filename) instead of an agent() call. Returns None for
        handler nodes with no discoverable asset (treated as ordinary
        executes — the backend's explicit error is the clearer failure).
        """
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        upload = cfg.get("upload") if isinstance(cfg.get("upload"), dict) else {}
        uri = str(upload.get("uri") or "").strip()
        path = str(cfg.get("user_reference_path") or "").strip()
        source = uri or path
        if not source:
            return None
        filename = Path(source).name if source else str(node.get("id") or "")
        label = str(node.get("label") or node.get("id") or "")
        ref_id = str(cfg.get("user_reference_id") or "").strip()
        head = f"[{label}] user-uploaded reference media ({ref_id or filename})"
        return json.dumps(head, ensure_ascii=False)

    @staticmethod
    def _node_version(node_id: str, run_outputs: dict[str, Any] | None) -> int:
        """Content-iteration version of a node (Store truth, defaults to 1).

        The coordinator bumps it on every single-node rerun / cascade
        execution of the node; untouched nodes keep their version so their
        signature (and WAL cache eligibility) stays stable across triggers.
        """
        if not isinstance(run_outputs, dict):
            return 1
        state = run_outputs.get(node_id)
        if isinstance(state, dict):
            version = state.get("version")
            if isinstance(version, int) and not isinstance(version, bool) and version > 0:
                return version
        return 1

    def _agent_call_line(
        self,
        emit: _NodeEmit,
        node: dict[str, Any],
        var: str,
        indent: str = "    ",
    ) -> str:
        """One agent() call line, with the node version encoded in ``phase``.

        ``phase`` rides the engine call signature's identity opts
        ({label, phase, model}) and the journal record — zero engine changes.
        A rerun bumps the version → new phase → new sig → WAL cache miss →
        live execution; an untouched node keeps its phase → stable sig →
        cross-trigger cache hit (free replay). ``options.agent_template``
        carries the node's AgentGroup member name (bare, SDD-0030; see
        :meth:`_agent_call_expr`).
        """
        node_id = str(node.get("id") or "")
        prompt_expr = self._prompt_expr(
            node, emit.graph, emit.nodes_by_id, emit.bindings, emit.run_outputs,
        )
        call = self._agent_call_expr(prompt_expr, node, node_id, emit.run_outputs)
        return f"{indent}{var} = await agent({call})"

    def _prompt_expr(
        self,
        node: dict[str, Any],
        graph: DesignerExecutionGraph,
        nodes_by_id: dict[str, dict[str, Any]],
        bindings: dict[str, str],
        run_outputs: dict[str, Any] | None = None,
    ) -> str:
        """Build the prompt expression: base prompt + inlined upstream values.

        Upstream text rides the inline (script variable when it executes this
        trigger, frozen literal when it completed earlier). Artifact files are
        deliberately NOT inlined: the preserved worker runtime discovers them
        itself (read_upstream reads run.node_states; media tools collect
        references from identity_refs / pipeline roles) — matching the old
        executor's decoupled-node contract, no new inter-node dependency.
        """
        nid = str(node.get("id") or "")
        ticket = ""
        if self._run_id:
            ticket = f"shot_ticket: design:{self._graph_id}:{self._run_id}:{nid}\n"
        base = json.dumps(ticket + _node_prompt(node), ensure_ascii=False)
        upstream_ids = [
            str(edge.get("source") or "")
            for edge in (graph.get("edges") or [])
            if str(edge.get("target") or "") == nid
        ]
        if not upstream_ids:
            return base
        outputs = run_outputs or {}
        parts = [f"{base} + {_UPSTREAM_HEADER!r}"]
        for index, upstream_id in enumerate(upstream_ids):
            upstream = nodes_by_id.get(upstream_id)
            label = _node_label(upstream) if upstream else upstream_id
            section_title = json.dumps(
                f"### {label} ({upstream_id})\n", ensure_ascii=False
            )
            parts.append(f"{section_title} + ")
            bound = bindings.get(upstream_id)
            if bound is None:
                parts.append(
                    json.dumps(
                        _NO_OUTPUT_PLACEHOLDER.format(node_id=upstream_id),
                        ensure_ascii=False,
                    )
                )
            else:
                parts.append(f"str({bound})")
            if index < len(upstream_ids) - 1:
                parts.append(" + '\\n' + ")
        return "".join(parts)


def _safe_ident(node_id: str) -> str:
    """Script-safe identifier fragment for a node id."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in node_id)
    if not safe or not safe[0].isalpha():
        safe = f"n{safe}"
    return safe


__all__ = ["GraphScriptCompiler"]
