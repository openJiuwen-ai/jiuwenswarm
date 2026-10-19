from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from models.retrieval import RetrieverCandidate, RetrieverItem, RetrieverNode, RetrieverTrace
from ..llm.config import LLMClientConfig, OpenAIClientConfig
from .codebooks import DEFAULT_COMPACT_BOUNDARY_CODEBOOK
from .render.disclosure import ExposedFragment, SelectableResolution


@dataclass(frozen=True)
class ProgressiveRetrieverConfig:
    top_k: int = 5
    max_tokens: int = 96
    trie_constrained_decoding_enabled: bool = False
    max_branch_choices: int = 6
    max_parallel_branches: int = 3
    enable_parallel_branches: bool = True
    branch_choice_slack: int = 2
    branch_candidate_slack: int = 1
    round_robin_branch_reduce: bool = True
    request_timeout: float | None = 120.0
    compact_boundary_codes_enabled: bool = True
    compact_boundary_codebook: tuple[str, ...] = DEFAULT_COMPACT_BOUNDARY_CODEBOOK
    flatten_full_tree_in_prompt: bool = True
    max_exposure_depth_per_call: int = 99
    exposure_threshold: int = 1_000_000_000
    selection_mode: str = "generate"
    scoring_require_single_token_codes: bool = True
    scoring_return_probabilities: bool = True
    scoring_fallback_mode: str = "error"
    scoring_max_candidates: int = 512
    scoring_min_probability: float | None = None
    scoring_trace_top_n: int = 10
    llm_client_config: LLMClientConfig = field(default_factory=OpenAIClientConfig)


@dataclass
class ProgressiveRetrieverResult:
    candidates: List[RetrieverCandidate]
    trace: RetrieverTrace
    candidate_records: List[Dict[str, object]] = field(default_factory=list)
    summary_lines: List[str] = field(default_factory=list)
    selected_payload: str | None = None
    selected_rank: int = -1
    raw_outputs: List[str] = field(default_factory=list)
    request_messages: List[Dict[str, str]] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class SearchCursor:
    node: RetrieverNode
    depth: int
    branch_path: tuple[str, ...]
    top_k: int


@dataclass(frozen=True)
class SelectableTarget:
    resolution: SelectableResolution

    @property
    def is_terminal(self) -> bool:
        return bool(self.resolution.is_terminal)

    @property
    def branch_path(self) -> tuple[str, ...]:
        return tuple(self.resolution.branch_path)


@dataclass(frozen=True)
class CurrentSubtree:
    cursor: SearchCursor
    fragment: ExposedFragment
    selectable_targets: tuple[SelectableTarget, ...]


@dataclass(frozen=True)
class SelectionProtocol:
    compact_codes_enabled: bool
    candidate_codes: tuple[str, ...]
    code_width: int
    abstain_token: str = "0"


@dataclass(frozen=True)
class PromptBundle:
    fragment: ExposedFragment
    protocol: SelectionProtocol
    messages: tuple[Dict[str, str], ...]


@dataclass(frozen=True)
class SelectionResult:
    raw_output: str
    selected_targets: tuple[SelectableTarget, ...]
    is_abstain: bool = False


@dataclass(frozen=True)
class ChildSearchCursor:
    cursor: SearchCursor
    target: SelectableTarget


@dataclass(frozen=True)
class ExpansionPlan:
    leaf_results: tuple[RetrieverCandidate, ...]
    child_cursors: tuple[ChildSearchCursor, ...]


@dataclass(frozen=True)
class NodeSearchResult:
    candidates: tuple[RetrieverCandidate, ...]
