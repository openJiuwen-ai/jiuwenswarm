import pytest

from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    _resolve_model_config_obj_for_validate,
)


class TestResolveModelConfigObjForValidate:
    """Tests for ``_resolve_model_config_obj_for_validate``."""

    @pytest.fixture
    def mock_models(self, monkeypatch):
        """Patch ``get_config`` and ``get_default_models`` with a list of model entries."""

        def _patch(entries):
            def fake_get_config():
                return {"models": {"defaults": entries}}

            def fake_get_default_models(_cfg):
                return entries

            monkeypatch.setattr(
                "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
                fake_get_config,
            )
            monkeypatch.setattr(
                "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_default_models",
                fake_get_default_models,
            )

        return _patch

    def test_match_by_model_name(self, mock_models):
        mock_models(
            [
                {
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": {"temperature": 0.7, "top_p": 0.9},
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate("gpt-4", {})
        assert result == {"temperature": 0.7, "top_p": 0.9}

    def test_match_by_alias(self, mock_models):
        mock_models(
            [
                {
                    "alias": "my-gpt",
                    "model_client_config": {"model_name": "gpt-4-turbo"},
                    "model_config_obj": {"temperature": 0.5},
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate("my-gpt", {})
        assert result == {"temperature": 0.5}

    def test_no_match_returns_empty_dict(self, mock_models):
        mock_models(
            [
                {
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": {"temperature": 0.7},
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate("unknown-model", {})
        assert result == {}

    def test_get_config_exception_returns_empty_dict(self, monkeypatch):
        def raise_exc():
            raise RuntimeError("config broken")

        monkeypatch.setattr(
            "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
            raise_exc,
        )
        result = _resolve_model_config_obj_for_validate("any", {})
        assert result == {}

    def test_reasoning_level_override_from_params(self, mock_models):
        mock_models(
            [
                {
                    "model_client_config": {"model_name": "deepseek-v3"},
                    "model_config_obj": {"temperature": 0.7, "reasoning_level": "low"},
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate(
            "deepseek-v3", {"reasoning_level": "high"}
        )
        assert result == {"temperature": 0.7, "reasoning_level": "high"}

    def test_reasoning_level_added_when_not_in_config(self, mock_models):
        mock_models(
            [
                {
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": {"temperature": 0.7},
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate(
            "gpt-4", {"reasoning_level": "medium"}
        )
        assert result == {"temperature": 0.7, "reasoning_level": "medium"}

    def test_reasoning_level_added_when_no_match(self, mock_models):
        mock_models([])
        result = _resolve_model_config_obj_for_validate(
            "unknown", {"reasoning_level": "low"}
        )
        assert result == {"reasoning_level": "low"}

    def test_model_config_obj_not_dict_ignored(self, mock_models):
        mock_models(
            [
                {
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": "not-a-dict",  # invalid type
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate("gpt-4", {})
        assert result == {}

    def test_non_dict_entry_skipped(self, mock_models):
        mock_models(
            [
                "not-a-dict-entry",  # invalid entry
                {
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": {"temperature": 0.8},
                },
            ]
        )
        result = _resolve_model_config_obj_for_validate("gpt-4", {})
        assert result == {"temperature": 0.8}

    def test_model_name_takes_precedence_over_alias(self, mock_models):
        """When model_name matches entry A but alias matches entry B,
        entry A should win because we iterate in order."""
        mock_models(
            [
                {
                    "alias": "custom-gpt",
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": {"temperature": 0.1},
                },
                {
                    "alias": "gpt-4",
                    "model_client_config": {"model_name": "gpt-4-turbo"},
                    "model_config_obj": {"temperature": 0.9},
                },
            ]
        )
        # model_name "gpt-4" matches first entry
        result = _resolve_model_config_obj_for_validate("gpt-4", {})
        assert result == {"temperature": 0.1}

    def test_alias_match_used_when_model_name_misses(self, mock_models):
        mock_models(
            [
                {
                    "alias": "custom-gpt",
                    "model_client_config": {"model_name": "gpt-4"},
                    "model_config_obj": {"temperature": 0.2},
                }
            ]
        )
        result = _resolve_model_config_obj_for_validate("custom-gpt", {})
        assert result == {"temperature": 0.2}
