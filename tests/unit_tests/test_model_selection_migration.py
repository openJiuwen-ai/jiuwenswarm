from __future__ import annotations

from copy import deepcopy

import pytest

from jiuwenswarm.common.model_config_validation import validate_models_config
from jiuwenswarm.common.model_errors import MODEL_SELECTION_DISABLED, ModelSelectionError
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_selection import ModelSelection, ResolvedModelGroup
from jiuwenswarm.common.config import _merge_model_update
from jiuwenswarm.server.runtime.model_routing_registry import ModelExecutionContext, ModelSelectionResolver


def _config():
    return {"models": {
        "defaults": [{
            "model_id": "mdl_a", "is_default": True,
            "model_client_config": {
                "model_name": "a", "client_provider": "OpenAI", "api_base": "x", "api_key": "secret",
                "endpoint_profile": "deepseek", "custom_headers": {"X-Test": "value"},
            },
            "model_detail": {"fallback_tag": "chat", "model_description": "primary model"},
            "model_config_obj": {"temperature": .7, "context_window": 100},
        }],
        "agentos": [{
            "model_id": "mdl_b",
            "model_client_config": {"model_name": "b", "client_provider": "OpenAI", "api_base": "y", "api_key": "backup"},
        }],
        "groups": [{
            "model_group_id": "mgp_a", "enabled": True, "is_default": True,
            "routes": [
                {"route_id": "primary", "model_id": "mdl_a"},
                {"route_id": "backup", "model_id": "mdl_b", "enabled": False},
            ],
            "request_config": {"temperature": .5}, "routing": {"strategy": "ordered-failover"},
        }],
    }}


def test_catalog_is_desensitized():
    catalog = ModelCatalog(_config())
    assert "api_key" not in catalog.list_public_models()[0]
    assert catalog.get_model("mdl_a")["entry"]["model_client_config"]["api_key"] == "secret"


@pytest.mark.parametrize("has_group", [False, True])
def test_multiple_legacy_defaults_remain_valid_with_or_without_groups(has_group):
    models = _config()["models"]
    second = deepcopy(models["defaults"][0])
    second["model_id"] = "mdl_c"
    second["model_client_config"]["model_name"] = "c"
    models["defaults"].append(second)
    if not has_group:
        models["groups"] = []

    assert validate_models_config(models) == []


@pytest.mark.parametrize("with_empty_groups", [False, True])
def test_legacy_migration_only_adds_stable_ids(monkeypatch, with_empty_groups):
    from jiuwenswarm.common import config as config_module

    state = _config()
    models = state["models"]
    models.pop("groups")
    if with_empty_groups:
        models["groups"] = []
    second = deepcopy(models["defaults"][0])
    second["model_client_config"]["model_name"] = "c"
    models["defaults"].append(second)
    for entry in models["defaults"] + models["agentos"]:
        entry.pop("model_id")
    original = deepcopy(state)
    writes = []

    def update_config(mutator):
        candidate = deepcopy(state)
        result = mutator(candidate)
        if result is not None:
            state.clear()
            state.update(result)
            writes.append(deepcopy(result))

    monkeypatch.setattr(config_module, "update_config", update_config)
    assert config_module.migrate_model_business_ids() is True
    migrated = deepcopy(state)
    entries = state["models"]["defaults"] + state["models"]["agentos"]
    ids = [entry["model_id"] for entry in entries]
    assert len(set(ids)) == len(entries)
    assert all(model_id.startswith("mdl_") for model_id in ids)
    without_ids = deepcopy(state)
    for entry in without_ids["models"]["defaults"] + without_ids["models"]["agentos"]:
        entry.pop("model_id")
    assert without_ids == original
    assert config_module.migrate_model_business_ids() is False
    assert state == migrated
    assert len(writes) == 1


def test_model_detail_is_editable_without_exposing_write_only_values():
    detail = ModelCatalog(_config()).get_public_model_detail("mdl_a")

    assert detail["read_only"] is False
    assert detail["source"] == "defaults"
    assert detail["model_detail"]["fallback_tag"] == "chat"
    assert "api_key" not in detail["model_client_config"]
    assert "custom_headers" not in detail["model_client_config"]
    assert detail["write_only_fields"] == [
        "model_client_config.api_key",
        "model_client_config.custom_headers",
    ]


def test_model_update_preserves_omitted_write_only_values():
    current = _config()["models"]["defaults"][0]

    merged = _merge_model_update(current, {
        "model_id": "mdl_a",
        "alias": "renamed",
        "model_client_config": {"model_name": "new-name"},
        "write_only_fields": ["ignored"],
    })

    assert merged["alias"] == "renamed"
    assert merged["model_client_config"]["model_name"] == "new-name"
    assert merged["model_client_config"]["api_key"] == "secret"
    assert merged["model_client_config"]["custom_headers"] == {"X-Test": "value"}
    assert "write_only_fields" not in merged


