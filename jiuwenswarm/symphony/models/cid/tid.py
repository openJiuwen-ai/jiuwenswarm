from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class WorkerProfile:
    worker_id: str
    name: str
    description: str
    keywords: Tuple[str, ...] = ()
    examples: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TIDBuildDecision:
    action: str
    cid: str
    reason: str


@dataclass(frozen=True)
class TIDBuildResult:
    worker_id: str
    tid: str
    created_nodes: Tuple[str, ...] = ()
    moved_nodes: Tuple[str, ...] = ()
    decisions: Tuple[TIDBuildDecision, ...] = ()
    rebalanced_branches: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TIDBuildConfig:
    max_children_per_branch: int = 10
    max_new_branch_depth: int = 2
    descend_threshold: float = 1.0
    sibling_margin: float = 0.25
    min_group_size_for_rebalance: int = 2
    protected_branch_terms: Tuple[str, ...] = field(default_factory=lambda: ("User", "Guard", "TaskAssistant"))
