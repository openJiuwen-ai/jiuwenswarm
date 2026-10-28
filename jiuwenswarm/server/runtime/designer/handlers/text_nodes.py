# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text intermediate handlers: brief, storyboard (includes camera script)."""

from __future__ import annotations

import re
from typing import Any, TypedDict

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CLIP,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    DesignerExecutionGraph,
    DesignerGraphNode,
    node_config,
    node_pipeline,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    write_workspace_text,
)
from jiuwenswarm.server.runtime.designer.subagent import complete_designer_node_text
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

_BRIEF_INSTRUCTION = """Turn the request below into an executable short-film Brief.
Write English Markdown with these sections:
- User prompt (verbatim intent)
- Logline
- Visual style (REQUIRED: preserve the user's requested medium and rendering details verbatim;
  if no style can be inferred, write "cartoonish animation — flat shapes, soft rendering,
  rounded forms")
- Creative concept (specific interpretation and promise; fill unspecified details creatively)
- Narrative / content arc (setup or hook → development/turn → payoff or CTA)
- Timed shot plan spanning the full requested duration; every shot adds new content, no filler or repetition
- Script / speech plan (speaker + timing + exact concise dialogue or voiceover; speech is the default; visual-only only when the user asked for mime, a silent film, or no dialogue)
- Cast (identity locks — face, hair, body, FULL costume for EACH on-screen person, including unnamed groups that share one look; never concatenate; list them on every shot where they are visible)
- Setting / scene geography, lighting, landmarks, opening blocking (who sits/stands where)
- Language / speech lock (film language; exact lines if the user gave them)
- Consistency gates: character consistency, scene consistency, shot consistency, camera views covering every shot
- Shot-view coverage list (distinct cameras/angles needed)
- Duration target and per-shot timing budget
- Audio policy (speech vs music)
- Production specs (style, axis, occupancy, wardrobe) — copy locks, do not drop them
- What to avoid
Preserve every on-screen person, including unnamed groups that share one look, and every shot from the user prompt in FULL DETAIL. Output Markdown only.
Explicit user facts and constraints are authoritative. For a sparse request, develop a coherent
story, celebration, advertisement, or other fitting concept rather than stretching one premise.

Request:
"""

_BRIEF_REVISION_INSTRUCTION = """Revise the approved short-film Brief below so it follows the edited brief task.
Apply what the edited task asks for and what it directly implies across every section;
keep everything else, including identity locks and explicit user facts, unchanged.
Output the full revised Brief as Markdown only.

Edited brief task:
"""

STORYBOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("shot_no", "Shot"),
    ("timeline", "Timeline"),
    ("camera", "Camera"),
    ("move", "Move"),
    ("on_screen", "On screen"),
    ("character_action", "Character action"),
    ("speech", "Speech"),
)

_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")


class StoryboardShot(TypedDict):
    shot_no: str
    timeline: str
    camera: str
    move: str
    on_screen: str
    character_action: str
    speech: str


_FIELD_BY_HEADER: dict[str, str] = {
    header.casefold(): field for field, header in STORYBOARD_COLUMNS
}


def _split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _empty_shot() -> StoryboardShot:
    return {
        "shot_no": "",
        "timeline": "",
        "camera": "",
        "move": "",
        "on_screen": "",
        "character_action": "",
        "speech": "",
    }


def _header_field_map(cells: list[str]) -> dict[int, str] | None:
    mapping = {
        index: _FIELD_BY_HEADER[cell.casefold()]
        for index, cell in enumerate(cells)
        if cell.casefold() in _FIELD_BY_HEADER
    }
    return mapping if "shot_no" in mapping.values() else None


def parse_storyboard_shots(text: str) -> list[StoryboardShot]:
    """Read shot rows from the storyboard's Markdown table."""
    shots: list[StoryboardShot] = []
    field_map: dict[int, str] | None = None
    for line in (text or "").splitlines():
        if "|" not in line:
            if field_map is not None:
                break
            continue
        cells = _split_markdown_row(line)
        if field_map is None:
            field_map = _header_field_map(cells)
            continue
        if all(_TABLE_SEP_CELL.match(cell) for cell in cells if cell):
            continue
        shot = _empty_shot()
        for index, field in field_map.items():
            if index < len(cells):
                shot[field] = cells[index]
        if not shot["shot_no"]:
            shot["shot_no"] = str(len(shots) + 1)
        shots.append(shot)
    return shots


def _table_cell(value: Any) -> str:
    return " ".join(str(value or "").replace("|", "/").split())


