"""Build concrete orchestration plans from Beam search state."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from jiuwenswarm.symphony.orchestration.planning.input_matching import (
    missing_inputs,
    output_keys,
)
from jiuwenswarm.symphony.orchestration.planning.models import (
    ArtifactRef,
    OrchestrationPlan,
    PlanStep,
    SearchState,
)
from jiuwenswarm.symphony.orchestration.planning.utils import skill_id


def state_to_plan(
    *,
    state: SearchState,
    seed_skill_ids: tuple[str, ...],
    skill_by_id: dict[str, dict[str, Any]],
    can_feed_edges: list[dict[str, Any]],
) -> OrchestrationPlan:
    available: set[tuple[str, str]] = set()
    steps: list[PlanStep] = []
    all_missing: list[dict[str, Any]] = []
    produced: set[tuple[str, str]] = set()

    for current_skill_id in state.skill_ids:
        skill = skill_by_id[current_skill_id]
        missing = missing_inputs(skill, available)
        all_missing.extend({**item, "skill_id": current_skill_id} for item in missing)
        outputs = output_keys(skill)
        produced.update(outputs)
        available.update(outputs)
        steps.append(
            PlanStep(
                skill_id=current_skill_id,
                name=skill.get("name", current_skill_id),
                inputs=[
                    {"name": item.get("name"), "type": item.get("type")}
                    for item in skill.get("inputs", [])
                ],
                outputs=[
                    {"name": item.get("name"), "type": item.get("type")}
                    for item in skill.get("outputs", [])
                ],
                missing_inputs=missing,
            )
        )

    edges = [can_feed_edges[index] for index in state.edges]
    edge_confidence = (
        sum(float(edge.get("confidence") or 0.0) for edge in edges) / len(edges)
        if edges
        else 1.0
    )
    status = "ready" if not all_missing else "needs_input"
    return OrchestrationPlan(
        steps=steps,
        produced_artifacts=[
            ArtifactRef(name=name, type=type_, source="skill_output")
            for name, type_ in sorted(produced)
        ],
        missing_inputs=all_missing,
        can_feed_edges=[edge_plan_item(edge) for edge in edges],
        goal_score=sum(10.0 for item in state.skill_ids if item in seed_skill_ids),
        edge_confidence=edge_confidence,
        consumed_user_artifacts=0,
        status=status,
        reasons=list(state.score_reasons[:8]),
    )


def build_outgoing_edges(edges: list[dict[str, Any]]) -> dict[str, list[int]]:
    outgoing: dict[str, list[int]] = defaultdict(list)
    for index, edge in enumerate(edges):
        outgoing[skill_id(edge.get("source"))].append(index)
    return outgoing


def build_incoming_edges(edges: list[dict[str, Any]]) -> dict[str, list[int]]:
    incoming: dict[str, list[int]] = defaultdict(list)
    for index, edge in enumerate(edges):
        incoming[skill_id(edge.get("target"))].append(index)
    return incoming


def plan_stages(
    steps: list[PlanStep],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    step_by_id = {step.skill_id: step for step in steps}
    remaining = set(step_by_id)
    incoming: dict[str, set[str]] = {
        current_skill_id: set() for current_skill_id in remaining
    }
    for edge in edges:
        source_id = str(edge.get("source_id") or "")
        target_id = str(edge.get("target_id") or "")
        if source_id in remaining and target_id in remaining:
            incoming[target_id].add(source_id)

    stages = []
    completed: set[str] = set()
    while remaining:
        ready = sorted(
            current_skill_id
            for current_skill_id in remaining
            if incoming[current_skill_id] <= completed
        )
        if not ready:
            ready = sorted(remaining)
        stages.append(
            {
                "stage": len(stages) + 1,
                "skills": [
                    step_by_id[current_skill_id].to_dict() for current_skill_id in ready
                ],
            }
        )
        completed.update(ready)
        remaining.difference_update(ready)
    return stages


def edge_plan_item(edge: dict[str, Any]) -> dict[str, Any]:
    evidence = edge.get("evidence") or {}
    supporting_fields = evidence.get("supporting_fields") or {}
    item = {
        "source_id": skill_id(edge.get("source")),
        "target_id": skill_id(edge.get("target")),
        "confidence": edge.get("confidence"),
        "method": edge.get("method"),
        "port_mappings": supporting_fields.get("port_mappings", [])[:5],
        "source_outputs": supporting_fields.get("source_outputs", [])[:3],
        "target_inputs": supporting_fields.get("target_inputs", [])[:3],
        "reasons": evidence.get("reasons", [])[:3],
    }
    runtime_evidence = edge.get("runtime_evidence")
    if isinstance(runtime_evidence, dict):
        item.update(
            {
                "effective_score": edge.get("effective_score"),
                "runtime_weight": edge.get("runtime_weight"),
                "runtime_evidence": runtime_evidence,
            }
        )
    return item
