# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text intermediate handlers: brief, storyboard (includes camera script)."""

from __future__ import annotations

import re
from typing import Any, TypedDict

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    DesignerGraphNode,
    node_config,
    node_pipeline,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    role_output_image_path,
    role_output_text,
    write_workspace_text,
)
from jiuwenswarm.server.runtime.designer.a2a_collab import collaboration_card
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
- Script / speech plan (speaker + timing + exact concise dialogue/voiceover when useful; explicitly visual-only if stronger; honor silence)
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

# Shot consistency is the storyboard column. Continuity remains an alias for older tables.
_STORYBOARD_COLUMNS = "Shot | Timeline | Camera | Move | Character action | Shot consistency | Comment"

_STORYBOARD_INSTRUCTION = """Write a time-coherent storyboard from the Brief. This is a camera script table, not a drawing.
Use English Markdown. Include this heading and one table:

## Storyboard

Before the table, write one line: `Visual style: ...` copied from the Brief. Never replace
that style with a leaf/model default.

Use a Markdown table whose columns MUST be:
Shot | Timeline | Camera | Move | Character action | Shot consistency | Comment

Rules:
- Cover every shot in the Brief. Do not stop at a fixed shot count
- Materialize every beat in the Brief's narrative/content arc; fill the full requested duration
- Every row advances action, information, product proof, or emotion; no filler, repeated action, or duplicate coverage
- Preserve the arc's setup/hook, development/turn, and payoff/CTA as applicable
- Timeline as start-end seconds, e.g. 0.0-4.0s — durations must sum coherently
- If the Brief already gives a shot a duration, copy that duration exactly
- If the Brief names a character, place, wardrobe, spoken line, or continuity rule, copy it exactly
- Camera is shot size + angle, e.g. wide/establishing, medium/eye-level, close-up/eye-level, medium/slow pan
- Move is push/pull/pan/dolly/static and speed
- Character action: FULL DETAIL for THIS shot only — who is on screen, where they sit/stand,
  what they do, wardrobe hold. Match cast identity locks. Consecutive windows concatenate;
  do not restage the whole user prompt from a new camera, and do not strip the row to a
  one-liner that drops blocking/speech.
- Shot consistency: explicit forbids from prior shots (do not undo a completed shot unless this
  row or the user prompt asks to repeat it; posture/facing/location locks)
- Comment is the composed-scene prompt: subjects, composition, light, action instant,
  environment, and the Brief's visual style — ready for image gen (composed scene with all
  opening-cast characters in the scene)
- Language: keep every planned speech_line exact and visibly associate speaker, line, and timing with its row; empty = silent
- Enhance sparse prompts: crowd, atmosphere, lighting, wardrobe detail — without inventing new lead characters
- Do not invent a new world that contradicts the brief

Do not output storyboard drawings. Do not explain.

Brief:
"""

_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")


class StoryboardShot(TypedDict):
    shot_no: str
    timeline: str
    camera: str
    move: str
    character_action: str
    scene_change: str
    comment: str


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "shot_no": ("Shot", "镜号"),
    "timeline": ("Timeline", "时间轴"),
    "camera": ("Camera", "镜头视角", "景别"),
    "move": ("Move", "运镜"),
    "character_action": ("Character action", "Character", "人物变化"),
    # Shot consistency is preferred. Older tables may still say Continuity.
    "scene_change": (
        "Shot consistency",
        "Character consistency",
        "Continuity",
        "Scene change",
        "Scene",
        "场景变化",
        "连续性",
    ),
    "comment": ("Comment", "Notes", "注释", "备注", "画面描述", "提示词"),
}
_POSITIONAL_FIELDS = (
    "shot_no",
    "timeline",
    "camera",
    "move",
    "character_action",
    "scene_change",  # Continuity column lands here positionally
    "comment",
)


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
        "character_action": "",
        "scene_change": "",
        "comment": "",
    }


def _header_field_map(cells: list[str]) -> dict[int, str] | None:
    mapping: dict[int, str] = {}
    for index, cell in enumerate(cells):
        name = cell.strip()
        if not name:
            continue
        for field, aliases in _FIELD_ALIASES.items():
            if any(alias == name or alias in name for alias in aliases):
                mapping[index] = field
                break
    if "shot_no" in mapping.values() or "timeline" in mapping.values():
        return mapping
    return None


