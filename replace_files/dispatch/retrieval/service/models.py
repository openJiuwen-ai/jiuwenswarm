from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from ..tree.codebooks import DEFAULT_COMPACT_BOUNDARY_CODEBOOK
from ..tree.types import ProgressiveRetrieverConfig


class RetrievalMethod(str, Enum):
    AUTO = "auto"
    PROGRESSIVE = "progressive"


@dataclass(frozen=True)
class SearchProgressiveTrieConfig:
    """Trie-constrained generation options for progressive generate mode."""

    trie_constrained_decoding_enabled: bool = False
    trie_constraint_allow_user_nodes: bool = True
    trie_constraint_max_candidates: int = 512
    trie_constraint_fallback_payload: str = ""


@dataclass(frozen=True)
class SearchProgressiveTraversalConfig:
    """Progressive tree traversal and branching controls."""

    progressive_batch_size: int = 1
    progressive_max_tokens: int = 48
    progressive_request_timeout: float | None = None
    progressive_max_branch_choices: int = 6
    progressive_auto_expand_child_threshold: int = 3
    progressive_collapse_single_chain: bool = True
    progressive_max_collapse_steps: int = 8
    progressive_max_parallel_branches: int = 3
    progressive_enable_parallel_branches: bool = True
    progressive_auto_terminal_item_threshold: int = 12
    progressive_branch_choice_slack: int = 2
    progressive_branch_candidate_slack: int = 1
    progressive_round_robin_branch_reduce: bool = True
    progressive_branch_max_tokens: int = 96
    progressive_item_max_tokens: int = 128


@dataclass(frozen=True)
class SearchProgressiveDisclosureConfig:
    """Controls how much of the tree is exposed in each progressive prompt."""

    progressive_compact_boundary_codes_enabled: bool = False
    progressive_compact_boundary_codebook: tuple[str, ...] = DEFAULT_COMPACT_BOUNDARY_CODEBOOK
    progressive_flatten_full_tree_in_prompt: bool = False
    progressive_max_exposure_depth_per_call: int = 2
    progressive_exposure_threshold: int = 12
    progressive_force_expand_single_child: bool = True


@dataclass(frozen=True)
class SearchProgressiveSelectionConfig:
    """Selection strategy for progressive retriever candidate choice."""

    progressive_single_forward_logit_selection_enabled: bool = False
    progressive_selection_mode: str = "generate"


@dataclass(frozen=True)
class SearchProgressiveScoringConfig:
    """Local / server-side logit-selection backend options."""

    progressive_scoring_backend: str = "transformers"
    progressive_scoring_backend_model_path: str = ""
    progressive_scoring_backend_tokenizer_path: str = ""
    progressive_scoring_backend_device: str = "auto"
    progressive_scoring_backend_dtype: str = "auto"
    progressive_scoring_backend_enable_prefix_caching: bool = True
    progressive_scoring_backend_vllm_kwargs: Dict[str, object] = field(default_factory=dict)
    progressive_scoring_backend_batching_enabled: bool = True
    progressive_scoring_require_single_token_codes: bool = True
    progressive_scoring_return_probabilities: bool = True
    progressive_scoring_fallback_mode: str = "error"
    progressive_scoring_max_candidates: int = 512
    progressive_scoring_min_probability: float | None = None
    progressive_scoring_trace_top_n: int = 10


@dataclass(frozen=True)
class SearchProgressiveGenerationConfig:
    """Generation backend options for progressive generate mode."""

    progressive_generation_backend: str = "openai"
    progressive_generation_model_path: str = ""
    progressive_generation_tokenizer_path: str = ""
    progressive_generation_device: str = "auto"
    progressive_generation_dtype: str = "bfloat16"
    progressive_generation_tp_size: int = 1
    progressive_generation_dp_size: int = 1
    progressive_generation_device_ids: tuple[int, ...] = ()
    progressive_generation_attn_implementation: str = ""
    progressive_generation_torch_compile: bool = False
    progressive_generation_tp_plan: str = ""
    progressive_generation_vllm_kwargs: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchProgressivePrefixCacheConfig:
    """Low-latency fixed-prefix KV cache controls."""

    progressive_prefix_cache_enabled: bool = False
    progressive_prefix_cache_warmup: str = "eager"
    progressive_prefix_cache_max_entries: int = 128
    progressive_prefix_cache_gpu_budget_gib: float | None = None
    progressive_prefix_cache_request_pool_size: int = 1
    progressive_prefix_cache_max_suffix_tokens: int = 256
    progressive_prefix_cache_max_new_tokens: int = 128
    progressive_prefix_cache_on_pool_exhausted: str = "reject"
    progressive_prefix_cache_on_query_too_long: str = "reject"
    progressive_prefix_cache_slot_acquire_timeout_ms: float = 0.0
    progressive_prefix_cache_clear_tail_on_release: bool = False
    progressive_prefix_cache_oom_recovery: str = "poison_slot_and_degrade_replica"
    progressive_prefix_cache_slot_rebuild: str = "async"


