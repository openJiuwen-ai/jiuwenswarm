# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""并发会话换模后懒重载：重建模型缓存不能丢掉老会话仍在用的模型。"""

from __future__ import annotations

from types import SimpleNamespace

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

from jiuwenswarm.common.model_identity import build_model_identity_reference
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _entry(name: str, *, api_base: str = "https://maas.example/v2", api_key: str = "k1") -> dict:
    return {
        "model_client_config": {
            "model_name": name,
            "api_base": api_base,
            "api_key": api_key,
            "client_provider": "OpenAI",
        },
        "model_config_obj": {},
    }


def _config(*entries: dict) -> dict:
    return {"models": {"defaults": list(entries)}}


def _ref(entry: dict) -> str:
    mcc = entry["model_client_config"]
    return build_model_identity_reference(mcc["model_name"], mcc)


def _adapter(monkeypatch) -> tuple[JiuWenSwarmDeepAdapter, list[str]]:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace(_react_agent=SimpleNamespace())
    built: list[str] = []

    def _build(mcc, _mco):
        built.append(mcc["model_name"])
        return Model(
            model_client_config=ModelClientConfig(
                client_provider="OpenAI",
                api_key=mcc["api_key"],
                api_base=mcc["api_base"],
                verify_ssl=False,
            ),
            model_config=ModelRequestConfig(model_name=mcc["model_name"]),
        )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._build_model_from_entry", _build)
    monkeypatch.setattr(adapter, "_register_models_json_cache_entries", lambda _counter: None)
    return adapter, built


def _name(model: Model) -> str:
    return model.model_config.model_name


def _use(adapter: JiuWenSwarmDeepAdapter, name: str) -> Model:
    model = adapter._resolve_model_by_name(name)
    adapter._apply_model_to_react_agent(model)
    return model


def test_reload_keeps_applied_model_dropped_from_new_config(monkeypatch):
    adapter, built = _adapter(monkeypatch)
    glm = _entry("glm-5.1")
    adapter._create_model(_config(glm))
    old_glm = _use(adapter, "glm-5.1")

    adapter._create_model(_config(_entry("deepseek-v4.1-flash")))

    assert _name(adapter._model) == "deepseek-v4.1-flash"
    resolved = adapter._resolve_model_by_name("glm-5.1")
    assert _name(resolved) == "glm-5.1"
    assert resolved is not old_glm
    assert built.count("glm-5.1") == 2
    assert adapter._resolve_model_by_identity(_ref(glm)) is resolved


def test_only_applied_models_are_retained(monkeypatch):
    adapter, built = _adapter(monkeypatch)
    glm = _entry("glm-5.1")
    adapter._create_model(_config(glm))
    _use(adapter, "glm-5.1")
    adapter._create_model(_config(_entry("glm-5.2")))
    adapter._create_model(_config(_entry("deepseek-v4.1-flash")))

    assert _name(adapter._resolve_model_by_name("glm-5.1")) == "glm-5.1"
    assert _name(adapter._resolve_model_by_identity(_ref(glm))) == "glm-5.1"
    assert _name(adapter._resolve_model_by_name("glm-5.2")) == "deepseek-v4.1-flash"
    assert built.count("glm-5.2") == 1


def test_retained_model_reapplied_after_reload_survives_next_reload(monkeypatch):
    adapter, _ = _adapter(monkeypatch)
    adapter._create_model(_config(_entry("glm-5.1")))
    _use(adapter, "glm-5.1")
    adapter._create_model(_config(_entry("glm-5.2")))
    _use(adapter, "glm-5.1")
    adapter._create_model(_config(_entry("deepseek-v4.1-flash")))

    assert _name(adapter._resolve_model_by_name("glm-5.1")) == "glm-5.1"
    assert len(adapter._model_name_to_keys["glm-5.1"]) == 1


def test_new_config_wins_for_same_identity(monkeypatch):
    adapter, _ = _adapter(monkeypatch)
    adapter._create_model(_config(_entry("glm-5.1", api_key="old")))
    _use(adapter, "glm-5.1")
    adapter._create_model(_config(_entry("glm-5.1", api_key="new")))

    resolved = adapter._resolve_model_by_name("glm-5.1")
    assert resolved.model_client_config.api_key == "new"
    assert len(adapter._model_identity_to_keys[_ref(_entry("glm-5.1"))]) == 1


def test_same_name_other_endpoint_stays_reachable_by_ref_only(monkeypatch):
    adapter, _ = _adapter(monkeypatch)
    old = _entry("glm-5.1", api_base="https://old.example/v1")
    adapter._create_model(_config(old))
    _use(adapter, "glm-5.1")
    adapter._create_model(_config(_entry("glm-5.1", api_base="https://new.example/v1")))

    by_name = adapter._resolve_model_by_name("glm-5.1")
    assert by_name.model_client_config.api_base == "https://new.example/v1"
    by_ref = adapter._resolve_model_by_identity(_ref(old))
    assert by_ref.model_client_config.api_base == "https://old.example/v1"
