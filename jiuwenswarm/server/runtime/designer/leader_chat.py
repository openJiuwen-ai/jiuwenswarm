# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Invisible Designer leader: chat-driven graph edits and output refine."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    DesignerExecutionGraph,
    DesignerGraphNode,
    apply_graph_patch,
    node_pipeline,
    utc_now_ms,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[..., None]

# Chinese action words a run request is built from. _RUN_HINT matches them
# positively and _DONT_RUN negates this same constant, so the two cannot drift
# apart. They did once: _RUN_HINT gained 开始|继续|确认|… while _DONT_RUN still
# negated only 生成|运行|重跑|跑, so 「不要开始」 matched nothing in _DONT_RUN and
# fell through to _RUN_HINT's 开始 — an instruction meaning the opposite of
# running returned True and started a generation.
#
# Both spellings are listed on purpose. The leader asks the user to reply 「确认」,
# but a user writing traditional Chinese sends 確認, which matched nothing here and
# was therefore read as "no run requested" — the confirmation was accepted, replied
# to, and silently produced no asset at all.
_ZH_ACTION_VERBS = (
    r"生成|重跑|重生成|运行|運行|合成|拼接|剪成|成片|出片"
    r"|确认|確認|确定|確定|同意|没问题|沒問題"
    r"|开始|開始|继续|繼續|就这样|就這樣|好的|可以|下一步|下个步骤|下個步驟"
)
# Negation prefixes, again in both spellings (別 / 無需 / 暫不 are traditional).
_ZH_NEGATIONS = r"不要|别|別|不用|无需|無需|暂不|暫不|不需要|先不"
# English verbs that read naturally when negated ("don't compose", "do not
# proceed"). ok / okay / yes / looks good are acknowledgements with no negated
# form, so they stay positive hints only.
_EN_NEGATABLE_VERBS = (
    r"run|rerun|generate|regenerate|compose|stitch|concatenate|confirm|proceed|go ahead"
)

# A bare "确认" / "好的" / "OK" answers a proposed next stage, so it authorises
# running it. Without these the edit_graph guard below saw no run intent and
# wiped the plan's run_node_ids, so confirming generated nothing at all.
_RUN_HINT = re.compile(
    rf"({_ZH_ACTION_VERBS}"
    r"|run\b|generate|rerun|regenerate|compose|stitch|concatenate|final cut"
    r"|\bconfirm(?:ed)?\b|\bok\b|\bokay\b|proceed|go ahead|looks good|\byes\b)",
    re.I,
)
_REFINE_HINT = re.compile(
    r"(改|更|精修|refine|more |make |变成|换成|prompt|规格|brief|storyboard|分镜)",
    re.I,
)
_ADD_HINT = re.compile(
    r"(加|添加|新增|add |new |删|去掉|remove|delete|connect|接到|连到)",
    re.I,
)
_CONNECT_HINT = re.compile(r"(接到|连到|connect(?:\s+to)?)", re.I)
# "完成下一步" / "next step" asks for the stage the leader just proposed, whatever
# it happens to be — so the graph, not the model, decides which nodes run.
_NEXT_STEP_HINT = re.compile(
    r"(下一步|下个步骤|下個步驟|下一个|接下来|next step|go ahead with the next)",
    re.I,
)
_VIDEO_HINT = re.compile(r"(视频|镜头|clip|video)", re.I)
# "调整分镜。至少 4 秒" changes the storyboard text itself, so the storyboard node has
# to re-author. Show-me requests ("给我看看分镜脚本") must not match, hence the
# separate change verb.
_STORYBOARD_HINT = re.compile(r"(分镜|分鏡|storyboard)", re.I)
_EDIT_VERB_HINT = re.compile(
    r"(调整|調整|修改|更改|改成|换成|重做|重新|至少"
    r"|adjust|change|modify|redo|regenerate|at least)",
    re.I,
)


def _storyboard_edit_requested(message: str) -> bool:
    """True when the user asked to change the storyboard itself."""
    text = str(message or "")
    return bool(_STORYBOARD_HINT.search(text) and _EDIT_VERB_HINT.search(text))


def _storyboard_node_ids(graph: DesignerExecutionGraph) -> list[str]:
    ids: list[str] = []
    for node in graph.get("nodes") or []:
        if node_pipeline(node) != NODE_ROLE_STORYBOARD:
            continue
        node_id = str(node.get("id") or "")
        if node_id:
            ids.append(node_id)
    return ids