def _cast_names(ids: Any, id_to_name: dict[str, str]) -> list[str]:
    if not isinstance(ids, list):
        return []
    return [id_to_name.get(str(cid), str(cid)) for cid in ids if str(cid).strip()]


def render_storyboard_table(
    shots: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    content_language: str = "",
) -> str:
    """Render structured shots as the storyboard's only content: one Markdown table.

    Column headers stay canonical English: the canvas parses them and localizes
    the display itself. Cell text is the authored content language.
    """
    zh = content_language == "zh"
    doing_label = "动作：" if zh else "Doing: "
    pair_sep = "；" if zh else "; "
    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }
    lines = [
        "| " + " | ".join(header for _, header in STORYBOARD_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in STORYBOARD_COLUMNS) + " |",
    ]
    for position, shot in enumerate((s for s in shots if isinstance(s, dict)), start=1):
        on_screen = _cast_names(
            shot.get("on_screen") or shot.get("visible_cast_ids") or shot.get("character_ids"),
            id_to_name,
        )
        action = str(shot.get("action") or shot.get("clip_prompt") or "").strip()
        cast_actions = shot.get("cast_actions") if isinstance(shot.get("cast_actions"), dict) else {}
        name_sep = "：" if zh else ": "
        doing = pair_sep.join(
            f"{id_to_name.get(str(cid), str(cid))}{name_sep}{act}"
            for cid, act in cast_actions.items()
            if str(act).strip()
        )
        if doing:
            action = f"{action} {doing_label}{doing}".strip()
        by_char = (
            shot.get("speech_by_character")
            if isinstance(shot.get("speech_by_character"), dict)
            else {}
        )
        speech = pair_sep.join(
            f"{id_to_name.get(str(cid), str(cid))}{name_sep}{line}"
            for cid, line in by_char.items()
            if str(line).strip()
        ) or str(shot.get("speech_line") or "").strip()
        row = {
            "shot_no": shot.get("clip_index") or position,
            "timeline": shot.get("timeline"),
            "camera": shot.get("camera"),
            "move": shot.get("move") or shot.get("camera_move"),
            "on_screen": ", ".join(on_screen),
            "character_action": action,
            "speech": speech,
        }
        lines.append(
            "| " + " | ".join(_table_cell(row.get(field)) for field, _ in STORYBOARD_COLUMNS) + " |"
        )
    return "\n".join(lines) + "\n"


def sync_shot_nodes_from_storyboard_markdown(
    graph: dict[str, Any],
    markdown: str,
) -> list[str]:
    """Refresh clip_action + camera from the authored storyboard table.

    Only updates beat text — does not touch identity/occupancy wiring.
    """
    notes: list[str] = []
    shots = parse_storyboard_shots(markdown or "")
    if not shots:
        return notes
    by_index: dict[int, StoryboardShot] = {i: shot for i, shot in enumerate(shots, start=1)}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        role = str(node_pipeline(node) or "").strip().lower()
        if role not in {NODE_ROLE_CLIP}:
            continue
        cfg = dict(node.get("config") or {})
        idx = int(cfg.get("clip_index") or 0)
        shot = by_index.get(idx)
        if not isinstance(shot, dict):
            continue
        narrative = str(shot.get("character_action") or "").strip()
        camera = str(shot.get("camera") or "").strip()
        timeline = str(shot.get("timeline") or "").strip()
        changed = False
        if narrative:
            cfg["clip_action"] = narrative[:500]
            changed = True
        if camera:
            cfg["camera"] = camera[:120]
            changed = True
        if timeline:
            cfg["timeline"] = timeline[:40]
            changed = True
        if narrative:
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            existing = str(gen.get("prompt") or "")
            if narrative[:80] not in existing:
                from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
                    shot_brief_prompt,
                )

                gen["prompt"] = shot_brief_prompt(
                    idx,
                    camera=str(cfg.get("camera") or ""),
                    action=narrative[:300],
                )[:1200]
                cfg["generate"] = gen
                changed = True
        if changed:
            node["config"] = cfg
            notes.append(f"{node.get('id')}: synced from storyboard shot {idx}")
    return notes


_LOGLINE_RE = re.compile(
    r"(?im)^(?:[-*]\s*)?(?:\*\*)?logline(?:\*\*)?\s*:\s*(.+)$",
)


def brief_logline(brief: str) -> str:
    text = brief or ""
    match = _LOGLINE_RE.search(text)
    if match:
        return match.group(1).strip().strip("*").strip()
    match = re.search(r"(?i)\*\*logline:\*\*\s*(.+)", text)
    if match:
        return match.group(1).strip()
    return ""


