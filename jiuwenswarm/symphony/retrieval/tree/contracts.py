from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, Sequence

from models.retrieval import RetrieverCandidate, RetrieverTrace

from .types import (
    CurrentSubtree,
    ExpansionPlan,
    NodeSearchResult,
    PromptBundle,
    SearchCursor,
    SelectionProtocol,
    SelectionResult,
)


class CurrentSubtreeProvider(Protocol):
    @abstractmethod
    def get_current_subtree(self, *, cursor: SearchCursor) -> CurrentSubtree:
        raise NotImplementedError


class SubtreeRenderer(Protocol):
    @abstractmethod
    def render_subtree(
        self,
        *,
        subtree: CurrentSubtree,
        query_messages: Sequence[dict[str, str]],
        protocol: SelectionProtocol,
    ) -> PromptBundle:
        raise NotImplementedError


class TopKSelector(Protocol):
    @abstractmethod
    def build_protocol(self, *, subtree: CurrentSubtree) -> SelectionProtocol:
        raise NotImplementedError

    @abstractmethod
    def select_topk(
        self,
        *,
        model: str,
        cursor: SearchCursor,
        query_messages: Sequence[dict[str, str]],
        subtree: CurrentSubtree,
        prompt: PromptBundle,
        trace: RetrieverTrace,
    ) -> SelectionResult:
        raise NotImplementedError


class TargetExpander(Protocol):
    @abstractmethod
    def expand_selected_targets(
        self,
        *,
        cursor: SearchCursor,
        selected_targets: Sequence,
    ) -> ExpansionPlan:
        raise NotImplementedError


class BranchReducer(Protocol):
    @abstractmethod
    def reduce_branch_results(
        self,
        *,
        cursor: SearchCursor,
        local_leaves: Sequence[RetrieverCandidate],
        child_results: Sequence[NodeSearchResult],
    ) -> NodeSearchResult:
        raise NotImplementedError
