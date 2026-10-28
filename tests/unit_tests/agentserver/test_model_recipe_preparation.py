# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Ordinary startup constructs required models; explicit routing retains catalogue identity."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as module


@pytest.fixture
def adapter(monkeypatch):
    value = object.__new__(module.JiuWenSwarmDeepAdapter)
    value._model_cache = module._RecipeModelCache(value)
    value._model_name_to_keys = {}
    value._global_index_to_cache_key = {}
    value._model_id_to_key = {}
    value._model_group_cache = {}
    value._last_models_config_fingerprint = None
    value._model = None
    built = []

    def build(mcc, mco, *, resource_id=""):
        built.append((resource_id, mcc["api_base"]))
        if mcc.get("invalid"):
            raise ValueError("invalid unused provider")
        return SimpleNamespace(model_client_config=mcc, model_config=SimpleNamespace(**mco))

    monkeypatch.setattr(value, "_build_kernel_model", build)
    monkeypatch.setattr(value, "_inject_attribution_to_config", lambda _config: None)
    monkeypatch.setattr(module, "get_available_models", lambda config: config["models"]["defaults"])
    monkeypatch.setattr(module, "is_placeholder_model_entry", lambda _entry: False)
    monkeypatch.setattr(module, "model_client_config_view", lambda config: config)
    return value, built


def config():
    return {"models": {"defaults": [
        {"model_id": "first", "model_client_config": {"model_name": "shared", "api_base": "http://first.invalid"}},
        {"model_id": "default", "is_default": True,
         "model_client_config": {"model_name": "shared", "api_base": "http://default.invalid"}},
        {"model_id": "third", "alias": "third-alias",
         "model_client_config": {"model_name": "third", "api_base": "http://third.invalid"}},
        {"model_id": "unused", "model_client_config": {"model_name": "unused", "api_base": "http://unused.invalid", "invalid": True}},
    ]}}


def test_default_startup_avoids_unused_clients_and_alias_resolves_on_first_use(adapter):
    value, built = adapter
    default = value._create_model(config())
    assert built == [("default", "http://default.invalid")]
    assert value._resolve_model_by_name("shared") is default
    selected = value._resolve_model_by_name("third-alias")
    assert selected.model_client_config["api_base"] == "http://third.invalid"
    assert value._resolve_model_by_name("third-alias") is selected
    assert built == [("default", "http://default.invalid"), ("third", "http://third.invalid")]


def test_lazy_same_name_entries_preserve_global_positions_and_stable_id(adapter):
    value, built = adapter
    default = value._create_model(config())
    assert value._resolve_model_by_name("shared#1") is default
    first = value._resolve_model_by_name("shared#0")
    assert first.model_client_config["api_base"] == "http://first.invalid"
    assert value._model_id_to_key == {"first": "shared#0", "default": "shared#1", "third": "third#0", "unused": "unused#0"}
    assert value._materialize_model_recipe(value._model_id_to_key["first"]) is first
    assert len(built) == 2


def test_custom_subagent_dictionary_lookup_materializes_its_selected_recipe(adapter):
    value, built = adapter
    value._create_model(config())
    model = value._model_cache.get("third-alias")
    assert model.model_client_config["api_base"] == "http://third.invalid"
    assert len(built) == 2
    fallback = object()
    assert value._model_cache.get("missing", fallback) is fallback


def test_hot_reload_discards_stale_lazy_recipes(adapter):
    value, built = adapter
    initial = config()
    value._create_model(initial)
    changed = deepcopy(initial)
    changed["models"]["defaults"][2]["model_client_config"]["api_base"] = "http://updated.invalid"
    value._create_model(changed)
    selected = value._resolve_model_by_name("third-alias")
    assert selected.model_client_config["api_base"] == "http://updated.invalid"
    assert ("third", "http://third.invalid") not in built


