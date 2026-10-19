"""Data models for orchestration planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ArtifactRef:
    """A normalized runtime artifact available to the orchestrator."""

    name: str
    type: str = "unknown"
    source: str = "user_query"

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.type)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "source": self.source}


@dataclass(frozen=True)
class PlanStep:
    """One Skill call in a candidate orchestration plan."""

    skill_id: str
    name: str
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    missing_inputs: list[dict[str, Any]] = field(default_factory=list)
    filled_inputs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "missing_inputs": self.missing_inputs,
            "filled_inputs": self.filled_inputs,
        }


@dataclass(frozen=True)
class OrchestrationPlan:
    """A candidate Skill orchestration plan."""

    steps: list[PlanStep]
    produced_artifacts: list[ArtifactRef]
    missing_inputs: list[dict[str, Any]]
    can_feed_edges: list[dict[str, Any]]
    goal_score: float
    edge_confidence: float
    consumed_user_artifacts: int
    status: str
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        from jiuwenswarm.symphony.orchestration.planning.plan_builder import (
            plan_stages,
        )

        return {
            "status": self.status,
            "goal_score": round(self.goal_score, 3),
            "edge_confidence": round(self.edge_confidence, 3),
            "consumed_user_artifacts": self.consumed_user_artifacts,
            "stages": plan_stages(self.steps, self.can_feed_edges),
            "steps": [
                {"step": index + 1, **step.to_dict()}
                for index, step in enumerate(self.steps)
            ],
            "produced_artifacts": [
                artifact.to_dict() for artifact in self.produced_artifacts
            ],
            "missing_inputs": self.missing_inputs,
            "can_feed_edges": self.can_feed_edges,
            "reasons": self.reasons,
        }


@dataclass(frozen=True)
class SearchState:
    """Internal forward-search state."""

    skill_ids: tuple[str, ...]
    edges: tuple[int, ...]
    score_reasons: tuple[str, ...] = ()
