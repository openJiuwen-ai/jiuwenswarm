# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for OpenRouter attribution detection and header injection."""

import pytest

from jiuwenswarm.common.openrouter_attribution import (
    OPENROUTER_ATTRIBUTION_HEADERS,
    inject_attribution_headers,
    inject_attribution_to_config,
    is_openrouter_client_config,
    is_openrouter_provider,
)


class TestIsOpenRouterProvider:
    @pytest.mark.parametrize("provider", ["OpenRouter", "openrouter", " OPENROUTER "])
    def test_matches_case_insensitively_with_whitespace(self, provider: str):
        assert is_openrouter_provider(provider) is True

    @pytest.mark.parametrize("provider", ["OpenAI", "Anthropic", "DeepSeek", "123"])
    def test_rejects_other_providers(self, provider):
        assert is_openrouter_provider(provider) is False

    @pytest.mark.parametrize("provider", [None, ""])
    def test_rejects_missing_provider(self, provider):
        assert is_openrouter_provider(provider) is False


class TestIsOpenRouterClientConfig:
    def test_endpoint_profile_openrouter_is_matched(self):
        assert is_openrouter_client_config({"endpoint_profile": "openrouter"}) is True

    def test_endpoint_profile_is_normalized(self):
        assert is_openrouter_client_config({"endpoint_profile": " OpenRouter "}) is True

    def test_legacy_client_provider_still_matches(self):
        assert is_openrouter_client_config({"client_provider": "OpenRouter"}) is True

    def test_endpoint_profile_wins_over_legacy_provider(self):
        # profile is the new source of truth; a non-openrouter profile with a
        # legacy-looking provider string must not match.
        assert (
            is_openrouter_client_config(
                {"endpoint_profile": "anthropic", "client_provider": "OpenAI"}
            )
            is False
        )

    @pytest.mark.parametrize("mcc", [{}, {"endpoint_profile": None}, {"client_provider": "OpenAI"}])
    def test_rejects_non_openrouter_configs(self, mcc):
        assert is_openrouter_client_config(mcc) is False


class TestInjectAttributionHeaders:
    def test_non_openrouter_config_is_returned_untouched(self):
        mcc = {"client_provider": "OpenAI"}
        result = inject_attribution_headers(mcc)
        assert result is mcc
        assert "custom_headers" not in mcc

    def test_missing_custom_headers_gets_full_attribution_block(self):
        mcc = {"endpoint_profile": "openrouter"}
        result = inject_attribution_headers(mcc)
        assert result is mcc
        assert mcc["custom_headers"] == OPENROUTER_ATTRIBUTION_HEADERS

    def test_existing_headers_are_preserved_and_not_overwritten(self):
        mcc = {
            "endpoint_profile": "openrouter",
            "custom_headers": {
                "HTTP-Referer": "https://myapp.example/",
                "X-Custom": "keep-me",
            },
        }
        inject_attribution_headers(mcc)
        headers = mcc["custom_headers"]
        assert headers["HTTP-Referer"] == "https://myapp.example/"
        assert headers["X-Custom"] == "keep-me"
        assert headers["X-OpenRouter-Title"] == OPENROUTER_ATTRIBUTION_HEADERS["X-OpenRouter-Title"]
        assert headers["X-OpenRouter-Categories"] == (
            OPENROUTER_ATTRIBUTION_HEADERS["X-OpenRouter-Categories"]
        )

    def test_input_custom_headers_dict_is_not_mutated(self):
        original = {"X-Custom": "keep-me"}
        mcc = {"endpoint_profile": "openrouter", "custom_headers": original}
        inject_attribution_headers(mcc)
        assert original == {"X-Custom": "keep-me"}
        assert mcc["custom_headers"] is not original

    def test_non_dict_custom_headers_is_replaced(self):
        mcc = {"endpoint_profile": "openrouter", "custom_headers": "garbage"}
        inject_attribution_headers(mcc)
        assert mcc["custom_headers"] == OPENROUTER_ATTRIBUTION_HEADERS

    def test_legacy_provider_config_also_receives_headers(self):
        mcc = {"client_provider": "OpenRouter"}
        inject_attribution_headers(mcc)
        assert mcc["custom_headers"]["X-OpenRouter-Title"] == "JiuwenSwarm"


class TestInjectAttributionToConfig:
    def _openrouter_mcc(self) -> dict:
        return {"endpoint_profile": "openrouter"}

    def test_injects_into_models_defaults_list_entries(self):
        mcc_a, mcc_b = self._openrouter_mcc(), self._openrouter_mcc()
        config = {
            "models": {
                "defaults": [
                    {"model_client_config": mcc_a},
                    "not-a-dict",
                    {"no_mcc": True},
                    {"model_client_config": mcc_b},
                ]
            }
        }
        inject_attribution_to_config(config)
        assert mcc_a["custom_headers"] == OPENROUTER_ATTRIBUTION_HEADERS
        assert mcc_b["custom_headers"] == OPENROUTER_ATTRIBUTION_HEADERS

    def test_injects_into_models_default_single(self):
        mcc = self._openrouter_mcc()
        config = {"models": {"default": {"model_client_config": mcc}}}
        inject_attribution_to_config(config)
        assert mcc["custom_headers"] == OPENROUTER_ATTRIBUTION_HEADERS

    def test_injects_into_react_section(self):
        mcc = self._openrouter_mcc()
        config = {"react": {"model_client_config": mcc}}
        inject_attribution_to_config(config)
        assert mcc["custom_headers"] == OPENROUTER_ATTRIBUTION_HEADERS

    def test_non_openrouter_mcc_is_skipped(self):
        mcc = {"client_provider": "OpenAI"}
        config = {"models": {"defaults": [{"model_client_config": mcc}]}}
        inject_attribution_to_config(config)
        assert "custom_headers" not in mcc

    def test_malformed_sections_do_not_raise(self):
        config = {
            "models": {"defaults": "not-a-list", "default": "not-a-dict"},
            "react": "not-a-dict",
        }
        inject_attribution_to_config(config)  # must not raise

    def test_missing_models_section_is_fine(self):
        inject_attribution_to_config({})  # must not raise