_LEADER_SYSTEM = """You are the invisible Designer Leader. Reply with a JSON object only.
Canvas node type and config.role must be one of: text, table, image, video, audio.
Character/Scene/Keyframe/Clip/Film are pipelines, never node kinds.
Do not rebuild the whole graph. Patch only what the user asked.
Do not create audio nodes. Audio generation is not implemented; users add and upload audio on the canvas.
Do not wire a new node into clip/compose unless the user asked to connect it.
user_canvas_edits is the user's canvas log: add, remove, connect, disconnect, replace.
Treat that log as fact. Do not recreate a removed node, restore a disconnected edge,
or undo a replaced output. Do not connect an added node unless the user asked.
connect and disconnect name node_id and peer_id. replace names the node whose output the user changed.
recent_conversation (if present) is the last few chat turns — use it for context (e.g. a short
follow-up like "make it brighter" refers back to whatever node you two were just discussing).
The user's message may reference an existing node by "@Label" (the node's own label, e.g.
"@Character 1"). When it does, that node's current output image has been attached to this request
so you can see it — keep that same subject/style/identity when you create or refine a node in
response, and mention the "@Label" you used for continuity in your "summary" the same way.
Any attached images at the end of this request (from an "@Label" mention or a file the user
uploaded) are the visual ground truth — describe new/updated node prompts in terms of what you see
in them rather than restating a generic description.

Schema:
{
  "intent": "edit_graph" | "refine_node" | "answer",
  "summary": "short user-facing Chinese or English summary",
  "thinking": "one-line peek of what you are doing",
  "patch": {
    "upsert_nodes": [],
    "upsert_edges": [],
    "remove_node_ids": [],
    "remove_edge_ids": []
  },
  "prompt_updates": [{"node_id": "", "prompt": ""}],
  "identity_updates": [{"node_id": "", "character_id": "", "description": ""}],
  "run_node_ids": []
}

Rules:
- 下一步 / next step means the earliest stage on the canvas that still has no output (has_output
  false). Name that stage's node ids, never a later stage — the canvas decides, not your prose.
- Every node carries has_output: true when its asset already exists. That flag is fact: never
  report an asset as missing, or as freshly generated, when has_output says otherwise. The user
  is looking at the canvas, so a status answer that contradicts it reads as the tool being broken.
- edit_graph: change topology. Leave run_node_ids empty unless the user asked to generate/run.
- refine_node: update that node's config.prompt (and brief/storyboard text if asked). Put the target in run_node_ids so it regenerates.
- Generate in the same turn: when the user asks you to make something new (e.g. "generate the
  keyframe for shot 1", "生成 Shot 1 的关键帧"), both upsert the node AND put its id in
  run_node_ids. Never leave a freshly created node as an empty placeholder and tell the user it is
  "queued" for them to ask again — the node and its asset are produced together in this one turn.
- A confirmation is a go-ahead, never a mere acknowledgement. When the user replies "确认", "好的",
  "可以", "OK" to the next stage you just proposed, actually start that stage this turn: put its
  exact node ids in run_node_ids (for example after a storyboard lands and you offered the
  character sheet and scene set, run those node ids). Do not answer with a sentence saying
  generation has begun while returning no run_node_ids.
- A character/person node carries its real identity (what it looks like — color, costume,
  build, etc.) in config.costume_lock and the story's cast list, NOT in config.prompt — a
  character or appearance change (e.g. "change @Character 1's color to white") MUST also be
  given in identity_updates (node_id, that node's config.character_id, and a short plain-fact
  description of the NEW appearance only — no wrapper phrasing, just the visual facts, same
  language as the existing description) or the regenerated image will keep the OLD appearance
  no matter what prompt_updates says.
- answer: no patch, just summary.
- Only the node(s) listed in run_node_ids are regenerated — downstream scenes and clips are NOT
  rebuilt. Changing one asset (e.g. a character image) must never be treated as a request to redo
  the rest of the film, and a freshly generated asset is never an invitation to continue: do not
  add downstream ids on your own.
- Never ask the user to confirm a generated image or clip. Do not write "角色图确认后…",
  "场景图确认后，下一步…", "分镜确认后…" or any "需要我继续吗？" / "shall I continue?" question.
  The user drives each step themselves and will ask in chat when they want a redo or a refinement —
  do not solicit confirmation.
- Always close by naming the next step. Every reply that produced or changed an asset must end with
  one short line naming the natural next stage of the film pipeline (角色设定 → 场景设定 →
  分镜/关键帧 → 镜头视频 → 合成成片), phrased as a plain statement rather than a question — for
  example "角色图已生成，下一步是场景设定图。" / "The character sheet is done; the next step is the
  scene set." Work out the stage from what already has an output: if the character sheet and the
  scene are done, the next step is the storyboard/keyframes, then the shot videos, then the compose.
  If the whole film is finished, say so and name the finished asset. Never end on a bare report of
  what was just done.
"""


# _DONT_RUN must recognise a negation in full, because _RUN_HINT matches the
# positive half of the very words a negation is built from — "生成" inside
# "不需要生成", "开始" inside "不要开始", "compose" inside "don't compose".
# Enumerating literal negatives ("不要生成") lets every other form fall through to
# _RUN_HINT and be reported as a run request, so the user's "don't generate" would
# start a generation. Two rules keep this honest:
#   * the verbs come from _ZH_ACTION_VERBS / _EN_NEGATABLE_VERBS, never re-typed,
#     so a verb added to _RUN_HINT is negatable the same day;
#   * the (negation)(重新?)(verb) composition stays a composition — flattening it
#     into literal phrases is what reopened the hole.
# 跑 keeps its own alternation: it is negatable ("不用跑") but has never been a
# positive run hint, so adding it to _ZH_ACTION_VERBS would change _RUN_HINT.
_DONT_RUN = re.compile(
    rf"(先别|先別"
    rf"|(?:{_ZH_NEGATIONS})\s*(?:重新)?(?:{_ZH_ACTION_VERBS}|跑)"
    rf"|without (?:running|generating|composing|stitching)"
    rf"|don'?t (?:{_EN_NEGATABLE_VERBS})"
    rf"|do not (?:{_EN_NEGATABLE_VERBS}))",
    re.I,
)


