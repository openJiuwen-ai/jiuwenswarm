"""JiuwenSwarm adapters for Symphony execution evidence and packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal

import yaml  # type: ignore[import-untyped]
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


class SkillPackNotInstallableError(ValueError):
    """A reviewed recipe cannot be represented by an SDD-0010 SkillPack."""


class JiuwenSwarmSkillAdapter:
    """Render a reviewed Core recipe as an SDD-0010 SkillPack root."""

    @staticmethod
    def render(package: dict[str, Any], artifact_dir: str | Path) -> list[Path]:
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
        structure = recipe.get("combination_structure")
        structure = structure if isinstance(structure, dict) else {}
        members, edges = _skill_chain(structure)
        narrative = str(recipe.get("execution_narrative") or "").strip()
        if not narrative:
            raise SkillPackNotInstallableError("execution_narrative is empty")

        frontmatter = yaml.safe_dump(
            {
                "name": name,
                "kind": "skillpack",
                "description": description,
                "skills": members,
            },
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        trigger = str(applicability.get("trigger_conditions") or "").strip()
        when_to_use = description
        if trigger and trigger != description:
            when_to_use = f"{description}\n\n触发条件：{trigger}"
        workflow_graph = {
            "graph": {
                "id": f"{name}-workflow",
                "type": "skillpack_workflow",
                "label": description,
                "directed": True,
                "nodes": {
                    member: {
                        "label": member,
                        "metadata": {"skill": member},
                    }
                    for member in members
                },
                "edges": edges,
            }
        }
        graph_json = json.dumps(workflow_graph, ensure_ascii=False, indent=2)
        body = "\n".join(
            [
                f"# {name}",
                "",
                "## When to use",
                "",
                when_to_use,
                "",
                "## Do not use",
                "",
                "仅需要其中一个成员 Skill 的单项能力时，不使用本技能包。",
                "",
                "## Required inputs",
                "",
                "执行前确认用户目标及各成员 Skill 要求的必要输入；缺失时先询问用户。",
                "",
                "## Side effects and confirmation",
                "",
                "遵循各成员 Skill 的权限与确认要求，不扩大其工具权限或副作用范围。",
                "",
                "## Included Skills",
                "",
                *[f"- `{member}`" for member in members],
                "",
                "## Execution Process",
                "",
                narrative,
                "",
                "## Failure handling",
                "",
                "成员执行失败时停止依赖该结果的后续步骤，并返回已有结果与明确失败原因。",
                "",
                "## Final output",
                "",
                "返回组合流程的最终结果，并说明任何失败、跳过或不完整部分。",
                "",
                "## Workflow Graph",
                "",
                "```json",
                graph_json,
                "```",
                "",
            ]
        )
        artifact_root = Path(artifact_dir)
        artifact_root.mkdir(parents=True, exist_ok=True)
        skill_md = artifact_root / "SKILL.md"
        skill_md.write_text(
            f"---\n{frontmatter}\n---\n\n{body}",
            encoding="utf-8",
        )
        return [skill_md]


def _skill_chain(
    structure: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    """Validate an experience recipe's Skill chain for SkillPack rendering.

    Private helper for ``JiuwenSwarmSkillAdapter.render`` that consumes the
    Core recipe's ``combination_structure``; it does not generate experiences.
    Return member IDs in execution order and normalized ``can_feed`` edges
    for the SkillPack's ``skills`` frontmatter and display-only Workflow Graph.
    Raise ``SkillPackNotInstallableError`` for malformed structures, non-Skill
    nodes, or graphs that are not a single linear chain (e.g. branches or cycles).
    """
    nodes = structure.get("nodes")
    raw_edges = structure.get("edges")
    if not isinstance(nodes, dict) or len(nodes) < 2 or not isinstance(raw_edges, list):
        raise SkillPackNotInstallableError("recipe is not a Skill chain")

    member_ids = {str(node_id) for node_id in nodes}
    indegree = {member: 0 for member in member_ids}
    adjacency: dict[str, str] = {}
    edges: list[dict[str, str]] = []
    for node_id, node in nodes.items():
        metadata = node.get("metadata") if isinstance(node, dict) else None
        capability_type = (
            str(metadata.get("capability_type") or "").strip().casefold()
            if isinstance(metadata, dict)
            else ""
        )
        if capability_type != "skill":
            raise SkillPackNotInstallableError(f"capability {node_id!s} is not a Skill")
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            raise SkillPackNotInstallableError("recipe edge is invalid")
        source = str(raw_edge.get("source") or "")
        target = str(raw_edge.get("target") or "")
        relation = str(raw_edge.get("relation") or "can_feed")
        if source not in member_ids or target not in member_ids or source == target:
            raise SkillPackNotInstallableError("recipe is not a simple Skill chain")
        if relation != "can_feed" or source in adjacency:
            raise SkillPackNotInstallableError("recipe is not a simple Skill chain")
        adjacency[source] = target
        indegree[target] += 1
        edges.append({"source": source, "target": target, "relation": "can_feed"})

    starts = [member for member, degree in indegree.items() if degree == 0]
    if len(edges) != len(member_ids) - 1 or len(starts) != 1:
        raise SkillPackNotInstallableError("recipe is not a simple Skill chain")
    ordered: list[str] = []
    current = starts[0]
    while current not in ordered:
        ordered.append(current)
        if current not in adjacency:
            break
        current = adjacency[current]
    if len(ordered) != len(member_ids):
        raise SkillPackNotInstallableError("recipe is not a simple Skill chain")
    return ordered, edges


__all__ = [
    "JiuwenSwarmSkillAdapter",
    "PublishedCapabilitySnapshotProvider",
    "SkillPackNotInstallableError",
]
