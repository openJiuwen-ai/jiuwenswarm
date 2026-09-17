from __future__ import annotations

import json
import logging
from typing import Any

from .base import ProgressiveLLMClient
from .base.errors import UnsupportedCapability
from .config import LLMClientConfig, TransformersClientConfig, VLLMClientConfig
from .openai_api import OpenAICompatibleClient
from .transformers_prefix_cached_generation import TransformersPrefixCachedGenerationClient
from .transformers_logit_selection import TransformersLogitSelectionClient
from .vllm import LocalVLLMClient
from ..tree.types import ProgressiveRetrieverConfig

LOGGER = logging.getLogger(__name__)


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
    llm_client_config = config.llm_client_config
    if (
        _needs_local_vllm_generation_client(llm_client_config)
        and not isinstance(resolved_generation_client, LocalVLLMClient)
    ):
        if not isinstance(llm_client_config, VLLMClientConfig):
            raise TypeError("local vllm generation requires VLLMClientConfig")
        vllm_config = llm_client_config
        model_path = str(vllm_config.model_path or "").strip()
        tokenizer_path = str(vllm_config.tokenizer_path or model_path).strip()
        if not model_path or not tokenizer_path:
            raise ValueError("local vllm generation requires model_path or tokenizer_path")
        vllm_kwargs, device, dtype = _vllm_runtime_options(vllm_config)
        vllm_kwargs.setdefault(
            "tensor_parallel_size",
            max(1, int(vllm_kwargs.get("tensor_parallel_size", 1) or 1)),
        )
        LOGGER.info(
            "creating progressive generation client backend=%s model=%s tokenizer=%s dtype=%s",
            _client_backend(vllm_config),
            model_path,
            tokenizer_path,
            dtype,
        )
        resolved_generation_client = LocalVLLMClient.from_pretrained(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            device=device,
            dtype=dtype,
            vllm_kwargs=vllm_kwargs,
            max_new_tokens=_derived_cache_max_new_tokens(config),
        )
    elif _needs_prefix_cached_generation_client(llm_client_config):
        if not isinstance(llm_client_config, TransformersClientConfig):
            raise TypeError("prefix-cached generation requires TransformersClientConfig")
        transformers_config = llm_client_config
        model_path = str(transformers_config.model_path or "").strip()
        tokenizer_path = str(transformers_config.tokenizer_path or model_path).strip()
        if not model_path or not tokenizer_path:
            raise ValueError("prefix-cached generation requires model_path or tokenizer_path")
        LOGGER.info(
            "creating progressive generation client backend=%s model=%s tokenizer=%s dtype=%s",
            _client_backend(transformers_config),
            model_path,
            tokenizer_path,
            str(transformers_config.dtype or "bfloat16"),
        )
        resolved_generation_client = TransformersPrefixCachedGenerationClient.from_pretrained(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            device=str(transformers_config.device or "auto"),
            dtype=str(transformers_config.dtype or "bfloat16"),
            tp_size=max(1, int(transformers_config.tensor_parallel_size)),
            dp_size=max(1, int(transformers_config.data_parallel_size)),
            device_ids=tuple(int(item) for item in transformers_config.device_ids),
            attn_implementation=str(transformers_config.attn_implementation or ""),
            torch_compile=bool(transformers_config.torch_compile),
            tp_plan=str(transformers_config.tensor_parallel_plan or ""),
            max_new_tokens=_derived_cache_max_new_tokens(config),
        )
    if not _needs_logit_selection_client(config):
        return resolved_generation_client
    backend_name = _client_backend(llm_client_config)
    model_path = _local_model_path(llm_client_config)
    tokenizer_path = _local_tokenizer_path(llm_client_config)
    LOGGER.info(
        "progressive logit selection requested backend=%s model=%s tokenizer=%s generation_client=%s",
        backend_name,
        model_path,
        tokenizer_path,
        type(resolved_generation_client).__name__ if resolved_generation_client is not None else "None",
    )
    if not model_path or not tokenizer_path:
        LOGGER.warning(
            "progressive logit selection skipped because local model/tokenizer path is empty backend=%s",
            backend_name,
        )
        return resolved_generation_client
    if isinstance(llm_client_config, TransformersClientConfig):
        scoring_client = TransformersLogitSelectionClient.from_pretrained(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            device=str(llm_client_config.device or "auto"),
            dtype=str(llm_client_config.dtype or "auto"),
            generation_client=resolved_generation_client,
        )
        LOGGER.info(
            "progressive logit selection client ready backend=%s client=%s",
            backend_name,
            type(scoring_client).__name__,
        )
        return scoring_client
    if isinstance(llm_client_config, VLLMClientConfig):
        LOGGER.warning("progressive logit selection is unsupported for local vllm backend")
        raise UnsupportedCapability("local vllm client is generation-only and does not support logit selection")
    LOGGER.info("progressive logit selection has no local scoring client for backend=%s", backend_name)
    return resolved_generation_client


