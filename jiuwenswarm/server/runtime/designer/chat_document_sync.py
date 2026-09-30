# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepare chat edits in memory, then explicitly save the validated candidate."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerExecutionRun,
    DesignerGraphNode,
    DesignerGraphValidationError,
    node_config,
    node_pipeline,
    node_shot_index,
    utc_now_ms,
)
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.chat_shot_references import (
    NODE_PROSE_FIELDS,
    map_shot_references,
    node_prose_config,
    redirects_removed_continuity,
    referenced_shot_indices,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_workspace_dir,
    path_from_uri,
    read_node_text,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardShot,
    parse_storyboard_shots,
)
from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
    reference_led_graph,
    refresh_reference_continuity,
    reject_removed_reference_stills,
)


class ChatDocumentConflict(DesignerGraphValidationError):
    """The user must select an accepted text version before editing it."""


@dataclass(frozen=True)
class ChatDocument:
    node_id: str
    pipeline: str
    text: str
    path: Path | None


def read_chat_documents(
    graph: DesignerExecutionGraph, run: DesignerExecutionRun | None = None
) -> dict[str, ChatDocument]:
    """Use current run output, then graph output, then the node's executable draft."""
    documents: dict[str, ChatDocument] = {}
    for node in graph.get("nodes", []):
        pipeline = node_pipeline(node)
        if pipeline in {"brief", "storyboard"}:
            text, path = read_node_text(node, run)
            documents[node["id"]] = ChatDocument(node["id"], pipeline, text, path)
    return documents


def apply_text_replacements(text: str, replacements: Any) -> str:
    """Locate every edit in the same original text, so replacements cannot cascade."""
    if not isinstance(replacements, list) or not replacements:
        raise DesignerGraphValidationError("Text replacements must be a non-empty array")
    spans: list[tuple[int, int, str]] = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise DesignerGraphValidationError("Each text replacement must be an object")
        old, new = replacement.get("old"), replacement.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise DesignerGraphValidationError("Text replacement needs a non-empty old string and a new string")
        if "start" in replacement:
            start = replacement["start"]
            if type(start) is not int or start < 0 or text[start:start + len(old)] != old:
                raise DesignerGraphValidationError("Text replacement offset must match the original document")
        else:
            start = text.find(old)
            if start < 0 or text.find(old, start + 1) >= 0:
                raise DesignerGraphValidationError("Text replacement must match exactly once in the original document")
        spans.append((start, start + len(old), new))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise DesignerGraphValidationError("Text replacement ranges must not overlap")
    for start, end, new in reversed(spans):
        text = text[:start] + new + text[end:]
    return text


def apply_document_edits(
    documents: dict[str, ChatDocument], text_edits: Any
) -> dict[str, str]:
    """Resolve model edits only through existing text node IDs; accept no file paths."""
    if not isinstance(text_edits, list):
        raise DesignerGraphValidationError("text_edits must be an array")
    texts = {node_id: document.text for node_id, document in documents.items()}
    edited: set[str] = set()
    for edit in text_edits:
        if not isinstance(edit, dict):
            raise DesignerGraphValidationError("Each text edit must be an object")
        node_id = edit.get("node_id")
        if not isinstance(node_id, str) or node_id not in documents or node_id in edited:
            raise DesignerGraphValidationError("Text edits must name distinct existing brief/storyboard nodes")
        texts[node_id] = apply_text_replacements(documents[node_id].text, edit.get("replacements"))
        edited.add(node_id)
    return texts


_SECONDS = r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s|秒)?"
_TIMELINE = re.compile(rf"\s*{_SECONDS}\s*(?:[-–—~～至]|to)\s*{_SECONDS}\s*", re.I)
_SHOT_NUMBER = re.compile(r"(?:Shot\s*)?(\d+)", re.I)


def timeline_seconds(value: str) -> tuple[Decimal, Decimal]:
    match = _TIMELINE.fullmatch(value)
    if match is None:
        raise DesignerGraphValidationError(f"Timeline must contain start/end seconds: {value!r}")
    start, end = (Decimal(part) for part in match.groups())
    if end <= start:
        raise DesignerGraphValidationError(f"Timeline must have a positive duration: {value!r}")
    return start, end


