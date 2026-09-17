from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal

from ..llm.config import LLMClientConfig, OpenAIClientConfig, TransformersClientConfig, VLLMClientConfig
from ..tree.codebooks import DEFAULT_COMPACT_BOUNDARY_CODEBOOK
from ..tree.types import ProgressiveRetrieverConfig


@dataclass(frozen=True)
class TraversalConfig:
    """Progressive traversal and branch-search controls."""

    # Maximum number of child branches considered at a node.
    max_branch_choices: int = 6

    # Maximum number of branches searched in parallel.
    max_parallel_branches: int = 3

    # Whether branch-level parallel search is enabled.
    enable_parallel_branches: bool = True

    # Extra child branches to consider beyond top_k.
    branch_choice_slack: int = 2

    # Extra candidates to keep per selected branch.
    branch_candidate_slack: int = 1

    # Whether multi-branch candidates are merged round-robin.
    round_robin_branch_reduce: bool = True


@dataclass(frozen=True)
class RenderConfig:
    """Prompt rendering and tree disclosure controls."""

    # Whether compact numeric candidate codes are exposed to the LLM.
    compact_codes_enabled: bool = True

    # Codebook for compact candidate code rendering.
    compact_codebook: tuple[str, ...] = DEFAULT_COMPACT_BOUNDARY_CODEBOOK

    # Whether the current candidate tree is rendered as a flat prompt payload.
    flatten_tree: bool = True

    # Maximum candidate tree depth exposed in one LLM call.
    max_exposure_depth: int = 99

    # Node expansion / exposure threshold.
    exposure_threshold: int = 1_000_000_000


@dataclass(frozen=True)
class GenerationConfig:
    """LLM candidate generation and logit-selection controls."""

    # Selection mode: generated compact codes or logit-based candidate scoring.
    mode: Literal["generate", "logit_selection"] = "generate"

    # Default generation token limit for selection requests.
    max_tokens: int = 96

    # Per-request timeout in seconds.
    request_timeout_seconds: float | None = 120.0

    # Whether trie constrained decoding is enabled for supporting clients.
    trie_constrained_decoding_enabled: bool = False

    # Whether logit-selection candidate codes must be single tokenizer tokens.
    logit_require_single_token_codes: bool = True

    # Whether logit-selection returns probabilities in traces.
    logit_return_probabilities: bool = True

    # Logit-selection fallback behavior.
    logit_fallback_mode: str = "error"

    # Maximum number of candidates scored in logit-selection mode.
    logit_max_candidates: int = 512

    # Optional minimum probability threshold for logit-selected candidates.
    logit_min_probability: float | None = None

    # Number of top scoring candidates included in trace debug output.
    logit_trace_top_n: int = 10


@dataclass(frozen=True)
class RetrieverConfig:
    """Public retriever initialization configuration."""

    # Maximum number of skills returned by the initialized retriever.
    top_k: int = 10

    # LLM client configuration. Defaults to OpenAI-compatible external-client mode.
    llm_client_config: LLMClientConfig = field(default_factory=OpenAIClientConfig)

    # Progressive traversal configuration.
    traversal_config: TraversalConfig = field(default_factory=TraversalConfig)

    # Prompt rendering configuration.
    render_config: RenderConfig = field(default_factory=RenderConfig)

    # Candidate generation / scoring configuration.
    generation_config: GenerationConfig = field(default_factory=GenerationConfig)


@dataclass(frozen=True)
class RequestConfig:
    """Per-search overrides that do not rebuild retriever runtime."""

    # Requested result count. None means using RetrieverConfig.top_k.
    top_k: int | None = None

    # Candidate tags required before tree traversal. Empty means no filtering.
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RuntimeRetrieverConfig:
    """Internal runtime configuration consumed by the retrieval engine."""

    top_k: int = 10
    progressive: ProgressiveRetrieverConfig = field(default_factory=ProgressiveRetrieverConfig)


@dataclass
class SearchResult:
    method: str
    payloads: List[str]
    candidate_records: List[Dict[str, object]]
    summary_lines: List[str]
    selected_payload: str | None
    selected_rank: int
    elapsed_ms: float = 0.0
    trace_events: List[Dict[str, object]] = field(default_factory=list)


def runtime_retriever_config_from_config(config: RetrieverConfig | None = None) -> _RuntimeRetrieverConfig:
    """Convert public retrieval config into the internal runtime config.

    Args:
        config: Public retriever configuration.

    Returns:
        Internal config passed to the progressive retriever implementation.
    """

    config = config or RetrieverConfig()
    resolved_top_k = max(1, int(config.top_k))
    traversal = config.traversal_config or TraversalConfig()
    render = config.render_config or RenderConfig()
    generation = config.generation_config or GenerationConfig()

    return _RuntimeRetrieverConfig(
        top_k=resolved_top_k,
        progressive=ProgressiveRetrieverConfig(
            top_k=resolved_top_k,
            llm_client_config=config.llm_client_config or OpenAIClientConfig(),
            max_tokens=max(1, int(generation.max_tokens)),
            trie_constrained_decoding_enabled=bool(generation.trie_constrained_decoding_enabled),
            max_branch_choices=max(1, int(traversal.max_branch_choices)),
            max_parallel_branches=max(1, int(traversal.max_parallel_branches)),
            enable_parallel_branches=bool(traversal.enable_parallel_branches),
            branch_choice_slack=max(0, int(traversal.branch_choice_slack)),
            branch_candidate_slack=max(0, int(traversal.branch_candidate_slack)),
            round_robin_branch_reduce=bool(traversal.round_robin_branch_reduce),
            request_timeout=generation.request_timeout_seconds,
            compact_boundary_codes_enabled=bool(render.compact_codes_enabled),
            compact_boundary_codebook=tuple(str(code) for code in render.compact_codebook),
            flatten_full_tree_in_prompt=bool(render.flatten_tree),
            max_exposure_depth_per_call=max(0, int(render.max_exposure_depth)),
            exposure_threshold=max(0, int(render.exposure_threshold)),
            selection_mode=_normalize_selection_mode(generation.mode),
            scoring_require_single_token_codes=bool(generation.logit_require_single_token_codes),
            scoring_return_probabilities=bool(generation.logit_return_probabilities),
            scoring_fallback_mode=str(generation.logit_fallback_mode or "error"),
            scoring_max_candidates=max(1, int(generation.logit_max_candidates)),
            scoring_min_probability=generation.logit_min_probability,
            scoring_trace_top_n=max(1, int(generation.logit_trace_top_n)),
        ),
    )


def _normalize_selection_mode(value: str | None) -> str:
    normalized = str(value or "generate").strip().lower()
    return normalized or "generate"


__all__ = [
    "GenerationConfig",
    "LLMClientConfig",
    "OpenAIClientConfig",
    "RequestConfig",
    "RenderConfig",
    "RetrieverConfig",
    "SearchResult",
    "TransformersClientConfig",
    "TraversalConfig",
    "VLLMClientConfig",
    "runtime_retriever_config_from_config",
]
