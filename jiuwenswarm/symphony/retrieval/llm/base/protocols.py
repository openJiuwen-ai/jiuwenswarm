from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Mapping, Sequence

from .errors import UnsupportedCapability
from .types import (
    CandidateScoringResult,
    GenerationConfig,
    LLMClientCapabilities,
    LLMStreamChunk,
    Message,
)


class ProgressiveLLMClient(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def capabilities(self) -> LLMClientCapabilities:
        raise NotImplementedError

    def default_generation_config(self) -> GenerationConfig:
        return GenerationConfig()

    def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        stop_sequences: Sequence[str] | None = None,
        generation_config: GenerationConfig | None = None,
        n: int = 1,
        request_timeout: float | None = None,
    ) -> list[str]:
        raise UnsupportedCapability(f"{self.name} does not support completion")

    def stream_complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        stop_sequences: Sequence[str] | None = None,
        generation_config: GenerationConfig | None = None,
        request_timeout: float | None = None,
        early_stop: object | None = None,
    ) -> Iterable[str | LLMStreamChunk]:
        del early_stop
        outputs = self.complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stop_sequences=stop_sequences,
            generation_config=generation_config,
            n=1,
            request_timeout=request_timeout,
        )
        yield outputs[0] if outputs else ""

    def score_candidate_codes(
        self,
        *,
        model: str,
        messages: list[Message],
        candidate_codes: Sequence[str],
        code_to_canonical_id: Mapping[str, str],
        top_k: int | None = None,
        require_single_token_codes: bool = True,
        request_timeout: float | None = None,
    ) -> CandidateScoringResult:
        raise UnsupportedCapability(f"{self.name} does not support candidate scoring")

    def close(self) -> None:
        return None


__all__ = ["ProgressiveLLMClient"]
