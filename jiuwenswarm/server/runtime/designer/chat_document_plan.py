# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Synchronize document and generation prose against an in-memory topology edit."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, TypedDict

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerGraphNode,
    DesignerGraphValidationError,
    node_config,
    node_pipeline,
    node_shot_index,
)
from jiuwenswarm.server.runtime.designer.chat_document_sync import (
    ChatDocument,
    map_shot_references,
    timeline_seconds,
)
from jiuwenswarm.server.runtime.designer.chat_shot_references import (
    apply_node_reference_edits,
    node_reference_fields,
)

_DOCUMENT_SYSTEM = """You synchronize existing brief/storyboard documents after a workflow edit.
Return a JSON object with documents and node_fields. Each blocks object maps the EXACT input block
keys to complete text strings. For example:
{"documents": [{"node_id": "n_brief", "blocks": {
  "a78b25c312de": "complete updated paragraph",
  "ef645db629a0": "unchanged original paragraph"
}}], "node_fields": {"field_key": "complete updated node text"}}

Every input block key must appear exactly once. Copy unchanged text verbatim; return
an empty string to delete a block's content. Keys identify ORIGINAL SOURCE blocks and
must never change, even when inserting or deleting shots. Do not add new keys.
The server has already removed whole blocks headed solely by a deleted shot. Their keys
are listed in removed_shot_blocks for context; do not return them. Review every remaining
block, including mixed tables and surviving shots' continuity claims.
Insert new paragraphs/shot sections INSIDE an adjacent existing key's text, separated
by blank lines. For example, an old shot section's value can become "old section\\n\\nnew section".
Insert new table rows into that table's existing value. Return complete tables with ALL
retained rows. Compare ALL sentences/cells in every block with the request and facts.

The candidate topology (node IDs, order, timing, edges) is fixed. Generation prose is
still a draft: node_fields supplies the ORIGINAL source for each reference-bearing string
that needs review after renumbering/deletion. Its candidate proposal is deliberately omitted.
Rewrite that source using the user request, mapped reference targets and new shot timing.
For nested config paths, the source contains the original enclosing config field; return
only the complete string at the requested path. Return EVERY listed field key in node_fields;
return {} when none are listed. Keys resolve to server-owned config paths. Do not return
node patches or change topology. Original reference targets define continuity.
Remove claims depending on deleted targets, keeping independent object/action details and
requested edits. Do not invent an earlier scene for an object's first surviving appearance.
Use the SAME corrected relationships in node_fields and both documents. Do not copy a
candidate prompt's relationship into the documents before checking its original target.
Follow the user request and the supplied structural facts.
Read BOTH original documents from beginning to end. Update every affected occurrence,
including titles, synopsis, prose rules, shot/scene tables, handoffs and node inventories.
A correct main storyboard table alone is insufficient. Counts and durations written in
words must agree with the supplied totals everywhere. Use the per-shot duration list;
update uniform-duration claims when shots now have different lengths. Preserve unrelated text verbatim.

Each original block must retain its ORIGINAL SUBJECT. The server has ALREADY mapped
explicit shot references in block text to their candidate indices using stable node IDs.
Keep those indices; do not renumber them again. A [[REMOVED_SHOT:node_id]] marker refers
to a deleted shot. Remove that shot's section and all dependent continuity claims; never
replace a marker with another surviving shot. No marker may remain in the final text.

When inserting a shot, preserve every existing shot section with its supplied NEW heading
and original events/details. Insert the new shot section alongside an existing one inside
that value. Never shift original content between block keys or replace it with a new shot.
Preserve all surviving auxiliary handoffs as well as the main table. Keep shared scene/prop
descriptions that still apply. Update counts, durations, synopsis and other prose according
to the request and the supplied facts.

Resolve object identity throughout the prose: a synopsis can describe the requested
object with a synonym or abbreviated name. Update those references too. Similar background
objects remain distinct and keep their original details. Keep actions, camera, characters,
lighting and all other details unchanged unless the user requested a change.

Block keys are temporary editing coordinates for these original documents. Preserve their
order and the existing Markdown format. Unchanged blocks are retained byte-for-byte.
Do not return paths, node patches, media requests, or additional documents. Do not omit
blocks or summarize a table/paragraph. Check all decisions against the structural facts.
"""


@dataclass(frozen=True)
class DocumentBlock:
    key: str
    start: int
    text: str