def test_catalog_finds_agent_and_team_references(monkeypatch, tmp_path):
    config = _config()
    selection = {"type": "model_group", "id": "mgp_a"}
    config["agents"] = [{"id": "agent-a", "model_selection": selection}]
    config["team"] = {"name": "team-a", "binding": {"model_selection": selection}}
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_cron_jobs_path", lambda: tmp_path / "cron.json")

    refs = ModelCatalog(config).find_references(ModelSelection(**selection))

    assert ("agent", "agent-a") in {(ref.scope, ref.scope_id) for ref in refs}
    assert ("team", "team-a") in {(ref.scope, ref.scope_id) for ref in refs}


def test_default_group_wins_and_keeps_route_order():
    resolved = ModelSelectionResolver(ModelCatalog(_config())).resolve(None)
    assert isinstance(resolved, ResolvedModelGroup)
    assert [route.route_id for route in resolved.routes] == ["primary", "backup"]
    assert resolved.routes[0].enabled is True
    assert resolved.routes[1].enabled is False
    assert resolved.routes[0].model.endpoint_profile == "deepseek"
    assert resolved.routes[0].model.fallback_tag == "chat"
    assert resolved.routes[0].model.model_description == "primary model"


def test_explicit_disabled_group_does_not_fall_back():
    config = _config()
    config["models"]["groups"][0]["enabled"] = False
    resolver = ModelSelectionResolver(ModelCatalog(config))
    with pytest.raises(ModelSelectionError) as caught:
        resolver.resolve(ModelSelection(type="model_group", id="mgp_a"))
    assert caught.value.code == MODEL_SELECTION_DISABLED


def test_validation_rejects_complete_catalog_conflicts_and_forbidden_fields():
    models = _config()["models"]
    models["groups"].append({**models["groups"][0], "model_group_id": "mgp_b"})
    models["groups"][0]["request_config"] = {"api_key": "bad"}
    errors = validate_models_config(models)
    assert any("default model group" in error for error in errors)
    assert any("api_key" in error for error in errors)


def test_permission_is_checked_for_every_route():
    resolver = ModelSelectionResolver(ModelCatalog(_config()))
    with pytest.raises(ModelSelectionError):
        resolver.resolve(ModelSelection(type="model_group", id="mgp_a"), ModelExecutionContext(
            can_access=lambda kind, resource_id: resource_id != "mdl_b"
        ))


def test_validation_rejects_invalid_routing_contract():
    models = _config()["models"]
    group = models["groups"][0]
    group["routes"][0]["enabled"] = "yes"
    group["routing"] = {"strategy": "tag-filtered", "strategy_kwargs": {}}

    errors = validate_models_config(models)

    assert any("enabled must be a boolean" in error for error in errors)
    assert any("fallback_tag must be a non-empty string" in error for error in errors)


def test_compiler_adapter_matches_final_core_dto():
    """真实联调 openjiuwen-core 的 compile_model_selection（无 mock）。

    agent-core 可用时，adapter 必须把 jiuwenswarm 的 ResolvedSelection 透传
    给 core 编译，并正确解包 CompiledModelSelection 为
    (model_client_config, model_request_config)。当 agent-core 不可用时
    （ImportError），应抛出 MODEL_RUNTIME_UNAVAILABLE。
    """
    from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection

    resolved = ModelSelectionResolver(ModelCatalog(_config())).resolve(None)

    try:
        client_cfg, request_cfg = compile_model_selection(resolved)
    except ModelSelectionError as exc:
        # agent-core 未安装（ImportError 兜底）——只允许这一种失败
        assert exc.code == "MODEL_RUNTIME_UNAVAILABLE"
        pytest.skip("agent-core compiler unavailable; skipping real integration")
        return

    # 真实编译成功：client_cfg 是 ModelClientConfig，且组路由进入 intelli_router
    assert getattr(client_cfg, "client_provider", None) == "intelli_router"
    router = getattr(client_cfg, "intelli_router", None)
    assert router is not None
    assert router.model_group_id == "mgp_a"
    assert [route.route_id for route in router.deployments] == ["primary"]
    # 第 2 条路由 enabled=False，被 core 过滤，只留下 primary
    assert router.deployments[0].model_id == "mdl_a"
    assert router.deployments[0].provider == "deepseek"
    assert router.deployments[0].model_name == "a"
    assert request_cfg is not None
