# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for task_tool model_name / model_tier resolution on DeepAdapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _dummy_model(name: str) -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_key=f"key-{name}",
            api_base="http://test",
            verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model_name=name),
    )


def _model_display_name(model: Model) -> str:
    cfg = model.model_config
    return str(getattr(cfg, "model_name", None) or getattr(cfg, "model", None) or "")


def _adapter_with_tier_models() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._model_identity_to_keys = {}
    adapter._model_identity_owners = {}
    adapter._tier_model_cache = {}
    adapter._model = None
    adapter._instance = None

    pro = _dummy_model("pro-model")
    lite = _dummy_model("lite-model")
    default = _dummy_model("default-model")

    built = {
        "pro-model": pro,
        "lite-model": lite,
        "default-model": default,
    }

    def _build_from_entry(mcc, _mco):
        return built[mcc["model_name"]]

    adapter._build_model_from_entry = _build_from_entry  # type: ignore[method-assign]

    name_counter: dict[str, int] = {}
    for entry in (
        {
            "tier": "pro",
            "is_default": True,
            "model_client_config": {"model_name": "pro-model"},
            "model_config_obj": {},
        },
        {
            "tier": "lite",
            "model_client_config": {"model_name": "lite-model"},
            "model_config_obj": {},
        },
        {
            "tier": "lite",  # first-wins: ignored
            "model_client_config": {"model_name": "default-model"},
            "model_config_obj": {},
        },
    ):
        adapter._register_model_cache_entry(entry, name_counter)

    adapter._model = adapter._model_cache["pro-model"]
    return adapter


def test_tier_cache_first_wins_and_resolve_by_tier():
    adapter = _adapter_with_tier_models()
    assert set(adapter._tier_model_cache) == {"lite", "pro"}
    assert _model_display_name(adapter._tier_model_cache["lite"]) == "lite-model"
    assert _model_display_name(adapter._tier_model_cache["pro"]) == "pro-model"

    model, err = adapter._resolve_model(model_tier="lite")
    assert err is None
    assert _model_display_name(model) == "lite-model"

    model, err = adapter._resolve_model(model_tier="pro")
    assert err is None
    assert _model_display_name(model) == "pro-model"


def test_resolve_model_name_wins_over_tier():
    adapter = _adapter_with_tier_models()
    model, err = adapter._resolve_model(model_name="lite-model", model_tier="pro")
    assert err is None
    assert _model_display_name(model) == "lite-model"


def test_resolve_invalid_name_falls_back_to_tier():
    adapter = _adapter_with_tier_models()
    model, err = adapter._resolve_model(model_name="missing", model_tier="lite")
    assert err is None
    assert _model_display_name(model) == "lite-model"


def test_resolve_invalid_or_missing_tier_falls_back_to_default():
    adapter = _adapter_with_tier_models()
    model, err = adapter._resolve_model(model_tier="ultra")
    assert err is None
    assert model is adapter._model

    # clear tier to simulate unconfigured
    adapter._tier_model_cache.pop("lite")
    model, err = adapter._resolve_model(model_tier="lite")
    assert err is None
    assert model is adapter._model


def test_bind_subagent_model_resolver_on_instance():
    adapter = _adapter_with_tier_models()
    instance = SimpleNamespace()
    adapter._instance = instance
    adapter._bind_subagent_model_resolver()
    assert callable(instance.resolve_subagent_model)
    model, err = instance.resolve_subagent_model(model_tier="lite")
    assert err is None
    assert _model_display_name(model) == "lite-model"


def test_apply_model_syncs_deep_config_and_adapter_default():
    adapter = _adapter_with_tier_models()
    react = MagicMock()
    react._config = SimpleNamespace(
        model_name="",
        model_client_config=None,
        model_config_obj=None,
    )
    deep_config = SimpleNamespace(model=adapter._model)
    adapter._instance = SimpleNamespace(_react_agent=react, _deep_config=deep_config)

    new_model = _dummy_model("switched")
    adapter._apply_model_to_react_agent(new_model)

    assert adapter._model is new_model
    assert deep_config.model is new_model
    react.set_llm.assert_called_once_with(new_model)
