"""Load Score artifacts for Skill orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from jiuwenswarm.symphony.fingerprint.utils import normalize_slug
from jiuwenswarm.symphony.score_storage import resolve_score_artifact_dir


@dataclass(frozen=True)
class ScoreArtifacts:
    """Offline Score artifacts needed by orchestration."""

    score_dir: Path
    manifest: dict[str, Any]
    skills: list[dict[str, Any]]
    graph: dict[str, Any]
    lookup: dict[str, Any]
    io_name_vocab: dict[str, Any] | None = None

    @property
    def skill_by_id(self) -> dict[str, dict[str, Any]]:
        return {skill["id"]: skill for skill in self.skills if skill.get("id")}


def load_score_artifacts(score_dir: str | Path) -> ScoreArtifacts:
    """Load Score artifacts through score_manifest.json."""

    root = resolve_score_artifact_dir(score_dir)
    manifest = _read_json(root / "score_manifest.json")
    artifacts = manifest.get("artifacts", {})
    skills_payload = _read_json(root / artifacts.get("skills", "skills.json"))
    graph = _read_json(root / artifacts.get("graph", "skill_graph.json"))
    lookup = _read_json(root / artifacts.get("score_lookup", "score_lookup.json"))
    io_name_vocab_path = root / artifacts.get("io_name_vocab", "io_name_vocab.json")
    return ScoreArtifacts(
        score_dir=root,
        manifest=manifest,
        skills=skills_payload.get("skills", []),
        graph=graph,
        lookup=lookup,
        io_name_vocab=_read_optional_json(io_name_vocab_path),
    )


def filter_disabled_score_artifacts(
    artifacts: ScoreArtifacts,
    disabled_skill_names: Sequence[str] | None,
) -> ScoreArtifacts:
    """Return Score artifacts with disabled skills removed from runtime views."""

    disabled_refs = _skill_ref_set(disabled_skill_names or [])
    if not disabled_refs:
        return artifacts

    disabled_skill_ids = {
        str(skill.get("id") or "")
        for skill in artifacts.skills
        if _skill_matches_refs(skill, disabled_refs)
    }
    disabled_skill_ids = {item for item in disabled_skill_ids if item}
    if not disabled_skill_ids:
        return artifacts

    all_disabled_refs = set(disabled_refs)
    all_disabled_refs.update(_skill_ref_set(disabled_skill_ids))
    skills = [
        skill
        for skill in artifacts.skills
        if not _skill_ref_is_disabled(skill.get("id"), all_disabled_refs)
    ]
    graph = _filter_graph(artifacts.graph, all_disabled_refs)
    lookup = _filter_lookup_payload(artifacts.lookup, all_disabled_refs)
    return ScoreArtifacts(
        score_dir=artifacts.score_dir,
        manifest=artifacts.manifest,
        skills=skills,
        graph=graph,
        lookup=lookup if isinstance(lookup, dict) else {},
        io_name_vocab=artifacts.io_name_vocab,
    )


def _filter_graph(
    graph: dict[str, Any],
    disabled_refs: set[str],
) -> dict[str, Any]:
    output = dict(graph)
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        output["nodes"] = [
            node
            for node in nodes
            if not _graph_node_is_disabled(node, disabled_refs)
        ]
    edges = graph.get("edges")
    if isinstance(edges, list):
        output["edges"] = [
            edge
            for edge in edges
            if not _graph_edge_is_disabled(edge, disabled_refs)
        ]
    return output


def _filter_lookup_payload(value: Any, disabled_refs: set[str]) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if _skill_ref_is_disabled(key, disabled_refs):
                continue
            filtered = _filter_lookup_payload(item, disabled_refs)
            if filtered in ({}, []):
                continue
            output[key] = filtered
        return output
    if isinstance(value, list):
        output = []
        for item in value:
            if _skill_ref_is_disabled(item, disabled_refs):
                continue
            filtered = _filter_lookup_payload(item, disabled_refs)
            if filtered in ({}, []):
                continue
            output.append(filtered)
        return output
    if _skill_ref_is_disabled(value, disabled_refs):
        return []
    return value


def _graph_edge_is_disabled(edge: Any, disabled_refs: set[str]) -> bool:
    if not isinstance(edge, dict):
        return False
    return (
        _skill_ref_is_disabled(edge.get("source") or edge.get("source_id"), disabled_refs)
        or _skill_ref_is_disabled(edge.get("target") or edge.get("target_id"), disabled_refs)
    )


def _graph_node_is_disabled(node: Any, disabled_refs: set[str]) -> bool:
    if not isinstance(node, dict):
        return False
    properties = node.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    values = (
        node.get("id"),
        node.get("node_id"),
        node.get("skill_id"),
        node.get("label"),
        node.get("name"),
        properties.get("skill_id"),
        properties.get("id"),
        properties.get("name"),
    )
    return any(_skill_ref_is_disabled(value, disabled_refs) for value in values)


def _skill_matches_refs(skill: dict[str, Any], disabled_refs: set[str]) -> bool:
    return (
        _skill_ref_is_disabled(skill.get("id"), disabled_refs)
        or _skill_ref_is_disabled(skill.get("name"), disabled_refs)
    )


def _skill_ref_is_disabled(value: Any, disabled_refs: set[str]) -> bool:
    return bool(_skill_ref_set([value]) & disabled_refs)


def _skill_ref_set(values: Sequence[Any]) -> set[str]:
    refs: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        unprefixed = text.removeprefix("skill:")
        for candidate in (text, unprefixed):
            if candidate:
                refs.add(candidate)
                slug = normalize_slug(candidate)
                if slug:
                    refs.add(slug)
    return refs


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing score artifact: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
