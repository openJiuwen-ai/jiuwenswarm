# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Invisible Designer leader: chat-driven graph edits and output refine."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any, Callable

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    PIPELINES,
    PIPELINE_TO_TYPE,
    DesignerExecutionGraph,
    DesignerGraphNode,
    DesignerGraphValidationError,
    apply_graph_patch,
    infer_pipeline_from_id,
    node_pipeline,
    node_shot_index,
    utc_now_ms,
)

from jiuwenswarm.server.runtime.designer.chat_document_sync import (
    ChatDocument,
    ChatDocumentConflict,
    graph_content_changed,
    prepare_document_update,
    validate_shot_topology,
)

from jiuwenswarm.server.runtime.designer.chat_document_plan import plan_document_edits

logger = logging.getLogger(__name__)

ProgressFn = Callable[..., None]

_RUN_HINT = re.compile(
    r"(生成|重跑|重生成|运行|run\b|generate|rerun|regenerate)",
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
_VIDEO_HINT = re.compile(r"(视频|镜头|clip|video)", re.I)

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

Schema:
{
  "intent": "edit_graph" | "refine_node" | "answer",
  "summary": "short user-facing Chinese or English summary",
  "thinking": "brief rationale: affected IDs, removed reference targets, and concrete details that must stay unchanged",
  "patch": {
    "description": "updated creative request, only when changed",
    "upsert_nodes": [{"id": "affected_node_id", "config": {"prompt": "complete updated generation prompt"}}],
    "upsert_edges": [],
    "remove_node_ids": [],
    "remove_edge_ids": []
  },
  "remove_shot_ids": [],
  "prompt_updates": [],
  "edit_documents": false,
  "run_node_ids": []
}

Rules:
- edit_graph: change topology. Leave run_node_ids empty unless the user asked to generate/run.
- refine_node: edit the existing node. Leave run_node_ids empty unless the user explicitly asks to generate/run.
- answer: return intent, summary and thinking only. Omit patch and prompt_updates entirely; edit_documents=false. A run-only request may list run_node_ids.
- Return ONE upsert_nodes entry per affected node, containing all its config changes. Leave prompt_updates empty. For existing nodes, config fields merge by ID. Include all required generation fields specified below even when some values stay unchanged; other unchanged fields may be omitted.
- Input and output use the SAME config paths. The effective generation prompt is config.prompt;
  update it there. The server also updates the execution prompt from that value. pipeline and
  review_fields_if_action_changes describe the input; do not copy them into the output node.
- Preserve existing node IDs and unrelated content. Renumber shot_index consecutively after inserting/deleting a shot, including its frame/clip; update each affected timeline, action, camera and prompt.
- Set edit_documents=true for content edits, including requests that only change prose. A separate document editor will receive the original documents and the applied graph changes. Do not return text replacements in this plan.
- Resolve the requested object before editing. Singular referents and background collections are distinct: e.g. changing one bicycle does not change other parked bicycles of the same color. Do not globally substitute color/material words in a sentence or record. In thinking, identify specific similar objects/details you will preserve.
- For deleting an ENTIRE SHOT, put an existing clip/frame ID in remove_shot_ids. Use the
  supplied shots inventory to resolve the target. The server removes ALL frame/clip nodes
  belonging to that original shot and their edges. Do not rely on remove_node_ids or your
  summary to express a whole-shot deletion. Use patch.remove_node_ids for explicit individual
  node deletions and requested exclusive scene cleanup; never delete shared dependencies.
  Leave remove_shot_ids empty when deleting only a scene/image/video node, keeping its shot.
- When deleting a shot, REMOVE each continuity claim that refers to it. Do not replace its number with the previous or next surviving number. A surviving shot does not inherit the deleted shot's events or props. Renumber references only if their original target survives.
- Keep description and brief/storyboard prompts consistent with the edited request. Do not create/remove brief/storyboard nodes unless explicitly requested; never edit output references or artifact paths.
- Update generation prompts of affected downstream nodes when changing a character, scene or explicit dependency.
- Generation uses the detailed config as well as prompt. Keep camera, cast_actions, blocking, scene_specs, start_state, end_state, pose_holds, spatial_lock, costume_lock, continuity_lock, relationship_lock and director_task consistent with the edit, preserving unrelated details. Nested records replace the entire field; include unchanged members.
- Camera descriptions may name the edited subject or prop. Update those details in config.camera while preserving framing, angle, focus target and camera motion unless the user asks to change them.
- Whenever shot_action changes, explicitly include every field in that node's review_fields_if_action_changes in upsert_nodes.config. Return each complete field with updated action/prop details, preserving unrelated members. These fields feed generation directly; do not leave hidden old action/prop details. Whenever a scene prompt or scene_specs changes, explicitly include both its complete scene_specs and updated config.prompt. For clips too, return config.prompt when it is listed.
- Costume/identity settings merge by field like other config. Update costume_lock when the request changes clothing or accessories; otherwise omit it to keep the original value. An action or prop edit alone does not require restating unchanged clothing.
- positioning_lock, action_lock, occupancy.cast_actions, identity_refs mirrors and scene architecture prompts are derived by the server. Do not author these copies. Only return pose_holds for custom holds; Opening hold / Opening facing entries are rebuilt from start_state.
- Before finishing each node entry, check its review_fields_if_action_changes list against the config keys you returned. If you changed shot_action, every listed key MUST be present with its complete value, including spatial_lock. Review nested landmarks, poses and cameras for the requested object as well as the main action.
- Pure layout/label edits use edit_documents=false. Never generate media just to edit text.
"""


_ACTION_DEPENDENT_FIELDS = (
    "camera", "cast_actions", "blocking", "start_state", "end_state", "scene_specs",
    "spatial_lock", "continuity_lock", "director_task",
)

_LEADER_CONFIG_FIELDS = {
    "prompt", "shot_index", "shot_action", "camera", "timeline", "shot_title",
    "character_id", "character_ids", "setting_id", "on_screen", "offscreen",
    "cast_actions", "scene_specs", "speech_line", "continuity_lock",
    "blocking", "start_state", "end_state", "pose_holds", "spatial_lock",
    "costume_lock", "relationship_lock", "director_task",
    "inputs", "character_node_ids", "scene_node_id", "identity_refs",
    "continuity_clip_node_id", "previous_clip_node_id", "continuity_frame_node_id",
}


_DONT_RUN = re.compile(
    r"(先别|(?:不要|别|不用|无需|暂不|不需要|先不)\s*(?:重新)?(?:生成|运行|重跑|跑)|without (?:running|generating)|don'?t (?:run|generate)|do not (?:run|generate))",
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



def _action_review_fields(config: dict[str, Any]) -> list[str]:
    fields = [key for key in _ACTION_DEPENDENT_FIELDS if config.get(key)]
    if config.get("prompt") or (config.get("generate") or {}).get("prompt"):
        fields.append("prompt")
    return fields


def _leader_node_context(node: DesignerGraphNode) -> dict[str, Any]:
    """Expose the execution prompt at the same config path accepted by chat edits."""
    config = node.get("config") or {}
    editable_config = {key: value for key, value in config.items() if key in _LEADER_CONFIG_FIELDS}
    prompt = (config.get("generate") or {}).get("prompt") or config.get("prompt")
    if prompt:
        editable_config["prompt"] = prompt
    return {
        "id": node["id"],
        "type": node["type"],
        "label": node.get("label"),
        "pipeline": node_pipeline(node),
        "review_fields_if_action_changes": _action_review_fields(config),
        "config": editable_config,
    }


def _stamp_missing_node_type(node: dict[str, Any]) -> None:
    """Fill canvas type for a new chat node the model described only by id.

    Existing nodes already carry type from the saved graph. Inserted shots such
    as n_scene_4 / n_clip_4 often omit it, and normalize_node rejects that
    before the rest of the edit is saved.
    """
    if str(node.get("type") or "").strip():
        return
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    pipeline = str(config.get("pipeline") or "").strip()
    if pipeline not in PIPELINES:
        role = str(config.get("role") or "").strip()
        pipeline = role if role in PIPELINES else infer_pipeline_from_id(str(node.get("id") or ""))
    node_type = PIPELINE_TO_TYPE.get(pipeline, "")
    if not node_type:
        node_id = str(node.get("id") or "").strip() or "new node"
        raise DesignerGraphValidationError(
            f"node.type must be a non-empty string ({node_id})"
        )
    node["type"] = node_type
    if not str(config.get("pipeline") or "").strip():
        config["pipeline"] = pipeline
    node["config"] = config


def _merge_prompt_updates(graph: DesignerExecutionGraph, plan: dict[str, Any]) -> dict[str, Any]:
    patch = deepcopy(plan.get("patch") or {})
    existing = {node["id"]: node for node in graph.get("nodes", [])}
    upserts = list(patch.get("upsert_nodes") or [])
    for update in plan.get("prompt_updates") or []:
        node_id = update.get("node_id")
        if node_id not in existing and not any(n.get("id") == node_id for n in upserts):
            raise DesignerGraphValidationError(f"Unknown prompt update node: {node_id}")
        config = {key: update[key] for key in ("prompt", "shot_index", "shot_action", "camera", "timeline") if key in update}
        upserts.append({"id": node_id, "config": config})
    merged: dict[str, Any] = {}
    supplied: dict[str, set[str]] = {}
    for update in upserts:
        node_id = update["id"]
        supplied.setdefault(node_id, set()).update(update.get("config") or {})
        old = merged.get(node_id) or existing.get(node_id) or {}
        node = {**deepcopy(old), **update}
        config = {**deepcopy(old.get("config") or {}), **(update.get("config") or {})}
        if "generate" in (update.get("config") or {}):
            config["generate"] = {**(old.get("config", {}).get("generate") or {}), **config["generate"]}
        if "prompt" in (update.get("config") or {}):
            config["generate"] = {**(config.get("generate") or {}), "prompt": config["prompt"], "prompt_origin": "user"}
        if node.get("output_ref") != old.get("output_ref"):
            raise DesignerGraphValidationError("Chat plans cannot change artifact references")
        node["config"] = config
        _stamp_missing_node_type(node)
        merged[node_id] = node
    for node_id, node in merged.items():
        old_cfg = existing.get(node_id, {}).get("config") or {}
        cfg = node["config"]
        required = set()
        if cfg.get("shot_action") != old_cfg.get("shot_action"):
            required.update(_action_review_fields(old_cfg))
        if node_pipeline(node) == "scene" and any(
            cfg.get(key) != old_cfg.get(key) for key in ("prompt", "scene_specs")
        ):
            required.update(key for key in ("scene_specs", "prompt") if key in _action_review_fields(old_cfg))
        if missing := required - supplied[node_id]:
            raise DesignerGraphValidationError(f"Updated node {node_id} must include its generation details: {', '.join(sorted(missing))}")
    if merged:
        patch["upsert_nodes"] = list(merged.values())
    return patch


def _shot_removal_patch(graph: DesignerExecutionGraph, patch: dict[str, Any], shot_ids: list[str]) -> dict[str, Any]:
    """Expand explicit whole-shot targets using the original stable identities."""
    shots = {node["id"]: node_shot_index(node) for node in graph["nodes"]
             if node_pipeline(node) in {"frame", "clip"}}
    unknown = set(shot_ids) - shots.keys()
    if unknown:
        raise DesignerGraphValidationError(f"Shot deletion requires current clip/frame IDs: {sorted(unknown)}")
    indices = {shots[node_id] for node_id in shot_ids}
    removed = {node_id for node_id, index in shots.items() if index in indices}
    return {**patch, "remove_node_ids": sorted(set(patch.get("remove_node_ids", [])) | removed)}


def apply_leader_plan(
    graph: DesignerExecutionGraph,
    plan: dict[str, Any],
) -> tuple[DesignerExecutionGraph, list[str], str]:
    intent = str(plan.get("intent") or "answer").strip() or "answer"
    summary = str(plan.get("summary") or "").strip()
    patch = _merge_prompt_updates(graph, plan)
    if plan.get("remove_shot_ids"):
        patch = _shot_removal_patch(graph, patch, plan["remove_shot_ids"])
    has_patch = bool(patch)
    next_graph = apply_graph_patch(graph, patch) if has_patch else graph
    raw_run_ids = plan.get("run_node_ids") or []
    run_ids = [str(item).strip() for item in raw_run_ids if str(item).strip()]
    known = {str(node.get("id") or "") for node in next_graph.get("nodes") or []}
    run_ids = [item for item in run_ids if item in known]
    if not summary:
        if intent == "refine_node":
            summary = "Updated the selected node."
        elif has_patch:
            summary = "Updated the workflow graph."
        else:
            summary = "No graph changes."
    return next_graph, run_ids, summary


def _sanitize_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("intent") not in {"edit_graph", "refine_node", "answer"}:
        raise DesignerGraphValidationError("Leader returned an invalid editing plan; please retry")
    for key, kind in (("patch", dict), ("prompt_updates", list), ("run_node_ids", list), ("edit_documents", bool)):
        if key in plan and not isinstance(plan[key], kind):
            raise DesignerGraphValidationError(f"Leader returned invalid {key}")
    shot_ids = plan.get("remove_shot_ids", [])
    if not isinstance(shot_ids, list) or any(not isinstance(item, str) or not item.strip() for item in shot_ids):
        raise DesignerGraphValidationError("remove_shot_ids must be an array of current clip/frame IDs")
    intent = str(plan.get("intent") or "answer").strip()
    if intent not in {"edit_graph", "refine_node", "answer"}:
        intent = "answer"
    patch = {
        key: value for key, value in plan.get("patch", {}).items()
        if key not in {"upsert_nodes", "upsert_edges", "remove_node_ids", "remove_edge_ids"} or value != []
    }
    # Some models fill optional schema slots with empty values even for an answer.
    if intent == "answer" and patch.get("description") == "":
        patch.pop("description")
    run_ids = plan.get("run_node_ids") if isinstance(plan.get("run_node_ids"), list) else []
    prompt_updates = plan.get("prompt_updates") if isinstance(plan.get("prompt_updates"), list) else []
    return {
        "intent": intent,
        "summary": str(plan.get("summary") or "").strip(),
        "thinking": str(plan.get("thinking") or "").strip(),
        "patch": patch,
        "remove_shot_ids": shot_ids,
        "prompt_updates": prompt_updates,
        "edit_documents": plan.get("edit_documents", False),
        "run_node_ids": [str(item).strip() for item in run_ids if str(item).strip()],
    }


async def _llm_leader_plan(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    selected_node_id: str = "",
    documents: dict[str, ChatDocument],
) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_API_ERROR,
        call_model_tool,
        model_text_or_raise,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    snapshot = {
        "selected_node_id": selected_node_id,
        "user_canvas_edits": list(meta.get("user_canvas_edits") or [])[-20:],
        "description": graph.get("description", ""),
        "documents": [{"node_id": doc.node_id, "pipeline": doc.pipeline, "text": doc.text} for doc in documents.values()],
        "shots": [
            {"index": index, "node_ids": [node["id"] for node in graph["nodes"]
                                        if node_pipeline(node) in {"frame", "clip"} and node_shot_index(node) == index]}
            for index in sorted({node_shot_index(node) for node in graph["nodes"]
                                 if node_pipeline(node) in {"frame", "clip"}})
        ],
        "nodes": [_leader_node_context(node) for node in graph["nodes"]],
        "edges": [
            {"id": edge.get("id"), "source": edge.get("source"), "target": edge.get("target")}
            for edge in graph.get("edges") or []
        ],
        "user": message,
    }
    try:
        result = await call_model_tool(
            prompt=json.dumps(snapshot, ensure_ascii=False),
            system=_LEADER_SYSTEM,
            optimize_for="quality",
            max_tokens=16384,
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
    documents: dict[str, ChatDocument],
    selected_node_id: str = "",
    run_new_nodes: bool = False,
    progress: ProgressFn | None = None,
    pending_documents: bool = False,
) -> dict[str, Any]:
    text = str(message or "").strip()
    _emit(progress, ACTIVITY_KIND_THINKING, "reading the canvas and current documents")
    plan = await _llm_leader_plan(graph, text, selected_node_id=selected_node_id, documents=documents)
    _emit(progress, ACTIVITY_KIND_THINKING, plan.get("thinking") or "preparing workflow edits")
    if not message_asks_to_run(text, run_new_nodes=run_new_nodes):
        plan["run_node_ids"] = []
    if plan.get("intent") == "answer" and any(plan.get(key) for key in ("patch", "remove_shot_ids", "prompt_updates", "edit_documents")):
        raise DesignerGraphValidationError("An answer cannot also modify the workflow")

    patch = plan.get("patch") or {}
    if pending_documents and (
        plan["edit_documents"] or plan["prompt_updates"] or plan.get("remove_shot_ids")
        or "description" in patch
        or any(patch.get(key) for key in ("remove_node_ids", "remove_edge_ids", "upsert_edges"))
        or any(set(node) - {"id", "label", "layout"} for node in patch.get("upsert_nodes", []))
    ):
        raise ChatDocumentConflict("大纲或分镜有待选择版本，请先保留原版或采用新版，再重试编辑。")
    next_graph, run_ids, summary = apply_leader_plan(deepcopy(graph), plan)
    text_edits = []
    if plan["edit_documents"] or graph_content_changed(graph, next_graph):
        validate_shot_topology(next_graph)
        remaining_ids = {node["id"] for node in next_graph["nodes"]}
        remaining_documents = {
            key: doc for key, doc in documents.items() if key in remaining_ids and doc.text
        }
        if remaining_documents:
            _emit(progress, ACTIVITY_KIND_STAGE, "synchronizing the complete brief and storyboard")
            next_graph, text_edits = await plan_document_edits(graph, next_graph, remaining_documents, text)
    next_graph, texts, changed = prepare_document_update(graph, next_graph, documents, text_edits)
    if changed:
        _emit(progress, ACTIVITY_KIND_TOOL_CALL, "validated workflow edits; preparing to save", tool="designer_graph_patch")
    return {
        "intent": plan["intent"],
        "summary": summary,
        "graph": next_graph,
        "texts": texts,
        "run_node_ids": run_ids,
        "changed": changed,
        "updated_at": utc_now_ms(),
    }
