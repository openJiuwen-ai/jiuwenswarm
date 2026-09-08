"""JiuwenSwarm adapters for Symphony execution evidence and packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
import openjiuwen.symphony as core_symphony  # type: ignore[import-untyped]
from openjiuwen.harness.rails.evolution import (  # type: ignore[import-untyped]
    CapabilityIdentity,
)

from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir


class PublishedCapabilitySnapshotProvider:
    """Freeze identities from the currently published Symphony graph version."""

    def __init__(self, graph_dir: str | Path) -> None:
        self._graph_dir = Path(graph_dir)

    def snapshot_capabilities(self) -> tuple[CapabilityIdentity, ...]:
        artifact_dir = resolve_graph_artifact_dir(self._graph_dir)
        graph = _read_json_object(artifact_dir / "graph.json")
        fingerprint = _read_json_object(artifact_dir / "fingerprint.json")
        fingerprint_items = fingerprint.get("fingerprints")
        by_identity = (
            {
                (
                    str(item.get("capability_type") or ""),
                    str(item.get("capability_id") or ""),
                ): item
                for item in fingerprint_items
                if isinstance(item, dict)
            }
            if isinstance(fingerprint_items, list)
            else {}
        )
        raw_hashes = graph.get("capability_hashes")
        hashes = raw_hashes if isinstance(raw_hashes, dict) else {}
        identities: list[CapabilityIdentity] = []
        capabilities = graph.get("capabilities")
        for raw in capabilities if isinstance(capabilities, list) else ():
            if not isinstance(raw, dict):
                continue
            capability_type = str(raw.get("capability_type") or raw.get("type") or "")
            capability_id = str(raw.get("capability_id") or raw.get("id") or "")
            if (
                capability_type not in {"skill", "tool", "subagent"}
                or not capability_id
            ):
                continue
            details = by_identity.get((capability_type, capability_id), raw)
            content_hash = str(
                details.get("content_hash")
                or hashes.get(f"{capability_type}:{capability_id}")
                or ""
            )
            if not content_hash:
                continue
            identities.append(
                CapabilityIdentity(
                    capability_id=capability_id,
                    capability_type=capability_type,
                    capability_name=str(
                        details.get("name") or raw.get("name") or capability_id
                    ),
                    version=str(
                        details.get("version") or raw.get("version") or "1.0.0"
                    ),
                    content_hash=content_hash,
                    input_ports=_port_names(details.get("inputs", raw.get("inputs"))),
                    output_ports=_port_names(
                        details.get("outputs", raw.get("outputs"))
                    ),
                )
            )
        return tuple(
            sorted(
                identities, key=lambda item: (item.capability_type, item.capability_id)
            )
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _port_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    names: list[str] = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        normalized = str(name or "").strip()
        if normalized and normalized not in names:
            names.append(normalized)
    return tuple(names)


class JiuwenSwarmSkillAdapter:
    """Render the Core Skill package with JiuwenSwarm-compatible frontmatter."""

    @staticmethod
    def render(package: dict[str, Any], artifact_dir: str | Path) -> list[Path]:
        outputs = core_symphony.flow.SkillAdapter.render(package, artifact_dir)
        skill_md = Path(artifact_dir) / "SKILL.md"
        body = skill_md.read_text(encoding="utf-8")
        materials = package.get("materials")
        materials = materials if isinstance(materials, dict) else {}
        recipe = materials.get("recipe")
        recipe = recipe if isinstance(recipe, dict) else {}
        applicability = recipe.get("applicability")
        applicability = applicability if isinstance(applicability, dict) else {}
        name = str(package.get("meta_name") or "symphony-combination").strip()
        description = str(
            applicability.get("task_description")
            or "由 Symphony 成功执行经验生成的组合 Skill"
        ).strip()
        frontmatter = yaml.safe_dump(
            {
                "name": name,
                "description": description,
                "kind": "swarm-skill",
            },
            allow_unicode=True,
            sort_keys=True,
        ).strip()
        skill_md.write_text(
            f"---\n{frontmatter}\n---\n\n{body}",
            encoding="utf-8",
        )
        return outputs


__all__ = ["JiuwenSwarmSkillAdapter", "PublishedCapabilitySnapshotProvider"]