def message_asks_to_run(message: str, *, run_new_nodes: bool = False) -> bool:
    text = str(message or "")
    if _DONT_RUN.search(text):
        return False
    if run_new_nodes:
        return True
    return bool(_RUN_HINT.search(text))


def _emit(progress: ProgressFn | None, kind: str, text: str, tool: str = "") -> None:
    if not callable(progress):
        return
    try:
        progress(kind, text, tool)
    except TypeError:
        progress(kind, text)


def _node_by_id(graph: DesignerExecutionGraph, node_id: str) -> DesignerGraphNode | None:
    target = str(node_id or "").strip()
    if not target:
        return None
    for node in graph.get("nodes") or []:
        if str(node.get("id") or "") == target:
            return node
    return None


def _match_node(graph: DesignerExecutionGraph, message: str) -> DesignerGraphNode | None:
    text = str(message or "").strip().lower()
    if not text:
        return None
    ranked: list[tuple[int, DesignerGraphNode]] = []
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        label = str(node.get("label") or "")
        score = 0
        if node_id and node_id.lower() in text:
            score += 3
        if label and label.lower() in text:
            score += 2
        if score:
            ranked.append((score, node))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked else None


_AT_LABEL = re.compile(r"@([^\s@][^\n]*?)(?=(?:\s@|[,，。.!！?？;；]|\s{2}|$))")

_MAX_LABEL_REFERENCE_IMAGES = 3


def resolve_label_references(graph: DesignerExecutionGraph, message: str) -> list[str]:
    """Resolve "@Label" mentions in ``message`` to that node's output image.

    Longest-label-first so e.g. "@Character 1" isn't shadowed by a shorter
    "@Character" match. Only image-kind outputs are usable as a vision
    reference; a mention of a text/table/video/audio node, or a node with no
    output yet, is silently skipped rather than erroring the whole turn.
    """
    text = str(message or "")
    if "@" not in text:
        return []
    by_label: dict[str, DesignerGraphNode] = {}
    for node in graph.get("nodes") or []:
        label = str(node.get("label") or "").strip()
        if label:
            by_label[label] = node
    if not by_label:
        return []
    ordered_labels = sorted(by_label, key=len, reverse=True)
    sources: list[str] = []
    seen_ids: set[str] = set()
    for match in _AT_LABEL.finditer(text):
        candidate = match.group(1).strip()
        label = next((lbl for lbl in ordered_labels if candidate.startswith(lbl)), None)
        if not label:
            continue
        node = by_label[label]
        node_id = str(node.get("id") or label)
        if node_id in seen_ids:
            continue
        output_ref = node.get("output_ref")
        if not isinstance(output_ref, dict):
            continue
        if str(output_ref.get("kind") or "") != NODE_TYPE_IMAGE:
            continue
        uri = str(output_ref.get("uri") or "").strip()
        if not uri:
            continue
        seen_ids.add(node_id)
        sources.append(uri)
        if len(sources) >= _MAX_LABEL_REFERENCE_IMAGES:
            break
    return sources


def _next_label(graph: DesignerExecutionGraph, node_type: str) -> str:
    count = sum(1 for node in graph.get("nodes") or [] if str(node.get("type") or "") == node_type)
    title = node_type[:1].upper() + node_type[1:]
    return f"{title} {count + 1}"


def _next_node_id(graph: DesignerExecutionGraph, prefix: str) -> str:
    used = {str(node.get("id") or "") for node in graph.get("nodes") or []}
    if prefix not in used:
        return prefix
    index = 2
    while f"{prefix}_{index}" in used:
        index += 1
    return f"{prefix}_{index}"


def _place_right(graph: DesignerExecutionGraph) -> dict[str, float]:
    max_x = 40.0
    y = 240.0
    for node in graph.get("nodes") or []:
        layout = node.get("layout") or {}
        x = float(layout.get("x") or 0)
        if x >= max_x:
            max_x = x
            y = float(layout.get("y") or y)
    return {"x": max_x + 368, "y": y, "width": 280, "height": 160}


def _compose_or_sink_id(graph: DesignerExecutionGraph) -> str | None:
    ids = {str(node.get("id") or "") for node in graph.get("nodes") or []}
    for candidate in ("n_compose", "n_final"):
        if candidate in ids:
            return candidate
    for node in reversed(list(graph.get("nodes") or [])):
        if str(node.get("type") or "") == NODE_TYPE_VIDEO:
            return str(node.get("id") or "") or None
    return None


# Film pipeline order, used to work out the next stage from what is already built.
_STAGE_ORDER: tuple[str, ...] = (
    "brief",
    "storyboard",
    "character_design",
    "scene",
    "frame",
    "clip",
    "compose",
)
_STAGE_LABELS: dict[str, tuple[str, str]] = {
    "brief": ("创意大纲", "the creative brief"),
    "storyboard": ("分镜脚本", "the storyboard"),
    "character_design": ("角色设定图", "the character sheet"),
    "scene": ("场景设定图", "the scene set"),
    "frame": ("关键帧", "the keyframes"),
    "clip": ("镜头视频", "the shot videos"),
    "compose": ("成片合成", "the final compose"),
}


def looks_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _looks_chinese(text: str) -> bool:
    return looks_chinese(text)


