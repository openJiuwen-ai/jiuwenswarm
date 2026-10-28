# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared implementation of the designer members' node tools.

Every canvas node runs once as its same-named member (``brief`` /
``character`` / ``scene`` / ``storyboard`` / ``clip`` / ``compose``). Each
member package declares three tools whose bodies delegate here (loaded by
path through the member's ``support.load_node_tools``):

* ``read_node_context`` — the node's context packet (node, upstream outputs,
  storyboard row, locks, continuity, reference bindings, prompt budget, user
  canvas edits);
* ``generate_<member>`` — ONE generation through the node's registered
  handler (``designer.handlers``), with the member's prompt as the node's
  focused prompt;
* ``submit_node_output`` — writes the generated product back to the run
  record through the trigger's design bridge.

The task prompt carries ``shot_ticket: design:{graph}:{run}:{node}``; the
tools resolve it against ``node_run_env`` (``graph`` / ``run`` / ``bridge`` /
``media_root`` / ``generations``), which the run coordinator binds on the
graph's design context for the duration of a trigger. Every entry point
returns a result dict and never raises.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TICKET_RE = re.compile(
    r"design:(?P<graph_id>[^:\s]+):(?P<run_id>[^:\s]+):(?P<node_id>[^:\s]+)"
)

#: What each member hands back: the node's output kind and the generate
#: tool's result key (mirrors the member manifests' ``metadata.output_kind``).
MEMBER_OUTPUT_KIND: dict[str, str] = {
    "brief": "text",
    "storyboard": "table",
    "character": "image",
    "scene": "image",
    "clip": "video",
    "compose": "video",
}

_URI_KEY_BY_KIND = {
    "text": "text_uri",
    "table": "text_uri",
    "image": "image_uri",
    "video": "video_uri",
}

_STAGE_BY_KIND = {
    "text": "calling text model",
    "table": "writing storyboard table",
    "image": "calling image model",
    "video": "calling video model",
}

#: Text is one cheap chat call; image/video generations are paid and are
#: never repeated by the tool.
_GENERATE_ATTEMPTS_BY_KIND = {"text": 2}


def image_generate_timeout_seconds() -> float:
    """AbilityManager budget for character / scene generate tools."""
    from jiuwenswarm.server.runtime.designer.media_generation import image_tool_timeout_seconds

    return image_tool_timeout_seconds()


def video_generate_timeout_seconds() -> float:
    """AbilityManager budget for clip / compose generate tools."""
    from jiuwenswarm.server.runtime.designer.media_generation import video_tool_timeout_seconds

    return video_tool_timeout_seconds()