def document_blocks(text: str) -> list[DocumentBlock]:
    """Keep blank-line separators untouched; no Markdown parsing or format migration."""
    blocks = []
    start = 0
    for index, part in enumerate(re.split(r"(\n[ \t]*\n)", text)):
        if index % 2 == 0 and part:
            key = hashlib.sha256(f"{start}:{part}".encode()).hexdigest()[:12]
            blocks.append(DocumentBlock(key, start, part))
        start += len(part)
    return blocks


def _document_replacements(
    response: Any,
    documents: dict[str, ChatDocument],
    removed_shot_blocks: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Validate complete block decisions and resolve their offsets against the original snapshot."""
    if not isinstance(response, dict) or not isinstance(response.get("documents"), list):
        raise DesignerGraphValidationError("Document editor must return a documents array")
    edits = []
    seen = set()
    for document in response["documents"]:
        if not isinstance(document, dict):
            raise DesignerGraphValidationError("Each document decision must be an object")
        node_id = document.get("node_id")
        if not isinstance(node_id, str) or node_id not in documents or node_id in seen:
            raise DesignerGraphValidationError("Document decisions must name distinct current documents")
        seen.add(node_id)
        original = documents[node_id].text
        blocks = document_blocks(original)
        decisions = document.get("blocks")
        deleted_keys = set((removed_shot_blocks or {}).get(node_id, []))
        expected_keys = {block.key for block in blocks} - deleted_keys
        if not isinstance(decisions, dict) or decisions.keys() != expected_keys:
            raise DesignerGraphValidationError(f"Document {node_id} must retain every input block key")
        replacements = []
        for block in blocks:
            text = "" if block.key in deleted_keys else decisions[block.key]
            if not isinstance(text, str):
                raise DesignerGraphValidationError("Each original block must include its complete text")
            if "[[REMOVED_SHOT:" in text:
                raise DesignerGraphValidationError("Document still contains a reference to a removed shot")
            if text != block.text:
                replacements.append({"start": block.start, "old": block.text, "new": text})
        edits.append({"node_id": node_id, "replacements": replacements or [{"old": original, "new": original}]})
    if seen != documents.keys():
        raise DesignerGraphValidationError("Document editor must review every current brief and storyboard")
    return edits


_REMOVED_SHOT_HEADING = re.compile(
    r"(?:\*\*\[\[REMOVED_SHOT:[^\]]+\]\]\*\*|#{1,6} +\[\[REMOVED_SHOT:[^\]]+\]\])"
)


def _document_context(doc: ChatDocument, reference_targets: dict[int, tuple[str, int | None]]) -> dict[str, Any]:
    blocks = {}
    removed = []
    for block in document_blocks(doc.text):
        text = map_shot_references(block.text, reference_targets)
        heading, _, body = text.partition("\n")
        # The explicit heading owns this block. Mixed tables/prose remain model decisions.
        if _REMOVED_SHOT_HEADING.fullmatch(heading.strip()) and not any(
            re.match(r"(?:#{1,6} |\*\*.*\*\*$)", line) for line in body.splitlines()
        ):
            removed.append(block.key)
        else:
            blocks[block.key] = text
    return {"node_id": doc.node_id, "pipeline": doc.pipeline, "blocks": blocks, "removed_shot_blocks": removed}


class ShotPosition(TypedDict):
    index: int
    timeline: str


def _shot_positions(graph: DesignerExecutionGraph) -> dict[str, ShotPosition]:
    return {
        node["id"]: {"index": node_shot_index(node), "timeline": node_config(node)["timeline"]}
        for node in graph["nodes"]
        if node_pipeline(node) in {"frame", "clip"}
    }


def _shot_totals(positions: dict[str, ShotPosition]) -> dict[str, Any]:
    # A frame and a clip with the same index describe one shot.
    timelines = {position["index"]: position["timeline"] for position in positions.values()}
    durations = {}
    for index, timeline in sorted(timelines.items()):
        start, end = timeline_seconds(timeline)
        durations[index] = float(end - start)
    return {"count": len(timelines), "duration_seconds": sum(durations.values()), "shot_durations_seconds": durations}


def _prose_node(node: DesignerGraphNode, reviewed_fields: set[str]) -> dict[str, Any]:
    config = node_config(node)
    fields = {
        "shot_index", "shot_action", "shot_title", "timeline", "camera", "character_id",
        "character_ids", "setting_id", "on_screen", "offscreen", "speech_line",
    }
    view: dict[str, Any] = {
        "id": node["id"],
        "pipeline": node_pipeline(node),
        "details": {key: value for key, value in config.items() if key in fields - reviewed_fields},
    }
    if "prompt" not in reviewed_fields:
        view["prompt"] = config.get("prompt") or config.get("generate", {}).get("prompt", "")
    return view


def document_edit_context(
    before: DesignerExecutionGraph,
    candidate: DesignerExecutionGraph,
    documents: dict[str, ChatDocument],
    message: str,
) -> dict[str, Any]:
    """Supply stable identities and deterministic totals instead of asking the model to infer them."""
    old_positions, new_positions = _shot_positions(before), _shot_positions(candidate)
    targets = {
        node_shot_index(node): node["id"]
        for role in ("frame", "clip")
        for node in before["nodes"] if node_pipeline(node) == role
    }
    reference_targets = {
        index: (node_id, new_positions[node_id]["index"] if node_id in new_positions else None)
        for index, node_id in targets.items()
    }
    fields = node_reference_fields(before, candidate, reference_targets)
    reviewed = {node["id"]: set() for node in before["nodes"] + candidate["nodes"]}
    for field in fields:
        reviewed[field.node_id].add(field.path[0])
    # Reference-bearing proposals must not become facts for the prose editor.
    old_nodes = {node["id"]: _prose_node(node, reviewed[node["id"]]) for node in before["nodes"]}
    new_nodes = {node["id"]: _prose_node(node, reviewed[node["id"]]) for node in candidate["nodes"]}
    return {
        "node_fields": {
            field.key: {"node_id": field.node_id, "config_path": list(field.path),
                        "original_with_mapped_references": field.original}
            for field in fields
        },
        "shot_reference_targets": {
            str(index): {"node_id": node_id, "current_index": current}
            for index, (node_id, current) in reference_targets.items()
        },
        "user": message,
        "documents": [
            _document_context(doc, reference_targets)
            for doc in documents.values()
        ],
        "description": {"before": before.get("description", ""), "after": candidate.get("description", "")},
        "shot_totals": {"before": _shot_totals(old_positions), "after": _shot_totals(new_positions)},
        "shot_changes": [
            {"node_id": node_id, "before": old_positions.get(node_id), "after": new_positions.get(node_id)}
            for node_id in sorted(old_positions.keys() | new_positions.keys())
        ],
        "node_changes": [
            {"node_id": node_id, "before": old_nodes.get(node_id), "after": new_nodes.get(node_id)}
            for node_id in sorted(old_nodes.keys() | new_nodes.keys())
            if old_nodes.get(node_id) != new_nodes.get(node_id)
        ],
        "current_nodes": [
            {"id": node["id"], "pipeline": node["pipeline"], **node["details"]}
            for node in new_nodes.values()
        ],
        "node_counts": dict(Counter(node["pipeline"] for node in new_nodes.values())),
    }


async def plan_document_edits(
    before: DesignerExecutionGraph,
    candidate: DesignerExecutionGraph,
    documents: dict[str, ChatDocument],
    message: str,
) -> tuple[DesignerExecutionGraph, list[dict[str, Any]]]:
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_API_ERROR,
        call_model_tool,
        model_text_or_raise,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

    context = document_edit_context(before, candidate, documents, message)
    try:
        result = await call_model_tool(
            prompt=json.dumps(context, ensure_ascii=False),
            system=_DOCUMENT_SYSTEM,
            optimize_for="quality",
            max_tokens=16384,
        )
        text = model_text_or_raise(result)
    except DesignerLlmError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DesignerLlmError(
            f"Chat model request failed while synchronizing workflow documents: {exc}",
            code=LLM_API_ERROR,
        ) from exc
    response = _extract_json_object(text)
    removed_shot_blocks = {doc["node_id"]: doc["removed_shot_blocks"] for doc in context["documents"]}
    edits = _document_replacements(response, documents, removed_shot_blocks)
    targets = {
        int(index): (target["node_id"], target["current_index"])
        for index, target in context["shot_reference_targets"].items()
    }
    fields = node_reference_fields(before, candidate, targets)
    graph = apply_node_reference_edits(candidate, fields, response.get("node_fields", {}))
    return graph, edits