def _node_built(node: dict[str, Any]) -> bool:
    """Whether this node already carries a real (non-placeholder) output."""
    ref = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
    uri = str((ref or {}).get("uri") or "").strip()
    return bool(uri) and not uri.startswith("designer://")


def _unbuilt_stages(graph: DesignerExecutionGraph) -> dict[str, list[str]]:
    """Pipeline stage -> ids of its nodes that have nothing built yet."""
    unbuilt: dict[str, list[str]] = {}
    for node in graph.get("nodes") or []:
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        pipeline = str((config or {}).get("pipeline") or "").strip()
        node_id = str(node.get("id") or "")
        if pipeline not in _STAGE_ORDER or not node_id or _node_built(node):
            continue
        unbuilt.setdefault(pipeline, []).append(node_id)
    return unbuilt


def _next_unbuilt_stage(graph: DesignerExecutionGraph) -> str | None:
    unbuilt = _unbuilt_stages(graph)
    if not unbuilt:
        return None
    return min(unbuilt, key=_STAGE_ORDER.index)


def next_stage_nodes(graph: DesignerExecutionGraph) -> list[str]:
    """Ids of the nodes in the earliest pipeline stage that has nothing built."""
    stage = _next_unbuilt_stage(graph)
    if stage is None:
        return []
    return _unbuilt_stages(graph).get(stage, [])


def next_stage_hint(graph: DesignerExecutionGraph, *, chinese: bool) -> str:
    """Name the earliest pipeline stage that still has nothing built."""
    stage = _next_unbuilt_stage(graph)
    if stage is not None:
        zh, en = _STAGE_LABELS[stage]
        return f"下一步是{zh}。" if chinese else f"The next step is {en}."
    staged = [
        node
        for node in graph.get("nodes") or []
        if str(((node.get("config") or {}).get("pipeline") or "")).strip() in _STAGE_ORDER
    ]
    if staged and all(_node_built(node) for node in staged):
        return "全部阶段均已完成，影片已生成。" if chinese else "Every stage is built; the film is complete."
    return ""


_NEXT_STEP_MARKERS = ("下一步", "下个步骤", "Next step", "next step")


def replace_next_step(summary: str, graph: DesignerExecutionGraph, *, chinese: bool) -> str:
    """Rewrite the closing next-step line from a *finished* graph.

    The leader writes its summary before the run executes, so on a turn that
    builds the last shots it still declared 下一步是镜头视频 — the clips were
    unbuilt when it wrote that line and built by the time the user read it. This
    is called after the run has landed, so it reports the stage that is genuinely
    next.
    """
    head = summary or ""
    for marker in _NEXT_STEP_MARKERS:
        index = head.find(marker)
        if index != -1:
            head = head[:index]
    head = head.rstrip(" \t　。．.,，;；:：、-—")
    if head and head[-1] not in "。．.!！?？":
        head = f"{head}{'。' if chinese else '.'}"
    hint = next_stage_hint(graph, chinese=chinese)
    if not hint:
        return head or (summary or "")
    return f"{head} {hint}".strip() if head else hint


def _node_with_output(node: dict[str, Any]) -> bool:
    """Whether the node itself carries a real (non-placeholder) output."""
    ref = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
    return bool(str((ref or {}).get("uri") or "").strip())


def _unbuilt_reason(
    state: dict[str, Any],
    *,
    run_finished: bool,
    chinese: bool,
) -> str:
    error = str(state.get("error") or "").strip()
    if error:
        return error
    status = str(state.get("status") or "").strip().lower()
    if status == "cancelled":
        return "已取消" if chinese else "cancelled"
    if status == "failed":
        return "生成失败" if chinese else "generation failed"
    if status == "running" or not run_finished:
        return "仍在生成中" if chinese else "still generating"
    return "未完成" if chinese else "did not finish"


def _split_already_built(
    node_ids: list[str], graph: DesignerExecutionGraph
) -> tuple[list[str], list[str]]:
    """(still to build, already has an output) for the named nodes."""
    pending: list[str] = []
    built: list[str] = []
    for node_id in node_ids:
        node = _node_by_id(graph, node_id)
        if isinstance(node, dict) and _node_with_output(node):
            built.append(node_id)
        else:
            pending.append(node_id)
    return pending, built


def _stage_run_summary(
    node_ids: list[str], graph: DesignerExecutionGraph, *, chinese: bool
) -> str:
    """Name the stage a graph-resolved "next step" will actually run."""
    labels: list[str] = []
    for node_id in node_ids:
        node = _node_by_id(graph, node_id) or {}
        pipeline = node_pipeline(node)
        label = _STAGE_LABELS.get(pipeline, (pipeline or node_id, pipeline or node_id))[
            0 if chinese else 1
        ]
        if label not in labels:
            labels.append(label)
    if not labels:
        return ""
    if chinese:
        return f"开始生成{'、'.join(labels)}。"
    return f"Building {', '.join(labels)}."


