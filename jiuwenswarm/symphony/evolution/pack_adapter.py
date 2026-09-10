"""Events to RecipeEvidence adapter for skill pack distillation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openjiuwen.symphony.flow.models import RecipeEvidence

from jiuwenswarm.symphony.evolution.models import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    skill_id,
)
from jiuwenswarm.symphony.evolution.store import read_events


def events_to_evidence(graph_dir: Path) -> list[RecipeEvidence]:
    """Convert plan.outcome events to RecipeEvidence for distillation.

    Args:
        graph_dir: Graph directory containing evolution/events.jsonl

    Returns:
        List of RecipeEvidence objects
    """
    events = read_events(graph_dir)
    evidence_list = []

    for event in events:
        if event.get("event_type") != "plan.outcome":
            continue

        outcome = event.get("outcome")
        if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE):
            # Skip needs_input and no_plan
            continue

        selected_edges = event.get("selected_edges") or []

        # Build set of failed edge keys from selected_edges with failed=True marker
        # record_plan_outcome marks failed edges with "failed": True field,
        # not as a top-level "failed_edges" list
        failed_edge_keys = set()
        for edge in selected_edges:
            if edge.get("failed") is True:
                source = skill_id(edge.get("source_id"))
                target = skill_id(edge.get("target_id"))
                if source and target:
                    failed_edge_keys.add((source, target))

        # Convert edges with success metadata
        edges = []
        node_ids = set()

        for edge in selected_edges:
            source = skill_id(edge.get("source_id"))
            target = skill_id(edge.get("target_id"))
            relation = edge.get("relation_type", "can_feed")

            if not source or not target:
                continue

            is_failed = (source, target) in failed_edge_keys
            edges.append({
                "source": source,
                "target": target,
                "relation": relation,
                "metadata": {"success": not is_failed},
            })

            node_ids.add(source)
            node_ids.add(target)

        # Build nodes dict
        nodes = {
            nid: {"label": nid, "metadata": {}}
            for nid in node_ids
        }

        # Create RecipeEvidence
        evidence = RecipeEvidence(
            trace_id=event.get("event_id", ""),
            query=event.get("query", ""),
            outcome=outcome,
            graph={
                "nodes": nodes,
                "edges": edges,
                "id": event.get("plan_id", ""),
                "type": "execution_graph",
                "directed": True,
            },
        )
        evidence_list.append(evidence)

    return evidence_list
