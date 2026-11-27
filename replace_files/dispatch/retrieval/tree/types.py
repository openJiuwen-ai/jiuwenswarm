from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ...models.retrieval import RetrieverCandidate, RetrieverItem, RetrieverNode, RetrieverTrace
from .codebooks import DEFAULT_COMPACT_BOUNDARY_CODEBOOK
from .render.disclosure import ExposedFragment, SelectableResolution


@dataclass(frozen=True)
class ProgressiveRetrieverConfig:
    top_k: int = 5
    batch_size: int = 1
    max_tokens: int = 48
    trie_constrained_decoding_enabled: bool = False
    trie_constraint_allow_user_nodes: bool = True
    trie_constraint_max_candidates: int = 512
    trie_constraint_fallback_payload: str = ""
    max_branch_choices: int = 6
    auto_expand_child_threshold: int = 3
    collapse_single_chain: bool = True
    max_collapse_steps: int = 8
    max_parallel_branches: int = 3
    enable_parallel_branches: bool = True
    auto_terminal_item_threshold: int = 12
    branch_choice_slack: int = 2
    branch_candidate_slack: int = 1
    round_robin_branch_reduce: bool = True
    branch_max_tokens: int = 96
    item_max_tokens: int = 128
    request_timeout: float | None = None
    compact_boundary_codes_enabled: bool = False
    compact_boundary_codebook: tuple[str, ...] = DEFAULT_COMPACT_BOUNDARY_CODEBOOK
    flatten_full_tree_in_prompt: bool = False
    max_exposure_depth_per_call: int = 2
    exposure_threshold: int = 12
    force_expand_single_child: bool = True
    single_forward_logit_selection_enabled: bool = False
    selection_mode: str = "generate"
    scoring_backend: str = "transformers"
    scoring_backend_model_path: str = ""
    scoring_backend_tokenizer_path: str = ""
    scoring_backend_device: str = "auto"
    scoring_backend_dtype: str = "auto"
    scoring_backend_enable_prefix_caching: bool = True
    scoring_backend_vllm_kwargs: dict[str, object] = field(default_factory=dict)
    scoring_backend_batching_enabled: bool = True
    scoring_require_single_token_codes: bool = True
    scoring_return_probabilities: bool = True
    scoring_fallback_mode: str = "error"
    scoring_max_candidates: int = 512
    scoring_min_probability: float | None = None
    scoring_trace_top_n: int = 10
    generation_backend: str = "openai"
    generation_model_path: str = ""
    generation_tokenizer_path: str = ""
    generation_device: str = "auto"
    generation_dtype: str = "bfloat16"
    generation_tp_size: int = 1
    generation_dp_size: int = 1
    generation_device_ids: tuple[int, ...] = ()
    generation_attn_implementation: str = ""
    generation_torch_compile: bool = False
    generation_tp_plan: str = ""
    generation_vllm_kwargs: dict[str, object] = field(default_factory=dict)
    prefix_cache_enabled: bool = False
    prefix_cache_warmup: str = "eager"
    prefix_cache_max_entries: int = 128
    prefix_cache_gpu_budget_gib: float | None = None
    prefix_cache_request_pool_size: int = 1
    prefix_cache_max_suffix_tokens: int = 256
    prefix_cache_max_new_tokens: int = 128
    prefix_cache_on_pool_exhausted: str = "reject"
    prefix_cache_on_query_too_long: str = "reject"
    prefix_cache_slot_acquire_timeout_ms: float = 0.0
    prefix_cache_clear_tail_on_release: bool = False
    prefix_cache_oom_recovery: str = "poison_slot_and_degrade_replica"
    prefix_cache_slot_rebuild: str = "async"


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