def _already_built_note(
    node_ids: list[str], graph: DesignerExecutionGraph, *, chinese: bool
) -> str:
    """Say the stage is already there rather than promising to rebuild it."""
    labels: list[str] = []
    for node_id in node_ids:
        node = _node_by_id(graph, node_id) or {}
        pipeline = str((node.get("config") or {}).get("pipeline") or "").strip()
        label = _STAGE_LABELS.get(pipeline, (pipeline or node_id, pipeline or node_id))[
            0 if chinese else 1
        ]
        if label not in labels:
            labels.append(label)
    if chinese:
        return f"{'、'.join(labels)}已生成，无需重复生成。"
    return f"{', '.join(labels)} is already generated; nothing to rebuild."


def report_unbuilt_nodes(
    summary: str,
    graph: DesignerExecutionGraph,
    node_ids: list[str] | tuple[str, ...],
    *,
    node_states: dict[str, Any] | None = None,
    run_finished: bool = True,
    chinese: bool,
    reason: str = "",
) -> str:
    """Replace a summary that announces nodes the run never actually produced.

    The leader writes its summary before the run executes, so it describes the
    plan as though it had already succeeded. ``replace_next_step`` fixes only the
    closing line, so the body kept announcing e.g. a character sheet while the
    run left that node pending — the user then hunted for an asset that did not
    exist. Whenever a named node has no output, its prose cannot be trusted, so
    the claim is replaced with what the run state actually says.

    ``reason`` overrides the per-node state wording, for when the run never
    started at all and the caller has the real error to give.
    """
    states = {str(key): value for key, value in (node_states or {}).items() if isinstance(value, dict)}
    parts: list[str] = []
    seen: set[str] = set()
    reported: set[str] = set()
    for raw_id in node_ids:
        node_id = str(raw_id or "").strip()
        if not node_id or node_id in reported:
            continue
        reported.add(node_id)
        node = _node_by_id(graph, node_id)
        if not isinstance(node, dict):
            continue
        state = states.get(node_id) or {}
        if _node_with_output(node) and str(state.get("status") or "") != "failed":
            continue
        pipeline = str((node.get("config") or {}).get("pipeline") or "").strip()
        label = _STAGE_LABELS.get(pipeline, (pipeline or node_id, pipeline or node_id))[
            0 if chinese else 1
        ]
        said = reason or _unbuilt_reason(state, run_finished=run_finished, chinese=chinese)
        # Three clips share one stage label, so report the stage once rather
        # than repeating "镜头视频（…）" per node.
        entry = f"{label}（{said}）"
        if entry not in seen:
            seen.add(entry)
            parts.append(entry)
    if not parts:
        return summary
    if chinese:
        return f"未生成：{'、'.join(parts)}。"
    return f"Not generated: {', '.join(parts)}."


def with_next_step(
    summary: str,
    graph: DesignerExecutionGraph,
    *,
    instruction: str,
    intent: str,
) -> str:
    """Guarantee the reply ends by naming the next step.

    The system prompt asks for it, but a terse summary regularly drops it and
    leaves the user with a report and no idea what to do next. The graph already
    knows the answer, so append it deterministically when the model omitted it.
    """
    if intent == "answer":
        return summary
    if "下一步" in summary or "next step" in summary.lower():
        return summary
    hint = next_stage_hint(graph, chinese=_looks_chinese(instruction))
    if not hint:
        return summary
    return f"{summary} {hint}".strip() if summary else hint



def _merge_prompt_updates(graph: DesignerExecutionGraph, plan: dict[str, Any]) -> dict[str, Any]:
    patch = dict(plan.get("patch") or {})
    updates = plan.get("prompt_updates") or []
    if not isinstance(updates, list) or not updates:
        return patch
    upsert = list(patch.get("upsert_nodes") or [])
    by_id = {str(item.get("id") or ""): dict(item) for item in upsert if isinstance(item, dict)}
    for item in updates:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not node_id or not prompt:
            continue
        node = by_id.get(node_id) or (_node_by_id(graph, node_id) and dict(_node_by_id(graph, node_id) or {}))
        if not node:
            continue
        cfg = dict(node.get("config") or {})
        cfg["prompt"] = prompt
        node["config"] = cfg
        by_id[node_id] = node
    if by_id:
        patch["upsert_nodes"] = list(by_id.values())
    return patch


_IDENTITY_OVERRIDE_MARKER = "USER-REQUESTED IDENTITY CHANGE (authoritative, not a stale field):"


