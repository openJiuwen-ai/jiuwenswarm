# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the pure KV-cache affinity config helpers."""

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common import kv_cache_affinity_config as kv


class TestNormalizeProvider:
    def test_plain_string_stripped(self):
        assert kv.normalize_provider("  OpenAI ") == "OpenAI"

    def test_enum_value_is_used(self):
        provider = SimpleNamespace(value="OpenAI")
        assert kv.normalize_provider(provider) == "OpenAI"

    def test_missing_value_is_empty_string(self):
        assert kv.normalize_provider(None) == ""
        assert kv.normalize_provider("") == ""

    def test_non_string_is_stringified(self):
        assert kv.normalize_provider(42) == "42"


class TestParseBool:
    @pytest.mark.parametrize("value", ["true", "TRUE", " 1 ", "yes", "Yes"])
    def test_truthy_values(self, value: str):
        assert kv.parse_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "maybe", None])
    def test_other_values_are_false(self, value: Any):
        assert kv.parse_bool(value) is False


class TestIsKvCacheAffinityConfig:
    def test_affinity_mode_is_detected(self):
        assert kv.is_kv_cache_affinity_config(
            {"extensions": {"kv_cache": {"mode": "affinity"}}}
        ) is True

    def test_mode_is_normalized_case_and_whitespace(self):
        assert kv.is_kv_cache_affinity_config(
            {"extensions": {"kv_cache": {"mode": "  AFFINITY "}}}
        ) is True

    @pytest.mark.parametrize("config", [{}, {"extensions": {}}, {"client_provider": "X"}])
    def test_non_affinity_configs(self, config: dict):
        assert kv.is_kv_cache_affinity_config(config) is False

    def test_object_form_is_supported(self):
        config_like = SimpleNamespace(
            extensions=SimpleNamespace(kv_cache=SimpleNamespace(mode="affinity"))
        )
        assert kv.is_kv_cache_affinity_config(config_like) is True

    def test_broken_object_fails_closed_to_empty_mode(self):
        assert kv.is_kv_cache_affinity_config(object()) is False


class TestHasKvCacheAffinityCapability:
    def test_explicit_affinity_mode_wins_over_unknown_provider(self):
        assert kv.has_kv_cache_affinity_capability(
            {"extensions": {"kv_cache": {"mode": "affinity"}}}, provider="DeepSeek"
        ) is True

    def test_legacy_ascend_affinity_provider_is_accepted(self):
        assert kv.has_kv_cache_affinity_capability(
            None, provider="AscendAffinity"
        ) is True

    def test_legacy_provider_resolved_from_model_client_config(self):
        assert kv.has_kv_cache_affinity_capability(
            {"client_provider": "AscendAffinity"}, provider=None
        ) is True

    def test_legacy_client_provider_beats_transport_provider(self):
        assert kv.has_kv_cache_affinity_capability(
            {
                "client_provider": "OpenAI",
                "legacy_client_provider": "AscendAffinity",
            },
            provider=None,
        ) is True

    def test_no_declaration_means_no_capability(self):
        assert kv.has_kv_cache_affinity_capability(
            {"client_provider": "OpenAI"}, provider="OpenAI"
        ) is False
        assert kv.has_kv_cache_affinity_capability(None, provider=None) is False


class TestSelectDefaultModelEntry:
    def test_is_default_flag_wins(self):
        entries = [
            {"name": "first", "is_default": False},
            {"name": "second", "is_default": True},
        ]
        assert kv.select_default_model_entry(entries) == entries[1]

    def test_first_dict_is_fallback_without_flag(self):
        entries = [{"name": "a"}, {"name": "b"}]
        assert kv.select_default_model_entry(entries) == entries[0]

    def test_non_dict_entries_are_skipped(self):
        assert kv.select_default_model_entry(["junk", {"name": "a"}]) == {
            "name": "a"
        }

    def test_empty_list_returns_none(self):
        assert kv.select_default_model_entry([]) is None


class TestDefaultModelClientConfigFromEntries:
    def test_returns_default_entry_config(self):
        entries = [
            {"model_client_config": {"client_provider": "OpenAI", "api_base": "x"}},
        ]
        assert kv.default_model_client_config_from_entries(entries) == {
            "client_provider": "OpenAI",
            "api_base": "x",
        }

    def test_non_dict_mcc_returns_none(self):
        assert kv.default_model_client_config_from_entries(
            [{"model_client_config": "junk"}]
        ) is None

    def test_no_entries_returns_none(self):
        assert kv.default_model_client_config_from_entries([]) is None


class TestSetDefaultModelProviderInEntries:
    def test_missing_provider_is_written_and_reports_change(self):
        entries = [{"model_client_config": {}}]
        assert kv.set_default_model_provider_in_entries(entries, "OpenAI") is True
        assert entries[0]["model_client_config"]["client_provider"] == "OpenAI"

    def test_unchanged_provider_reports_no_change(self):
        entries = [{"model_client_config": {"client_provider": "OpenAI"}}]
        assert kv.set_default_model_provider_in_entries(entries, "OpenAI") is False

    def test_non_dict_mcc_is_replaced(self):
        entries = [{"model_client_config": "junk"}]
        assert kv.set_default_model_provider_in_entries(entries, "DeepSeek") is True
        assert entries[0]["model_client_config"] == {"client_provider": "DeepSeek"}

    def test_missing_mcc_is_created(self):
        entries = [{}]
        assert kv.set_default_model_provider_in_entries(entries, "DeepSeek") is True
        assert entries[0]["model_client_config"]["client_provider"] == "DeepSeek"

    def test_no_entries_reports_no_change(self):
        assert kv.set_default_model_provider_in_entries([], "OpenAI") is False


class TestGetDefaultModelClientConfig:
    def test_defaults_list_wins_over_single_default(self):
        config = {
            "models": {
                "defaults": [{"model_client_config": {"client_provider": "A"}}],
                "default": {"model_client_config": {"client_provider": "B"}},
            }
        }
        assert kv.get_default_model_client_config(config) == {
            "client_provider": "A"
        }

    def test_single_default_used_when_no_list(self):
        config = {
            "models": {"default": {"model_client_config": {"client_provider": "B"}}}
        }
        assert kv.get_default_model_client_config(config) == {
            "client_provider": "B"
        }

    def test_react_section_is_last_resort(self):
        config = {"react": {"model_client_config": {"client_provider": "C"}}}
        assert kv.get_default_model_client_config(config) == {
            "client_provider": "C"
        }

    def test_env_fallback_when_nothing_configured(self, monkeypatch):
        monkeypatch.setenv("MODEL_PROVIDER", "  OpenAI ")
        assert kv.get_default_model_client_config(None) == {
            "client_provider": "OpenAI"
        }
        monkeypatch.setenv("MODEL_PROVIDER", "")
        assert kv.get_default_model_client_config({}) == {
            "client_provider": ""
        }

    def test_malformed_sections_are_ignored(self, monkeypatch):
        monkeypatch.delenv("MODEL_PROVIDER", raising=False)
        config = {
            "models": {"defaults": "not-a-list", "default": "not-a-dict"},
            "react": "not-a-dict",
        }
        result = kv.get_default_model_client_config(config)
        assert result == {"client_provider": ""}
