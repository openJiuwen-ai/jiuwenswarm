from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping, Sequence

from ..base import (
    GenerationConfig,
    LLMClientCapabilities,
    LLMRequestError,
    LLMStreamChunk,
    Message,
    ProgressiveLLMClient,
)


class OpenAICompatibleClient(ProgressiveLLMClient):
    DEFAULT_TEMPERATURE = 0.0
    DEFAULT_TOP_P = 1.0
    DEFAULT_SEED = 1223

    name = "openai_api"

    def __init__(
        self,
        client: Any,
        *,
        log_io: bool = False,
        logger: logging.Logger | None = None,
        seed: int | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._log_io = bool(log_io)
        self._logger = logger or logging.getLogger(__name__)
        self._seed = int(self.DEFAULT_SEED if seed is None else seed)
        self._extra_body = dict(extra_body or {})

    @property
    def capabilities(self) -> LLMClientCapabilities:
        return LLMClientCapabilities(
            completion=True,
            streaming=True,
            candidate_scoring=False,
            trie_constrained_decoding=True,
            thread_safe=True,
            local_resources=False,
        )

    def default_generation_config(self) -> GenerationConfig:
        return GenerationConfig(seed=self._seed)

    def stream_complete(
        self,
        model: str,
        messages: list[Message],
        early_stop: object | None = None,
        *,
        max_tokens: int | None = None,
        stop_sequences: Sequence[str] | None = None,
        generation_config: GenerationConfig | None = None,
        request_timeout: float | None = None,
    ) -> Iterable[str | LLMStreamChunk]:
        config = generation_config or self.default_generation_config()
        controller = _EarlyStopController(early_stop)
        stop = list(controller.stop_sequences() or [])
        for item in stop_sequences or []:
            if item and item not in stop:
                stop.append(str(item))
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": self.extra_body_from_generation_config(config),
        }
        if config.seed is not None:
            kwargs["seed"] = int(config.seed)
        elif self._seed is not None:
            kwargs["seed"] = int(self._seed)
        if self._log_io:
            self._emit_io("LLM REQUEST", {"model": model, "messages": messages, "stream": True, "stop": list(stop)})
        if stop:
            kwargs["stop"] = stop
        if max_tokens is not None and max_tokens > 0:
            kwargs["max_tokens"] = int(max_tokens)
        if request_timeout is not None and float(request_timeout) > 0:
            kwargs["timeout"] = float(request_timeout)
        try:
            stream = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMRequestError(f"OpenAI-compatible streaming request failed: {exc}") from exc
        chunks: list[str] = []
        usage: dict[str, Any] | None = None
        try:
            for chunk in stream:
                chunk_usage = self._usage_to_dict(getattr(chunk, "usage", None))
                if chunk_usage:
                    usage = chunk_usage
                delta = self._extract_delta_content(chunk)
                if not delta:
                    if chunk_usage:
                        yield LLMStreamChunk("", usage=chunk_usage)
                    continue
                chunks.append(delta)
                current = "".join(chunks)
                yield LLMStreamChunk(delta, usage=chunk_usage)
                if controller.should_abort(current):
                    break
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        if self._log_io:
            self._emit_io(
                "LLM RESPONSE", {"model": model, "content": "".join(chunks), "stream": True, "usage": usage or {}}
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
        config = generation_config or self.default_generation_config()
        stop = [str(item) for item in (stop_sequences or []) if item]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "extra_body": self.extra_body_from_generation_config(config),
        }
        if config.seed is not None:
            kwargs["seed"] = int(config.seed)
        elif self._seed is not None:
            kwargs["seed"] = int(self._seed)
        if stop:
            kwargs["stop"] = stop
        if max_tokens is not None and max_tokens > 0:
            kwargs["max_tokens"] = int(max_tokens)
        if n > 1:
            kwargs["n"] = int(n)
        if request_timeout is not None and float(request_timeout) > 0:
            kwargs["timeout"] = float(request_timeout)
        if self._log_io:
            self._emit_io("LLM REQUEST", {"model": model, "messages": messages, "stream": False, "n": int(max(1, n))})
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMRequestError(f"OpenAI-compatible completion request failed: {exc}") from exc
        results = self._extract_completion_contents(response)
        if self._log_io:
            self._emit_io("LLM RESPONSE", {"model": model, "stream": False, "choices": results})
        return results

    def extra_body_from_generation_config(self, config: GenerationConfig | None) -> dict[str, Any]:
        resolved = config or self.default_generation_config()
        extra_body: dict[str, Any] = {
            "temperature": float(resolved.temperature),
            "top_p": float(resolved.top_p),
        }
        if resolved.disable_thinking:
            extra_body["thinking"] = {"type": "disabled"}
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}
        if resolved.seed is not None:
            extra_body["seed"] = int(resolved.seed)
        trie = resolved.constraints.trie
        if trie is not None:
            version = trie.version
            if not version:
                version = _constraint_version(trie.allowed_output_ids)
            extra_body["vllm_xargs"] = {
                "constraint_type": "leaf_id_multicid_trie",
                "constraint_version": version,
                "top_k": max(1, int(trie.top_k)),
                "leaf_ids_json": json.dumps(list(trie.allowed_output_ids), ensure_ascii=False),
                "excluded_leaf_ids_json": json.dumps(list(trie.excluded_output_ids), ensure_ascii=False),
            }
        extra_body.update(self._extra_body)
        return extra_body

    def _emit_io(self, title: str, payload: Mapping[str, Any]) -> None:
        text = f"\n=== {title} ===\n{dict(payload)}\n=== END {title} ===\n"
        try:
            self._logger.info(text)
        except Exception as exc:
            self._logger.debug("failed to emit OpenAI API IO log: %s", exc)

    @staticmethod
    def _extract_delta_content(chunk: Any) -> str:
        try:
            delta = chunk.choices[0].delta
        except Exception:
            return ""
        if isinstance(delta, Mapping):
            return str(delta.get("content") or "")
        return str(getattr(delta, "content", "") or "")

    @classmethod
    def _extract_completion_contents(cls, response: Any) -> list[str]:
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, Mapping):
            choices = response.get("choices")
        results: list[str] = []
        for choice in choices or []:
            message = getattr(choice, "message", None)
            if message is None and isinstance(choice, Mapping):
                message = choice.get("message")
            content = ""
            if isinstance(message, Mapping):
                content = str(message.get("content") or "")
            elif message is not None:
                content = str(getattr(message, "content", "") or "")
            results.append(content)
        return results

    @staticmethod
    def _usage_to_dict(usage: Any) -> dict[str, Any]:
        if usage is None:
            return {}
        if isinstance(usage, Mapping):
            return dict(usage)
        model_dump = getattr(usage, "model_dump", None)
        if callable(model_dump):
            try:
                return dict(model_dump())
            except Exception:
                return {}
        data: dict[str, Any] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value is not None:
                data[key] = value
        return data


class _EarlyStopController:
    def __init__(self, policy: object | None) -> None:
        self._policy = policy

    def stop_sequences(self) -> Sequence[str]:
        policy = self._policy
        if policy is None:
            return ()
        method = getattr(policy, "stop_sequences", None)
        if callable(method):
            try:
                return tuple(str(item) for item in (method() or ()) if item)
            except Exception:
                return ()
        return ()

    def should_abort(self, text: str) -> bool:
        policy = self._policy
        if policy is None:
            return False
        method = getattr(policy, "should_abort", None)
        if callable(method):
            try:
                return bool(method(text))
            except Exception:
                return False
        return False


def _constraint_version(allowed_output_ids: Sequence[str]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(list(allowed_output_ids), ensure_ascii=False).encode("utf-8")).hexdigest()


__all__ = ["OpenAICompatibleClient"]