def _apply_identity_updates(
    graph: DesignerExecutionGraph,
    next_graph: DesignerExecutionGraph,
    plan: dict[str, Any],
) -> DesignerExecutionGraph:
    """A character node's real look lives in config.costume_lock + the cast list in
    graph.metadata.script_analysis, not config.prompt — leaf agents read those, so an
    appearance change has to land there too or regeneration keeps the old look."""
    updates = plan.get("identity_updates") or []
    if not isinstance(updates, list) or not updates:
        return next_graph
    nodes_by_id = {str(n.get("id") or ""): dict(n) for n in next_graph.get("nodes") or []}
    meta = dict(next_graph.get("metadata") or {})
    script_analysis = dict(meta.get("script_analysis") or {})
    characters = [dict(c) for c in (script_analysis.get("characters") or []) if isinstance(c, dict)]
    chars_by_id = {str(c.get("id") or ""): c for c in characters if c.get("id")}
    nodes_changed = False
    meta_changed = False
    for item in updates:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        description = str(item.get("description") or "").strip()
        node = nodes_by_id.get(node_id)
        if not node or not description:
            continue
        cfg = dict(node.get("config") or {})
        name = str(cfg.get("character_name") or item.get("character_id") or "").strip()
        costume_lock = f"{name}: {description}" if name else description
        cfg["costume_lock"] = costume_lock
        director_task = str(cfg.get("director_task") or "").strip()
        if director_task and _IDENTITY_OVERRIDE_MARKER in director_task:
            director_task = director_task.split(_IDENTITY_OVERRIDE_MARKER, 1)[0].rstrip()
        if director_task:
            cfg["director_task"] = (
                f"{director_task}\n\n{_IDENTITY_OVERRIDE_MARKER} {costume_lock}\n"
                "This is the user's deliberate, just-given instruction for THIS character, "
                "given through chat moments ago. It outranks anything you read via read_upstream "
                "(brief, storyboard, PRODUCTION LOCK BIBLE, other clips' costume_lock) or the "
                "character's own name/label that still says otherwise — those have not been "
                "regenerated yet and describe the OLD appearance. Use the appearance stated here, "
                "not the old one, even though other sources you read still disagree with it."
            )
        node["config"] = cfg
        nodes_by_id[node_id] = node
        nodes_changed = True
        # Prefer the node's own config.character_id (ground truth) over whatever id the
        # LLM guessed in identity_updates — the LLM sometimes fabricates a plausible-looking
        # id ("character_1") that doesn't match the story's real id ("char_1").
        char_id = str(cfg.get("character_id") or item.get("character_id") or "").strip()
        character = chars_by_id.get(char_id)
        if character is not None:
            character["description"] = description
            character["costume_lock"] = costume_lock
            attrs = character.get("identity_attrs")
            if isinstance(attrs, dict) and "wardrobe" in attrs:
                attrs = dict(attrs)
                attrs["wardrobe"] = costume_lock
                character["identity_attrs"] = attrs
            meta_changed = True
        if char_id:
            # Every other node featuring this same character (other scenes/clips) carries
            # its own copy of costume_lock too — leave those stale and a regen there (or
            # even this one, via read_upstream) can see a conflict and side with the old
            # majority text instead of the just-requested change.
            for other_id, other in nodes_by_id.items():
                if other_id == node_id:
                    continue
                other_cfg = other.get("config")
                if not isinstance(other_cfg, dict):
                    continue
                other_char_id = str(other_cfg.get("character_id") or "").strip()
                other_char_ids = [str(x) for x in (other_cfg.get("character_ids") or [])]
                if char_id != other_char_id and char_id not in other_char_ids:
                    continue
                other_cfg = dict(other_cfg)
                other_cfg["costume_lock"] = costume_lock
                other["config"] = other_cfg
                nodes_by_id[other_id] = other
    if not nodes_changed and not meta_changed:
        return next_graph
    patched = dict(next_graph)
    if nodes_changed:
        patched["nodes"] = list(nodes_by_id.values())
    if meta_changed:
        script_analysis["characters"] = characters
        if str(script_analysis.get("production_bible") or "").strip():
            from jiuwenswarm.server.runtime.designer.pipeline.production_bible import (
                build_production_bible,
            )

            script_analysis["production_bible"] = build_production_bible(
                script_analysis, user_prompt=str(script_analysis.get("summary") or "")
            )
        meta["script_analysis"] = script_analysis
        patched["metadata"] = meta
    return patched


def apply_leader_plan(
    graph: DesignerExecutionGraph,
    plan: dict[str, Any],
    *,
    include_new_nodes: bool = False,
    include_next_stage: bool = False,
) -> tuple[DesignerExecutionGraph, list[str], str]:
    intent = str(plan.get("intent") or "answer").strip() or "answer"
    summary = str(plan.get("summary") or "").strip()
    patch = _merge_prompt_updates(graph, plan)
    has_patch = any(patch.get(key) for key in ("upsert_nodes", "upsert_edges", "remove_node_ids", "remove_edge_ids"))
    next_graph = apply_graph_patch(graph, patch) if has_patch else graph
    next_graph = _apply_identity_updates(graph, next_graph, plan)
    raw_run_ids = plan.get("run_node_ids") or []
    run_ids = [str(item).strip() for item in raw_run_ids if str(item).strip()]
    if intent != "refine_node":
        # Topology edits only run when the plan explicitly listed ids.
        run_ids = run_ids
    known = {str(node.get("id") or "") for node in next_graph.get("nodes") or []}
    run_ids = [item for item in run_ids if item in known]
    if include_new_nodes:
        # "generate the keyframe for shot 1" both creates the node and should
        # build it. run_node_ids usually names only nodes that already existed,
        # so a brand-new keyframe/clip landed as an empty placeholder and the
        # user had to ask a second time to get the actual asset. Since the turn
        # explicitly asked to generate, run the nodes this patch just added.
        before_ids = {str(node.get("id") or "") for node in graph.get("nodes") or []}
        for node in next_graph.get("nodes") or []:
            node_id = str(node.get("id") or "")
            if node_id and node_id not in before_ids and node_id not in run_ids:
                run_ids.append(node_id)
    if include_next_stage:
        # "完成下一步" asks for the next *unbuilt* stage, so resolve it from the
        # graph and use exactly those nodes. Extending instead let the model's
        # over-broad list through — asking for the next step re-generated all
        # three shots when only the third was still missing.
        staged_next = next_stage_nodes(next_graph)
        if staged_next:
            run_ids = [node_id for node_id in staged_next if node_id in known]
    if include_new_nodes or include_next_stage:
        # Run in graph order so upstream nodes build before their consumers.
        order = {
            str(node.get("id") or ""): index
            for index, node in enumerate(next_graph.get("nodes") or [])
        }
        run_ids.sort(key=lambda nid: order.get(nid, 1 << 30))
    if not summary:
        if intent == "refine_node":
            summary = "Updated the selected node."
        elif has_patch:
            summary = "Updated the workflow graph."
        else:
            summary = "No graph changes."
    return next_graph, run_ids, summary


