from __future__ import annotations

import json
from typing import Any

from .base import ProgressiveLLMClient
from .base.errors import UnsupportedCapability
from .openai_api import OpenAICompatibleClient
from .transformers_prefix_cached_generation import TransformersPrefixCachedGenerationClient
from .transformers_logit_selection import TransformersLogitSelectionClient
from .vllm import LocalVLLMClient
from ..tree.types import ProgressiveRetrieverConfig


def coerce_generation_client(client: Any | None) -> ProgressiveLLMClient | None:
    if client is None:
        return None
    if isinstance(client, ProgressiveLLMClient):
        return client
    if _is_openai_compatible_client(client):
        return OpenAICompatibleClient(client)
    raise TypeError(
        "llm client must be a ProgressiveLLMClient or an OpenAI-compatible client exposing chat.completions.create"
    )


def create_progressive_client(
    *,
    generation_client: ProgressiveLLMClient | None,
    config: ProgressiveRetrieverConfig,
) -> ProgressiveLLMClient | None:
    resolved_generation_client = generation_client
    if _needs_local_vllm_generation_client(config) and not isinstance(resolved_generation_client, LocalVLLMClient):
        model_path = str(config.generation_model_path or "").strip()
        tokenizer_path = str(config.generation_tokenizer_path or model_path).strip()
        if not model_path or not tokenizer_path:
            raise ValueError("local vllm generation requires generation_model_path or generation_tokenizer_path")
        vllm_kwargs = dict(config.generation_vllm_kwargs or {})
        vllm_kwargs.setdefault("tensor_parallel_size", max(1, int(config.generation_tp_size)))
        resolved_generation_client = LocalVLLMClient.from_pretrained(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            device=str(config.generation_device or "auto"),
            dtype=str(config.generation_dtype or "auto"),
            enable_prefix_caching=bool(config.prefix_cache_enabled),
            vllm_kwargs=vllm_kwargs,
            max_suffix_tokens=max(1, int(config.prefix_cache_max_suffix_tokens)),
            max_new_tokens=max(1, int(config.prefix_cache_max_new_tokens)),
        )
    if _needs_prefix_cached_generation_client(config):
        model_path = str(config.generation_model_path or "").strip()
        tokenizer_path = str(config.generation_tokenizer_path or model_path).strip()
        if not model_path or not tokenizer_path:
            raise ValueError("prefix-cached generation requires generation_model_path or generation_tokenizer_path")
        resolved_generation_client = TransformersPrefixCachedGenerationClient.from_pretrained(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            device=str(config.generation_device or "auto"),
            dtype=str(config.generation_dtype or "bfloat16"),
            tp_size=max(1, int(config.generation_tp_size)),
            dp_size=max(1, int(config.generation_dp_size)),
            device_ids=tuple(int(item) for item in config.generation_device_ids),
            attn_implementation=str(getattr(config, "generation_attn_implementation", "") or ""),
            torch_compile=bool(getattr(config, "generation_torch_compile", False)),
            tp_plan=str(getattr(config, "generation_tp_plan", "") or ""),
            max_suffix_tokens=max(1, int(config.prefix_cache_max_suffix_tokens)),
            max_new_tokens=max(1, int(config.prefix_cache_max_new_tokens)),
            pool_size_per_prefix=max(1, int(config.prefix_cache_request_pool_size)),
            on_pool_exhausted=str(config.prefix_cache_on_pool_exhausted or "reject"),
            slot_acquire_timeout_ms=max(0.0, float(config.prefix_cache_slot_acquire_timeout_ms)),
            slot_rebuild=str(config.prefix_cache_slot_rebuild or "async"),
        )
    if not _needs_logit_selection_client(config):
        return resolved_generation_client
    backend_name = str(config.scoring_backend or "").strip().lower()
    model_path = str(config.scoring_backend_model_path or "").strip()
    tokenizer_path = str(config.scoring_backend_tokenizer_path or model_path).strip()
    if not model_path or not tokenizer_path:
        return resolved_generation_client
    if backend_name == "transformers":
        return TransformersLogitSelectionClient.from_pretrained(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            device=str(config.scoring_backend_device or "auto"),
            dtype=str(config.scoring_backend_dtype or "auto"),
            generation_client=resolved_generation_client,
        )
    if backend_name == "vllm":
        raise UnsupportedCapability("local vllm client is generation-only and does not support logit selection")
    return resolved_generation_client