def brief_story_focus(prompt: str) -> str:
    text = (prompt or "").strip()
    text = re.sub(
        r"^(?:generate|create|make|please\s+(?:make|create))\s+"
        r"(?:a\s+)?(?:\d{3,4}p\s+)?(?:video|film|clip|short)?"
        r"(?:\s+in\s+\d+\s+seconds?)?"
        r"(?:\s*,\s*(?:at least\s+)?(?:two|2)\s+cams?)?"
        r"[,:]?\s*",
        "",
        text,
        flags=re.I,
    )
    return text.strip(" ,.")


def _stamp_bible_on_text(text: str, ctx: NodeExecutionContext) -> str:
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.production_specs import (
            append_specs_to_markdown,
            build_production_specs,
        )

        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        bible = str(meta.get("production_specs") or "").strip()
        if not bible:
            analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
            bible = build_production_specs(
                analysis,
                user_prompt=str(ctx.graph.get("description") or ""),
            )
        return append_specs_to_markdown(text, bible)
    except Exception:  # noqa: BLE001
        return text


def _sync_style_authority(text: str, ctx: NodeExecutionContext) -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        synchronize_graph_style_from_brief,
    )

    synchronize_graph_style_from_brief(ctx.graph, text)


def mark_brief_prompt_edits(before: DesignerExecutionGraph, after: DesignerExecutionGraph) -> None:
    """Keep a brief prompt edit made after the director approved a brief as ``user_edit_prompt``."""
    meta = after.get("metadata") if isinstance(after.get("metadata"), dict) else {}
    if not str(meta.get("approved_brief") or "").strip():
        return
    previous = {
        str(node.get("id") or ""): str(node_config(node).get("prompt") or "").strip()
        for node in before.get("nodes") or []
        if isinstance(node, dict)
    }
    for node in after.get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) != NODE_ROLE_BRIEF:
            continue
        prompt = str(node_config(node).get("prompt") or "").strip()
        old = previous.get(str(node.get("id") or ""))
        if prompt and old and prompt != old:
            node["config"] = {**node_config(node), "user_edit_prompt": prompt}


async def _revise_approved_brief(approved: str, edit: str, cfg: dict[str, Any]) -> str:
    text = await complete_designer_node_text(
        f"{_BRIEF_REVISION_INSTRUCTION}{edit}\n\nApproved Brief:\n{approved}",
        delegate=str(cfg.get("delegate") or ""),
    )
    if not str(text or "").strip():
        raise RuntimeError("Chat model did not return a usable revised brief.")
    return text


class BriefNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node_config(node)
        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        approved = str(meta.get("approved_brief") or "").strip()
        if approved:
            edit = str(cfg.get("user_edit_prompt") or "").strip()
            if edit:
                approved = await _revise_approved_brief(approved, edit, cfg)
            _sync_style_authority(approved, ctx)
            text = _stamp_bible_on_text(approved, ctx)
            path = write_workspace_text(f"designer_brief_{ctx.run_id}_{ctx.node_id}", text)
            return NodeResult(
                output_ref=file_output_ref(path, kind=NODE_TYPE_TEXT, mime_type="text/markdown"),
                message="brief written (director)",
            )
        source = graph_prompt(ctx.graph, node)
        skill = str(cfg.get("skill_excerpt") or "")
        audio = (ctx.graph.get("metadata") or {}).get("audio_intent") or {}
        instruction = _BRIEF_INSTRUCTION
        if skill:
            instruction = skill[:2500] + "\n\n" + instruction
        if audio:
            instruction += f"\nAudio policy: {audio}\n"
        text = await complete_designer_node_text(
            instruction + source,
            delegate=str(cfg.get("delegate") or ""),
        )
        if not str(text or "").strip():
            raise RuntimeError("Chat model did not return a usable brief.")
        _sync_style_authority(text, ctx)
        text = _stamp_bible_on_text(text, ctx)
        path = write_workspace_text(f"designer_brief_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TEXT, mime_type="text/markdown"),
            message="brief written",
        )


class StoryboardNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        text = str(meta.get("approved_storyboard") or "").strip()
        if not text:
            planned = node_config(node).get("planned_shots")
            if not isinstance(planned, list) or not planned:
                raise RuntimeError(
                    "Storyboard has no planned shots because this graph was not built "
                    "by the Director chain; rebuild the graph."
                )
            analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
            characters = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
            from jiuwenswarm.server.runtime.designer.pipeline.content_language import (
                current_content_language,
            )

            text = render_storyboard_table(planned, characters, current_content_language())
        sync_shot_nodes_from_storyboard_markdown(ctx.graph, text)
        path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
            message="storyboard table written",
        )
