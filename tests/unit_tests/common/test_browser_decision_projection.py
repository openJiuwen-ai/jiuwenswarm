# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the credential-free browser decision projection."""

import pytest

from jiuwenswarm.common.browser_settings import (
    BROWSER_DECISION_MODES,
    JEV_PROVIDER_DEFAULTS,
    project_browser_decision,
)


class TestProjectBrowserDecision:
    def test_empty_section_uses_typesafe_defaults(self):
        result = project_browser_decision({}, environment={})
        defaults = JEV_PROVIDER_DEFAULTS["typesafe"]
        assert result == {
            "decision_mode": "llm",
            "decision_provider": "typesafe",
            "decision_model": defaults["model"],
            "decision_api_base": defaults["api_base"],
            "jev_key_configured": False,
        }

    @pytest.mark.parametrize("provider", list(JEV_PROVIDER_DEFAULTS))
    def test_known_provider_uses_its_defaults(self, provider: str):
        defaults = JEV_PROVIDER_DEFAULTS[provider]
        result = project_browser_decision({"provider": provider}, environment={})
        assert result["decision_provider"] == provider
        assert result["decision_model"] == defaults["model"]
        assert result["decision_api_base"] == defaults["api_base"]

    def test_unknown_provider_falls_back_to_typesafe(self):
        result = project_browser_decision({"provider": "bogus"}, environment={})
        assert result["decision_provider"] == "typesafe"
        assert result["decision_model"] == JEV_PROVIDER_DEFAULTS["typesafe"]["model"]

    def test_non_string_provider_falls_back_to_typesafe(self):
        result = project_browser_decision({"provider": 42}, environment={})
        assert result["decision_provider"] == "typesafe"

    @pytest.mark.parametrize("mode", BROWSER_DECISION_MODES)
    def test_known_modes_are_kept(self, mode: str):
        result = project_browser_decision({"mode": mode}, environment={})
        assert result["decision_mode"] == mode

    def test_unknown_mode_falls_back_to_llm(self):
        result = project_browser_decision({"mode": "weird"}, environment={})
        assert result["decision_mode"] == "llm"

    def test_section_overrides_win_over_provider_defaults(self):
        result = project_browser_decision(
            {"provider": "typesafe", "model": "custom-model", "api_base": "https://x.ai/v1"},
            environment={},
        )
        assert result["decision_model"] == "custom-model"
        assert result["decision_api_base"] == "https://x.ai/v1"

    def test_blank_model_falls_back_to_default(self):
        result = project_browser_decision(
            {"provider": "typesafe", "model": ""}, environment={}
        )
        assert result["decision_model"] == JEV_PROVIDER_DEFAULTS["typesafe"]["model"]

    def test_custom_api_key_env_is_respected(self):
        result = project_browser_decision(
            {"api_key_env": "MY_KEY"}, environment={"MY_KEY": "secret"}
        )
        assert result["jev_key_configured"] is True

    def test_api_key_configured_false_for_missing_or_blank_env(self):
        assert (
            project_browser_decision({}, environment={})["jev_key_configured"] is False
        )
        assert (
            project_browser_decision(
                {"api_key_env": "K"}, environment={"K": "   "}
            )["jev_key_configured"]
            is False
        )

    def test_env_value_with_whitespace_stripped_is_configured(self):
        result = project_browser_decision(
            {"api_key_env": "K"}, environment={"K": "  s3cret  "}
        )
        assert result["jev_key_configured"] is True

    def test_never_leaks_key_value(self):
        environment = {"TYPESAFE_API_KEY": "super-secret"}
        result = project_browser_decision({}, environment=environment)
        assert "super-secret" not in str(result)
        assert set(result) == {
            "decision_mode",
            "decision_provider",
            "decision_model",
            "decision_api_base",
            "jev_key_configured",
        }

    def test_environment_none_reads_real_os_environ(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "from-os")
        result = project_browser_decision({})
        assert result["jev_key_configured"] is True
