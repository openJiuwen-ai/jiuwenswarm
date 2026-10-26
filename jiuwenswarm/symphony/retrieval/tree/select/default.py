from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Sequence

from models.retrieval import RetrieverTrace
from .selection import GenerateFragmentSelector, LogitSelectionFragmentSelector

from ..contracts import TopKSelector
from ..types import (
    CurrentSubtree,
    ProgressiveRetrieverConfig,
    PromptBundle,
    SearchCursor,
    SelectableTarget,
    SelectionProtocol,
    SelectionResult,
)

_ABSTAIN_HINT_RE = re.compile(
    r"(none|no suitable|no relevant|not relevant|unrelated|cannot determine|can't determine|"
    r"无合适|没有合适|无相关|没有相关|不相关|无法判断|无法确定)",
    re.IGNORECASE,
)


@dataclass
class DefaultTopKSelector(TopKSelector):
    config: ProgressiveRetrieverConfig
    build_generate_selector: Callable[[], GenerateFragmentSelector | LogitSelectionFragmentSelector]

    def build_protocol(self, *, subtree: CurrentSubtree) -> SelectionProtocol:
        return SelectionProtocol(
            compact_codes_enabled=bool(subtree.fragment.compact_codes_enabled),
            candidate_codes=tuple(subtree.fragment.candidate_codes),
            code_width=int(subtree.fragment.code_width),
            abstain_token="0",
        )

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
        selector = self.build_generate_selector()
        output, selected = selector.select(
            model=model,
            query_messages=list(query_messages),
            node=cursor.node,
            depth=cursor.depth,
            top_k=cursor.top_k,
            trace=trace,
            fragment=subtree.fragment,
        )
        return SelectionResult(
            raw_output=output,
            selected_targets=tuple(SelectableTarget(resolution=item) for item in selected),
            is_abstain=not selected and self._is_abstain_output(output),
        )

    @staticmethod
    def _is_abstain_output(output: str) -> bool:
        text = str(output or "").strip()
        if not text:
            return False
        if text == "0":
            return True
        return _ABSTAIN_HINT_RE.search(text) is not None