@dataclass(frozen=True)
class SearchProgressiveConfig:
    """Progressive tree retriever options, with nested backend sections."""

    traversal: SearchProgressiveTraversalConfig = field(default_factory=SearchProgressiveTraversalConfig)
    disclosure: SearchProgressiveDisclosureConfig = field(default_factory=SearchProgressiveDisclosureConfig)
    selection: SearchProgressiveSelectionConfig = field(default_factory=SearchProgressiveSelectionConfig)
    trie: SearchProgressiveTrieConfig | None = None
    scoring: SearchProgressiveScoringConfig | None = None
    generation: SearchProgressiveGenerationConfig | None = None
    prefix_cache: SearchProgressivePrefixCacheConfig | None = None


@dataclass(frozen=True)
class SearchConfig:
    """Retriever initialization config, grouped by retrieval subsystem."""

    top_k: int
    method: RetrievalMethod = RetrievalMethod.AUTO
    llm_top_k: int | None = None
    progressive: SearchProgressiveConfig | None = None


@dataclass(frozen=True)
class SearchRequestConfig:
    """Lightweight per-search controls that do not reconfigure retriever runtime."""

    top_k: int | None = None


@dataclass(frozen=True)
class RetrieverConfig:
    method: str = "auto"
    top_k: int = 10
    llm_top_k: int | None = None
    progressive: ProgressiveRetrieverConfig = field(default_factory=ProgressiveRetrieverConfig)


@dataclass
class RetrieverSearchResult:
    method: str
    payloads: List[str]
    candidate_records: List[Dict[str, object]]
    summary_lines: List[str]
    selected_payload: str | None
    selected_rank: int
    elapsed_ms: float = 0.0
    trace_events: List[Dict[str, object]] = field(default_factory=list)


