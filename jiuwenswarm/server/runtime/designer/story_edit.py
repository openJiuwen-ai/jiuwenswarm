# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Story-wide edits of the Director-approved texts.

The Director chain approves a Brief, a storyboard table and a cast
(``script_analysis.characters``, the source of ``production_specs``) once,
and the brief / storyboard handlers replay those approved texts on every run.
A chat edit that changes a story fact ("make the female clerk a man") must
therefore rewrite the approved texts themselves; patching node prompts alone
never reaches them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CLIP,
    NODE_ROLE_STORYBOARD,
    DesignerExecutionGraph,
    node_config,
    node_pipeline,
)
from jiuwenswarm.server.runtime.designer.subagent import complete_designer_node_text

_BRIEF_REVISION_INSTRUCTION = """Revise the approved short-film Brief below so it follows the edit.
Apply what the edit asks for and what it directly implies across every section;
keep everything else, including identity locks and explicit user facts, unchanged.
Keep the Brief's language and section structure.
Output the full revised Brief as Markdown only.

Edit:
"""

_STORYBOARD_REVISION_INSTRUCTION = """Revise the approved storyboard table below so it follows the edit
and the revised Brief. Apply what the edit asks for and what it directly implies in every row;
keep everything else unchanged. Keep exactly the same header row, the same number of rows,
the same row order and the same Shot numbers. Keep the cell language.
Output the Markdown table only.

Edit:
"""

_CAST_REVISION_INSTRUCTION = """Revise the cast entries below so they follow the edit.
Change only the values the edit asks for or directly implies (for example sex, wardrobe,
description, name); keep every other value unchanged. Keep every "id", the same entries
in the same order and the same keys. Output a JSON array only.

Edit:
"""

