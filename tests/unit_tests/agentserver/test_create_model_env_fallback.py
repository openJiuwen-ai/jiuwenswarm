# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""First-create model path: sealed overlay env + client_provider fallbacks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenclaw.local_env_config import (
    bind_task_env_overlay,
    reset_task_env_overlay,
)


@pytest.fixture(autouse=True)
def _clear_overlay():
    token = bind_task_env_overlay({})
    try:
        yield
    finally:
        reset_task_env_overlay(token)


class TestBuildModelFromEntryFallbacks:
    @staticmethod
    def _call(mcc: dict, mco: dict | None = None):
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        fake_model = MagicMock(name="Model")
        with (
            patch(
                "jiuwenclaw.agentserver.deep_agent.interface_deep.Model",
                return_value=fake_model,
            ) as model_cls,
            patch(
                "jiuwenclaw.agentserver.deep_agent.interface_deep.ModelClientConfig",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
            patch(
                "jiuwenclaw.agentserver.deep_agent.interface_deep.ModelRequestConfig",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            result = JiuWenClawDeepAdapter._build_model_from_entry(dict(mcc), mco or {})
        return result, model_cls

    def test_fills_client_provider_from_overlay_env(self):
        token = bind_task_env_overlay(
            {
                "MODEL_PROVIDER": "OpenAI",
                "API_KEY": "k-from-overlay",
                "API_BASE": "https://example.test/v1",
            }
        )
        try:
            _result, model_cls = self._call(
                {
                    "model_name": "glm-5.1",
                    "client_provider": "",
                    "api_key": "",
                    "api_base": "",
                }
            )
        finally:
            reset_task_env_overlay(token)

        kwargs = model_cls.call_args.kwargs
        mcc = kwargs["model_client_config"]
        assert mcc.client_provider == "OpenAI"
        assert mcc.api_key == "k-from-overlay"
        assert mcc.api_base == "https://example.test/v1"

    def test_defaults_client_provider_to_openai_when_unset(self):
        _result, model_cls = self._call(
            {
                "model_name": "glm-5.1",
                "client_provider": "",
                "api_key": "k",
                "api_base": "https://example.test/v1",
            }
        )
        mcc = model_cls.call_args.kwargs["model_client_config"]
        assert mcc.client_provider == "OpenAI"

    def test_fills_model_name_from_env_when_empty(self):
        token = bind_task_env_overlay({"MODEL_NAME": "from-env-model", "API_KEY": "k"})
        try:
            _result, model_cls = self._call(
                {
                    "model_name": "",
                    "client_provider": "OpenAI",
                    "api_key": "k",
                    "api_base": "https://example.test/v1",
                }
            )
        finally:
            reset_task_env_overlay(token)

        req = model_cls.call_args.kwargs["model_config"]
        assert req.model == "from-env-model"


class TestCreateModelWithEnvOverrides:
    @staticmethod
    def test_create_model_patches_empty_provider_from_env_overrides():
        from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter

        captured: dict = {}
        fake_model = MagicMock(name="Model")
        fake_model.model_client_config = SimpleNamespace(client_provider="OpenAI")
        fake_model.model_config = SimpleNamespace(model="glm-5.1")

        def _capture_entry(mcc, mco):
            captured["mcc"] = dict(mcc)
            captured["mco"] = dict(mco)
            return fake_model

        adapter = SimpleNamespace(
            _model_cache={},
            _tier_model_cache={},
            _default_model_name=None,
            _model=None,
            _model_client_config=None,
            _model_request_config=None,
            _merge_service_model_cache=lambda: None,
        )
        adapter._build_model_from_entry = _capture_entry
        adapter._build_model_cache_from_defaults = (
            lambda config: JiuWenClawDeepAdapter._build_model_cache_from_defaults(
                adapter, config
            )
        )
        adapter._build_model_cache_legacy = (
            lambda config: JiuWenClawDeepAdapter._build_model_cache_legacy(adapter, config)
        )

        config = {
            "models": {
                "default": {
                    "model_client_config": {
                        "model_name": "glm-5.1",
                        "client_provider": "",
                        "api_key": "",
                        "api_base": "",
                        "timeout": 60,
                        "max_retries": 1,
                        "verify_ssl": False,
                    },
                    "model_config_obj": {"temperature": 0.6},
                }
            }
        }
        env = {
            "MODEL_PROVIDER": "OpenAI",
            "API_KEY": "real-key",
            "API_BASE": "https://api.example/v1",
            "MODEL_NAME": "glm-5.1",
        }

        model = JiuWenClawDeepAdapter._create_model(adapter, config, env_overrides=env)

        assert model is fake_model
        assert captured["mcc"]["client_provider"] == "OpenAI"
        assert captured["mcc"]["api_key"] == "real-key"
        assert captured["mcc"]["api_base"] == "https://api.example/v1"
        assert captured["mcc"]["model_name"] == "glm-5.1"


@pytest.mark.asyncio
async def test_create_instance_clears_cache_and_passes_overlay_env():
    """Scheme 1+3: create path clears global config cache and passes overlay to _create_model."""
    from jiuwenclaw.agentserver.deep_agent import interface_deep as mod

    calls: dict = {"clear": 0, "create_env": None}

    def _tracking_clear():
        calls["clear"] += 1

    config_base = {
        "react": {"agent_name": "main_agent", "max_iterations": 5},
        "permissions": {"enabled": False},
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "glm-5.1",
                    "client_provider": "",
                    "api_key": "",
                    "api_base": "",
                },
                "model_config_obj": {},
            }
        },
    }

    adapter = MagicMock()
    adapter._env_ns_ids = MagicMock(return_value=("default", "default"))
    adapter._instance_overrides = {}
    adapter._session_id = None
    adapter._workspace_dir = "/tmp/ws"
    adapter._latest_config_base = None
    adapter._config_cache = {}
    adapter._agent_name = "main"
    adapter._tool_cards = []
    adapter._resolve_agent_card_id = MagicMock(return_value="card-1")
    adapter._get_tool_cards = AsyncMock(return_value=[])
    adapter._refresh_multimodal_configs = MagicMock()
    adapter.set_checkpoint = AsyncMock()
    adapter.load_user_rails = AsyncMock()
    adapter._abort_active_subagents = AsyncMock()
    adapter._sync_preinstance_runtime_tools_to_ability_manager = MagicMock()
    adapter._sync_multimodal_tools_for_runtime = MagicMock()
    adapter._tenant_disk_ids = MagicMock(return_value=("default", "default"))
    adapter._embed_config_fingerprint = MagicMock(return_value="fp")
    adapter._embed_fingerprint = None
    adapter._memory_cache_fingerprint = None
    adapter._task_memory_fingerprint = None

    def _capture_create(cfg, env=None):
        calls["create_env"] = env
        raise RuntimeError("stop-after-create-model")

    adapter._create_model = _capture_create

    overlay = {
        "MODEL_PROVIDER": "OpenAI",
        "API_KEY": "k",
        "API_BASE": "https://api.example/v1",
        "MODEL_NAME": "glm-5.1",
    }
    token = bind_task_env_overlay(overlay)
    try:
        with (
            patch.object(mod, "clear_global_config_cache", side_effect=_tracking_clear),
            patch.object(mod, "get_config", return_value=config_base),
            patch.object(mod, "AgentCard", return_value=MagicMock()),
            patch.object(mod, "init_permission_engine"),
            patch.object(
                mod,
                "get_permission_engine",
                return_value=SimpleNamespace(enabled=False),
            ),
        ):
            with pytest.raises(RuntimeError, match="stop-after-create-model"):
                await mod.JiuWenClawDeepAdapter._create_instance_in_env_ns(
                    adapter, {}, mode="agent", session_id="s1"
                )
    finally:
        reset_task_env_overlay(token)

    assert calls["clear"] >= 1
    assert calls["create_env"] is not None
    assert calls["create_env"].get("MODEL_PROVIDER") == "OpenAI"
