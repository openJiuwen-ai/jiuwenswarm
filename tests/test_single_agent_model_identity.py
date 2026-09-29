import copy
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.model_routing import capability as capability_module
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.model_identity import build_model_identity_reference
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _adapter(*models):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model = models[0] if models else None
    adapter._model_cache = {
        f"{model.model_config.model_name}#{i}": model for i, model in enumerate(models)
    }
    adapter._model_name_to_keys = {}
    for i, model in enumerate(models):
        adapter._model_name_to_keys.setdefault(model.model_config.model_name, []).append(
            f"{model.model_config.model_name}#{i}"
        )
    adapter._model_identity_to_keys = {}
    for i, model in enumerate(models):
        config = model.model_client_config
        ref = build_model_identity_reference(model.model_config.model_name, vars(config))
        adapter._model_identity_to_keys.setdefault(ref, []).append(f"glm-5.3#{i}")
    return adapter


def _model(base, name="glm-5.3"):
    return SimpleNamespace(
        model_client_config=SimpleNamespace(api_base=base, client_provider="OpenAI"),
        model_config=SimpleNamespace(model_name=name),
    )


def _request(**params):
    return AgentRequest(request_id="r1", params=params)


def test_single_request_resolves_same_name_by_identity():
    first, second = _model("https://one.example"), _model("https://two.example")
    adapter = _adapter(first, second)
    ref = build_model_identity_reference("glm-5.3", vars(second.model_client_config))
    assert adapter._resolve_model_for_request(_request(model_name="glm-5.3", model_ref=ref)) is second


def test_missing_owner_reference_fails_closed():
    adapter = _adapter(_model("https://one.example"))
    with pytest.raises(ValueError, match="not found"):
        adapter._resolve_model_for_request(_request(model_name="glm-5.3", model_ref="model-identity-v1:" + "0" * 64))


def test_malformed_reference_fails_closed():
    adapter = _adapter(_model("https://one.example"))
    with pytest.raises(ValueError, match="invalid"):
        adapter._resolve_model_for_request(_request(model_name="glm-5.3", model_ref="bad-ref"))


def test_ambiguous_reference_fails_closed():
    model = _model("https://one.example")
    adapter = _adapter(model, model)
    ref = build_model_identity_reference("glm-5.3", vars(model.model_client_config))
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_model_for_request(_request(model_name="glm-5.3", model_ref=ref))


def test_name_mismatch_fails_closed():
    adapter = _adapter(_model("https://one.example"))
    ref = build_model_identity_reference("glm-5.3", vars(adapter._model.model_client_config))
    with pytest.raises(ValueError, match="does not match"):
        adapter._resolve_model_for_request(_request(model_name="other-model", model_ref=ref))


def test_without_model_ref_preserves_name_lookup():
    first = _model("https://one.example")
    second = _model("https://two.example", name="qwen3-32b")
    adapter = _adapter(first, second)
    assert adapter._resolve_model_for_request(_request(model_name="glm-5.3")) is first


def test_name_only_resolution_fails_closed_for_duplicate_model_names():
    adapter = _adapter(_model("https://one.example"), _model("https://two.example"))
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_model_for_request(_request(model_name="glm-5.3"))


def test_subagent_name_only_resolution_fails_closed_for_duplicate_model_names():
    adapter = _adapter(_model("https://one.example"), _model("https://two.example"))
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_model(model_name="glm-5.3")


def test_legacy_cache_build_populates_identity_index(monkeypatch):
    model = _model("https://legacy.example")
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache = {}
    adapter._model_identity_to_keys = {}
    monkeypatch.setattr(adapter, "_build_model_from_entry", lambda mcc, mco: model)

    adapter._build_model_cache_legacy(
        {
            "models": {
                "default": {
                    "model_client_config": {
                        "api_base": "https://legacy.example",
                        "model_name": "glm-5.3",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {},
                }
            }
        }
    )

    ref = build_model_identity_reference(
        "glm-5.3",
        {
            "api_base": "https://legacy.example",
            "model_name": "glm-5.3",
            "client_provider": "OpenAI",
        },
    )
    assert adapter._resolve_model_for_request(_request(model_name="glm-5.3", model_ref=ref)) is model


# ---- 统一注册入口 _register_model_cache_entry 的 owner 判定 ---- #


def _registry_adapter():
    """仅装配注册所需状态；_build_model_from_entry 用桩，避免真实凭证/网络依赖。"""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model = None
    adapter._model_cache = {}
    adapter._model_name_to_keys = {}
    adapter._model_identity_to_keys = {}
    adapter._model_identity_owners = {}
    adapter._tier_model_cache = {}

    def _build(mcc, mco):
        return SimpleNamespace(
            model_client_config=SimpleNamespace(
                api_key=mcc.get("api_key"),
                custom_headers=dict(mcc.get("custom_headers") or {}),
            )
        )

    adapter._build_model_from_entry = _build
    return adapter


def _entry(model_name, api_key, *, api_base, provider="OpenAI"):
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": api_base,
            "client_provider": provider,
            "api_key": api_key,
        },
        "model_config_obj": {},
    }