def _candidate_shots(text: str) -> list[StoryboardShot]:
    """Keep auxiliary tables out of the existing parser's storyboard row scan."""
    shots: list[StoryboardShot] = []
    prose: list[str] = []
    for is_table, block in groupby(text.splitlines(), key=lambda line: "|" in line):
        lines = list(block)
        if not is_table:
            prose.extend(lines)
            continue
        headers = [cell.strip() for cell in lines[0].strip().strip("|").split("|")]
        headers = [
            "Shot" if cell.casefold() in {"shot", "镜号", "分镜#"}
            else "Timeline" if cell.casefold() in {"timeline", "时间轴"}
            else cell
            for cell in headers
        ]
        if "Shot" not in headers or "Timeline" not in headers:
            continue
        parsed = parse_storyboard_shots("\n".join(["|".join(headers), *lines[1:]]))
        # The shared parser caps its output; never validate a silently truncated table.
        if len(parsed) != len(lines) - 2:
            raise DesignerGraphValidationError("Could not parse every storyboard table row")
        shot_column = headers.index("Shot")
        for line in lines[2:]:
            cells = line.strip().strip("|").split("|")
            if shot_column >= len(cells) or not cells[shot_column].strip():
                raise DesignerGraphValidationError("Storyboard rows must include a shot number")
        shots.extend(parsed)
    if shots:
        return shots
    hierarchical = "\n".join(prose)
    shots = parse_storyboard_shots(hierarchical)
    if len(shots) != len(re.findall(r"(?im)^\s*###\s+Shot\s+\d+\b", hierarchical)):
        raise DesignerGraphValidationError("Could not parse every storyboard shot heading")
    return shots


def validate_shot_topology(graph: DesignerExecutionGraph) -> dict[int, list[DesignerGraphNode]]:
    """Reject invalid shot structure before spending a document-editing model call."""
    groups: dict[int, list[DesignerGraphNode]] = {}
    for node in graph.get("nodes", []):
        pipeline = node_pipeline(node)
        if pipeline not in {"frame", "clip"}:
            continue
        raw_index = node_config(node).get("shot_index", node_shot_index(node))
        if isinstance(raw_index, bool) or not re.fullmatch(r"[1-9]\d*", str(raw_index).strip()):
            raise DesignerGraphValidationError(f"Invalid shot index on node {node['id']}")
        index = int(raw_index)
        group = groups.setdefault(index, [])
        if any(node_pipeline(member) == pipeline for member in group):
            raise DesignerGraphValidationError(f"Duplicate {pipeline} node for shot {index}")
        group.append(node)
    if sorted(groups) != list(range(1, len(groups) + 1)):
        raise DesignerGraphValidationError("Graph shot indices must be consecutive")
    for group in groups.values():
        timings = {timeline_seconds(str(node_config(node).get("timeline") or "")) for node in group}
        if len(timings) != 1:
            raise DesignerGraphValidationError("Frame and clip timelines must match within a shot")
    return groups


def validate_storyboard(graph: DesignerExecutionGraph, text: str) -> None:
    """Compare shot order and timing without repairing or rebuilding the graph."""
    shots = _candidate_shots(text)
    groups = validate_shot_topology(graph)
    expected = list(range(1, len(shots) + 1))
    if not shots or sorted(groups) != expected:
        raise DesignerGraphValidationError("Storyboard count/order does not match consecutive graph shot indices")
    for index, shot in enumerate(shots, 1):
        number = _SHOT_NUMBER.fullmatch(shot["shot_no"].strip())
        if number is None or int(number.group(1)) != index:
            raise DesignerGraphValidationError(f"Storyboard row {index} has an inconsistent shot number")
        timing = timeline_seconds(shot["timeline"])
        for node in groups[index]:
            node_timing = timeline_seconds(str(node_config(node).get("timeline") or ""))
            if node_timing != timing:
                raise DesignerGraphValidationError(f"Storyboard timeline does not match node {node['id']}")


def _content_view(graph: DesignerExecutionGraph) -> dict[str, Any]:
    return {
        "description": graph.get("description"),
        "nodes": {node["id"]: {key: value for key, value in node.items() if key not in {"layout", "label"}}
                  for node in graph.get("nodes", [])},
        "edges": graph.get("edges", []),
    }


