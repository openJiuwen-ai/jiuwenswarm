from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from time import perf_counter
from typing import Mapping, Sequence

from ..base import (
    CandidateScoringResult,
    GenerationConfig,
    LLMClientCapabilities,
    LLMStreamChunk,
    Message,
    ProgressiveLLMClient,
    UnsupportedCapability,
)
from ..base.scoring import build_candidate_scoring_result, prepare_candidate_token_ids
from ..base.tokenization import CandidateCodeTokenizer, join_messages

LOGGER = logging.getLogger("retrieval.llm.transformers_logit_selection")


@dataclass
class TransformersLogitSelectionClient(ProgressiveLLMClient):
    model_obj: object
    candidate_tokenizer: CandidateCodeTokenizer
    device: str = "cpu"
    generation_client: ProgressiveLLMClient | None = None

    name = "transformers_logit_selection"

    @classmethod
    def from_pretrained(
        cls,
        *,
        model_path: str,
        tokenizer_path: str | None = None,
        device: str = "auto",
        dtype: str = "auto",
        generation_client: ProgressiveLLMClient | None = None,
    ) -> "TransformersLogitSelectionClient":
        try:
            import torch
            from transformers import AutoModelForCausalLM
        except ImportError as exc:
            raise RuntimeError(
                "transformers and torch are required for the transformers logit-selection client"
            ) from exc

        resolved_device = (
            "cuda" if device == "auto" and torch.cuda.is_available() else ("cpu" if device == "auto" else device)
        )
        torch_dtype = None
        if dtype == "float16":
            torch_dtype = torch.float16
        elif dtype == "bfloat16":
            torch_dtype = torch.bfloat16
        elif dtype == "float32":
            torch_dtype = torch.float32
        resolved_tokenizer_path = tokenizer_path or model_path
        tokenizer = CandidateCodeTokenizer.from_pretrained(resolved_tokenizer_path)
        model_obj = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch_dtype, trust_remote_code=True)
        model_obj.to(resolved_device)
        model_obj.eval()
        return cls(
            model_obj=model_obj,
            candidate_tokenizer=tokenizer,
            device=resolved_device,
            generation_client=generation_client,
        )

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> LLMClientCapabilities:
        generation_caps = self.generation_client.capabilities if self.generation_client is not None else None
        return LLMClientCapabilities(
            completion=bool(generation_caps.completion) if generation_caps is not None else False,
            streaming=bool(generation_caps.streaming) if generation_caps is not None else False,
            candidate_scoring=True,
            trie_constrained_decoding=False,
            thread_safe=False,
            local_resources=True,
        )

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
        if self.generation_client is None:
            raise UnsupportedCapability("transformers logit-selection client does not provide generation")
        return self.generation_client.complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stop_sequences=stop_sequences,
            generation_config=generation_config,
            n=n,
            request_timeout=request_timeout,
        )

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
    ):
        if self.generation_client is None:
            yield from super().stream_complete(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                stop_sequences=stop_sequences,
                generation_config=generation_config,
                request_timeout=request_timeout,
                early_stop=early_stop,
            )
            return
        yield from self.generation_client.stream_complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stop_sequences=stop_sequences,
            generation_config=generation_config,
            request_timeout=request_timeout,
            early_stop=early_stop,
        )

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
        del model, request_timeout
        started = perf_counter()
        candidate_codes_tuple = tuple(str(code) for code in candidate_codes)
        encode_started = perf_counter()
        encoded = self.candidate_tokenizer.encode_many(candidate_codes_tuple, messages=messages)
        tokenization = prepare_candidate_token_ids(
            candidate_codes=candidate_codes_tuple,
            encoded_codes=encoded,
            require_single_token_codes=require_single_token_codes,
        )
        encode_ms = (perf_counter() - encode_started) * 1000.0
        if not tokenization.token_to_code:
            return CandidateScoringResult(
                scores=(),
                candidate_codes=tokenization.candidate_codes,
                candidate_token_ids=(),
                latency_breakdown={
                    "encode_ms": round(encode_ms, 3),
                    "backend_ms": 0.0,
                    "normalize_ms": 0.0,
                    "total_ms": round((perf_counter() - started) * 1000.0, 3),
                },
            )
        backend_started = perf_counter()
        scored_pairs = self._score_token_ids(messages=messages, candidate_token_ids=tokenization.candidate_token_ids)
        backend_ms = (perf_counter() - backend_started) * 1000.0
        result = build_candidate_scoring_result(
            tokenization=tokenization,
            scored_pairs=scored_pairs,
            code_to_canonical_id=code_to_canonical_id,
            latency_breakdown={
                "encode_ms": round(encode_ms, 3),
                "backend_ms": round(backend_ms, 3),
                "total_ms": round((perf_counter() - started) * 1000.0, 3),
            },
        )
        LOGGER.info(
            "transformers logit-selection complete candidates=%d returned=%d",
            len(candidate_codes_tuple),
            len(result.scores),
        )
        return result

    def _score_token_ids(
        self, *, messages: list[Message], candidate_token_ids: Sequence[int]
    ) -> list[tuple[int, float]]:
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("torch is required for the transformers logit-selection client") from exc
        tokenizer = self.candidate_tokenizer.tokenizer
        with self._lock:
            if hasattr(tokenizer, "apply_chat_template"):
                encoded = tokenizer.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    return_tensors="pt",
                    tokenize=True,
                    enable_thinking=False,
                    preserve_thinking=False,
                    add_vision_id=False,
                )
                if hasattr(encoded, "to"):
                    input_ids = encoded.to(self.device)
                else:
                    input_ids = torch.as_tensor(encoded, device=self.device)
                attention_mask = torch.ones_like(input_ids, device=self.device)
                model_inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
            else:
                prompt = join_messages(messages)
                model_inputs = tokenizer(prompt, return_tensors="pt")
                model_inputs = {key: value.to(self.device) for key, value in model_inputs.items()}
            with torch.no_grad():
                outputs = self.model_obj(**model_inputs)
            logits = outputs.logits[(slice(None), -1, slice(None))].detach().float()[0]
            pairs = [(int(token_id), float(logits[int(token_id)].item())) for token_id in candidate_token_ids]
        pairs.sort(key=lambda item: item[1], reverse=True)
        return pairs

    def close(self) -> None:
        if self.generation_client is not None:
            self.generation_client.close()


__all__ = ["TransformersLogitSelectionClient"]
