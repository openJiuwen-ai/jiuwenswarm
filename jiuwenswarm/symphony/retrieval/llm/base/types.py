from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

Message = dict[str, str]


@dataclass(frozen=True)
class LLMClientCapabilities:
    # Supports complete(...) one-shot generation.
    completion: bool = True
    # Supports stream_complete(...) streaming generation.
    streaming: bool = False
    # Supports score_candidate_codes(...) for logit-selection.
    candidate_scoring: bool = False
    # Supports structured trie constraints through GenerationConfig.
    trie_constrained_decoding: bool = False
    # Supports progressive retriever fixed-prefix KV cache handles.
    progressive_prefix_kv_cache: bool = False
    # Safe to invoke concurrently from multiple threads.
    thread_safe: bool = True
    # Holds local model/device/cache resources that may need lifecycle control.
    local_resources: bool = False


class LLMStreamChunk(str):
    def __new__(cls, content: str, *, usage: Mapping[str, Any] | None = None):
        obj = str.__new__(cls, content)
        obj.usage = dict(usage or {})
        return obj


@dataclass(frozen=True)
class TrieConstraint:
    allowed_output_ids: tuple[str, ...]
    excluded_output_ids: tuple[str, ...] = ()
    top_k: int = 1
    version: str | None = None


@dataclass(frozen=True)
class GenerationConstraints:
    trie: TrieConstraint | None = None


@dataclass(frozen=True)
class PromptCacheHint:
    handle: Any | None = None
    suffix_text: str = ""
    suffix_token_ids: tuple[int, ...] | None = None
    expected_prefix_len: int | None = None


@dataclass(frozen=True)
class GenerationConfig:
    disable_thinking: bool = True
    temperature: float = 0.0
    top_p: float = 1.0
    seed: int | None = 1223
    constraints: GenerationConstraints = field(default_factory=GenerationConstraints)
    prompt_cache: PromptCacheHint | None = None


@dataclass(frozen=True)
class CandidateScore:
    code: str
    canonical_id: str
    token_id: int | None
    logit: float
    probability: float
    rank: int


@dataclass(frozen=True)
class CandidateScoringResult:
    scores: tuple[CandidateScore, ...]
    candidate_codes: tuple[str, ...]
    candidate_token_ids: tuple[int, ...] = ()
    latency_breakdown: Mapping[str, float] = field(default_factory=dict)


def generation_config_to_debug_dict(config: GenerationConfig | None) -> dict[str, Any]:
    resolved = config or GenerationConfig()
    constraints: dict[str, Any] = {}
    if resolved.constraints.trie is not None:
        trie = resolved.constraints.trie
        constraints["trie"] = {
            "allowed_output_ids": list(trie.allowed_output_ids),
            "excluded_output_ids": list(trie.excluded_output_ids),
            "top_k": int(trie.top_k),
            "version": trie.version,
        }
    prompt_cache: dict[str, Any] = {}
    if resolved.prompt_cache is not None:
        hint = resolved.prompt_cache
        handle = hint.handle
        prompt_cache = {
            "enabled": handle is not None,
            "suffix_text_len": len(str(hint.suffix_text or "")),
            "suffix_token_count": None if hint.suffix_token_ids is None else len(tuple(hint.suffix_token_ids)),
            "expected_prefix_len": hint.expected_prefix_len,
        }
        for attr in ("cache_id", "prefix_len", "dp_replica_id"):
            value = getattr(handle, attr, None)
            if value is not None:
                prompt_cache[attr] = value
    return {
        "disable_thinking": bool(resolved.disable_thinking),
        "temperature": float(resolved.temperature),
        "top_p": float(resolved.top_p),
        "seed": resolved.seed,
        "constraints": constraints,
        "prompt_cache": prompt_cache,
    }


__all__ = [
    "CandidateScore",
    "CandidateScoringResult",
    "GenerationConfig",
    "GenerationConstraints",
    "LLMClientCapabilities",
    "LLMStreamChunk",
    "Message",
    "PromptCacheHint",
    "TrieConstraint",
    "generation_config_to_debug_dict",
]