def graph_content_changed(before: DesignerExecutionGraph, after: DesignerExecutionGraph) -> bool:
    """Ignore layout and labels when deciding whether prose needs synchronization."""
    return _content_view(before) != _content_view(after)


def _sync_shots(
    rows: list[dict[str, Any]], before: DesignerExecutionGraph, graph: DesignerExecutionGraph
) -> list[dict[str, Any]]:
    old_nodes = {node["id"]: node for node in before.get("nodes", [])}
    old_rows = {int(row.get("shot_index") or i): row for i, row in enumerate(rows, 1)}
    # Prefer clip configuration when both frame and clip describe a shot.
    shots = {node_shot_index(node): node for role in ("frame", "clip")
             for node in graph.get("nodes", []) if node_pipeline(node) == role}
    synced = []
    for index, node in sorted(shots.items()):
        old = old_nodes.get(node["id"])
        row = deepcopy(old_rows.get(node_shot_index(old), {})) if old else {}
        cfg = node_config(node)
        row.update({key: deepcopy(value) for key, value in cfg.items()
                    if key in row or key in {"timeline", "camera", "setting_id", "character_ids", "on_screen", "offscreen", "speech_line"}})
        row["shot_index"] = index
        for source, targets in {"shot_action": ("action", "character_action"), "shot_title": ("title",)}.items():
            if source in cfg:
                for target in targets:
                    row[target] = cfg[source]
        if old is None or cfg.get("shot_action") != node_config(old).get("shot_action"):
            row["keyframe_prompt"] = cfg.get("prompt") or cfg.get("shot_action", "")
        elif cfg.get("prompt") != node_config(old).get("prompt"):
            row["keyframe_prompt"] = cfg.get("prompt", "")
        synced.append(row)
    return synced


def _sync_entity_records(before: DesignerExecutionGraph, graph: DesignerExecutionGraph) -> None:
    analysis = graph.get("metadata", {}).get("script_analysis")
    if not isinstance(analysis, dict):
        return
    for pipeline, collection, id_key in (("character_design", "characters", "character_id"), ("scene", "scenes", "setting_id")):
        if collection not in analysis:
            continue
        old = {node_config(n).get(id_key): n for n in before.get("nodes", [])
               if node_pipeline(n) == pipeline and node_config(n).get(id_key)}
        current = {node_config(n).get(id_key): n for n in graph.get("nodes", [])
                   if node_pipeline(n) == pipeline and node_config(n).get(id_key)}
        removed = old.keys() - current.keys()
        records = [row for row in analysis[collection] if row.get("id") not in removed]
        existing_ids = {row.get("id") for row in records}
        records.extend({"id": entity_id, "description": ""} for entity_id in current.keys() - old.keys() - existing_ids)
        for row in records:
            entity_id = row.get("id")
            node = current.get(entity_id)
            if node is None or node == old.get(entity_id):
                continue
            cfg = node_config(node)
            row.update({key: deepcopy(value) for key, value in cfg.items() if key in row and key != "id"})
            if cfg.get("prompt") != node_config(old.get(entity_id, {})).get("prompt"):
                row["description"] = cfg.get("prompt", "")
        analysis[collection] = records


_DEPENDENCY_LISTS = ("inputs", "character_node_ids")
_DEPENDENCY_IDS = (
    "scene_node_id", "master_scene_node_id", "continuity_frame_node_id",
    "prior_keyframe_node_id", "continuity_clip_node_id", "previous_clip_node_id",
)
_GENERATION_CACHE = ("regenerate_packet", "last_wan_prompt", "last_approved_prompt")


