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

_SHOT_REFERENCE = re.compile(
    r"(?:\bshot\s*#?\s*|分镜\s*#?\s*|镜头\s*#?\s*)([1-9]\d*)(?![0-9A-Za-z_])",
    re.I,
)
_CONTINUITY_REFERENCE = re.compile(
    r"(?:承接|延续|接续|continu(?:ing|es?)\s+from)\s*(?:shot|分镜|镜头)\s*#?\s*([1-9]\d*)",
    re.I,
)
# Duration units after a captured number — not a shot index (e.g. 每个镜头 5 秒).
# Chinese units must not require \b: 秒的节奏 has no word boundary after 秒.
_DURATION_AFTER = re.compile(
    r"^\s*(?:"
    r"秒钟|秒|"
    r"seconds?\b|secs?\b|s\b|"
    r"分钟|"
    r"minutes?\b|mins?\b|min\b"
    r")",
    re.I,
)
# Distributive quantifiers immediately before 镜头/shot/分镜.
_DISTRIBUTIVE_BEFORE = re.compile(
    r"(?:每个|每一|各|每|each|every|per)\s*$",
    re.I,
)
_REMOVED_SHOT_MARKER = re.compile(r"\[\[REMOVED_SHOT:[^\]]+\]\]")
_SHOT_HEADING_TITLE = re.compile(
    r"(?:shot|分镜|镜头)\s*#?\s*([1-9]\d*)\b.*",
    re.I,
)


# Authored generation prose; derived copies and document bodies are synchronized later.
NODE_PROSE_FIELDS = (
    "prompt",
    "shot_action",
    "shot_title",
    "camera",
    "cast_actions",
    "blocking",
    "start_state",
    "end_state",
    "pose_holds",
    "scene_specs",
    "spatial_lock",
    "costume_lock",
    "continuity_lock",
    "relationship_lock",
    "director_task",
)


def _is_shot_index_reference(text: str, match: re.Match[str]) -> bool:
    """True when the match names a concrete shot index, not a duration/distributive idiom."""
    after = text[match.end(1) :]
    if _DURATION_AFTER.match(after):
        return False
    before = text[: match.start()]
    if _DISTRIBUTIVE_BEFORE.search(before):
        return False
    return True


def iter_shot_reference_matches(text: str) -> Iterator[re.Match[str]]:
    """Yield `_SHOT_REFERENCE` matches that are real shot indices."""
    for match in _SHOT_REFERENCE.finditer(text or ""):
        if _is_shot_index_reference(text, match):
            yield match


def referenced_shot_indices(text: str) -> set[int]:
    """Read explicit shot references, including numbers next to Chinese prose."""
    return {int(match[1]) for match in iter_shot_reference_matches(text or "")}


def map_shot_references(text: str, targets: dict[int, tuple[str, int | None]]) -> str:
    """Map explicit references by stable identity for the prose editor's input."""
    source = text or ""

    def replace(match: re.Match[str]) -> str:
        if not _is_shot_index_reference(source, match):
            return match[0]
        target = targets.get(int(match[1]))
        if target is None:
            return match[0]
        node_id, index = target
        if index is None:
            return f"[[REMOVED_SHOT:{node_id}]]"
        return match[0][: -len(match[1])] + str(index)

    return _SHOT_REFERENCE.sub(replace, source)


def missing_shot_tombstones(
    text: str,
    live_indices: set[int],
    existing: dict[int, tuple[str, int | None]] | None = None,
) -> dict[int, tuple[str, int | None]]:
    """Map prose indices absent from the live graph to REMOVED_SHOT:missing:{n} markers."""
    targets = dict(existing or {})
    for index in referenced_shot_indices(text):
        if index in live_indices:
            continue
        if index in targets:
            node_id, current = targets[index]
            if current is None:
                continue
            # Live mapping already points elsewhere; leave it for renumber paths.
            continue
        targets[index] = (f"missing:{index}", None)
    return targets


def _collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_film_wide_note_line(line: str) -> bool:
    """True for blank / duration / BGM-style notes that must not die with a shot section."""
    stripped = line.strip()
    if not stripped:
        return True
    if re.search(r"(?i)\b(?:bgm|timeline|duration)\b|节奏", stripped):
        return True
    # Duration idioms look like shot refs to the raw regex but are filtered out.
    if _SHOT_REFERENCE.search(stripped) and not referenced_shot_indices(stripped):
        return True
    return False


def _drop_missing_shot_sections(text: str, missing: set[int]) -> str:
    """Delete Markdown sections whose heading owns a missing shot index."""
    if not missing:
        return text
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    skip_level = 0
    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        bold = (
            stripped.startswith("**")
            and stripped.endswith("**")
            and len(stripped) > 4
            and "\n" not in stripped
        )
        title = ""
        level = 0
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().strip("#").strip()
        elif bold:
            level = 2
            title = stripped[2:-2].strip()
        if title:
            owned = _SHOT_HEADING_TITLE.fullmatch(title)
            if owned is not None and int(owned.group(1)) in missing:
                skipping = True
                skip_level = level or 2
                continue
            if skipping and (
                (heading and level <= skip_level) or (bold and level <= skip_level)
            ):
                skipping = False
        if skipping and _is_film_wide_note_line(line):
            # Film-level notes after a deleted shot heading are not shot body.
            skipping = False
        if skipping:
            continue
        kept.append(line)
    return "".join(kept)


