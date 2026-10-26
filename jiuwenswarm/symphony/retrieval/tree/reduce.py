from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from models.retrieval import RetrieverCandidate

from .contracts import BranchReducer
from .types import NodeSearchResult, ProgressiveRetrieverConfig, SearchCursor


@dataclass(frozen=True)
class DefaultBranchReducer(BranchReducer):
    config: ProgressiveRetrieverConfig

    def reduce_branch_results(
        self,
        *,
        cursor: SearchCursor,
        local_leaves: Sequence[RetrieverCandidate],
        child_results: Sequence[NodeSearchResult],
    ) -> NodeSearchResult:
        branch_results = [list(local_leaves)] + [list(result.candidates) for result in child_results]
        reduced = self._merge_branch_candidates(branch_results=branch_results, top_k=cursor.top_k)
        return NodeSearchResult(candidates=tuple(reduced))

    def _merge_branch_candidates(
        self,
        *,
        branch_results: Sequence[Sequence[RetrieverCandidate] | None],
        top_k: int,
    ) -> list[RetrieverCandidate]:
        if not self.config.round_robin_branch_reduce:
            merged: list[RetrieverCandidate] = []
            for branch_candidates in branch_results:
                if branch_candidates:
                    merged.extend(branch_candidates)
            return self._dedupe_candidates(merged)[:top_k]
        reduced: list[RetrieverCandidate] = []
        seen: set[str] = set()
        index = 0
        while len(reduced) < top_k:
            added = False
            for branch_candidates in branch_results:
                if not branch_candidates or index >= len(branch_candidates):
                    continue
                candidate = branch_candidates[index]
                dedupe_key = candidate.payload or candidate.item_id
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                reduced.append(candidate)
                added = True
                if len(reduced) >= top_k:
                    break
            if not added:
                break
            index += 1
        if len(reduced) >= top_k:
            return reduced[:top_k]
        merged: list[RetrieverCandidate] = []
        for branch_candidates in branch_results:
            if branch_candidates:
                merged.extend(branch_candidates)
        for candidate in self._dedupe_candidates(merged):
            dedupe_key = candidate.payload or candidate.item_id
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            reduced.append(candidate)
            if len(reduced) >= top_k:
                break
        return reduced[:top_k]

    @staticmethod
    def _dedupe_candidates(candidates: Sequence[RetrieverCandidate]) -> list[RetrieverCandidate]:
        reduced: list[RetrieverCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            dedupe_key = candidate.payload or candidate.item_id
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            reduced.append(candidate)
        return reduced