def _refresh_generation_details(before: DesignerExecutionGraph, graph: DesignerExecutionGraph) -> None:
    """Refresh derived copies from the edited semantic config, retaining custom locks."""
    from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import architecture_clause_from_bible
    from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import build_action_lock, build_positioning_lock
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import stamp_shot_states_on_clip_cfg

    old_nodes = {node["id"]: node for node in before.get("nodes", [])}
    characters = (graph.get("metadata", {}).get("script_analysis") or {}).get("characters", [])
    for node in graph.get("nodes", []):
        cfg = node_config(node)
        old = node_config(old_nodes.get(node["id"], {}))
        changed = {key for key in cfg.keys() | old.keys() if cfg.get(key) != old.get(key)}
        if not changed:
            continue
        if changed & {"cast_actions", "on_screen", "offscreen"} and "occupancy" in cfg:
            occupancy = cfg["occupancy"]
            for source, target in (("cast_actions", "cast_actions"), ("on_screen", "must_appear"), ("offscreen", "offscreen")):
                if source in changed:
                    occupancy[target] = deepcopy(cfg.get(source, {} if source == "cast_actions" else []))
        if changed & {"shot_action", "cast_actions", "blocking", "camera", "on_screen", "setting_id"}:
            source = {key: value for key, value in cfg.items() if key not in {"positioning_lock", "action_lock"}}
            if "positioning_lock" in cfg:
                cfg["positioning_lock"] = build_positioning_lock(source, characters)
            if "action_lock" in cfg:
                cfg["action_lock"] = build_action_lock(source, characters)
        if "start_state" in changed:
            # Remove only holds generated from the previous opening state.
            old_holds = stamp_shot_states_on_clip_cfg({"start_state": old.get("start_state"), "pose_holds": []}).get("pose_holds", [])
            cfg["pose_holds"] = [hold for hold in cfg.get("pose_holds", []) if hold not in old_holds and not hold.startswith(("Opening hold: ", "Opening facing: "))]
            stamped = stamp_shot_states_on_clip_cfg(cfg)
            cfg["pose_holds"] = stamped.get("pose_holds", [])
            if "seat_anchors" in cfg or (cfg.get("start_state") or {}).get("seats"):
                cfg["seat_anchors"] = deepcopy((cfg.get("start_state") or {}).get("seats", {}))
        end_state = cfg.get("end_state")
        # Reference-led clips keep end_state as prose, not a shot-state record.
        if "end_state" in changed and isinstance(end_state, dict) and (
            "exiting_character_ids" in cfg or end_state.get("exited")
        ):
            cfg["exiting_character_ids"] = list(end_state.get("exited", []))
        if "scene_specs" in changed:
            architecture = architecture_clause_from_bible(cfg.get("scene_specs"))
            for key in ("scene_architecture_clause", "scene_master_prompt"):
                if key in cfg:
                    cfg[key] = architecture if key == "scene_architecture_clause" else architecture[:900]
        # These fields are duplicated in identity_refs when the graph is constructed.
        refs = cfg.get("identity_refs") or {}
        for key in changed | {"occupancy"}:
            if key in refs and key in cfg and refs[key] == (old.get("identity_refs") or {}).get(key):
                refs[key] = deepcopy(cfg[key])


def _invalidate_generation_cache(before: DesignerExecutionGraph, graph: DesignerExecutionGraph) -> None:
    old_nodes = {node["id"]: node for node in before.get("nodes", [])}
    nodes = {node["id"]: node for node in graph.get("nodes", [])}
    # Document edits alone do not invalidate every shot fed by that document.
    media_ids = {key for key, node in {**old_nodes, **nodes}.items()
                 if node.get("type") in {"image", "video", "audio"}}
    affected = {key for key in media_ids
                if node_config(old_nodes.get(key, {})) != node_config(nodes.get(key, {}))}
    old_edges = {(edge["source"], edge["target"]) for edge in before.get("edges", [])}
    new_edges = {(edge["source"], edge["target"]) for edge in graph.get("edges", [])}
    affected.update(target for _, target in old_edges ^ new_edges if target in media_ids)
    dependencies: dict[str, set[str]] = {key: set() for key in nodes}
    for key, node in nodes.items():
        cfg = node_config(node)
        for source in (cfg, cfg.get("identity_refs") or {}):
            for field in _DEPENDENCY_LISTS:
                dependencies[key].update(source.get(field) or [])
            dependencies[key].update(source[field] for field in _DEPENDENCY_IDS if source.get(field))
    for edge in graph.get("edges", []):
        dependencies[edge["target"]].add(edge["source"])
    while True:
        downstream = {key for key in media_ids & nodes.keys() if dependencies[key] & affected}
        if downstream <= affected:
            break
        affected.update(downstream)
    for key in affected & nodes.keys():
        cfg = node_config(nodes[key])
        old_cfg = node_config(old_nodes.get(key, {}))
        generate = cfg.get("generate")
        if isinstance(generate, dict):
            if cfg.get("prompt") != old_cfg.get("prompt"):
                generate["prompt"] = cfg.get("prompt", "")
            elif generate.get("prompt") != (old_cfg.get("generate") or {}).get("prompt"):
                cfg["prompt"] = generate.get("prompt", "")
        for field in _GENERATION_CACHE:
            cfg.pop(field, None)