def _shot_from_cells(
    cells: list[str],
    field_map: dict[int, str] | None,
    fallback_no: int,
) -> StoryboardShot | None:
    shot = _empty_shot()
    if field_map:
        for index, field in field_map.items():
            if index < len(cells):
                shot[field] = cells[index]
    else:
        for index, field in enumerate(_POSITIONAL_FIELDS):
            if index < len(cells):
                shot[field] = cells[index]
    if not shot["shot_no"]:
        shot["shot_no"] = str(fallback_no)
    if not re.match(r"^\d+", shot["shot_no"]) and len(cells) < 4:
        return None
    return shot


def parse_storyboard_shots(text: str) -> list[StoryboardShot]:
    """Read shot rows from storyboard markdown (pipe table OR hierarchical ### Shot)."""
    table = _parse_storyboard_table(text)
    if table:
        return table
    return _parse_storyboard_hierarchical(text)


def _parse_storyboard_table(text: str) -> list[StoryboardShot]:
    shots: list[StoryboardShot] = []
    header_seen = False
    field_map: dict[int, str] | None = None
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        cells = _split_markdown_row(line)
        if not cells or not any(cells):
            continue
        if all(_TABLE_SEP_CELL.match(cell) for cell in cells if cell):
            continue
        joined = "".join(cells)
        header_hit = any(
            marker.casefold() in joined.casefold()
            for marker in ("Shot", "Timeline", "镜号", "时间轴")
        )
        if not header_seen and header_hit:
            header_seen = True
            field_map = _header_field_map(cells)
            continue
        if not header_seen:
            continue
        shot = _shot_from_cells(cells, field_map, len(shots) + 1)
        if shot is None:
            continue
        shots.append(shot)
    return shots


_HIER_SHOT_RE = re.compile(
    r"(?im)^###\s*Shot\s+(\d+)\s*[—\-–:]?\s*(.*)$"
)
_HIER_FIELD_RE = re.compile(
    r"(?im)^-\s*(Timeline|Camera|Camera move|Move|Action|Character action|"
    r"Comment|Keyframe|Doing|Speech)\s*:\s*(.*)$"
)


def _parse_storyboard_hierarchical(text: str) -> list[StoryboardShot]:
    """Parse smart_graph hierarchical storyboard (### Shot N / - Action: …)."""
    shots: list[StoryboardShot] = []
    current: StoryboardShot | None = None
    for line in (text or "").splitlines():
        head = _HIER_SHOT_RE.match(line.strip())
        if head:
            if current is not None:
                shots.append(current)
            idx = int(head.group(1))
            title = str(head.group(2) or "").strip()
            current = {
                "shot_no": str(idx),
                "timeline": "",
                "camera": "",
                "move": "",
                "character_action": "",
                "scene_change": "",
                "comment": title,
            }
            continue
        if current is None:
            continue
        field = _HIER_FIELD_RE.match(line.strip())
        if not field:
            continue
        key = field.group(1).strip().casefold()
        val = field.group(2).strip()
        if key == "timeline":
            current["timeline"] = val
        elif key == "camera":
            current["camera"] = val
        elif key in {"camera move", "move"}:
            current["move"] = val
        elif key in {"action", "character action", "doing"}:
            # Prefer Action over Doing if both appear; first non-empty wins unless Action.
            if key == "action" or not current.get("character_action"):
                current["character_action"] = val
            if key == "action":
                current["comment"] = val or current.get("comment") or ""
        elif key in {"comment", "keyframe"}:
            current["comment"] = val
        elif key == "speech" and not current.get("character_action"):
            current["character_action"] = val
    if current is not None:
        shots.append(current)
    return shots