class _ToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _resolve(shot_ticket: Any, member: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    from jiuwenswarm.common.schema.designer_graph import node_agent_template
    from jiuwenswarm.server.runtime.design.context import get_design_context_registry

    match = _TICKET_RE.search(str(shot_ticket or ""))
    if match is None:
        raise _ToolError("invalid_shot_ticket")
    graph_id, run_id, node_id = (
        match.group("graph_id"),
        match.group("run_id"),
        match.group("node_id"),
    )
    context = get_design_context_registry().for_graph(graph_id)
    env = getattr(context, "node_run_env", None)
    if not isinstance(env, dict):
        raise _ToolError("no_active_run")
    if str(env["run"].get("run_id") or "") != run_id:
        raise _ToolError("ticket_run_mismatch")
    node = next(
        (
            candidate
            for candidate in env["graph"].get("nodes") or []
            if isinstance(candidate, dict) and str(candidate.get("id") or "") == node_id
        ),
        None,
    )
    if node is None:
        raise _ToolError("ticket_node_not_found")
    if member is not None and node_agent_template(node) != member:
        raise _ToolError("ticket_member_mismatch")
    return env, node


def _execution_context(env: dict[str, Any], node_id: str) -> Any:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    return NodeExecutionContext(
        graph=env["graph"],
        run_id=str(env["run"].get("run_id") or ""),
        node_id=node_id,
        run=env["run"],
        emit_activity=env["bridge"].node_activity_sink(node_id),
        media_dir=env["media_root"],
    )


def _output_kind(node: dict[str, Any]) -> str:
    from jiuwenswarm.common.schema.designer_graph import node_agent_template

    return MEMBER_OUTPUT_KIND.get(node_agent_template(node), "text")


def _note_member_error(
    env: dict[str, Any], node_id: str, error: str | None, *, keep_existing: bool = False
) -> None:
    bridge = env.get("bridge")
    note = getattr(bridge, "note_member_error", None)
    if not callable(note):
        return
    if keep_existing:
        existing = getattr(bridge, "member_error_for", None)
        if callable(existing) and existing(node_id):
            return
    note(node_id, error)


def note_rejection(shot_ticket: Any, member: str, tool: str, error: str) -> None:
    """Record why ``tool`` refused the member's call on its node.

    A member stops without ``structured_output`` once a tool fails, so this
    note is the only reason the node can show. An earlier, more specific error
    stays, and a refusal after the node already generated explains nothing.
    """
    try:
        env, node = _resolve(shot_ticket, member)
    except _ToolError as exc:
        _warn_unresolved(tool, shot_ticket, f"{error} ({exc.code})")
        return
    node_id = str(node.get("id") or "")
    if node_id in env["generations"]:
        return
    _note_member_error(env, node_id, f"{tool} rejected: {error}", keep_existing=True)


def _warn_unresolved(tool: str, shot_ticket: Any, error: str) -> None:
    """A refusal no node can carry; the worker log is the only trace."""
    logger.warning("[design-tools] %s rejected: error=%s ticket=%s", tool, error, shot_ticket)


def _same_file(left: str, right: str) -> bool:
    from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri

    a, b = path_from_uri(left), path_from_uri(right)
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# ─── read_node_context ────────────────────────────────────────────────────


async def read_node_context(shot_ticket: Any) -> dict[str, Any]:
    """The node's context packet for its member."""
    try:
        env, node = _resolve(shot_ticket)
    except _ToolError as exc:
        _warn_unresolved("read_node_context", shot_ticket, exc.code)
        return {"error": exc.code}
    try:
        return _context_packet(env, node)
    except Exception as exc:  # noqa: BLE001 — tools answer, never raise
        logger.exception("[design-tools] read_node_context failed")
        return {"error": f"context_read_failed: {exc}"}


def _context_packet(env: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    from jiuwenswarm.common.schema.designer_graph import node_config, node_pipeline

    node_id = str(node.get("id") or "")
    ctx = _execution_context(env, node_id)
    cfg = node_config(node)
    meta = env["graph"].get("metadata") if isinstance(env["graph"].get("metadata"), dict) else {}
    kind = _output_kind(node)
    pipeline = node_pipeline(node)
    approved_key = {"brief": "approved_brief", "storyboard": "approved_storyboard"}.get(pipeline)
    approved = str(meta.get(approved_key) or "").strip() if approved_key else ""
    edits = meta.get("user_canvas_edits")
    return {
        "node": {
            "id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "pipeline": pipeline,
            "output_kind": kind,
            "prompt": str(cfg.get("prompt") or ""),
        },
        "graph": {
            "title": env["graph"].get("title"),
            "description": env["graph"].get("description"),
        },
        "approved_text": approved or None,
        "upstream_outputs": _upstream_outputs(ctx, node),
        "storyboard_row": _storyboard_row(ctx, node),
        "locks": _locks(cfg, meta),
        "continuity": _continuity(cfg, meta),
        "reference_bindings": _reference_bindings(ctx, node),
        "prompt_limit": _prompt_limit(kind),
        "user_canvas_edits": list(edits) if isinstance(edits, list) else [],
    }


def _upstream_outputs(ctx: Any, node: dict[str, Any]) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    items = predecessor_outputs(ctx, node) or []
    return [
        {
            "node_id": item.node_id,
            "pipeline": item.role,
            "label": item.label,
            "kind": item.kind,
            "uri": item.path.as_uri() if item.path is not None else None,
            "text": item.text or None,
        }
        for item in items
    ]


def _storyboard_row(ctx: Any, node: dict[str, Any]) -> dict[str, Any] | None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_CLIP, node_pipeline

    if node_pipeline(node) != NODE_ROLE_CLIP:
        return None
    from jiuwenswarm.server.runtime.designer.handlers.clip import _shot_for_node

    index, shot = _shot_for_node(ctx.graph, node, ctx)
    if shot is None:
        return None
    return {"clip_index": index, **dict(shot)}


def _locks(cfg: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Graph-level ``*_lock`` entries overlaid by the node's own."""
    locks: dict[str, Any] = {}
    for source in (meta, cfg):
        for key, value in source.items():
            if key.endswith("_lock") and key != "continuity_lock" and value:
                locks[key] = value
    specs = str(meta.get("production_specs") or "").strip()
    if specs:
        locks["production_specs"] = specs
    return locks


_CONTINUITY_KEYS = (
    "first_of_setting",
    "already_done",
    "end_state",
    "previous_clip_speech",
    "continuity_clip_node_id",
)


def _continuity(cfg: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    continuity: dict[str, Any] = {}
    lock = cfg.get("continuity_lock")
    if not isinstance(lock, dict) or not lock:
        by_index = meta.get("continuity_locks")
        index = cfg.get("clip_index")
        if isinstance(by_index, dict) and index is not None:
            lock = by_index.get(str(index))
    if isinstance(lock, dict) and lock:
        continuity["lock"] = lock
    for key in _CONTINUITY_KEYS:
        value = cfg.get(key)
        if value not in (None, "", [], {}):
            continuity[key] = value
    return continuity


def _reference_bindings(ctx: Any, node: dict[str, Any]) -> list[dict[str, Any]]:
    """The reference files the node's generation attaches, in attach order."""
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        node_clip_index,
        node_pipeline,
    )

    pipeline = node_pipeline(node)
    paths: list[Path] = []
    if pipeline == NODE_ROLE_CLIP:
        from jiuwenswarm.server.runtime.designer.handlers.clip import (
            collect_clip_reference_images,
        )

        paths = list(collect_clip_reference_images(ctx, node_clip_index(node), node=node))
    elif pipeline == NODE_ROLE_COMPOSE:
        from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

        paths = [
            item.path
            for item in predecessor_outputs(ctx, node) or []
            if item.kind == "video" and item.path is not None
        ]
    else:
        from jiuwenswarm.server.runtime.designer.user_references import (
            wired_user_reference_images,
        )

        paths = list(wired_user_reference_images(ctx, node))
    bindings: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        bindings.append(
            {
                "order": len(bindings) + 1,
                "uri": path.resolve().as_uri(),
                "label": path.name,
            }
        )
    return bindings


def _prompt_limit(kind: str) -> dict[str, Any] | None:
    if kind not in {"image", "video"}:
        return None
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    limit = resolve_prompt_limit(kind)
    return {
        "kind": limit.kind,
        "model": limit.model,
        "max_chars": limit.max_chars,
        "unit": limit.unit,
    }


# ─── generate_<member> ────────────────────────────────────────────────────

_MISSING = object()


async def generate_node_output(
    shot_ticket: Any,
    *,
    member: str,
    prompt: str,
    subject: str = "",
    setting: str = "",
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the node's handler with the member's prompt; text gets one retry."""
    kind = MEMBER_OUTPUT_KIND.get(member, "text")
    uri_key = _URI_KEY_BY_KIND[kind]

    def failure(error: str) -> dict[str, Any]:
        return {
            "success": False,
            uri_key: None,
            "sent_prompt": prompt,
            "prompt_source": "argument",
            "error": error,
        }

    try:
        env, node = _resolve(shot_ticket, member)
    except _ToolError as exc:
        _warn_unresolved(f"{member} generate", shot_ticket, exc.code)
        return failure(exc.code)
    node_id = str(node.get("id") or "")
    focused = prompt.strip()
    lead = (subject or setting or "").strip()
    if lead and lead not in focused:
        focused = f"{lead}\n{focused}"
    injected: dict[str, Any] = {"prompt": focused}
    authored_duration = (node.get("config") or {}).get("duration_sec")
    has_authored_duration = isinstance(authored_duration, (int, float)) and not isinstance(
        authored_duration, bool
    )
    if duration_seconds is not None and duration_seconds > 0 and not has_authored_duration:
        injected["duration_sec"] = int(round(duration_seconds))
    attempts = _GENERATE_ATTEMPTS_BY_KIND.get(kind, 1)
    result: Any = None
    sent_prompt = ""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result, sent_prompt = await _run_handler(env, node, injected, kind)
            break
        except asyncio.CancelledError:
            # CancelledError is shared by tool fail_after, the node wall-clock
            # cap, and user/session stop — we cannot tell which here, so keep
            # the note neutral. Without it the node only shows the engine's
            # "no structured_output" mask.
            message = f"{member} generation cancelled or timed out"
            _note_member_error(env, node_id, message)
            logger.warning(
                "[design-tools] generate cancelled or timed out: node=%s member=%s",
                node_id,
                member,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — a failed generation is an answer
            last_error = exc
            logger.warning(
                "[design-tools] generate failed: node=%s member=%s attempt=%d/%d err=%s",
                node_id,
                member,
                attempt,
                attempts,
                exc,
            )
    if result is None:
        _note_member_error(env, node_id, f"{member} generation failed: {last_error}")
        return failure(f"generation_failed: {last_error}")
    _note_member_error(env, node_id, None)
    env["generations"][node_id] = result
    primary = result.output_ref or {}
    return {
        "success": True,
        uri_key: str(primary.get("uri") or ""),
        "sent_prompt": sent_prompt,
        "prompt_source": "argument" if sent_prompt == focused else "host_composed",
        "message": result.message,
    }


async def _run_handler(
    env: dict[str, Any],
    node: dict[str, Any],
    injected: dict[str, Any],
    kind: str,
) -> tuple[Any, str]:
    """Execute the node handler with ``injected`` config, then restore it.

    The member's prompt only steers this call: the node's authored config
    (``prompt`` is its task text on the next compile) is put back, while the
    handler's own stamps (last prompt, last frame, …) stay.
    """
    from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

    node_id = str(node.get("id") or "")
    cfg = dict(node.get("config") or {})
    original = {key: cfg.get(key, _MISSING) for key in injected}
    previous_sent = cfg.get("last_wan_prompt")
    cfg.update(injected)
    node["config"] = cfg
    ctx = _execution_context(env, node_id)
    emit = env["bridge"].node_activity_sink(node_id)
    if emit is not None:
        emit("stage", _STAGE_BY_KIND.get(kind, "generating"), "generate")
    try:
        result = await get_node_handler(node).execute(node, ctx)
        after = node.get("config") if isinstance(node.get("config"), dict) else {}
        sent = after.get("last_wan_prompt")
        sent_prompt = (
            str(sent) if sent and sent != previous_sent else str(injected["prompt"])
        )
    finally:
        restored = dict(node.get("config") or {})
        for key, value in original.items():
            if value is _MISSING:
                restored.pop(key, None)
            else:
                restored[key] = value
        node["config"] = restored
    _check_family(result, node, ctx, kind)
    return result, sent_prompt


def _check_family(result: Any, node: dict[str, Any], ctx: Any, kind: str) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri
    from jiuwenswarm.server.runtime.designer.node_agent import (
        _node_expects_media,
        _result_satisfies_required_media,
    )

    primary = getattr(result, "output_ref", None)
    if not isinstance(primary, dict) or not primary.get("uri"):
        raise RuntimeError("handler returned no output file")
    path = path_from_uri(str(primary.get("uri") or ""))
    if path is None or not path.is_file():
        raise RuntimeError(f"handler output file missing: {primary.get('uri')}")
    if kind in {"image", "video"} and _node_expects_media(node):
        if not _result_satisfies_required_media(result, node, ctx):
            raise RuntimeError(f"handler did not produce the required {kind}")


# ─── submit_node_output ───────────────────────────────────────────────────


async def submit_node_output(
    shot_ticket: Any,
    *,
    uri: str | None = None,
    extra_uris: list[str] | None = None,
    text: str = "",
    label: str = "",
) -> dict[str, Any]:
    """Write this node's generated product back to the run record."""

    def rejected(error: str) -> dict[str, Any]:
        return {"success": False, "status": "rejected", "output_ref": None, "error": error}

    try:
        env, node = _resolve(shot_ticket)
    except _ToolError as exc:
        _warn_unresolved("submit_node_output", shot_ticket, exc.code)
        return rejected(exc.code)
    node_id = str(node.get("id") or "")
    result = _submit(env, node, node_id, uri=uri, extra_uris=extra_uris, text=text, label=label)
    if result.get("success"):
        _note_member_error(env, node_id, None)
    else:
        _note_member_error(
            env,
            node_id,
            f"submit_node_output rejected: {result.get('error')}",
            keep_existing=True,
        )
    return result


def _submit(
    env: dict[str, Any],
    node: dict[str, Any],
    node_id: str,
    *,
    uri: str | None,
    extra_uris: list[str] | None,
    text: str,
    label: str,
) -> dict[str, Any]:
    def rejected(error: str) -> dict[str, Any]:
        return {"success": False, "status": "rejected", "output_ref": None, "error": error}

    generated = env["generations"].get(node_id)
    if generated is None:
        return rejected("not_generated")
    produced_refs = [
        ref
        for ref in [generated.output_ref, *(generated.output_refs or [])]
        if isinstance(ref, dict) and ref.get("uri")
    ]
    primary_uri = str(uri or "").strip() or str(produced_refs[0].get("uri") or "")
    primary = next((ref for ref in produced_refs if _same_file(str(ref["uri"]), primary_uri)), None)
    if primary is None:
        return rejected("uri_not_generated_by_this_node")
    refs: list[dict[str, Any]] = [dict(primary)]
    for extra in extra_uris or []:
        match = next(
            (ref for ref in produced_refs if _same_file(str(ref["uri"]), str(extra or ""))),
            None,
        )
        if match is None:
            return rejected("uri_not_generated_by_this_node")
        if all(not _same_file(str(ref["uri"]), str(match["uri"])) for ref in refs):
            refs.append(dict(match))
    for ref in produced_refs:
        if all(not _same_file(str(ref["uri"]), str(have["uri"])) for have in refs):
            refs.append(dict(ref))
    if str(label or "").strip():
        refs[0]["label"] = str(label).strip()
    kind = _output_kind(node)
    if kind in {"text", "table"}:
        body = _read_text(refs[0]) or str(text or "")
    else:
        body = str(text or "").strip() or str(generated.message or "") or str(refs[0].get("label") or "")
    try:
        env["bridge"].on_node_result(
            node_id,
            {"text": body, "output_ref": refs[0], "output_refs": refs},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[design-tools] submit writeback failed: node=%s", node_id)
        return rejected(f"writeback_failed: {exc}")
    return {"success": True, "status": "completed", "output_ref": refs[0], "output_refs": refs}


def _read_text(ref: dict[str, Any]) -> str:
    from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri

    path = path_from_uri(str(ref.get("uri") or ""))
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


__all__ = [
    "MEMBER_OUTPUT_KIND",
    "generate_node_output",
    "image_generate_timeout_seconds",
    "note_rejection",
    "read_node_context",
    "submit_node_output",
    "video_generate_timeout_seconds",
]