def prepare_document_update(
    before: DesignerExecutionGraph,
    candidate: DesignerExecutionGraph,
    documents: dict[str, ChatDocument],
    text_edits: Any,
) -> tuple[DesignerExecutionGraph, dict[str, str], bool]:
    """Prepare a detached candidate; this phase never writes files or starts tasks."""
    graph = deepcopy(candidate)
    nodes = {node["id"]: node for node in graph.get("nodes", [])}
    remaining = {key: doc for key, doc in documents.items() if key in nodes}
    texts = apply_document_edits(remaining, text_edits)
    content_changed = graph_content_changed(before, graph)
    texts_changed = any(texts[key] != doc.text for key, doc in remaining.items())
    if content_changed or text_edits:
        edited = {edit["node_id"] for edit in text_edits}
        required = {key for key, doc in remaining.items() if doc.text}
        if not required <= edited:
            raise DesignerGraphValidationError("Content edits require text_edits for the current brief and storyboard")
    if content_changed or texts_changed:
        old_targets = {node_shot_index(node): node["id"] for node in before.get("nodes", []) if node_pipeline(node) == "clip"}
        new_targets = {node_shot_index(node): node["id"] for node in nodes.values() if node_pipeline(node) == "clip"}
        removed_targets = set(old_targets.values()) - set(new_targets.values())
        if removed_targets:
            for edit in text_edits:
                for replacement in edit["replacements"]:
                    if redirects_removed_continuity(replacement["old"], replacement["new"], old_targets, new_targets):
                        raise DesignerGraphValidationError("Remove continuity claims about deleted shots instead of redirecting them to unrelated shots")
        shot_indices = {node_shot_index(node) for node in nodes.values() if node_pipeline(node) in {"frame", "clip"}}
        for key, doc in remaining.items():
            invalid = referenced_shot_indices(texts[key]) - shot_indices
            if invalid:
                raise DesignerGraphValidationError(f"Document {key} references missing shots: {sorted(invalid)}")
            if doc.pipeline == "storyboard":
                validate_storyboard(graph, texts[key])
        removed = {node["id"] for node in before.get("nodes", [])} - nodes.keys()
        reference_led = reference_led_graph(graph)
        if reference_led:
            reject_removed_reference_stills(graph, removed)
        for node in nodes.values():
            cfg = node_config(node)
            for source in (cfg, cfg.get("identity_refs") or {}):
                for key in _DEPENDENCY_LISTS:
                    if key in source:
                        source[key] = [item for item in source[key] if item not in removed]
                for key in _DEPENDENCY_IDS:
                    if source.get(key) in removed:
                        source.pop(key)
        _refresh_generation_details(before, graph)
        if reference_led:
            refresh_reference_continuity(before, graph)
        _invalidate_generation_cache(before, graph)
        if removed_targets:
            old_nodes = {node["id"]: node for node in before["nodes"]}
            for node in nodes.values():
                if node["id"] not in old_nodes or node_pipeline(node) in {"brief", "storyboard"}:
                    continue
                old_config = node_prose_config(old_nodes[node["id"]])
                config = node_prose_config(node)
                for field in NODE_PROSE_FIELDS:
                    old_text = json.dumps(old_config.get(field), ensure_ascii=False)
                    new_text = json.dumps(config.get(field), ensure_ascii=False)
                    if redirects_removed_continuity(old_text, new_text, old_targets, new_targets):
                        raise DesignerGraphValidationError(
                            f"Node {node['id']}.{field} redirects continuity from a deleted shot"
                        )
        meta = graph.setdefault("metadata", {})
        if set(nodes) != {node["id"] for node in before.get("nodes", [])}:
            meta.update(user_topology_edit=True, freeze_shot_topology=True)
        analysis = meta.get("script_analysis")
        if isinstance(analysis, dict) and "shots" in analysis:
            analysis["shots"] = _sync_shots(analysis["shots"], before, graph)
            if "target_shot_count" in analysis:
                analysis["target_shot_count"] = len(analysis["shots"])
            if "target_duration_sec" in analysis:
                durations = [timeline_seconds(row["timeline"]) for row in analysis["shots"]]
                analysis["target_duration_sec"] = float(sum(end - start for start, end in durations))
        if "target_shot_count" in meta:
            meta["target_shot_count"] = len({node_shot_index(n) for n in nodes.values() if node_pipeline(n) in {"frame", "clip"}})
        _sync_entity_records(before, graph)
        for node in nodes.values():
            cfg = node_config(node)
            if "planned_shots" in cfg:
                cfg["planned_shots"] = _sync_shots(cfg["planned_shots"], before, graph)
        if graph.get("description") != before.get("description"):
            if "user_prompt" in meta:
                meta["user_prompt"] = graph.get("description", "")
            for key in remaining:
                cfg = node_config(nodes[key])
                if cfg.get("prompt") == before.get("description"):
                    cfg["prompt"] = graph.get("description", "")
        for key, doc in remaining.items():
            cfg = node_config(nodes[key])
            for field in ("prewritten", "draft_prewritten"):
                if field in cfg:
                    cfg[field] = texts[key]
            approved = f"approved_{doc.pipeline}"
            if approved in meta:
                meta[approved] = texts[key]
    changed = texts_changed or any(graph.get(key) != before.get(key) for key in ("description", "title", "nodes", "edges", "metadata"))
    return graph, texts if (content_changed or texts_changed) else {}, changed


