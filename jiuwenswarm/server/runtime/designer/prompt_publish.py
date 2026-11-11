# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persist a node's finalized media prompt and push it to the canvas immediately.

Scene / character stills used to stamp ``last_wan_prompt`` / ``generate.prompt``
on the in-memory graph only, then wait for the (slow) image call to finish
before the store and websocket saw the text. The submenu kept showing the
director's fallback (often an empty-room scaffold) and looked like that
fallback was what the model was using.

Call :func:`emit_prompt_artifact` as soon as the prompt that will be sent is
known, before the generation request starts.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PROMPT_KEYS = (
    "prompt",
    "generate",
    "last_wan_prompt",
    "last_approved_prompt",
    "user_edit_prompt",
    "regenerate_packet",
    "director_image_prompt_notes",
    "director_image_prompt_approved",
    "prompt_char_limit",
    "clip_prompt_preview",
)


def _node_by_id(graph: dict[str, Any] | None, node_id: str) -> dict[str, Any] | None:
    if not isinstance(graph, dict):
        return None
    wanted = str(node_id or "").strip()
    if not wanted:
        return None
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("id") or "") == wanted:
            return node
    return None


def _ensure_prompt_stamped(node: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Write the sent prompt onto the live node config if it is not there yet."""
    text = str(prompt or "").strip()
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    if cfg is not node.get("config"):
        node["config"] = cfg
    if not text:
        return cfg
    if str(cfg.get("last_wan_prompt") or "").strip() != text:
        generate = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        generate["prompt"] = text
        cfg["generate"] = generate
        cfg["prompt"] = text
        cfg["last_wan_prompt"] = text
        cfg["last_approved_prompt"] = text
        node["config"] = cfg
    return cfg


def persist_and_publish_node_prompt(
    graph: dict[str, Any] | None,
    node_id: str,
    prompt: str | None = None,
) -> dict[str, Any] | None:
    """Merge this node's prompt surfaces into the stored graph and push the canvas.

    Only prompt-related config keys are written onto the stored record so a
    concurrent layout save (drag / auto-layout while the agent is working)
    is not overwritten by a stale in-memory execution graph.
    """
    live = _node_by_id(graph, node_id)
    if live is None:
        return None
    if prompt:
        _ensure_prompt_stamped(live, prompt)
    live_cfg = live.get("config") if isinstance(live.get("config"), dict) else {}
    if not str(live_cfg.get("last_wan_prompt") or live_cfg.get("prompt") or "").strip():
        return None
    graph_id = str((graph or {}).get("graph_id") or "").strip()
    if not graph_id:
        return None
    try:
        from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore

        store = DesignerGraphStore()
        stored = store.get_graph(graph_id)
        target = stored if isinstance(stored, dict) else dict(graph or {})
        stored_node = _node_by_id(target, node_id)
        if stored_node is None:
            return None
        cfg = dict(stored_node.get("config") or {}) if isinstance(stored_node.get("config"), dict) else {}
        for key in _PROMPT_KEYS:
            if key in live_cfg:
                cfg[key] = live_cfg[key]
        stored_node["config"] = cfg
        saved = store.save_graph(target)
    except Exception:  # noqa: BLE001
        logger.debug("persist node prompt failed: node=%s", node_id, exc_info=True)
        return None
    _push_saved_graph(saved)
    return saved


def _push_saved_graph(saved: dict[str, Any]) -> None:
    session_id = str((saved.get("metadata") or {}).get("session_id") or "").strip()
    graph_id = str(saved.get("graph_id") or "").strip()
    channel_id = "web"
    try:
        from jiuwenswarm.server.runtime.design.context import get_design_context_registry

        context = get_design_context_registry().for_graph(graph_id) if graph_id else None
        if context is not None:
            channel_id = str(getattr(context, "channel_id", "") or channel_id)
            spawner = getattr(context, "spawner", None)
            callback = getattr(spawner, "_on_graph_update", None) if spawner is not None else None
            if callable(callback):
                callback(dict(saved))
                return
    except Exception:  # noqa: BLE001
        logger.debug("prompt graph push via context failed", exc_info=True)
    if not session_id:
        return
    try:
        from jiuwenswarm.server.runtime.design.team_bridge import push_graph_update

        push_graph_update(channel_id, session_id, dict(saved))
    except Exception:  # noqa: BLE001
        logger.debug("prompt graph push failed: session=%s", session_id, exc_info=True)


def emit_prompt_artifact(ctx: Any, prompt: str) -> None:
    """Notify the canvas of a finalized media prompt, if one is available."""
    text = str(prompt or "").strip()
    if not text:
        return
    callback = getattr(ctx, "on_prompt_artifact", None) if ctx is not None else None
    if callable(callback):
        try:
            callback(text)
            return
        except Exception:  # noqa: BLE001
            logger.debug("on_prompt_artifact callback failed", exc_info=True)
    if ctx is None:
        return
    persist_and_publish_node_prompt(
        ctx.graph if isinstance(getattr(ctx, "graph", None), dict) else None,
        str(getattr(ctx, "node_id", "") or ""),
        text,
    )