def progressive_client_cache_key(config: ProgressiveRetrieverConfig) -> tuple[object, ...] | None:
    parts: list[object] = []
    if _needs_prefix_cached_generation_client(config):
        backend_name = _normalized_generation_backend(config)
        model_path = str(config.generation_model_path or "").strip()
        tokenizer_path = str(config.generation_tokenizer_path or model_path).strip()
        if model_path and tokenizer_path:
            parts.extend(
                [
                    (
                        "generation",
                        backend_name,
                        model_path,
                        tokenizer_path,
                        str(config.generation_device or "auto"),
                        str(config.generation_dtype or "bfloat16"),
                        max(1, int(config.generation_tp_size)),
                        max(1, int(config.generation_dp_size)),
                        tuple(int(item) for item in config.generation_device_ids),
                        str(getattr(config, "generation_attn_implementation", "") or ""),
                        bool(getattr(config, "generation_torch_compile", False)),
                        str(getattr(config, "generation_tp_plan", "") or ""),
                        max(1, int(config.prefix_cache_request_pool_size)),
                        max(1, int(config.prefix_cache_max_suffix_tokens)),
                        max(1, int(config.prefix_cache_max_new_tokens)),
                        str(config.prefix_cache_on_pool_exhausted or "reject"),
                        max(0.0, float(config.prefix_cache_slot_acquire_timeout_ms)),
                        str(config.prefix_cache_slot_rebuild or "async"),
                    )
                ]
            )
    if _needs_local_vllm_generation_client(config):
        backend_name = _normalized_generation_backend(config)
        model_path = str(config.generation_model_path or "").strip()
        tokenizer_path = str(config.generation_tokenizer_path or model_path).strip()
        if model_path and tokenizer_path:
            parts.extend(
                [
                    (
                        "generation",
                        backend_name,
                        model_path,
                        tokenizer_path,
                        str(config.generation_device or "auto"),
                        str(config.generation_dtype or "auto"),
                        max(1, int(config.generation_tp_size)),
                        bool(config.prefix_cache_enabled),
                        max(1, int(config.prefix_cache_max_suffix_tokens)),
                        max(1, int(config.prefix_cache_max_new_tokens)),
                        json.dumps(
                            config.generation_vllm_kwargs or {}, ensure_ascii=False, sort_keys=True, default=str
                        ),
                    )
                ]
            )
    if not _needs_logit_selection_client(config):
        return tuple(parts) if parts else None
    logit_key = _logit_selection_cache_key(config)
    if logit_key is not None:
        parts.append(logit_key)
    return tuple(parts) if parts else None


def _logit_selection_cache_key(config: ProgressiveRetrieverConfig) -> tuple[object, ...] | None:
    if not _needs_logit_selection_client(config):
        return None
    backend_name = str(config.scoring_backend or "").strip().lower()
    if backend_name not in {"transformers", "vllm"}:
        return None
    model_path = str(config.scoring_backend_model_path or "").strip()
    tokenizer_path = str(config.scoring_backend_tokenizer_path or model_path).strip()
    if not model_path or not tokenizer_path:
        return None
    backend_options = (
        str(config.scoring_backend_device or "auto"),
        str(config.scoring_backend_dtype or "auto"),
        bool(config.scoring_backend_enable_prefix_caching),
        json.dumps(config.scoring_backend_vllm_kwargs or {}, ensure_ascii=False, sort_keys=True, default=str),
    )
    return (backend_name, model_path, tokenizer_path, backend_options)


def _needs_prefix_cached_generation_client(config: ProgressiveRetrieverConfig) -> bool:
    return bool(config.prefix_cache_enabled) and _normalized_generation_backend(config) in {
        "transformers_prefix_cached",
        "transformers_prefix_cached_generation",
    }


def _needs_local_vllm_generation_client(config: ProgressiveRetrieverConfig) -> bool:
    backend = _normalized_generation_backend(config)
    return backend in {"vllm", "local_vllm"}


def _normalized_generation_backend(config: ProgressiveRetrieverConfig) -> str:
    return str(config.generation_backend or "openai").strip().lower() or "openai"


def _needs_logit_selection_client(config: ProgressiveRetrieverConfig) -> bool:
    return (
        bool(config.single_forward_logit_selection_enabled)
        and str(config.selection_mode or "").strip().lower() == "logit_selection"
        and str(config.scoring_backend or "").strip().lower() in {"transformers", "vllm"}
    )


def _is_openai_compatible_client(client: Any) -> bool:
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None)
    create = getattr(completions, "create", None)
    return callable(create)


__all__ = [
    "coerce_generation_client",
    "create_progressive_client",
    "progressive_client_cache_key",
]
