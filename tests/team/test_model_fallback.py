# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ModelFallbackWrapper.

Covers:
- Normal call path (primary succeeds)
- Fallback path (primary fails, fallback succeeds)
- All models fail
- Error pattern matching (retryable vs non-retryable)
- Stream method (if applicable)
- Edge cases: empty fallback list, custom error patterns
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.team.model_fallback import (
    ModelFallbackWrapper,
    _DEFAULT_FALLBACK_PATTERNS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeModel:
    """Minimal mock model with async ``ainvoke`` and ``astream`` methods."""

    def __init__(self, *, side_effect=None, return_value="ok"):
        self.ainvoke = AsyncMock(side_effect=side_effect, return_value=return_value)
        # astream is an async generator — side_effect/return_value handled in tests
        self.astream = AsyncMock()


def _make_wrapper(
    primary_side_effect=None,
    primary_return="primary_ok",
    fallbacks=None,
    error_patterns=None,
    primary_name="primary-model",
    fallback_names=None,
) -> ModelFallbackWrapper:
    """Convenience factory for a wrapper with one primary + optional fallbacks."""
    primary = FakeModel(side_effect=primary_side_effect, return_value=primary_return)
    fb_models = []
    for fb in (fallbacks or []):
        fb_models.append(
            FakeModel(
                side_effect=fb.get("side_effect"),
                return_value=fb.get("return_value", "fb_ok"),
            )
        )
    return ModelFallbackWrapper(
        primary_model=primary,
        fallback_models=fb_models,
        error_patterns=error_patterns,
        primary_model_name=primary_name,
        fallback_model_names=fallback_names or [f"fb-{i}" for i in range(len(fb_models))],
    )


# ===========================================================================
# Tests: Normal call path (primary succeeds)
# ===========================================================================

class TestPrimarySuccess:
    """Primary model succeeds — no fallback needed."""

    @pytest.mark.asyncio
    async def test_primary_returns_value(self):
        """Primary model call succeeds and returns expected result."""
        wrapper = _make_wrapper(primary_return="hello")
        result = await wrapper.call([{"role": "user", "content": "hi"}])
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_primary_called_with_correct_args(self):
        """Primary model receives the messages and kwargs."""
        wrapper = _make_wrapper()
        msgs = [{"role": "user", "content": "test"}]
        await wrapper.call(msgs, temperature=0.7)
        wrapper.primary_model.ainvoke.assert_awaited_once_with(msgs, temperature=0.7)

    @pytest.mark.asyncio
    async def test_no_fallback_called_when_primary_succeeds(self):
        """Fallback models are never invoked when primary succeeds."""
        fb = FakeModel(return_value="should_not_be_called")
        wrapper = ModelFallbackWrapper(
            primary_model=FakeModel(return_value="primary_ok"),
            fallback_models=[fb],
            primary_model_name="p",
            fallback_model_names=["f"],
        )
        await wrapper.call([])
        fb.ainvoke.assert_not_awaited()


# ===========================================================================
# Tests: Fallback path (primary fails, fallback succeeds)
# ===========================================================================

class TestFallbackPath:
    """Primary fails with retryable error → fallback succeeds."""

    @pytest.mark.asyncio
    async def test_fallback_on_quota_error(self):
        """Quota error triggers fallback to next model."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("quota exceeded"),
            fallbacks=[{"return_value": "fallback_ok"}],
        )
        result = await wrapper.call([])
        assert result == "fallback_ok"

    @pytest.mark.asyncio
    async def test_fallback_on_rate_limit_error(self):
        """Rate limit error triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("rate_limit exceeded"),
            fallbacks=[{"return_value": "fb_result"}],
        )
        result = await wrapper.call([])
        assert result == "fb_result"

    @pytest.mark.asyncio
    async def test_fallback_on_429_error(self):
        """HTTP 429 error triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("HTTP 429 Too Many Requests"),
            fallbacks=[{"return_value": "recovered"}],
        )
        result = await wrapper.call([])
        assert result == "recovered"

    @pytest.mark.asyncio
    async def test_fallback_on_503_error(self):
        """HTTP 503 error triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("HTTP 503 Service Unavailable"),
            fallbacks=[{"return_value": "ok_503"}],
        )
        result = await wrapper.call([])
        assert result == "ok_503"

    @pytest.mark.asyncio
    async def test_fallback_chain_second_model(self):
        """First fallback also fails → second fallback succeeds."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("quota exhausted"),
            fallbacks=[
                {"side_effect": Exception("rate_limit hit"), "return_value": None},
                {"return_value": "second_fb_ok"},
            ],
        )
        result = await wrapper.call([])
        assert result == "second_fb_ok"

    @pytest.mark.asyncio
    async def test_fallback_on_overloaded_error(self):
        """'overloaded' keyword triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("server overloaded"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_insufficient_balance(self):
        """'insufficient balance' triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("insufficient balance for this request"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_model_not_available(self):
        """'model not available' triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("model not available right now"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_billing_error(self):
        """'billing' keyword triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("billing issue detected"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_credit_error(self):
        """'credit' keyword triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("no credit remaining"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_capacity_error(self):
        """'capacity' keyword triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("capacity exceeded"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_on_token_exhausted(self):
        """'tokens exhausted' triggers fallback."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("tokens exhausted for this period"),
            fallbacks=[{"return_value": "ok"}],
        )
        result = await wrapper.call([])
        assert result == "ok"


# ===========================================================================
# Tests: All models fail
# ===========================================================================

class TestAllModelsFail:
    """All models in the chain fail → last error is raised."""

    @pytest.mark.asyncio
    async def test_all_fail_raises_last_error(self):
        """When every model fails with retryable errors, the last error is raised."""
        last_err = Exception("final failure")
        wrapper = _make_wrapper(
            primary_side_effect=Exception("quota"),
            fallbacks=[
                {"side_effect": Exception("rate_limit")},
                {"side_effect": last_err},
            ],
        )
        with pytest.raises(Exception, match="final failure"):
            await wrapper.call([])

    @pytest.mark.asyncio
    async def test_single_model_no_fallback_fails(self):
        """Primary fails, no fallbacks → error raised immediately."""
        wrapper = _make_wrapper(
            primary_side_effect=Exception("quota exceeded"),
            fallbacks=[],
        )
        with pytest.raises(Exception, match="quota exceeded"):
            await wrapper.call([])


# ===========================================================================
# Tests: Non-retryable error (no fallback triggered)
# ===========================================================================

class TestNonRetryableError:
    """Non-retryable errors should NOT trigger fallback."""

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self):
        """A non-matching error message raises without trying fallbacks."""
        fb = FakeModel(return_value="should_not_reach")
        wrapper = ModelFallbackWrapper(
            primary_model=FakeModel(side_effect=ValueError("invalid input format")),
            fallback_models=[fb],
            primary_model_name="p",
            fallback_model_names=["f"],
        )
        with pytest.raises(ValueError, match="invalid input format"):
            await wrapper.call([])
        fb.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_syntax_error_no_fallback(self):
        """SyntaxError-like errors don't match any pattern."""
        wrapper = _make_wrapper(
            primary_side_effect=TypeError("unexpected type"),
            fallbacks=[{"return_value": "nope"}],
        )
        with pytest.raises(TypeError, match="unexpected type"):
            await wrapper.call([])


# ===========================================================================
# Tests: Error pattern matching
# ===========================================================================

class TestErrorPatternMatching:
    """Test _is_retryable_error with various error messages."""

    def test_default_patterns_cover_expected_errors(self):
        """All default patterns match their intended error types."""
        wrapper = _make_wrapper()
        retryable_messages = [
            "quota exceeded",
            "rate.limit hit",
            "rate_limit exceeded",
            "tokens exhausted",
            "insufficient balance",
            "model not available",
            "HTTP 429",
            "error 429",
            "status: 503",
            "server overloaded",
            "capacity reached",
            "billing error",
            "no credits remaining",
        ]
        for msg in retryable_messages:
            assert wrapper._is_retryable_error(Exception(msg)), f"Should match: {msg}"

    def test_non_retryable_messages(self):
        """Messages that should NOT trigger fallback."""
        wrapper = _make_wrapper()
        non_retryable = [
            "invalid API key",
            "connection timeout",
            "permission denied",
            "file not found",
            "syntax error in prompt",
            "null pointer",
            "processed 4291 tokens",  # bare 4291 should NOT match 429 pattern
        ]
        for msg in non_retryable:
            assert not wrapper._is_retryable_error(Exception(msg)), f"Should NOT match: {msg}"

    def test_case_insensitive_matching(self):
        """Pattern matching is case-insensitive."""
        wrapper = _make_wrapper()
        assert wrapper._is_retryable_error(Exception("QUOTA EXCEEDED"))
        assert wrapper._is_retryable_error(Exception("Rate_Limit"))
        assert wrapper._is_retryable_error(Exception("OVERLOADED"))

    def test_custom_error_patterns(self):
        """Custom patterns override defaults."""
        wrapper = _make_wrapper(error_patterns=["custom_error", "my_special_fail"])
        # Custom patterns match
        assert wrapper._is_retryable_error(Exception("custom_error occurred"))
        assert wrapper._is_retryable_error(Exception("my_special_fail"))
        # Default patterns no longer match
        assert not wrapper._is_retryable_error(Exception("quota exceeded"))

    def test_regex_patterns(self):
        """Patterns are compiled as regex."""
        wrapper = _make_wrapper(error_patterns=[r"error_\d{3}"])
        assert wrapper._is_retryable_error(Exception("error_500 occurred"))
        assert wrapper._is_retryable_error(Exception("error_404"))
        assert not wrapper._is_retryable_error(Exception("error_abc"))


# ===========================================================================
# Tests: Edge cases
# ===========================================================================

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_messages_list(self):
        """Call with empty messages list works."""
        wrapper = _make_wrapper(primary_return="ok")
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_names_shorter_than_models(self):
        """When fallback_model_names is shorter, auto-generated names are used."""
        primary = FakeModel(side_effect=Exception("quota"))
        fb1 = FakeModel(side_effect=Exception("rate_limit"))
        fb2 = FakeModel(return_value="ok")
        wrapper = ModelFallbackWrapper(
            primary_model=primary,
            fallback_models=[fb1, fb2],
            primary_model_name="p",
            fallback_model_names=["fb-0"],  # Only one name for two models
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fallback_names_none(self):
        """When fallback_model_names is None, auto-generated names are used."""
        primary = FakeModel(side_effect=Exception("quota"))
        fb = FakeModel(return_value="ok")
        wrapper = ModelFallbackWrapper(
            primary_model=primary,
            fallback_models=[fb],
            primary_model_name="p",
            fallback_model_names=None,
        )
        result = await wrapper.call([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_kwargs_passed_to_all_models(self):
        """kwargs are forwarded to each model's ainvoke."""
        primary = FakeModel(side_effect=Exception("quota"))
        fb = FakeModel(return_value="ok")
        wrapper = ModelFallbackWrapper(
            primary_model=primary,
            fallback_models=[fb],
            primary_model_name="p",
            fallback_model_names=["f"],
        )
        await wrapper.call([{"role": "user", "content": "hi"}], temperature=0.5, max_tokens=100)
        primary.ainvoke.assert_awaited_once()
        fb.ainvoke.assert_awaited_once()
        # Check kwargs were passed
        call_args = fb.ainvoke.call_args
        assert call_args[1]["temperature"] == 0.5
        assert call_args[1]["max_tokens"] == 100

    def test_wrapper_attributes(self):
        """Wrapper stores attributes correctly."""
        primary = FakeModel()
        fb = FakeModel()
        wrapper = ModelFallbackWrapper(
            primary_model=primary,
            fallback_models=[fb],
            primary_model_name="my-primary",
            fallback_model_names=["my-fallback"],
        )
        assert wrapper.primary_model is primary
        assert wrapper.fallback_models == [fb]
        assert wrapper.primary_model_name == "my-primary"
        assert wrapper.fallback_model_names == ["my-fallback"]

    @pytest.mark.asyncio
    async def test_default_patterns_constant(self):
        """_DEFAULT_FALLBACK_PATTERNS is a non-empty list of strings."""
        assert isinstance(_DEFAULT_FALLBACK_PATTERNS, list)
        assert len(_DEFAULT_FALLBACK_PATTERNS) > 0
        for p in _DEFAULT_FALLBACK_PATTERNS:
            assert isinstance(p, str)