def runtime_retriever_config_from_search(config: SearchConfig) -> RetrieverConfig:
    resolved_top_k = max(1, int(config.top_k))
    progressive = config.progressive or SearchProgressiveConfig()
    traversal = progressive.traversal
    disclosure = progressive.disclosure
    selection = progressive.selection
    trie = progressive.trie or SearchProgressiveTrieConfig()
    scoring = progressive.scoring or SearchProgressiveScoringConfig()
    generation = progressive.generation or SearchProgressiveGenerationConfig()
    prefix_cache = progressive.prefix_cache or SearchProgressivePrefixCacheConfig()
    return RetrieverConfig(
        method=str(config.method.value),
        top_k=resolved_top_k,
        llm_top_k=None if config.llm_top_k is None else max(0, int(config.llm_top_k)),
        progressive=ProgressiveRetrieverConfig(
            top_k=resolved_top_k,
            batch_size=max(1, int(traversal.progressive_batch_size)),
            max_tokens=max(1, int(traversal.progressive_max_tokens)),
            trie_constrained_decoding_enabled=bool(trie.trie_constrained_decoding_enabled),
            trie_constraint_allow_user_nodes=bool(trie.trie_constraint_allow_user_nodes),
            trie_constraint_max_candidates=max(1, int(trie.trie_constraint_max_candidates)),
            trie_constraint_fallback_payload=str(trie.trie_constraint_fallback_payload or ""),
            max_branch_choices=max(1, int(traversal.progressive_max_branch_choices)),
            auto_expand_child_threshold=max(1, int(traversal.progressive_auto_expand_child_threshold)),
            collapse_single_chain=bool(traversal.progressive_collapse_single_chain),
            max_collapse_steps=max(1, int(traversal.progressive_max_collapse_steps)),
            max_parallel_branches=max(1, int(traversal.progressive_max_parallel_branches)),
            enable_parallel_branches=bool(traversal.progressive_enable_parallel_branches),
            auto_terminal_item_threshold=max(1, int(traversal.progressive_auto_terminal_item_threshold)),
            branch_choice_slack=max(0, int(traversal.progressive_branch_choice_slack)),
            branch_candidate_slack=max(0, int(traversal.progressive_branch_candidate_slack)),
            round_robin_branch_reduce=bool(traversal.progressive_round_robin_branch_reduce),
            branch_max_tokens=max(1, int(traversal.progressive_branch_max_tokens)),
            item_max_tokens=max(1, int(traversal.progressive_item_max_tokens)),
            request_timeout=traversal.progressive_request_timeout,
            compact_boundary_codes_enabled=bool(disclosure.progressive_compact_boundary_codes_enabled),
            compact_boundary_codebook=tuple(str(code) for code in disclosure.progressive_compact_boundary_codebook),
            flatten_full_tree_in_prompt=bool(disclosure.progressive_flatten_full_tree_in_prompt),
            max_exposure_depth_per_call=max(0, int(disclosure.progressive_max_exposure_depth_per_call)),
            exposure_threshold=max(0, int(disclosure.progressive_exposure_threshold)),
            force_expand_single_child=bool(disclosure.progressive_force_expand_single_child),
            single_forward_logit_selection_enabled=bool(selection.progressive_single_forward_logit_selection_enabled),
            selection_mode=str(selection.progressive_selection_mode or "generate").strip().lower() or "generate",
            scoring_backend=str(scoring.progressive_scoring_backend or "transformers"),
            scoring_backend_model_path=str(scoring.progressive_scoring_backend_model_path or ""),
            scoring_backend_tokenizer_path=str(scoring.progressive_scoring_backend_tokenizer_path or ""),
            scoring_backend_device=str(scoring.progressive_scoring_backend_device or "auto"),
            scoring_backend_dtype=str(scoring.progressive_scoring_backend_dtype or "auto"),
            scoring_backend_enable_prefix_caching=bool(scoring.progressive_scoring_backend_enable_prefix_caching),
            scoring_backend_vllm_kwargs=dict(scoring.progressive_scoring_backend_vllm_kwargs or {}),
            scoring_backend_batching_enabled=bool(scoring.progressive_scoring_backend_batching_enabled),
            scoring_require_single_token_codes=bool(scoring.progressive_scoring_require_single_token_codes),
            scoring_return_probabilities=bool(scoring.progressive_scoring_return_probabilities),
            scoring_fallback_mode=str(scoring.progressive_scoring_fallback_mode or "error"),
            scoring_max_candidates=max(1, int(scoring.progressive_scoring_max_candidates)),
            scoring_min_probability=scoring.progressive_scoring_min_probability,
            scoring_trace_top_n=max(1, int(scoring.progressive_scoring_trace_top_n)),
            generation_backend=str(generation.progressive_generation_backend or "openai"),
            generation_model_path=str(generation.progressive_generation_model_path or ""),
            generation_tokenizer_path=str(generation.progressive_generation_tokenizer_path or ""),
            generation_device=str(generation.progressive_generation_device or "auto"),
            generation_dtype=str(generation.progressive_generation_dtype or "bfloat16"),
            generation_tp_size=max(1, int(generation.progressive_generation_tp_size)),
            generation_dp_size=max(1, int(generation.progressive_generation_dp_size)),
            generation_device_ids=tuple(int(item) for item in generation.progressive_generation_device_ids),
            generation_attn_implementation=str(generation.progressive_generation_attn_implementation or ""),
            generation_torch_compile=bool(generation.progressive_generation_torch_compile),
            generation_tp_plan=str(generation.progressive_generation_tp_plan or ""),
            generation_vllm_kwargs=dict(generation.progressive_generation_vllm_kwargs or {}),
            prefix_cache_enabled=bool(prefix_cache.progressive_prefix_cache_enabled),
            prefix_cache_warmup=str(prefix_cache.progressive_prefix_cache_warmup or "eager"),
            prefix_cache_max_entries=max(1, int(prefix_cache.progressive_prefix_cache_max_entries)),
            prefix_cache_gpu_budget_gib=prefix_cache.progressive_prefix_cache_gpu_budget_gib,
            prefix_cache_request_pool_size=max(1, int(prefix_cache.progressive_prefix_cache_request_pool_size)),
            prefix_cache_max_suffix_tokens=max(1, int(prefix_cache.progressive_prefix_cache_max_suffix_tokens)),
            prefix_cache_max_new_tokens=max(1, int(prefix_cache.progressive_prefix_cache_max_new_tokens)),
            prefix_cache_on_pool_exhausted=str(prefix_cache.progressive_prefix_cache_on_pool_exhausted or "reject"),
            prefix_cache_on_query_too_long=str(prefix_cache.progressive_prefix_cache_on_query_too_long or "reject"),
            prefix_cache_slot_acquire_timeout_ms=max(
                0.0,
                float(prefix_cache.progressive_prefix_cache_slot_acquire_timeout_ms),
            ),
            prefix_cache_clear_tail_on_release=bool(prefix_cache.progressive_prefix_cache_clear_tail_on_release),
            prefix_cache_oom_recovery=str(
                prefix_cache.progressive_prefix_cache_oom_recovery or "poison_slot_and_degrade_replica"
            ),
            prefix_cache_slot_rebuild=str(prefix_cache.progressive_prefix_cache_slot_rebuild or "async"),
        ),
    )


__all__ = [
    "RetrievalMethod",
    "RetrieverConfig",
    "RetrieverSearchResult",
    "SearchConfig",
    "SearchProgressiveConfig",
    "SearchProgressiveDisclosureConfig",
    "SearchProgressiveGenerationConfig",
    "SearchProgressivePrefixCacheConfig",
    "SearchProgressiveScoringConfig",
    "SearchProgressiveSelectionConfig",
    "SearchProgressiveTraversalConfig",
    "SearchProgressiveTrieConfig",
    "SearchRequestConfig",
    "runtime_retriever_config_from_search",
]
