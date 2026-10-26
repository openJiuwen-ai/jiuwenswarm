from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from models.retrieval import RetrieverCandidate

from .contracts import TargetExpander
from .types import ChildSearchCursor, ExpansionPlan, ProgressiveRetrieverConfig, SearchCursor, SelectableTarget


@dataclass(frozen=True)
class DefaultTargetExpander(TargetExpander):
    config: ProgressiveRetrieverConfig

    def expand_selected_targets(
        self,
        *,
        cursor: SearchCursor,
        selected_targets: Sequence[SelectableTarget],
    ) -> ExpansionPlan:
        branch_targets = [
            target for target in selected_targets if not target.is_terminal and target.resolution.node is not None
        ]
        branch_top_k = self._resolve_branch_top_k(top_k=cursor.top_k, branch_count=max(1, len(selected_targets)))
        leaf_results: list[RetrieverCandidate] = []
        child_cursors: list[ChildSearchCursor] = []
        for target in selected_targets:
            resolution = target.resolution
            if resolution.is_terminal and resolution.item is not None:
                leaf_results.append(
                    RetrieverCandidate(
                        rank=1,
                        item_id=resolution.item.item_id,
                        payload=resolution.item.payload,
                        branch_path=resolution.branch_path,
                        label=resolution.item.label,
                        description=resolution.item.description,
                    )
                )
                continue
            if resolution.node is None:
                continue
            child_cursors.append(
                ChildSearchCursor(
                    cursor=SearchCursor(
                        node=resolution.node,
                        depth=cursor.depth + 1,
                        branch_path=resolution.branch_path,
                        top_k=branch_top_k,
                    ),
                    target=target,
                )
            )
        return ExpansionPlan(leaf_results=tuple(leaf_results), child_cursors=tuple(child_cursors))

    def _resolve_branch_top_k(self, *, top_k: int, branch_count: int) -> int:
        if branch_count <= 0:
            return max(1, int(top_k))
        slack = max(0, int(self.config.branch_candidate_slack))
        budget = math.ceil(max(1, int(top_k)) / branch_count) + slack
        return min(max(1, int(top_k)), max(1, budget))