def _sanitize_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {"intent": "answer", "summary": "Could not understand that request.", "patch": {}, "run_node_ids": []}
    intent = str(plan.get("intent") or "answer").strip()
    if intent not in {"edit_graph", "refine_node", "answer"}:
        intent = "answer"
    patch = plan.get("patch") if isinstance(plan.get("patch"), dict) else {}
    run_ids = plan.get("run_node_ids") if isinstance(plan.get("run_node_ids"), list) else []
    prompt_updates = plan.get("prompt_updates") if isinstance(plan.get("prompt_updates"), list) else []
    identity_updates = plan.get("identity_updates") if isinstance(plan.get("identity_updates"), list) else []
    return {
        "intent": intent,
        "summary": str(plan.get("summary") or "").strip(),
        "thinking": str(plan.get("thinking") or "").strip(),
        "patch": patch,
        "prompt_updates": prompt_updates,
        "identity_updates": identity_updates,
        "run_node_ids": [str(item).strip() for item in run_ids if str(item).strip()],
    }


def _node_prompt_for_snapshot(node: dict[str, Any]) -> str:
    """The text this node would actually generate from.

    A clip node keeps its film prompt under ``config.generate.prompt`` (written by
    the storyboard sync) and ``config.prompt`` is usually empty, so reading only
    ``config.prompt`` made the leader tell the user "Shot 2 and Shot 3 have no
    prompt" about clips that had one.
    """
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    for value in (cfg.get("prompt"), generate.get("prompt"), cfg.get("shot_action")):
        text = str(value or "").strip()
        if text:
            return text[:240]
    return ""


def _snapshot_nodes(graph: DesignerExecutionGraph) -> list[dict[str, Any]]:
    """Canvas nodes as the leader sees them.

    ``has_output`` is the part that matters most: without it the model can only
    guess whether an asset exists, and it reported three finished clips as
    "还没有生成" while the user was looking at them on the canvas.
    """
    return [
        {
            "id": node.get("id"),
            "type": node.get("type"),
            "label": node.get("label"),
            "pipeline": node_pipeline(node),
            "prompt": _node_prompt_for_snapshot(node),
            "has_output": _node_built(node),
        }
        for node in graph.get("nodes") or []
    ]


async def _llm_leader_plan(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    selected_node_id: str = "",
    history: list[dict[str, str]] | None = None,
    images: list[str] | None = None,
) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_API_ERROR,
        call_model_tool,
        model_text_or_raise,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    snapshot: dict[str, Any] = {
        "selected_node_id": selected_node_id,
        "user_canvas_edits": list(meta.get("user_canvas_edits") or [])[-20:],
        "nodes": _snapshot_nodes(graph),
        "edges": [
            {"id": edge.get("id"), "source": edge.get("source"), "target": edge.get("target")}
            for edge in graph.get("edges") or []
        ],
        "user": message,
    }
    if history:
        # Last few turns only — this is context for a short follow-up, not a
        # transcript; keeps the snapshot small and avoids re-litigating old asks.
        snapshot["recent_conversation"] = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")[:600]}
            for item in history[-8:]
            if str(item.get("content") or "").strip()
        ]
    try:
        result = await call_model_tool(
            prompt=json.dumps(snapshot, ensure_ascii=False),
            system=_LEADER_SYSTEM,
            optimize_for="quality",
            max_tokens=16384,
            images=images or None,
        )
        text = model_text_or_raise(result)
    except DesignerLlmError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info("Leader chat model call failed", exc_info=True)
        raise DesignerLlmError(
            f"Chat model request failed while planning canvas edits: {exc}",
            code=LLM_API_ERROR,
        ) from exc
    parsed = _extract_json_object(text)
    plan = _sanitize_plan(parsed)
    if plan.get("intent") == "answer" and not str(plan.get("summary") or "").strip():
        raise DesignerLlmError(
            "Chat model did not return a usable canvas edit plan.",
            code=LLM_API_ERROR,
        )
    return plan


