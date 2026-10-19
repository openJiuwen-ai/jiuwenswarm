"""Stable execution graph projection for Orchestration plan results."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts


def build_execution_graph(
    planning_result: dict[str, Any],
    artifacts: ScoreArtifacts,
) -> dict[str, Any]:
    """Project an orchestration result into stable nodes, edges, and gaps."""

    primary_plan = select_primary_plan(planning_result)
    steps = list(primary_plan.get("steps") or [])
    skill_ids = [
        str(step.get("skill_id") or "").strip()
        for step in steps
        if str(step.get("skill_id") or "").strip()
    ]
    skill_id_set = set(skill_ids)

    graph_edges = [
        _project_edge(edge)
        for edge in list(primary_plan.get("can_feed_edges") or [])
        if _edge_touches_selected_skills(edge, skill_id_set)
    ]
    if not graph_edges and skill_id_set:
        graph_edges = [
            _project_edge(edge)
            for edge in artifacts.graph.get("edges", [])
            if _edge_touches_selected_skills(edge, skill_id_set)
        ]

    missing_inputs = _collect_missing_inputs(planning_result, primary_plan)
    diagnostics = []
    decision = planning_result.get("decision")
    if isinstance(decision, dict):
        diagnostics.append({"source": "decision", **decision})
    runtime_scoring = planning_result.get("runtime_scoring_summary")
    if isinstance(runtime_scoring, dict):
        diagnostics.append({"source": "runtime_scoring", **runtime_scoring})

    return {
        "plan_id": planning_result.get("plan_id"),
        "dynamic_overlay_used": bool(planning_result.get("dynamic_overlay_used")),
        "nodes": [
            _project_skill(artifacts.skill_by_id[skill_id])
            for skill_id in skill_ids
            if skill_id in artifacts.skill_by_id
        ],
        "edges": graph_edges,
        "recommended_plans": list(planning_result.get("recommended_plans") or []),
        "missing_inputs": missing_inputs,
        "diagnostics": diagnostics,
    }


def select_primary_plan(planning_result: dict[str, Any]) -> dict[str, Any]:
    for key in ("recommended_plans", "plans"):
        plans = planning_result.get(key)
        if isinstance(plans, list):
            for plan in plans:
                if isinstance(plan, dict):
                    return plan
    return {}


def _project_skill(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": skill.get("id"),
        "name": skill.get("name"),
        "description": skill.get("description"),
        "inputs": list(skill.get("inputs") or []),
        "outputs": list(skill.get("outputs") or []),
    }


def _project_edge(edge: dict[str, Any]) -> dict[str, Any]:
    source = edge.get("source") or edge.get("source_id")
    target = edge.get("target") or edge.get("target_id")
    return {
        "source": source,
        "target": target,
        "type": edge.get("type") or edge.get("relation_type") or "can_feed",
        "confidence": edge.get("confidence"),
        "method": edge.get("method"),
        "evidence": edge.get("evidence") or edge.get("supporting_fields") or {},
    }


def _edge_touches_selected_skills(edge: dict[str, Any], skill_ids: set[str]) -> bool:
    source = str(edge.get("source") or edge.get("source_id") or "").strip()
    target = str(edge.get("target") or edge.get("target_id") or "").strip()
    return source in skill_ids and target in skill_ids


def _collect_missing_inputs(
    planning_result: dict[str, Any],
    primary_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for item in primary_plan.get("missing_inputs") or []:
        if isinstance(item, dict):
            missing.append(item)
    if not missing:
        for plan in planning_result.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            for item in plan.get("missing_inputs") or []:
                if isinstance(item, dict):
                    missing.append(item)
    return missing