_STORY_EDIT_KEYS = ("approved_brief", "approved_storyboard", "script_analysis")

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(?P<body>.*?)\n```\s*$", re.S)


def _strip_fence(text: str) -> str:
    body = str(text or "").strip()
    match = _FENCE_RE.match(body)
    return match.group("body").strip() if match else body


def _metadata(graph: DesignerExecutionGraph) -> dict[str, Any]:
    meta = graph.get("metadata")
    return meta if isinstance(meta, dict) else {}


def has_approved_story(graph: DesignerExecutionGraph) -> bool:
    """True when the Director approved a brief or a storyboard for this graph."""
    meta = _metadata(graph)
    return any(str(meta.get(key) or "").strip() for key in ("approved_brief", "approved_storyboard"))


def mark_story_prompt_edits(before: DesignerExecutionGraph, after: DesignerExecutionGraph) -> None:
    """Keep a brief / storyboard prompt edit made after approval as ``user_edit_prompt``.

    The handlers replay the approved text and never read the prompt, so the
    edit is parked on the node and applied when the node next runs.
    """
    meta = _metadata(after)
    approved_by_role = {
        NODE_ROLE_BRIEF: str(meta.get("approved_brief") or "").strip(),
        NODE_ROLE_STORYBOARD: str(meta.get("approved_storyboard") or "").strip(),
    }
    previous = {
        str(node.get("id") or ""): str(node_config(node).get("prompt") or "").strip()
        for node in before.get("nodes") or []
        if isinstance(node, dict)
    }
    for node in after.get("nodes") or []:
        if not isinstance(node, dict) or not approved_by_role.get(node_pipeline(node)):
            continue
        prompt = str(node_config(node).get("prompt") or "").strip()
        old = previous.get(str(node.get("id") or ""))
        if prompt and old and prompt != old:
            node["config"] = {**node_config(node), "user_edit_prompt": prompt}


def _clip_prompts(node: Any) -> tuple[str, str]:
    cfg = node_config(node)
    generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    return str(cfg.get("prompt") or "").strip(), str(generate.get("prompt") or "").strip()


def mark_clip_prompt_edits(before: DesignerExecutionGraph, after: DesignerExecutionGraph) -> None:
    """Give a chat edit of a clip prompt the same authority as a toolbar edit.

    Without user origin the Director rewrites the clip call from the storyboard
    beat and stamps that text back over ``prompt`` / ``generate.prompt``, so the
    edit is lost on every run. Mirrors ``writeMediaGeneratePatch`` in the web
    designer (``mediaNodeConfig.ts``).
    """
    previous = {
        str(node.get("id") or ""): _clip_prompts(node)
        for node in before.get("nodes") or []
        if isinstance(node, dict)
    }
    for node in after.get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) != NODE_ROLE_CLIP:
            continue
        old = previous.get(str(node.get("id") or ""))
        if old is None:
            continue
        prompt, generate_prompt = _clip_prompts(node)
        if prompt and prompt != old[0]:
            text = prompt
        elif generate_prompt and generate_prompt != old[1]:
            text = generate_prompt
        else:
            continue
        cfg = node_config(node)
        generate = dict(cfg["generate"]) if isinstance(cfg.get("generate"), dict) else {}
        packet = dict(cfg["regenerate_packet"]) if isinstance(cfg.get("regenerate_packet"), dict) else {}
        node["config"] = {
            **cfg,
            "prompt": text,
            "user_edit_prompt": text[:4000],
            "generate": {**generate, "prompt": text, "prompt_origin": "user"},
            "regenerate_packet": {**packet, "prompt": text[:4000]},
        }


def _clear_pending_edits(graph: DesignerExecutionGraph, roles: set[str]) -> None:
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) not in roles:
            continue
        cfg = node_config(node)
        if "user_edit_prompt" in cfg:
            node["config"] = {key: value for key, value in cfg.items() if key != "user_edit_prompt"}


async def _complete(prompt: str, delegate: str, what: str) -> str:
    text = _strip_fence(await complete_designer_node_text(prompt, delegate=delegate))
    if not text:
        raise RuntimeError(f"Chat model did not return a usable revised {what}.")
    return text


async def revise_brief(approved: str, edit: str, *, delegate: str = "") -> str:
    return await _complete(
        f"{_BRIEF_REVISION_INSTRUCTION}{edit}\n\nApproved Brief:\n{approved}",
        delegate,
        "brief",
    )


async def revise_storyboard(approved: str, edit: str, brief: str, *, delegate: str = "") -> str:
    """Revised storyboard table; refuses a revision that changes the shot rows."""
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
        parse_storyboard_shots,
        render_storyboard_shots,
    )

    original = parse_storyboard_shots(approved)
    if not original:
        raise RuntimeError("The approved storyboard has no shot rows to revise.")
    context = f"\n\nRevised Brief:\n{brief}" if brief else ""
    text = await _complete(
        f"{_STORYBOARD_REVISION_INSTRUCTION}{edit}{context}\n\nApproved storyboard:\n{approved}",
        delegate,
        "storyboard",
    )
    revised = parse_storyboard_shots(text)
    if len(revised) != len(original):
        raise RuntimeError(
            f"The revised storyboard has {len(revised)} shots instead of {len(original)}; "
            "add or remove shots with designer_graph_patch instead."
        )
    for before, after in zip(original, revised):
        after["shot_no"] = before["shot_no"]
    return render_storyboard_shots(revised)


def _cast_view(character: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        key: value for key, value in character.items() if isinstance(value, str)
    }
    attrs = character.get("identity_attrs")
    if isinstance(attrs, dict):
        view["identity_attrs"] = {key: value for key, value in attrs.items() if isinstance(value, str)}
    return view


def _merge_cast(original: list[dict[str, Any]], revised: Any) -> list[dict[str, Any]]:
    if not isinstance(revised, list):
        raise RuntimeError("Chat model did not return the revised cast as a JSON array.")
    by_id = {
        str(item.get("id") or ""): item
        for item in revised
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    merged: list[dict[str, Any]] = []
    for character in original:
        update = by_id.get(str(character.get("id") or ""))
        if update is None:
            merged.append(character)
            continue
        out = dict(character)
        for key, value in update.items():
            if key != "id" and isinstance(value, str) and isinstance(character.get(key), str):
                out[key] = value
        attrs = character.get("identity_attrs")
        new_attrs = update.get("identity_attrs")
        if isinstance(attrs, dict) and isinstance(new_attrs, dict):
            out["identity_attrs"] = {
                **attrs,
                **{
                    key: value
                    for key, value in new_attrs.items()
                    if isinstance(value, str) and isinstance(attrs.get(key), str)
                },
            }
        merged.append(out)
    return merged


async def revise_cast(characters: list[dict[str, Any]], edit: str, *, delegate: str = "") -> list[dict[str, Any]]:
    views = [_cast_view(character) for character in characters]
    text = await _complete(
        f"{_CAST_REVISION_INSTRUCTION}{edit}\n\nCast:\n{json.dumps(views, ensure_ascii=False, indent=2)}",
        delegate,
        "cast",
    )
    try:
        revised = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chat model returned an unreadable revised cast: {exc}") from exc
    return _merge_cast(characters, revised)


async def apply_story_edit(
    graph: DesignerExecutionGraph,
    edit: str,
    *,
    delegate: str = "",
) -> list[str]:
    """Revise the approved brief, cast and storyboard in ``graph`` to follow ``edit``.

    All revisions are computed before any is written, so a failure leaves the
    graph untouched. Returns the metadata keys that changed. The clip nodes'
    beats are re-synced from the revised storyboard and the brief / storyboard
    nodes' parked prompt edits are cleared (``edit`` supersedes them).
    """
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
        sync_shot_nodes_from_storyboard_markdown,
    )
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        synchronize_graph_style_from_brief,
    )

    edit = str(edit or "").strip()
    if not edit:
        raise ValueError("edit is required")
    meta = dict(_metadata(graph))
    brief = str(meta.get("approved_brief") or "").strip()
    storyboard = str(meta.get("approved_storyboard") or "").strip()
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]

    updates: dict[str, Any] = {}
    if brief:
        brief = await revise_brief(brief, edit, delegate=delegate)
        updates["approved_brief"] = brief
    if characters:
        updates["script_analysis"] = {
            **analysis,
            "characters": await revise_cast(characters, edit, delegate=delegate),
        }
    if storyboard:
        updates["approved_storyboard"] = await revise_storyboard(storyboard, edit, brief, delegate=delegate)

    graph["metadata"] = {**meta, **updates}
    _clear_pending_edits(graph, {NODE_ROLE_BRIEF, NODE_ROLE_STORYBOARD})
    if "approved_storyboard" in updates:
        sync_shot_nodes_from_storyboard_markdown(graph, updates["approved_storyboard"])
    if brief and ("approved_brief" in updates or "script_analysis" in updates):
        synchronize_graph_style_from_brief(graph, brief)
    return [key for key in _STORY_EDIT_KEYS if key in updates]


async def apply_storyboard_edit(graph: DesignerExecutionGraph, edit: str, *, delegate: str = "") -> bool:
    """Revise only the approved storyboard (a storyboard-node prompt edit)."""
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
        sync_shot_nodes_from_storyboard_markdown,
    )

    meta = dict(_metadata(graph))
    storyboard = str(meta.get("approved_storyboard") or "").strip()
    if not storyboard:
        return False
    brief = str(meta.get("approved_brief") or "").strip()
    revised = await revise_storyboard(storyboard, edit, brief, delegate=delegate)
    graph["metadata"] = {**meta, "approved_storyboard": revised}
    _clear_pending_edits(graph, {NODE_ROLE_STORYBOARD})
    sync_shot_nodes_from_storyboard_markdown(graph, revised)
    return True


__all__ = [
    "apply_story_edit",
    "apply_storyboard_edit",
    "has_approved_story",
    "mark_clip_prompt_edits",
    "mark_story_prompt_edits",
    "revise_brief",
    "revise_cast",
    "revise_storyboard",
]
