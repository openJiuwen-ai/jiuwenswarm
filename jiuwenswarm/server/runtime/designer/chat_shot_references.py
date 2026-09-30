# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Track prose references by shot identity while the workflow is edited."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterator

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerGraphValidationError,
    node_config,
    node_pipeline,
)

_SHOT_REFERENCE = re.compile(r"(?:\bshot\s*#?\s*|分镜\s*#?\s*|镜头\s*#?\s*)([1-9]\d*)(?![0-9A-Za-z_])", re.I)
_CONTINUITY_REFERENCE = re.compile(r"(?:承接|延续|接续|continu(?:ing|es?)\s+from)\s*(?:shot|分镜|镜头)\s*#?\s*([1-9]\d*)", re.I)


# Authored generation prose; derived copies and document bodies are synchronized later.
NODE_PROSE_FIELDS = (
    "prompt", "shot_action", "shot_title", "camera", "cast_actions", "blocking",
    "start_state", "end_state", "pose_holds", "scene_specs", "spatial_lock",
    "costume_lock", "continuity_lock", "relationship_lock", "director_task",
)


def referenced_shot_indices(text: str) -> set[int]:
    """Read explicit shot references, including numbers next to Chinese prose."""
    return {int(match[1]) for match in _SHOT_REFERENCE.finditer(text)}


def map_shot_references(text: str, targets: dict[int, tuple[str, int | None]]) -> str:
    """Map explicit references by stable identity for the prose editor's input."""
    def replace(match: re.Match[str]) -> str:
        target = targets.get(int(match[1]))
        if target is None:
            return match[0]
        node_id, index = target
        if index is None:
            return f"[[REMOVED_SHOT:{node_id}]]"
        return match[0][:-len(match[1])] + str(index)

    return _SHOT_REFERENCE.sub(replace, text)


def redirects_removed_continuity(
    old_text: str,
    new_text: str,
    old_targets: dict[int, str],
    new_targets: dict[int, str],
) -> bool:
    removed = set(old_targets.values()) - set(new_targets.values())
    old_refs = {old_targets.get(int(m[1])) for m in _CONTINUITY_REFERENCE.finditer(old_text)}
    new_refs = {new_targets.get(int(m[1])) for m in _CONTINUITY_REFERENCE.finditer(new_text)}
    return bool(old_refs & removed and new_refs - old_refs)

@dataclass(frozen=True)
class NodeProseField:
    node_id: str
    path: tuple[str | int, ...]
    original: str

    @property
    def key(self) -> str:
        identity = json.dumps([self.node_id, self.path], ensure_ascii=False)
        return hashlib.sha256(identity.encode()).hexdigest()[:12]


def _text_fields(value: Any, path: tuple[str | int, ...]) -> Iterator[tuple[tuple[str | int, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _text_fields(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _text_fields(item, (*path, index))


def node_prose_config(node: dict[str, Any]) -> dict[str, Any]:
    """Read legacy generate.prompt through the same canonical prompt field."""
    config = dict(node_config(node))
    if not config.get("prompt"):
        config["prompt"] = (config.get("generate") or {}).get("prompt", "")
    return config


def node_reference_fields(
    before: DesignerExecutionGraph,
    candidate: DesignerExecutionGraph,
    targets: dict[int, tuple[str, int | None]],
) -> list[NodeProseField]:
    """Select only prose whose original shot references are affected by topology."""
    changed_indices = {index for index, (_, current) in targets.items() if current != index}
    old_nodes = {node["id"]: node for node in before["nodes"]}
    fields = []
    for node in candidate["nodes"]:
        if node["id"] not in old_nodes or node_pipeline(node) in {"brief", "storyboard"}:
            continue
        old_config = node_prose_config(old_nodes[node["id"]])
        config = node_prose_config(node)
        for name in NODE_PROSE_FIELDS:
            original = json.dumps(old_config.get(name), ensure_ascii=False)
            if not referenced_shot_indices(original) & changed_indices:
                continue
            for path, text in _text_fields(config.get(name), (name,)):
                if referenced_shot_indices(text):
                    source = old_config[name] if isinstance(old_config[name], str) else original
                    fields.append(NodeProseField(
                        node["id"], path, map_shot_references(source, targets),
                    ))
    return fields


def apply_node_reference_edits(
    candidate: DesignerExecutionGraph,
    fields: list[NodeProseField],
    decisions: Any,
) -> DesignerExecutionGraph:
    """Resolve model strings through server-owned paths, on a detached candidate."""
    if not isinstance(decisions, dict) or decisions.keys() != {field.key for field in fields}:
        raise DesignerGraphValidationError("Document editor must review every requested node prose field")
    graph = deepcopy(candidate)
    nodes = {node["id"]: node for node in graph["nodes"]}
    for field in fields:
        text = decisions[field.key]
        if not isinstance(text, str) or "[[REMOVED_SHOT:" in text:
            raise DesignerGraphValidationError("Node prose must contain complete text without removed-shot markers")
        container = node_config(nodes[field.node_id])
        for part in field.path[:-1]:
            container = container[part]
        container[field.path[-1]] = text
    return graph