def progressive_client_cache_key(config: ProgressiveRetrieverConfig) -> tuple[object, ...] | None:
    parts: list[object] = []
    llm_client_config = config.llm_client_config
    if _needs_prefix_cached_generation_client(llm_client_config):
        if not isinstance(llm_client_config, TransformersClientConfig):
            raise TypeError("prefix-cached generation requires TransformersClientConfig")
        transformers_config = llm_client_config
        backend_name = _client_backend(transformers_config)
        model_path = str(transformers_config.model_path or "").strip()
        tokenizer_path = str(transformers_config.tokenizer_path or model_path).strip()
        if model_path and tokenizer_path:
            parts.extend(
                [
                    (
                        "generation",
                        backend_name,
                        model_path,
                        tokenizer_path,
                        str(transformers_config.device or "auto"),
                        str(transformers_config.dtype or "bfloat16"),
                        max(1, int(transformers_config.tensor_parallel_size)),
                        max(1, int(transformers_config.data_parallel_size)),
                        tuple(int(item) for item in transformers_config.device_ids),
                        str(transformers_config.attn_implementation or ""),
                        bool(transformers_config.torch_compile),
                        str(transformers_config.tensor_parallel_plan or ""),
                        _derived_cache_max_new_tokens(config),
                    )
                ]
            )
    elif _needs_local_vllm_generation_client(llm_client_config):
        if not isinstance(llm_client_config, VLLMClientConfig):
            raise TypeError("local vllm generation requires VLLMClientConfig")
        vllm_config = llm_client_config
        backend_name = _client_backend(vllm_config)
        model_path = str(vllm_config.model_path or "").strip()
        tokenizer_path = str(vllm_config.tokenizer_path or model_path).strip()
        if model_path and tokenizer_path:
            vllm_kwargs, device, dtype = _vllm_runtime_options(vllm_config)
            parts.extend(
                [
                    (
                        "generation",
                        backend_name,
                        model_path,
                        tokenizer_path,
                        device,
                        dtype,
                        max(1, int(vllm_kwargs.get("tensor_parallel_size", 1) or 1)),
                        _derived_cache_max_new_tokens(config),
                        json.dumps(vllm_kwargs, ensure_ascii=False, sort_keys=True, default=str),
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
    llm_client_config = config.llm_client_config
    backend_name = _client_backend(llm_client_config)
    if not isinstance(llm_client_config, (TransformersClientConfig, VLLMClientConfig)):
        return None
    model_path = _local_model_path(llm_client_config)
    tokenizer_path = _local_tokenizer_path(llm_client_config)
    if not model_path or not tokenizer_path:
        return None
    if isinstance(llm_client_config, VLLMClientConfig):
        vllm_kwargs, device, dtype = _vllm_runtime_options(llm_client_config)
        backend_options = (device, dtype, json.dumps(vllm_kwargs, ensure_ascii=False, sort_keys=True, default=str))
    else:
        backend_options = (str(llm_client_config.device or "auto"), str(llm_client_config.dtype or "auto"))
    return (backend_name, model_path, tokenizer_path, backend_options)


def _needs_prefix_cached_generation_client(config: LLMClientConfig) -> bool:
    return isinstance(config, TransformersClientConfig) and _client_backend(config) in {
        "transformers_prefix_cached",
        "transformers_prefix_cached_generation",
    }


def _needs_local_vllm_generation_client(config: LLMClientConfig) -> bool:
    return isinstance(config, VLLMClientConfig) and _client_backend(config) in {"vllm", "local_vllm"}


def _client_backend(config: LLMClientConfig) -> str:
    return str(getattr(config, "backend", "openai") or "openai").strip().lower() or "openai"


def _needs_logit_selection_client(config: ProgressiveRetrieverConfig) -> bool:
    return str(config.selection_mode or "").strip().lower() == "logit_selection"


def _local_model_path(config: LLMClientConfig) -> str:
    return str(getattr(config, "model_path", "") or "").strip()


def _local_tokenizer_path(config: LLMClientConfig) -> str:
    model_path = _local_model_path(config)
    return str(getattr(config, "tokenizer_path", "") or model_path).strip()


def _vllm_runtime_options(config: VLLMClientConfig) -> tuple[dict[str, object], str, str]:
    vllm_kwargs = dict(config.vllm_kwargs or {})
    dtype = str(vllm_kwargs.pop("dtype", "bfloat16") or "bfloat16")
    device = str(vllm_kwargs.pop("device", "") or "")
    request_model = str(config.request_model or "").strip()
    if request_model and "request_model" not in vllm_kwargs:
        vllm_kwargs["request_model"] = request_model
    return vllm_kwargs, device, dtype


def _derived_cache_max_new_tokens(config: ProgressiveRetrieverConfig) -> int:
    return max(
        1,
        int(config.max_tokens),
        _compact_code_generation_max_tokens(
            config.top_k,
            generated_decimal_codes=not bool(config.compact_boundary_codebook),
        ),
    )


def _compact_code_generation_max_tokens(top_k: int, *, generated_decimal_codes: bool) -> int:
    code_token_budget = 8 if generated_decimal_codes else 1
    return max(1, (code_token_budget + 1) * max(1, int(top_k)) - 1)


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
