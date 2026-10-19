from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Sequence

from models.retrieval import RetrieverCandidate, RetrieverTrace

from .contracts import BranchReducer, CurrentSubtreeProvider, SubtreeRenderer, TargetExpander, TopKSelector
from .types import NodeSearchResult, SearchCursor


@dataclass
class RecursiveSearchEngine:
    subtree_provider: CurrentSubtreeProvider
    renderer: SubtreeRenderer
    selector: TopKSelector
    expander: TargetExpander
    reducer: BranchReducer
    enable_parallel_branches: bool = True
    max_parallel_branches: int = 3

    def search(
        self,
        *,
        model: str,
        query_messages: Sequence[dict[str, str]],
        root_cursor: SearchCursor,
        trace: RetrieverTrace,
    ) -> list[RetrieverCandidate]:
        result = self._search_node(
            model=model,
            query_messages=query_messages,
            cursor=root_cursor,
            trace=trace,
        )
        return list(result.candidates)

    def _search_node(
        self,
        *,
        model: str,
        query_messages: Sequence[dict[str, str]],
        cursor: SearchCursor,
        trace: RetrieverTrace,
    ) -> NodeSearchResult:
        subtree = self.subtree_provider.get_current_subtree(cursor=cursor)
        trace.record(
            "fragment_built",
            node_id=cursor.node.node_id,
            depth=cursor.depth,
            detail={
                "selectable_count": len(subtree.selectable_targets),
                "rendered_tree": subtree.fragment.rendered_tree,
            },
        )
        if not subtree.selectable_targets:
            return NodeSearchResult(candidates=())
        if len(subtree.selectable_targets) == 1:
            only = subtree.selectable_targets[0]
            trace.record(
                "fragment_selected",
                node_id=cursor.node.node_id,
                depth=cursor.depth,
                detail={
                    "mode": "single_selectable_shortcut",
                    "selected_codes": [only.resolution.code],
                    "selected_canonical_ids": [only.resolution.canonical_id],
                },
            )
            plan = self.expander.expand_selected_targets(cursor=cursor, selected_targets=subtree.selectable_targets)
            return self._continue(cursor=cursor, trace=trace, model=model, query_messages=query_messages, plan=plan)

        protocol = self.selector.build_protocol(subtree=subtree)
        prompt = self.renderer.render_subtree(subtree=subtree, query_messages=query_messages, protocol=protocol)
        selection = self.selector.select_topk(
            model=model,
            cursor=cursor,
            query_messages=query_messages,
            subtree=subtree,
            prompt=prompt,
            trace=trace,
        )
        trace.record(
            "fragment_selected",
            node_id=cursor.node.node_id,
            depth=cursor.depth,
            detail={
                "selected_codes": [item.resolution.code for item in selection.selected_targets],
                "selected_canonical_ids": [item.resolution.canonical_id for item in selection.selected_targets],
                "raw_output": selection.raw_output,
            },
        )
        if selection.is_abstain or not selection.selected_targets:
            return NodeSearchResult(candidates=())
        plan = self.expander.expand_selected_targets(cursor=cursor, selected_targets=selection.selected_targets)
        return self._continue(cursor=cursor, trace=trace, model=model, query_messages=query_messages, plan=plan)

    def _continue(
        self,
        *,
        cursor: SearchCursor,
        trace: RetrieverTrace,
        model: str,
        query_messages: Sequence[dict[str, str]],
        plan,
    ) -> NodeSearchResult:
        trace.record(
            "fragment_continue",
            node_id=cursor.node.node_id,
            depth=cursor.depth,
            detail={
                "leaf_count": len(plan.leaf_results),
                "branch_count": len(plan.child_cursors),
                "selected_terminal_ids": [item.item_id for item in plan.leaf_results],
                "selected_branch_ids": [child.cursor.node.node_id for child in plan.child_cursors],
            },
        )
        child_results: list[NodeSearchResult] = []
        if plan.child_cursors:
            if len(plan.child_cursors) > 1 and self.enable_parallel_branches:
                max_workers = min(len(plan.child_cursors), max(1, int(self.max_parallel_branches)))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [
                        executor.submit(
                            self._search_node,
                            model=model,
                            query_messages=query_messages,
                            cursor=child.cursor,
                            trace=trace,
                        )
                        for child in plan.child_cursors
                    ]
                    for future in futures:
                        child_results.append(future.result())
            else:
                for child in plan.child_cursors:
                    child_results.append(
                        self._search_node(
                            model=model,
                            query_messages=query_messages,
                            cursor=child.cursor,
                            trace=trace,
                        )
                    )
        reduced = self.reducer.reduce_branch_results(
            cursor=cursor,
            local_leaves=plan.leaf_results,
            child_results=child_results,
        )
        trace.record(
            "reduce_complete",
            node_id=cursor.node.node_id,
            depth=cursor.depth,
            detail={
                "input_candidates": len(plan.leaf_results) + sum(len(result.candidates) for result in child_results),
                "output_candidates": len(reduced.candidates),
            },
        )
        return reduced
