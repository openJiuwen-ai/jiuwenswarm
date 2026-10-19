# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The old host-layer node guarantees, re-armed on the worker backend (plan A).

User ruling (2026-10-12, 方案 A "完全照搬"): the picked Director executor ran
every node agent with three host-side guarantees that the swarmflow migration
dropped (they lived in ``node_agent.py``'s post-turn block, which the manifest
worker tools never inherited):

* **text guarantee** — a node that ends with text but no ``output_ref`` gets
  its text written to the workspace and a file ref stamped (the old
  ``node_complete`` contract; without it the canvas cell stays empty);
* **media materialization** — a node that expects media (clip/character/scene/…)
  whose result lacks the required family gets the media materialized by the
  node handler — the SAME ``handlers/*`` generation backends the package's
  ``generate_image`` / ``generate_video`` tools call, so there is one
  implementation and the LLM keeps the creative path (agents author direction;
  the host guarantees the product);
* **family gate** — if materialization still does not satisfy the required
  family, the node FAILS (never ``completed`` with a markdown stub).

The guarantees run per node at the worker call's completion — the exact point
the old backstop occupied. A gate failure declines the call
(``AgentResult.skipped``) so the engine does not retry the worker's LLM round
(the old executor failed the node on the spot), and the run's own failed-node
gate at trigger end reports it.

All family/satisfaction/merge helpers are imported from ``node_agent`` —
zero reimplementation, original behavior.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.workflow.engine.backends.base import AgentResult

logger = logging.getLogger(__name__)


class _MediaGuaranteeDeclined(Exception):
    """Internal: the guarantee failed; ``run`` converts it to a skipped call."""


class MediaGuaranteeBackend:
    """Delegating worker backend that re-arms the old host guarantees per node.

    Wraps the engine-facing backend (``TeamWorkerBackend`` or the fallback
    ``DesignerWorkerBackend``): every attribute forwards to the inner backend
    except ``run``, which enforces the guarantees after the worker's turn.
    """

    def __init__(
        self,
        inner: Any,
        *,
        graph: dict[str, Any],
        run: dict[str, Any],
        bridge: Any,
        media_root: "Path | None" = None,
    ) -> None:
        self._inner = inner
        self._graph = graph
        self._run = run
        self._bridge = bridge
        self._media_root = media_root

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def run(
        self,
        prompt: str,
        opts: dict,
        schema_json: dict | None,
        *,
        call_key: str | None = None,
    ) -> AgentResult:
        node_id = str(opts.get("label") or "")
        member_call = schema_json is not None and bool(node_id)
        try:
            result = await self._inner.run(prompt, opts, schema_json, call_key=call_key)
        except Exception as exc:
            if not member_call:
                raise
            # The engine retries a raising call, and every retry re-runs the
            # member's paid generation: a member call ends here instead.
            receipt = self._receipt(node_id)
            if receipt is not None:
                return receipt
            self._bridge.mark_node_failed(
                node_id,
                self._member_error(node_id) or str(exc) or "member worker failed",
            )
            return AgentResult(text=None, skipped=True)
        try:
            await self._enforce(opts, result, member_call=member_call)
        except _MediaGuaranteeDeclined:
            return AgentResult(text=None, skipped=True)
        if member_call:
            # The receipt comes from the Store write, never the worker's own
            # structured_output (a malformed one would trigger a retry).
            return self._receipt(node_id) or result
        return result

    def _receipt(self, node_id: str) -> AgentResult | None:
        produced = self._bridge.produced_for(node_id)
        if produced is None or not produced.get("output_ref"):
            return None
        receipt: dict[str, Any] = {"output_ref": produced["output_ref"]}
        if produced.get("output_refs"):
            receipt["output_refs"] = produced["output_refs"]
        return AgentResult(text=str(produced.get("text") or ""), structured=receipt)

    def _member_error(self, node_id: str) -> str | None:
        lookup = getattr(self._bridge, "member_error_for", None)
        return lookup(node_id) if callable(lookup) else None

    # ------------------------------------------------------------------
    # the old host guarantees (node_agent post-turn block, 52469f4c)
    # ------------------------------------------------------------------

    async def _enforce(
        self, opts: dict, result: AgentResult, *, member_call: bool = False
    ) -> None:
        from jiuwenswarm.server.runtime.designer.node_agent import (
            _node_expects_media,
            _result_satisfies_required_media,
        )
        from jiuwenswarm.server.runtime.designer.handlers.types import (
            NodeExecutionContext,
            NodeResult,
        )

        node_id = str(opts.get("label") or "")
        if not node_id:
            return
        node = None
        for candidate in self._graph.get("nodes") or []:
            if str(candidate.get("id") or "") == node_id:
                node = candidate
                break
        if node is None:
            return
        state = (self._run.get("node_states") or {}).get(node_id) or {}
        # The node-result sink has already written this trigger's product
        # into the node's current slots (single triggers write the same
        # slots as full runs), so the state reads below see this trigger's
        # fresh refs, not an earlier version's.
        if member_call:
            # Member nodes write their product through submit_node_output; only
            # THIS trigger's write counts (state may still hold an older
            # run's refs), and the worker's final text is its receipt, never
            # a creative prompt for the handler.
            produced = self._bridge.produced_for(node_id) or {}
            agent_result = NodeResult(
                output_ref=produced.get("output_ref"),
                output_refs=produced.get("output_refs"),
                message=str(produced.get("text") or ""),
            )
        else:
            agent_result = NodeResult(
                output_ref=state.get("output_ref"),
                output_refs=state.get("output_refs"),
                message=str(result.text or state.get("text") or ""),
            )
        ctx = NodeExecutionContext(
            graph=self._graph,
            run_id=str(self._run.get("run_id") or ""),
            node_id=node_id,
            run=self._run,
            media_dir=self._media_root,
        )

        if _node_expects_media(node) and not _result_satisfies_required_media(
            agent_result, node, ctx
        ):
            await self._materialize_media(node, agent_result, ctx)
            return

        # A text member that never submitted has no product: its final
        # message is a receipt, not the brief / storyboard.
        if member_call:
            if agent_result.output_ref is None:
                message = (
                    self._member_error(node_id)
                    or f"node {node_id} finished without submit_node_output"
                )
                logger.warning("[design-guarantee] %s", message)
                self._bridge.mark_node_failed(node_id, message)
                raise _MediaGuaranteeDeclined()
            return

        # Text guarantee: text landed but no product ref → write it to the
        # workspace so the canvas cell has a uri (old node_complete contract).
        if not state.get("output_ref") and str(agent_result.message or "").strip():
            self._write_text_result(node_id, agent_result.message)

    async def _materialize_media(
        self, node: dict[str, Any], agent_result: Any, ctx: Any
    ) -> None:
        from jiuwenswarm.server.runtime.designer.handlers import get_node_handler
        from jiuwenswarm.server.runtime.designer.node_agent import (
            _prefer_media_primary,
            _required_media_family,
            _result_satisfies_required_media,
            _seed_handler_prompt,
        )

        node_id = str(node.get("id") or ctx.node_id)
        required = _required_media_family(node)
        _seed_handler_prompt(node, agent_result)
        logger.info(
            "[design-guarantee] materializing media via handler: node=%s required=%s",
            node_id,
            required,
        )
        try:
            media = await get_node_handler(node).execute(node, ctx)
        except Exception as exc:  # noqa: BLE001 — gate failure, not a crash
            logger.exception(
                "[design-guarantee] materialization failed: node=%s", node_id
            )
            self._bridge.mark_node_failed(
                node_id, f"media materialization failed: {exc}"
            )
            raise _MediaGuaranteeDeclined() from exc
        merged = _prefer_media_primary(media, agent_result, required=required)
        if merged is None:
            merged = media
        if not _result_satisfies_required_media(merged, node, ctx):
            message = (
                f"node {node_id} handler did not produce required "
                f"{required or 'media'}"
            )
            logger.warning("[design-guarantee] %s", message)
            self._bridge.mark_node_failed(node_id, message)
            raise _MediaGuaranteeDeclined()
        # Media primary, agent-authored text kept — the old merged writeback.
        # Full text, never truncated: downstream nodes compile against
        # state["text"], so a trimmed fallback would silently cut the input
        # every successor node sees.
        self._bridge.on_node_result(
            node_id,
            {
                "text": str(agent_result.message or media.message or ""),
                "output_ref": merged.output_ref,
                "output_refs": merged.output_refs,
            },
        )
        logger.info(
            "[design-guarantee] media materialized: node=%s uri=%s",
            node_id,
            str((merged.output_ref or {}).get("uri") or ""),
        )

    def _write_text_result(self, node_id: str, text: str) -> None:
        from jiuwenswarm.server.runtime.designer.handlers.common import (
            file_output_ref,
            write_workspace_text,
        )

        run_id = str(self._run.get("run_id") or "designer")
        path = write_workspace_text(f"designer_agent_{run_id}_{node_id}", text)
        ref = file_output_ref(path, kind="text", mime_type="text/markdown")
        self._bridge.on_node_result(
            node_id, {"text": text, "output_ref": ref, "output_refs": [ref]}
        )
        logger.info(
            "[design-guarantee] text written to workspace: node=%s path=%s",
            node_id,
            path,
        )


__all__ = ["MediaGuaranteeBackend"]
