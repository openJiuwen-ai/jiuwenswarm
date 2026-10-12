"""Core data models for the Symphony prompt optimizer.

``TaskCase`` / ``TaskSpec`` are jiuwenswarm's own, product-facing task shape
(flat input/expected strings) — kept stable here since the ``optimize_prompt``
tool, tests, and RPC layer all speak this shape. Everything else (candidates,
executions, rewards, results, memory records) is re-exported directly from
``openjiuwen.dev_tools.tune.optimizer.prompt_search`` — the RLAF-P algorithm
itself lives there now; see docs/en/PromptOptimization.md. ``to_prompt_task_spec``
is the one conversion point between the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openjiuwen.dev_tools.tune.optimizer.prompt_search.models import (
    CaseResult,
    Evaluation,
    Execution,
    IterationRecord,
    OptimizationResult,
    PromptCandidate,
    PromptRecord,
    PromptTaskCase,
    PromptTaskSpec,
    RewardBreakdown,
)


@dataclass
class TaskCase:
    """One input/expectation pair used to exercise a candidate prompt."""

    input: str
    expected: str = ""
    hidden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"input": self.input, "expected": self.expected, "hidden": self.hidden}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskCase":
        return cls(
            input=str(data.get("input", "")),
            expected=str(data.get("expected", "")),
            hidden=bool(data.get("hidden", False)),
        )


@dataclass
class TaskSpec:
    """What the optimizer is optimizing a prompt *for*.

    ``objective`` is the immutable intent used for drift protection. ``cases``
    are the evaluation inputs; those flagged ``hidden`` form the held-out set used
    to detect reward hacking (a candidate that overfits the visible cases).
    """

    objective: str
    cases: list[TaskCase] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    base_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def visible_cases(self) -> list[TaskCase]:
        return [c for c in self.cases if not c.hidden]

    @property
    def hidden_cases(self) -> list[TaskCase]:
        return [c for c in self.cases if c.hidden]

    @property
    def characteristics(self) -> str:
        """A compact text signature used for memory embedding / retrieval."""
        parts = [self.objective]
        if self.constraints:
            parts.append("constraints: " + "; ".join(self.constraints))
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "cases": [c.to_dict() for c in self.cases],
            "constraints": list(self.constraints),
            "base_prompt": self.base_prompt,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        return cls(
            objective=str(data.get("objective", "")),
            cases=[TaskCase.from_dict(c) for c in data.get("cases", []) if isinstance(c, dict)],
            constraints=[str(x) for x in data.get("constraints", [])],
            base_prompt=str(data.get("base_prompt", "")),
            metadata=dict(data.get("metadata", {})),
        )


def to_prompt_task_spec(task: "TaskSpec") -> PromptTaskSpec:
    """Convert jiuwenswarm's ``TaskSpec`` into the shared ``PromptTaskSpec``.

    The one place that bridges jiuwenswarm's flat input/expected task shape
    into the case format the RLAF-P algorithm (now in agent-core) actually
    consumes.
    """
    return PromptTaskSpec(
        objective=task.objective,
        cases=[
            PromptTaskCase.from_text(c.input, c.expected, hidden=c.hidden)
            for c in task.cases
        ],
        constraints=list(task.constraints),
        base_prompt=task.base_prompt,
        metadata=dict(task.metadata),
    )


__all__ = [
    "TaskCase",
    "TaskSpec",
    "to_prompt_task_spec",
    "PromptCandidate",
    "CaseResult",
    "Execution",
    "RewardBreakdown",
    "Evaluation",
    "IterationRecord",
    "OptimizationResult",
    "PromptRecord",
]
