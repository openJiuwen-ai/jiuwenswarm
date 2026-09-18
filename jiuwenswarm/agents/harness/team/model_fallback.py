# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model fallback wrapper for team member LLM calls.

When the primary model fails with a retryable error (quota exhausted,
rate limit, network error), automatically retry with the next model
in the fallback chain.
"""

from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Default error patterns that trigger fallback to the next model.
# Use word-boundary anchors (\b) and contextual prefixes to avoid false
# positives — e.g. a bare "429" could match a legitimate token count
# like "processed 4291 tokens", so we require it to appear as an HTTP
# status code or standalone error code.
_DEFAULT_FALLBACK_PATTERNS = [
    r"\bquota\b",
    r"rate[\s._-]?limit",
    r"\btoken[s]?\s+exhausted\b",
    r"insufficient[\s._-]?balance",
    r"model[\s._-]?not[\s._-]?available",
    r"\b(?:HTTP|status|code|error)[\s:]*429\b",
    r"\b(?:HTTP|status|code|error)[\s:]*503\b",
    r"\boverloaded\b",
    r"\bcapacity\b",
    r"\bbilling\b",
    r"\bcredit[s]?\b",
]


class ModelFallbackWrapper:
    """Wraps a primary LLM call with a fallback model chain.

    When the primary model fails with a retryable error (matched by
    ``error_patterns``), the wrapper tries each fallback model in order
    until one succeeds or all are exhausted.

    The wrapper supports two invocation modes:

    * **call** — standard async invocation via ``model.ainvoke()``
    * **stream** — streaming invocation via ``model.astream()``

    Both modes walk the same fallback chain and apply the same
    retryable-error matching logic.

    Usage::

        wrapper = ModelFallbackWrapper(
            primary_model=primary_llm,
            fallback_models=[fallback_llm_1, fallback_llm_2],
            primary_model_name="qwen3.8-max",
            fallback_model_names=["qwen3.7-max", "ali-glm-5.2"],
        )
        result = await wrapper.call(messages, **kwargs)
        # or for streaming:
        async for chunk in wrapper.stream(messages, **kwargs):
            process(chunk)
    """

    def __init__(
        self,
        primary_model: Any,
        fallback_models: list[Any],
        error_patterns: list[str] | None = None,
        primary_model_name: str = "",
        fallback_model_names: list[str] | None = None,
    ):
        self.primary_model = primary_model
        self.fallback_models = fallback_models
        self.primary_model_name = primary_model_name
        self.fallback_model_names = fallback_model_names or []
        self._error_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (error_patterns or _DEFAULT_FALLBACK_PATTERNS)
        ]

    def _is_retryable_error(self, error: Exception) -> bool:
        """Check if the error matches any retryable pattern."""
        error_msg = str(error)
        return any(p.search(error_msg) for p in self._error_patterns)

    def _build_model_chain(self) -> list[tuple[Any, str]]:
        """Build the ordered (model, name) chain from primary + fallbacks."""
        models: list[tuple[Any, str]] = [
            (self.primary_model, self.primary_model_name),
        ]
        for i, fb in enumerate(self.fallback_models):
            name = (
                self.fallback_model_names[i]
                if i < len(self.fallback_model_names)
                else f"fallback-{i}"
            )
            models.append((fb, name))
        return models

    def _log_fallback_success(self, model_name: str) -> None:
        """Log when a fallback model succeeds (no-op for primary)."""
        if model_name != self.primary_model_name:
            logger.info(
                "[ModelFallback] call succeeded with fallback model: %s "
                "(primary was %s)",
                model_name,
                self.primary_model_name,
            )

    @staticmethod
    def _log_retryable_error(model_name: str, error: Exception) -> None:
        """Log a retryable error and signal fallback advancement."""
        logger.warning(
            "[ModelFallback] model %s failed with retryable error: %s. "
            "Trying next fallback...",
            model_name,
            str(error)[:200],
        )

    @staticmethod
    def _log_non_retryable_error(model_name: str, error: Exception) -> None:
        """Log a non-retryable error (debug level — will be re-raised)."""
        logger.debug(
            "[ModelFallback] model %s failed with non-retryable error: %s",
            model_name,
            str(error)[:200],
        )

    @staticmethod
    def _log_all_failed(total: int, last_error: Exception) -> None:
        """Log when every model in the chain has failed."""
        logger.error(
            "[ModelFallback] all %d models failed. Last error: %s",
            total,
            str(last_error)[:500],
        )

    async def call(self, messages: list[dict], **kwargs) -> Any:
        """Execute LLM call with fallback chain.

        Tries primary model first via ``model.ainvoke()``. On retryable
        error, tries each fallback model in order. Returns the first
        successful result. Raises the last error if all models fail.
        """
        models = self._build_model_chain()
        last_error: Exception | None = None
        for model, model_name in models:
            try:
                result = await model.ainvoke(messages, **kwargs)
                self._log_fallback_success(model_name)
                return result
            except Exception as e:
                last_error = e
                if self._is_retryable_error(e):
                    self._log_retryable_error(model_name, e)
                else:
                    self._log_non_retryable_error(model_name, e)
                    raise

        self._log_all_failed(len(models), last_error)  # type: ignore[arg-type]
        raise last_error  # type: ignore[misc]

    async def stream(
        self, messages: list[dict], **kwargs
    ) -> AsyncIterator[Any]:
        """Execute LLM streaming call with fallback chain.

        Tries primary model first via ``model.astream()``. On retryable
        error, tries each fallback model in order. Yields chunks from
        the first successful model. Raises the last error if all models
        fail.

        Note: If a retryable error occurs *mid-stream* (after some chunks
        have already been yielded), the error is re-raised immediately
        rather than switching to a fallback — partial output cannot be
        seamlessly continued by a different model.
        """
        models = self._build_model_chain()
        last_error: Exception | None = None
        for model, model_name in models:
            yielded_any = False
            try:
                async for chunk in model.astream(messages, **kwargs):
                    yielded_any = True
                    yield chunk
                # Stream completed successfully
                self._log_fallback_success(model_name)
                return
            except Exception as e:
                last_error = e
                if yielded_any:
                    # Chunks already emitted — cannot seamlessly switch
                    # to a different model mid-stream
                    raise
                if self._is_retryable_error(e):
                    self._log_retryable_error(model_name, e)
                else:
                    self._log_non_retryable_error(model_name, e)
                    raise

        self._log_all_failed(len(models), last_error)  # type: ignore[arg-type]
        raise last_error  # type: ignore[misc]
