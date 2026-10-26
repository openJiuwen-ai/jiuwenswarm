from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

TransformersBackend: TypeAlias = Literal[
    "transformers",
    "transformers_prefix_cached",
    "transformers_prefix_cached_generation",
]


@dataclass(frozen=True)
class OpenAIClientConfig:
    """OpenAI-compatible LLM client configuration."""

    # Client type discriminator used by the resolver.
    backend: Literal["openai"] = "openai"

    # OpenAI-compatible model name used for generation requests.
    model: str = ""

    # Optional externally-created OpenAI-compatible client.
    client: Any | None = None

    # API key used when creating the OpenAI-compatible client internally.
    api_key: str = ""

    # OpenAI-compatible base URL.
    base_url: str = ""

    # Optional request seed for deterministic-compatible providers.
    seed: int | None = None

    # Provider-specific request body extensions, such as disabling thinking.
    extra_body: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VLLMClientConfig:
    """Local custom vLLM client configuration."""

    # Client type discriminator used by the resolver.
    backend: Literal["vllm"] = "vllm"

    # Local model directory loaded by custom vLLM.
    model_path: str = ""

    # Local tokenizer directory. Defaults to model_path in the runtime adapter.
    tokenizer_path: str = ""

    # Model name sent with requests and logs.
    request_model: str = ""

    # Custom vLLM startup and private adapter options.
    vllm_kwargs: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TransformersClientConfig:
    """Local transformers client configuration."""

    # Client type discriminator used by the resolver.
    backend: TransformersBackend = "transformers"

    # Local model directory.
    model_path: str = ""

    # Local tokenizer directory. Defaults to model_path in the runtime adapter.
    tokenizer_path: str = ""

    # Device selection, such as auto, cpu, or cuda:0.
    device: str = "auto"

    # Model dtype for local inference.
    dtype: str = "bfloat16"

    # Tensor parallel size for transformers local inference.
    tensor_parallel_size: int = 1

    # Data parallel size for transformers local inference.
    data_parallel_size: int = 1

    # Explicit device ids. Empty means the client resolves devices itself.
    device_ids: tuple[int, ...] = ()

    # Optional transformers attention implementation.
    attn_implementation: str = ""

    # Whether to enable torch.compile for the local model.
    torch_compile: bool = False

    # Optional tensor parallel plan name.
    tensor_parallel_plan: str = ""


LLMClientConfig: TypeAlias = OpenAIClientConfig | VLLMClientConfig | TransformersClientConfig


__all__ = [
    "LLMClientConfig",
    "OpenAIClientConfig",
    "TransformersClientConfig",
    "VLLMClientConfig",
]