@pytest.mark.parametrize("requested", ["third-alias", "third#2", "model:third"])
def test_cold_selected_model_does_not_construct_default_provider(adapter, requested):
    value, built = adapter
    value._initial_execution_model_name = requested
    selected = value._create_model(config())
    assert selected.model_client_config["api_base"] == "http://third.invalid"
    assert built == [("third", "http://third.invalid")]
    # A later invalid/empty selection retains the original configured fallback.
    default = value._resolve_model_by_name("not-configured")
    assert default.model_client_config["api_base"] == "http://default.invalid"
    assert value._resolve_model_by_name("") is default
    assert built == [
        ("third", "http://third.invalid"),
        ("default", "http://default.invalid"),
    ]


def test_invalid_cold_selection_preserves_configured_fallback(adapter):
    value, built = adapter
    value._initial_execution_model_name = "unused"
    selected = value._create_model(config())
    assert selected.model_client_config["api_base"] == "http://default.invalid"
    assert built == [
        ("unused", "http://unused.invalid"),
        ("default", "http://default.invalid"),
    ]


def test_default_group_does_not_construct_unused_single_model(adapter, monkeypatch):
    from jiuwenswarm.server.runtime import (
        model_compiler_adapter,
        model_routing_registry,
    )

    value, built = adapter
    group = SimpleNamespace(model_client_config={}, model_config=SimpleNamespace())
    resolved = object()
    monkeypatch.setattr(
        model_routing_registry,
        "ModelSelectionResolver",
        lambda: SimpleNamespace(
            resolve=lambda selection: (
                resolved if selection.id == "default-group" else None
            ),
        ),
    )
    monkeypatch.setattr(
        model_compiler_adapter, "build_model_from_selection", lambda selection: group
    )
    initial = config()
    initial["models"]["groups"] = [
        {"model_group_id": "default-group", "is_default": True}
    ]
    assert value._create_model(initial) is group
    assert value._default_model_name == "model_group:default-group"
    assert built == []
    assert value._resolve_model_by_name("") is group


def test_invalid_selected_default_tries_next_valid_recipe(adapter):
    value, built = adapter
    initial = config()
    initial["models"]["defaults"][1]["model_client_config"]["invalid"] = True
    selected = value._create_model(initial)
    assert selected.model_client_config["api_base"] == "http://first.invalid"
    assert all(resource_id in {"default", "first"} for resource_id, _url in built)


def test_cold_typed_group_uses_real_request_before_default_construction(
    adapter, monkeypatch
):
    value, built = adapter
    request = object()
    group = SimpleNamespace(model_client_config={}, model_config=SimpleNamespace())
    value._initial_execution_request = request
    value._initial_execution_model_name = "model_group:selected-group:route"
    seen = []

    def resolve(actual):
        seen.append(actual)
        return group

    monkeypatch.setattr(value, "_resolve_model_for_request", resolve)
    assert value._create_model(config()) is group
    assert seen == [request]
    assert built == []
    assert value._initial_execution_request is None


def test_cold_typed_selection_rejects_access_before_any_provider_construction(
    adapter, monkeypatch
):
    value, built = adapter
    value._initial_execution_request = object()
    value._initial_execution_model_name = "model:third"

    def reject(request):
        raise PermissionError("model access denied")

    monkeypatch.setattr(value, "_resolve_model_for_request", reject)
    with pytest.raises(PermissionError, match="model access denied"):
        value._create_model(config())
    assert built == []


def test_duplicate_name_and_alias_skip_invalid_recipes_without_changing_index_identity(adapter):
    value, _built = adapter
    initial = config()
    entries = initial["models"]["defaults"]
    entries[0]["model_client_config"]["model_name"] = "duplicate"
    entries[0]["model_client_config"]["invalid"] = True
    entries[0]["alias"] = "shared-alias"
    entries[2]["model_client_config"]["model_name"] = "duplicate"
    entries[2]["alias"] = "shared-alias"
    default = value._create_model(initial)
    valid = value._resolve_model_by_name("duplicate")
    assert valid.model_client_config["api_base"] == "http://third.invalid"
    assert value._model_cache.get("duplicate") is valid
    assert value._resolve_model_by_name("shared-alias") is valid
    assert value._model_cache.get("shared-alias") is valid
    # Explicit global positions select their exact entry, including failure.
    assert value._resolve_model_by_name("duplicate#0") is default
    assert value._resolve_model_by_name("duplicate#2") is valid