async def run_leader_chat(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    selected_node_id: str = "",
    run_new_nodes: bool = False,
    progress: ProgressFn | None = None,
    history: list[dict[str, str]] | None = None,
    attached_images: list[str] | None = None,
) -> dict[str, Any]:
    text = str(message or "").strip()
    _emit(progress, ACTIVITY_KIND_THINKING, "reading the canvas and your request")
    # "@Label" mentions (an existing node's own label) resolve to that node's
    # output image so the model sees it, same spirit as a file the user
    # attached directly — both just become vision references for this turn.
    label_images = resolve_label_references(graph, text)
    images = [*label_images, *(attached_images or [])][:_MAX_LABEL_REFERENCE_IMAGES]
    plan = await _llm_leader_plan(
        graph, text, selected_node_id=selected_node_id, history=history, images=images or None
    )
    thinking = str(plan.get("thinking") or "applying graph edits")
    _emit(progress, ACTIVITY_KIND_THINKING, thinking)
    # An edit_graph plan may only execute nodes when the message asked to run;
    # "继续合成" resolves through _RUN_HINT, so a compose request keeps its
    # run_node_ids instead of being silently emptied into a no-op.
    # Snapshot the ids first: the log below must show what the plan asked for,
    # otherwise a wipe is indistinguishable from a plan that scheduled nothing.
    plan_run_ids = [str(item) for item in (plan.get("run_node_ids") or [])]
    if plan.get("intent") == "edit_graph" and not message_asks_to_run(
        text, run_new_nodes=run_new_nodes
    ):
        plan["run_node_ids"] = []
    if plan.get("intent") == "refine_node" and not plan.get("run_node_ids") and selected_node_id:
        plan["run_node_ids"] = [selected_node_id]
    # A plan that scheduled nothing — or aimed at the clips — while the user asked to
    # change the storyboard left them with a reply describing an edit the canvas never
    # received. Shot durations live in the storyboard, so the storyboard is the target,
    # and it replaces the plan's ids: rebuilding the clips instead fails on
    # "upstream not ready" and never touches the timings. The intent is forced to
    # refine_node so the edit_graph guard cannot empty it again.
    if _storyboard_edit_requested(text):
        storyboard_ids = _storyboard_node_ids(graph)
        if storyboard_ids:
            plan["run_node_ids"] = storyboard_ids
            plan["intent"] = "refine_node"
            # The storyboard is authored from the brief plus its own prompt, so the
            # requirement has to land in that prompt. Scheduling the node without it
            # rebuilt the storyboard from the unchanged brief and reproduced the old
            # timings while the reply claimed they had been adjusted.
            if not plan.get("prompt_updates"):
                updates: list[dict[str, str]] = []
                for node_id in storyboard_ids:
                    node = _node_by_id(graph, node_id) or {}
                    existing = str((node.get("config") or {}).get("prompt") or "").strip()
                    merged = text if not existing or text in existing else f"{existing}\n{text}"
                    updates.append({"node_id": node_id, "prompt": merged})
                if updates:
                    plan["prompt_updates"] = updates

    _emit(progress, ACTIVITY_KIND_TOOL_CALL, "designer_graph_patch", tool="designer_graph_patch")
    asked_to_run = message_asks_to_run(text, run_new_nodes=run_new_nodes)
    next_graph, run_ids, summary = apply_leader_plan(
        graph,
        plan,
        # A turn that asked to generate must also build the nodes it just added.
        include_new_nodes=asked_to_run,
        # "完成下一步" must actually run the next stage, not just describe it.
        include_next_stage=bool(_NEXT_STEP_HINT.search(text)),
    )
    # "Generate the shot videos" builds what is missing. A node that already has
    # an output is not a leftover: rebuilding it costs minutes and leaves a
    # second version nobody asked for. Redoing a specific node deliberately is
    # the refine / selected-node path, which is left alone.
    if asked_to_run and str(plan.get("intent") or "") != "refine_node":
        run_ids, already_built = _split_already_built(run_ids, next_graph)
        if not run_ids and already_built:
            summary = _already_built_note(already_built, next_graph, chinese=looks_chinese(text))
    if _NEXT_STEP_HINT.search(text) and run_ids and set(run_ids) != set(plan_run_ids):
        # "下一步" is resolved from the graph, not from the model's plan, so it can target a
        # different stage than the prose named: the reply promised the character sheet and
        # the scene while the run built the storyboard, and the user waited for images that
        # were never scheduled. Say the stage that will actually run.
        summary = _stage_run_summary(run_ids, next_graph, chinese=looks_chinese(text)) or summary
    summary = with_next_step(
        summary,
        next_graph,
        instruction=text,
        intent=str(plan.get("intent") or ""),
    )
    # A turn can end with nothing to run while its prose promises generation
    # ("开始生成三段镜头视频" with run_node_ids emptied out). Record the plan's
    # ids next to the resolved ones: without this the only symptom is a reply
    # that claims work it never scheduled, which is invisible in the logs.
    logger.info(
        "[Designer] leader chat intent=%s asked_to_run=%s plan_run_ids=%s resolved_run_ids=%s",
        plan.get("intent"),
        message_asks_to_run(text, run_new_nodes=run_new_nodes),
        plan_run_ids,
        run_ids,
    )
    changed = next_graph is not graph and next_graph.get("updated_at") != graph.get("updated_at")
    if not changed:
        # apply_graph_patch always writes updated_at; compare node/edge identity.
        changed = (next_graph.get("nodes") != graph.get("nodes")) or (
            next_graph.get("edges") != graph.get("edges")
        )
    result = {
        "intent": plan.get("intent"),
        "summary": summary,
        "graph": next_graph,
        "run_node_ids": run_ids,
        "changed": changed or bool(plan.get("prompt_updates")) or bool(plan.get("identity_updates")),
        "updated_at": utc_now_ms(),
    }
    _emit(progress, ACTIVITY_KIND_STAGE, summary or "done", tool="")
    return result
