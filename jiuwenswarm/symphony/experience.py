"""JiuwenSwarm adapters for Symphony execution evidence and packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal

import yaml  # type: ignore[import-untyped]
import openjiuwen.symphony as core_symphony  # type: ignore[import-untyped]
from openjiuwen.harness.rails.evolution import (  # type: ignore[import-untyped]
    CapabilityIdentity,
)

from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir


def _build_graph_evolution_rail(
    graph_dir: Path,
    *,
    capture_mode: Literal["agent", "team"],
    model: Any,
    channel_id: Callable[[], str | None],
    trajectory_span_processor: Any = None,
) -> Any:
    """Share evidence wiring while callers retain their model and route ownership."""
    from openjiuwen.extensions.observability.demand import get_trajectory_span_processor
    from openjiuwen.harness.rails.evolution import (
        SymphonyGraphEvolutionRail,
        TeamSymphonyGraphEvolutionRail,
    )
    from jiuwenswarm.symphony.service import get_swarm_symphony_service

    service = get_swarm_symphony_service()
    runtime = service.runtime()
    mode = capture_mode

    async def submit_evolution(
        planned_graph: dict[str, Any] | None,
        execution_graph: dict[str, Any],
        *,
        session_id: str,
        capture_mode: str,
    ) -> None:
        await service.submit_evolution_and_notify(
            planned_graph,
            execution_graph,
            session_id=session_id,
            capture_mode=mode,
            channel_id=channel_id(),
        )

    rail = (
        TeamSymphonyGraphEvolutionRail if mode == "team" else SymphonyGraphEvolutionRail
    )
    return rail(
        trajectory_span_processor=trajectory_span_processor
        or get_trajectory_span_processor(),
        graph_snapshot_provider=runtime.capture_graph_snapshot,
        capability_snapshot_provider=PublishedCapabilitySnapshotProvider(graph_dir),
        edge_evaluator_llm=model,
        submit_evolution=submit_evolution,
    )


def _parse_recipe_reference(recipe_id: Any, raw_version: Any) -> tuple[str, int]:
    """Normalize a candidate reference, preserving the Host error reasons."""
    recipe_id = str(recipe_id or "").strip()
    if isinstance(raw_version, bool) or not isinstance(raw_version, (int, str)):
        raise ValueError("invalid_recipe_version")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_recipe_version") from exc
    if not recipe_id or version < 1:
        raise ValueError("invalid_recipe")
    return recipe_id, version


class PublishedCapabilitySnapshotProvider:
    """Freeze identities from the currently published Symphony graph version."""

    def __init__(self, graph_dir: str | Path) -> None:
        self._graph_dir = Path(graph_dir)

    def snapshot_capabilities(self) -> tuple[CapabilityIdentity, ...]:
        artifact_dir = resolve_graph_artifact_dir(self._graph_dir)
        graph = _read_json_object(artifact_dir / "graph.json")
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
            identities.append(
                CapabilityIdentity(
                    capability_id=capability_id,
                    capability_type=capability_type,
                    capability_name=str(raw.get("name") or capability_id),
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