def _neutralize_inline_missing_refs(text: str, missing: set[int]) -> str:
    """Remove inline shot refs / continuity claims for missing indices without redirecting."""
    if not missing:
        return text

    def replace_shot(match: re.Match[str]) -> str:
        if not _is_shot_index_reference(text, match):
            return match[0]
        if int(match[1]) in missing:
            return ""
        return match[0]

    out = _SHOT_REFERENCE.sub(replace_shot, text)

    def replace_continuity(match: re.Match[str]) -> str:
        if int(match[1]) in missing:
            return ""
        return match[0]

    out = _CONTINUITY_REFERENCE.sub(replace_continuity, out)
    # Clean orphaned chain arrows left after dropping a missing index.
    out = re.sub(r"[→\-–—]+(?:\s*[→\-–—]+)+", "→", out)
    out = re.sub(r"(?:^|[\s,，、])[→\-–—]+", " ", out)
    out = re.sub(r"[→\-–—]+(?:\s|$)", " ", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def scrub_missing_shot_references(text: str, live_indices: set[int]) -> str:
    """Drop missing-shot sections/refs and tombstone markers before fail-closed validate."""
    source = text or ""
    missing = referenced_shot_indices(source) - set(live_indices)
    if not missing and "[[REMOVED_SHOT:" not in source:
        return source
    out = _drop_missing_shot_sections(source, missing)
    # Tombstone then strip so LLM-left markers and prior-canvas gaps share one path.
    targets = missing_shot_tombstones(out, set(live_indices))
    if targets:
        out = map_shot_references(out, targets)
    out = _REMOVED_SHOT_MARKER.sub("", out)
    out = _neutralize_inline_missing_refs(out, missing)
    # Second pass: any remaining true missing refs (e.g. rebuilt after section edits).
    still_missing = referenced_shot_indices(out) - set(live_indices)
    if still_missing:
        out = _neutralize_inline_missing_refs(out, still_missing)
        out = _drop_missing_shot_sections(out, still_missing)
    return _collapse_blank_lines(out)


def redirects_removed_continuity(
    old_text: str,
    new_text: str,
    old_targets: dict[int, str],
    new_targets: dict[int, str],
) -> bool:
    removed = set(old_targets.values()) - set(new_targets.values())
    old_refs = {
        old_targets.get(int(m[1])) for m in _CONTINUITY_REFERENCE.finditer(old_text)
    }
    new_refs = {
        new_targets.get(int(m[1])) for m in _CONTINUITY_REFERENCE.finditer(new_text)
    }
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


def _text_fields(
    value: Any, path: tuple[str | int, ...]
) -> Iterator[tuple[tuple[str | int, ...], str]]:
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
    changed_indices = {
        index for index, (_, current) in targets.items() if current != index
    }
    old_nodes = {node["id"]: node for node in before["nodes"]}
    fields = []
    for node in candidate["nodes"]:
        if node["id"] not in old_nodes or node_pipeline(node) in {
            "brief",
            "storyboard",
        }:
            continue
        old_config = node_prose_config(old_nodes[node["id"]])
        config = node_prose_config(node)
        for name in NODE_PROSE_FIELDS:
            original = json.dumps(old_config.get(name), ensure_ascii=False)
            if not referenced_shot_indices(original) & changed_indices:
                continue
            for path, text in _text_fields(config.get(name), (name,)):
                if referenced_shot_indices(text):
                    source = (
                        old_config[name]
                        if isinstance(old_config[name], str)
                        else original
                    )
                    fields.append(
                        NodeProseField(
                            node["id"],
                            path,
                            map_shot_references(source, targets),
                        )
                    )
    return fields


def apply_node_reference_edits(
    candidate: DesignerExecutionGraph,
    fields: list[NodeProseField],
    decisions: Any,
) -> DesignerExecutionGraph:
    """Resolve model strings through server-owned paths, on a detached candidate."""
    if not isinstance(decisions, dict) or decisions.keys() != {
        field.key for field in fields
    }:
        raise DesignerGraphValidationError(
            "Document editor must review every requested node prose field"
        )
    graph = deepcopy(candidate)
    nodes = {node["id"]: node for node in graph["nodes"]}
    for field in fields:
        text = decisions[field.key]
        if not isinstance(text, str) or "[[REMOVED_SHOT:" in text:
            raise DesignerGraphValidationError(
                "Node prose must contain complete text without removed-shot markers"
            )
        container = node_config(nodes[field.node_id])
        for part in field.path[:-1]:
            container = container[part]
        container[field.path[-1]] = text
    return graph