def shot_generate_prompt(shot: StoryboardShot) -> str:
    """Turn one storyboard row into the keyframe/clip generate prompt."""
    comment = str(shot.get("comment") or "").strip()
    if comment:
        return comment
    parts: list[str] = []
    timeline = str(shot.get("timeline") or "").strip()
    if timeline:
        parts.append(f"Timeline {timeline}")
    for label, key in (
        ("Camera", "camera"),
        ("Camera move", "move"),
        ("Character action", "character_action"),
        ("Scene change", "scene_change"),
    ):
        value = str(shot.get(key) or "").strip()
        if value:
            parts.append(f"{label} {value}")
    return "; ".join(parts)


def sync_shot_nodes_from_storyboard_markdown(
    graph: dict[str, Any],
    markdown: str,
) -> list[str]:
    """Refresh frame/Shot_action + camera from the authored storyboard table.

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
        if role not in {NODE_ROLE_FRAME, NODE_ROLE_CLIP, "keyframe"}:
            continue
        cfg = dict(node.get("config") or {})
        idx = int(cfg.get("shot_index") or 0)
        shot = by_index.get(idx)
        if not isinstance(shot, dict):
            continue
        narrative = str(shot.get("comment") or shot.get("character_action") or "").strip()
        camera = str(shot.get("camera") or "").strip()
        timeline = str(shot.get("timeline") or "").strip()
        changed = False
        if narrative:
            cfg["shot_action"] = narrative[:500]
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
            lead = (
                f"Film shot {idx} only. Camera {cfg.get('camera') or 'medium / eye-level'}. "
                f"Action: {narrative[:300]}."
            )
            if "Action:" not in existing or narrative[:80] not in existing:
                if existing and any(
                    m in existing.upper()
                    for m in ("SCENE SPECS", "STAGING LOCK", "OCCUPANCY", "COSTUME")
                ):
                    gen["prompt"] = f"{lead}\n{existing}"[:2000]
                else:
                    gen["prompt"] = lead[:1200]
                cfg["generate"] = gen
                changed = True
        if changed:
            node["config"] = cfg
            notes.append(f"{node.get('id')}: synced from storyboard shot {idx}")
    return notes


_DURATION_FIELD_RE = re.compile(
    r"(?im)^(?:[-*]\s*)?(?:\*\*)?duration(?:\*\*)?\s*:?\s*~?\s*(\d{1,2}(?:\.\d+)?)",
)
_DURATION_INLINE_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?)\s*-?\s*(?:seconds?|secs?|秒)",
    re.I,
)
_LOGLINE_RE = re.compile(
    r"(?im)^(?:[-*]\s*)?(?:\*\*)?logline(?:\*\*)?\s*:\s*(.+)$",
)


def brief_duration_seconds(text: str, default: int = 5) -> int:
    """Read an explicit duration from a brief or user request."""
    from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
        requested_film_duration_sec,
    )

    asked = requested_film_duration_sec(text or "")
    if asked is not None:
        return int(asked)
    source = text or ""
    field = _DURATION_FIELD_RE.search(source)
    if field:
        try:
            sec = int(round(float(field.group(1))))
        except (TypeError, ValueError):
            sec = 0
        if 1 <= sec <= 30:
            return sec
    match = _DURATION_INLINE_RE.search(source)
    if match:
        try:
            sec = int(round(float(match.group(1))))
        except (TypeError, ValueError):
            sec = 0
        if 1 <= sec <= 30:
            return sec
    return default


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
        from jiuwenswarm.server.runtime.designer.pipeline.production_bible import (
            append_bible_to_markdown,
            build_production_bible,
        )

        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        bible = str(meta.get("production_bible") or "").strip()
        if not bible:
            analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
            bible = build_production_bible(
                analysis,
                user_prompt=str(ctx.graph.get("description") or ""),
            )
        return append_bible_to_markdown(text, bible)
    except Exception:  # noqa: BLE001
        return text


def _sync_style_authority(text: str, ctx: NodeExecutionContext) -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        synchronize_graph_style_from_brief,
    )

    synchronize_graph_style_from_brief(ctx.graph, text)


class BriefNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node_config(node)
        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        approved = str(meta.get("approved_brief") or "").strip()
        if approved:
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


def _storyboard_alignment_context(ctx: NodeExecutionContext) -> str:
    parts: list[str] = []
    character_notes = (
        collaboration_card(ctx.run_id, NODE_ROLE_CHARACTER_DESIGN)
        or role_output_text(ctx, NODE_ROLE_CHARACTER_DESIGN)
    )
    scene_notes = (
        collaboration_card(ctx.run_id, NODE_ROLE_SCENE)
        or role_output_text(ctx, NODE_ROLE_SCENE)
    )
    if character_notes:
        parts.append("Character sheet / notes (character action must match):\n" + character_notes)
    elif role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN) is not None:
        parts.append("A character sheet exists. Character action must match that look, costume, and materials. Do not invent a new character.")
    if scene_notes:
        parts.append("Scene sheet / notes (scene change must match):\n" + scene_notes)
    elif role_output_image_path(ctx, NODE_ROLE_SCENE) is not None:
        parts.append("A scene sheet exists. Scene change must match that space, weather, and lighting. Do not change location.")
    return "\n\n".join(parts)


def _storyboard_rerun_requested(ctx: NodeExecutionContext, node: DesignerGraphNode) -> bool:
    """True when this run exists because the user asked to redo THIS node.

    The storyboard node normally replays ``metadata.approved_storyboard`` verbatim,
    so an approved film cannot drift. That is right for ordinary pipeline
    execution, but it made an explicit "重新生成分镜脚本" a silent no-op: a run was
    created, the node reported completed, and the file was rewritten
    byte-identical with the new requirement dropped. A single-node rerun scoped to
    this node is the user deliberately overriding the approved text, so author it
    again instead of replaying.
    """
    run = ctx.run if isinstance(ctx.run, dict) else {}
    run_meta = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    if not run_meta.get("single_node_rerun"):
        return False
    scope = [str(item) for item in (run_meta.get("scope_node_ids") or []) if str(item)]
    return str(node.get("id") or "") in scope if scope else True


def _storyboard_source(ctx: NodeExecutionContext, node: DesignerGraphNode) -> str:
    """Brief text plus the node's own current requirement.

    The node's prompt used to be only a fallback *behind* the brief, so a
    requirement the user added through chat ("每个分镜至少 4 秒") never reached the
    model while a brief output existed — the storyboard came back unchanged.
    """
    brief = role_output_text(ctx, NODE_ROLE_BRIEF)
    instruction = graph_prompt(ctx.graph, node)
    if not brief:
        return instruction
    if instruction and instruction not in brief:
        return (
            f"{brief}\n\nCURRENT USER REQUIREMENT for this storyboard (authoritative — where it "
            "conflicts with the Brief above, including per-shot durations, follow this and not the "
            f"Brief):\n{instruction}"
        )
    return brief


class StoryboardNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        import asyncio

        cfg = node_config(node)
        planned = cfg.get("planned_shots")
        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        approved = str(meta.get("approved_storyboard") or "").strip()
        if approved and not _storyboard_rerun_requested(ctx, node):
            _sync_style_authority(approved, ctx)
            sync_shot_nodes_from_storyboard_markdown(ctx.graph, approved)
            text = _stamp_bible_on_text(approved, ctx)
            path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
            return NodeResult(
                output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
                message="storyboard written (director)",
            )
        source = _storyboard_source(ctx, node)
        alignment = _storyboard_alignment_context(ctx)
        planned_block = ""
        if isinstance(planned, list) and planned:
            import json as _json

            planned_block = (
                "\n\nPlanned shots from director casting (honor these beats; expand camera detail):\n"
                + _json.dumps(planned, ensure_ascii=False, indent=2)
                + "\n"
            )
        prompt = _STORYBOARD_INSTRUCTION + source + planned_block
        if alignment:
            prompt = f"{prompt}\n\n{alignment}\n"
        text = await asyncio.wait_for(
            complete_designer_node_text(
                prompt,
                delegate=str(cfg.get("delegate") or ""),
                max_tokens=16384,
            ),
            timeout=45.0,
        )
        if not str(text or "").strip():
            raise RuntimeError("Chat model did not return a usable storyboard.")
        _sync_style_authority(text, ctx)
        sync_shot_nodes_from_storyboard_markdown(ctx.graph, text)
        text = _stamp_bible_on_text(text, ctx)
        path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
            message="storyboard table written",
        )