def _entry_ref(entry):
    mcc = entry["model_client_config"]
    return build_model_identity_reference(mcc["model_name"], mcc)


def _register(adapter, entries):
    counter = {}
    for entry in entries:
        adapter._register_model_cache_entry(copy.deepcopy(entry), counter)
    return counter


def test_registry_reuses_cache_key_for_equivalent_owner():
    adapter = _registry_adapter()
    entry = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1")
    _register(adapter, [entry, entry])

    ref = _entry_ref(entry)
    assert adapter._model_identity_to_keys[ref] == ["glm-5.2#0"]
    assert adapter._model_name_to_keys["glm-5.2"] == ["glm-5.2#0"]
    assert adapter._resolve_model_by_identity(ref) is adapter._model_cache["glm-5.2#0"]
    assert adapter._resolve_model_by_name("glm-5.2") is adapter._model_cache["glm-5.2#0"]


def test_registry_keeps_ambiguous_for_same_identity_different_api_key():
    adapter = _registry_adapter()
    first = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1")
    second = _entry("glm-5.2", "sk-B", api_base="https://maas.example/v1")
    _register(adapter, [first, second])

    ref = _entry_ref(first)
    assert len(adapter._model_identity_to_keys[ref]) == 2
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_model_by_identity(ref)


def test_registry_keeps_ambiguous_for_same_identity_different_headers():
    adapter = _registry_adapter()
    first = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1")
    second = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1")
    first["model_client_config"]["custom_headers"] = {"Authorization": "Basic one"}
    second["model_client_config"]["custom_headers"] = {"Authorization": "Basic two"}
    _register(adapter, [first, second])

    ref = _entry_ref(first)
    assert len(adapter._model_identity_to_keys[ref]) == 2
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_model_by_identity(ref)


def test_same_model_name_different_provider_keeps_both_owners():
    adapter = _registry_adapter()
    first = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1", provider="OpenAI")
    second = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1", provider="Anthropic")
    _register(adapter, [first, second])

    assert _entry_ref(first) != _entry_ref(second)
    assert adapter._resolve_model_by_identity(_entry_ref(first)) is adapter._model_cache["glm-5.2#0"]
    assert adapter._resolve_model_by_identity(_entry_ref(second)) is adapter._model_cache["glm-5.2#1"]
    # 同名多 owner：名称查询歧义，调用方须改用 model_ref（既有 fail-closed 语义）
    with pytest.raises(ValueError, match="ambiguous"):
        adapter._resolve_model_by_name("glm-5.2")


def test_config_defaults_and_models_json_same_id_equivalent_owner_registers_once(monkeypatch):
    entry = _entry("glm-5.2", "sk-A", api_base="https://maas.example/v1")
    monkeypatch.setattr(
        capability_module, "_load_models_json", lambda: {"defaults": [copy.deepcopy(entry)]}
    )
    adapter = _registry_adapter()

    adapter._build_model_cache_from_defaults({"models": {"defaults": [copy.deepcopy(entry)]}})

    ref = _entry_ref(entry)
    assert adapter._model_identity_to_keys[ref] == ["glm-5.2#0"]
    assert adapter._model_name_to_keys["glm-5.2"] == ["glm-5.2#0"]
    assert adapter._resolve_model_by_identity(ref) is adapter._model_cache["glm-5.2#0"]


def test_config_defaults_and_models_json_same_id_distinct_endpoint_keeps_both_owners(monkeypatch):
    config_entry = _entry("glm-5.2", "sk-volcano", api_base="https://volcano.example/v1")
    json_entry = _entry("glm-5.2", "sk-maas", api_base="https://maas.example/v1")
    monkeypatch.setattr(
        capability_module, "_load_models_json", lambda: {"defaults": [copy.deepcopy(json_entry)]}
    )
    adapter = _registry_adapter()

    adapter._build_model_cache_from_defaults({"models": {"defaults": [copy.deepcopy(config_entry)]}})

    # models.json 独有的 owner 不再被「同名跳过」丢掉，各自 model_ref 均可解析
    assert _entry_ref(config_entry) != _entry_ref(json_entry)
    assert adapter._resolve_model_by_identity(_entry_ref(config_entry)) is adapter._model_cache["glm-5.2#0"]
    assert adapter._resolve_model_by_identity(_entry_ref(json_entry)) is adapter._model_cache["glm-5.2#1"]