def save_document_update(
    store: DesignerGraphStore,
    graph: DesignerExecutionGraph,
    run: DesignerExecutionRun | None,
    documents: dict[str, ChatDocument],
    texts: dict[str, str],
) -> tuple[DesignerExecutionGraph, DesignerExecutionRun | None, list[str]]:
    """Save a validated candidate; report partial progress if any write fails."""
    graph, run = deepcopy(graph), deepcopy(run)
    nodes = {node["id"]: node for node in graph["nodes"]}
    written: list[str] = []
    updated_uris: list[str] = []
    run_changed = False
    target = "text files"
    try:
        for node_id, text in texts.items():
            doc = documents[node_id]
            path = doc.path or (graph_workspace_dir(graph) / f"chat-{uuid.uuid4().hex}.md")
            target = str(path)
            if text != doc.text or doc.path is None:
                # Each text file is replaced atomically; graph/run are separate writes.
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as output:
                        temporary = Path(output.name)
                        output.write(text)
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                written.append(target)
                updated_uris.append(path.resolve().as_uri())
            ref = file_output_ref(path, kind="text", mime_type="text/markdown")
            nodes[node_id]["output_ref"] = ref
            state = (run.get("node_states") or {}).get(node_id) if run else None
            if state is not None:
                refs = list(state.get("output_refs") or [])
                primary = state.get("output_ref")
                if primary and primary not in refs:
                    refs.append(primary)
                media_refs = [item for item in refs
                              if item.get("kind") != "text"
                              and not str(item.get("mime_type") or "").startswith("text/")
                              and Path(str(item.get("uri") or "")).suffix.lower()
                              not in {".md", ".txt", ".markdown", ".csv"}]
                state["output_ref"] = ref
                state["output_refs"] = [ref, *media_refs]
                run_changed = True
        if run_changed:
            target = f"run:{run['run_id']}"
            run = store.save_run(run)
            written.append(target)
        target = f"graph:{graph['graph_id']}"
        graph["updated_at"] = utc_now_ms()
        saved = store.save_graph(graph)
    except Exception as exc:
        completed = ", ".join(written) or "none"
        raise RuntimeError(f"Workflow save failed at {target}; completed writes: {completed}. Check the failed target before retrying. {exc}") from exc
    return saved, run, updated_uris
