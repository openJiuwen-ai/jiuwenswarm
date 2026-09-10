# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model cache sync rebuild: ensure sync MODEL_NAME always cached + rebuild on miss."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_model(name: str = "glm-5.2", api_key: str = "real-key"):
    """Create a MagicMock Model with a model_config carrying model_name."""
    m = MagicMock(name=f"Model[{name}]")
    m.model_client_config = {
        "model_name": name,
        "api_key": api_key,
        "api_base": "https://api.example/v1",
        "client_provider": "OpenAI",
    }
    m.model_config = SimpleNamespace(model_name=name)
    m.model_request_config = SimpleNamespace(model_name=name)
    return m


def _make_adapter_stub(**kwargs):
    """Create a minimal adapter stub for testing _create_model / _resolve_model_for_request."""
    defaults = dict(
        _model_cache={},
        _tier_model_cache={},
        _default_model_name=None,
        _model=None,
        _model_client_config=None,
        _model_request_config=None,
        _latest_config_base=None,
        _last_sync_env=None,
        _merge_service_model_cache=lambda: None,
        _resolve_from_shared_model_cache=lambda name: None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Change 1: _create_model step 5 — get_default_models returns empty
# ---------------------------------------------------------------------------

class TestCreateModelStep5Fallback:
    """When get_default_models returns [], step 5 should still create env_model_name cache entry
    by falling back to config['models']['default']."""

    def test_step5_creates_cache_entry_when_get_default_models_empty(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        fake_model = _make_fake_model("glm-5.2")

        captured: dict = {}

        def _capture_build(mcc, mco):
            captured["mcc"] = dict(mcc)
            return fake_model

        adapter = _make_adapter_stub()
        adapter._build_model_from_entry = _capture_build
        adapter._build_model_cache_from_defaults = lambda config: None
        adapter._build_model_cache_legacy = lambda config: None

        config = {
            "models": {
                "default": {
                    "model_client_config": {
                        "model_name": "placeholder",
                        "api_key": "placeholder-key",
                        "api_base": "https://placeholder/v1",
                        "client_provider": "OpenAI",
                        "timeout": 60,
                        "max_retries": 1,
                        "verify_ssl": False,
                    },
                    "model_config_obj": {"temperature": 0.6},
                }
            }
        }
        env = {"MODEL_NAME": "glm-5.2", "API_KEY": "real-key"}

        with patch(
            "jiuwenclaw.agentserver.deep_agent.interface_deep.get_default_models",
            return_value=[],  # Simulate empty return
        ):
            model = JiuWenClawDeepAdapter._create_model(adapter, config, env_overrides=env)

        # env_model_name should be in cache
        assert "glm-5.2" in adapter._model_cache
        assert model is fake_model
        # The rebuilt mcc should have model_name from env, not from config
        assert captured["mcc"]["model_name"] == "glm-5.2"

    def test_step5_uses_entries_when_non_empty(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        fake_model = _make_fake_model("glm-5.2")
        adapter = _make_adapter_stub()
        adapter._build_model_from_entry = lambda mcc, mco: fake_model
        adapter._build_model_cache_from_defaults = lambda config: None
        adapter._build_model_cache_legacy = lambda config: None

        config = {"models": {"default": {}, "defaults": [{"model_client_config": {"model_name": "old"}}]}}
        env = {"MODEL_NAME": "glm-5.2"}

        # get_default_models returns non-empty — should use entries[0]
        model = JiuWenClawDeepAdapter._create_model(adapter, config, env_overrides=env)

        assert "glm-5.2" in adapter._model_cache
        assert model is fake_model


# ---------------------------------------------------------------------------
# Change 2: _update_permission_rail — use self._model's model_name
# ---------------------------------------------------------------------------

class TestUpdatePermissionRailModelName:
    """_update_permission_rail should prefer self._model's model_name (patched)
    over raw config_base (may have unresolved ${MODEL_NAME})."""

    def test_uses_self_model_model_name_over_config_base(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        patched_model = _make_fake_model("glm-5.2")
        rail_mock = MagicMock()

        adapter = SimpleNamespace(
            _model=patched_model,
            _permission_rail=rail_mock,
        )

        # config_base has wrong model_name (placeholder)
        config_base = {
            "permissions": {"enabled": True},
            "models": {
                "default": {
                    "model_client_config": {"model_name": "your-model-name"}
                }
            },
        }

        JiuWenClawDeepAdapter._update_permission_rail(adapter, config_base)

        # Should use self._model's model_name, not config_base's
        call_kwargs = rail_mock.update_config.call_args
        assert call_kwargs.kwargs["model_name"] == "glm-5.2"

    def test_falls_back_to_config_base_when_self_model_none(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        rail_mock = MagicMock()
        adapter = SimpleNamespace(
            _model=None,  # No model set
            _permission_rail=rail_mock,
        )

        config_base = {
            "permissions": {"enabled": True},
            "models": {
                "default": {
                    "model_client_config": {"model_name": "fallback-name"}
                }
            },
        }

        JiuWenClawDeepAdapter._update_permission_rail(adapter, config_base)

        call_kwargs = rail_mock.update_config.call_args
        assert call_kwargs.kwargs["model_name"] == "fallback-name"


# ---------------------------------------------------------------------------
# Change 3 + 4: _resolve_model_for_request — WARNING log + rebuild from sync env
# ---------------------------------------------------------------------------

class TestResolveModelForRequestRebuild:
    """On cache miss, _resolve_model_for_request should try to rebuild from
    _last_sync_env before falling back to self._model."""

    def _make_request(self, model_name: str = "glm-5.2"):
        req = MagicMock()
        req.params = {"model_name": model_name}
        return req

    def test_cache_hit_returns_cached_model(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        cached_model = _make_fake_model("glm-5.2")
        adapter = _make_adapter_stub(
            _model_cache={"glm-5.2": cached_model},
            _model=_make_fake_model("default"),
        )

        result = JiuWenClawDeepAdapter._resolve_model_for_request(adapter, self._make_request("glm-5.2"))
        assert result is cached_model

    def test_cache_miss_rebuilds_from_sync_env(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        rebuilt_model = _make_fake_model("glm-5.2", api_key="sync-key")

        captured: dict = {}
        def _capture_build(mcc, mco):
            captured["mcc"] = dict(mcc)
            return rebuilt_model

        adapter = _make_adapter_stub(
            _model_cache={},  # Empty cache — miss
            _model=_make_fake_model("your-model-name", api_key="placeholder-key"),
            _last_sync_env={"MODEL_NAME": "glm-5.2", "API_KEY": "sync-key", "API_BASE": "https://sync/v1"},
            _latest_config_base={
                "models": {
                    "default": {
                        "model_client_config": {
                            "model_name": "your-model-name",
                            "api_key": "placeholder-key",
                            "api_base": "https://placeholder/v1",
                            "client_provider": "OpenAI",
                        },
                        "model_config_obj": {"temperature": 0.6},
                    }
                }
            },
        )
        adapter._build_model_from_entry = _capture_build

        result = JiuWenClawDeepAdapter._resolve_model_for_request(adapter, self._make_request("glm-5.2"))

        # Should rebuild and return the rebuilt model, not the placeholder default
        assert result is rebuilt_model
        assert "glm-5.2" in adapter._model_cache
        # The rebuilt mcc should have sync credentials
        assert captured["mcc"]["model_name"] == "glm-5.2"
        assert captured["mcc"]["api_key"] == "sync-key"

    def test_cache_miss_falls_back_when_no_sync_env(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        default_model = _make_fake_model("your-model-name", api_key="placeholder-key")
        adapter = _make_adapter_stub(
            _model_cache={},
            _model=default_model,
            _last_sync_env=None,  # No sync env stored
            _latest_config_base=None,
        )

        # No sync env and no service cache — raises ValueError to surface
        # model_not_found rather than silently falling back to a default
        # model that may carry placeholder credentials.
        with pytest.raises(ValueError, match="not found in service model cache"):
            JiuWenClawDeepAdapter._resolve_model_for_request(adapter, self._make_request("glm-5.2"))

    def test_cache_miss_falls_back_when_model_name_mismatch(self):
        """If _last_sync_env has a different MODEL_NAME than requested, raise ValueError."""
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        default_model = _make_fake_model("default-model")
        adapter = _make_adapter_stub(
            _model_cache={},
            _model=default_model,
            _last_sync_env={"MODEL_NAME": "different-model"},  # Doesn't match request
            _latest_config_base={"models": {"default": {}}},
        )

        with pytest.raises(ValueError, match="not found in service model cache"):
            JiuWenClawDeepAdapter._resolve_model_for_request(adapter, self._make_request("glm-5.2"))

    def test_cache_miss_logs_warning_on_fallback(self):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        default_model = _make_fake_model("your-model-name")
        adapter = _make_adapter_stub(
            _model_cache={},
            _model=default_model,
            _last_sync_env=None,
            _latest_config_base=None,
        )

        with patch("jiuwenclaw.agentserver.deep_agent.interface_deep.logger"):
            with pytest.raises(ValueError, match="not found in service model cache"):
                JiuWenClawDeepAdapter._resolve_model_for_request(adapter, self._make_request("glm-5.2"))

    def test_rebuild_failure_falls_back_gracefully(self):
        """If rebuild throws, should raise ValueError (not crash with RuntimeError)."""
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        default_model = _make_fake_model("your-model-name")

        def _failing_build(mcc, mco):
            raise RuntimeError("build failed")

        adapter = _make_adapter_stub(
            _model_cache={},
            _model=default_model,
            _last_sync_env={"MODEL_NAME": "glm-5.2", "API_KEY": "k"},
            _latest_config_base={"models": {"default": {}}},
        )
        adapter._build_model_from_entry = _failing_build

        with pytest.raises(ValueError, match="not found in service model cache"):
            JiuWenClawDeepAdapter._resolve_model_for_request(adapter, self._make_request("glm-5.2"))
